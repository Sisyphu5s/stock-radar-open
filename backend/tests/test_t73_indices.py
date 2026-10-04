"""T-73 全球指数监控：指数清单 / per-market 交易时段 / indices API 测试。

- 指数清单完整性：8 只、CN/US/HK 三市场、字段完备；
- per-market 时段函数边界：CN/US/HK 在/不在交易时段、周末、跨日（美股夜盘）；
- index_spot_ttl：交易时段 90s / 非交易 base×30；
- indices API：正常数据、全空数据（503）、数据源异常（503）、部分市场降级。
全部走 mock（monkeypatch get_indices / provider），不触碰网络与生产库。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from app.api import market as MK
from app.lib import session as sess
from app.lib.indices import INDEXES, INDEXES_BY_MARKET, MARKET_ORDER


# ===== 指数清单完整性 =====


def test_index_list_complete():
    """清单恰 8 只、覆盖三市场、code 唯一、字段完备。"""
    assert len(INDEXES) == 8
    markets = {s.market for s in INDEXES}
    assert markets == {"CN", "US", "HK"}
    codes = [s.code for s in INDEXES]
    assert len(codes) == len(set(codes))
    for spec in INDEXES:
        assert spec.name
        assert spec.currency
        assert spec.tz
        assert spec.is_index
    # 各市场至少 1 只
    assert {m: len(INDEXES_BY_MARKET[m]) for m in MARKET_ORDER} == {
        "CN": 4,
        "US": 3,
        "HK": 1,
    }
    # 关键映射：任务卡指定 8 只
    names = {s.name for s in INDEXES}
    assert names == {
        "上证指数",
        "深证成指",
        "创业板指",
        "沪深300",
        "道琼斯",
        "纳斯达克",
        "标普500",
        "恒生指数",
    }


# ===== per-market 交易时段 =====


# 北京时间构造辅助（naive → Asia/Shanghai）
def _cn(y, mo, d, h, mi, s=0):
    return datetime(
        y, mo, d, h, mi, s, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai")
    )


def test_market_session_cn():
    """CN=现 A 股语义：工作日盘中 True，午休/周末/夜间 False。"""
    assert sess.in_market_session("CN", _cn(2026, 8, 10, 10, 0)) is True  # 周一盘中
    assert sess.in_market_session("CN", _cn(2026, 8, 10, 13, 30)) is True  # 下午盘
    assert sess.in_market_session("CN", _cn(2026, 8, 10, 12, 0)) is False  # 午休
    assert sess.in_market_session("CN", _cn(2026, 8, 10, 22, 0)) is False  # 夜间
    assert sess.in_market_session("CN", _cn(2026, 8, 15, 10, 0)) is False  # 周六


def test_market_session_us():
    """US=北京时间 21:30-次日 04:00；周六凌晨=周五美股尾盘仍算。"""
    assert (
        sess.in_market_session("US", _cn(2026, 8, 10, 21, 30)) is True
    )  # 周一 21:30 开盘
    assert sess.in_market_session("US", _cn(2026, 8, 10, 23, 59)) is True  # 跨日窗口
    assert (
        sess.in_market_session("US", _cn(2026, 8, 11, 3, 59)) is True
    )  # 周二凌晨 03:59
    assert sess.in_market_session("US", _cn(2026, 8, 11, 4, 0)) is False  # 04:00 收盘
    assert (
        sess.in_market_session("US", _cn(2026, 8, 11, 12, 0)) is False
    )  # 北京时间白天
    # 周六凌晨 02:00 = 周五美股夜盘 → True
    assert sess.in_market_session("US", _cn(2026, 8, 15, 2, 0)) is True
    # 周六 21:30 / 周日凌晨 = 周末无交易 → False
    assert sess.in_market_session("US", _cn(2026, 8, 15, 21, 30)) is False
    assert (
        sess.in_market_session("US", _cn(2026, 8, 16, 1, 0)) is False
    )  # 周一凌晨? 8-16 是周日
    # 周一凌晨 00:00-04:00 前一自然日=周日 → False
    assert sess.in_market_session("US", _cn(2026, 8, 17, 1, 0)) is False
    # 周二凌晨 = 周一美股夜盘 → True
    assert sess.in_market_session("US", _cn(2026, 8, 18, 1, 0)) is True


def test_market_session_hk():
    """HK=09:30-17:00（含 1h 收盘缓冲）工作日。"""
    assert sess.in_market_session("HK", _cn(2026, 8, 10, 9, 30)) is True
    assert sess.in_market_session("HK", _cn(2026, 8, 10, 16, 0)) is True  # 收盘仍算
    assert sess.in_market_session("HK", _cn(2026, 8, 10, 17, 0)) is True  # +1h 缓冲内
    assert sess.in_market_session("HK", _cn(2026, 8, 10, 17, 1)) is False
    assert sess.in_market_session("HK", _cn(2026, 8, 10, 8, 0)) is False
    assert sess.in_market_session("HK", _cn(2026, 8, 15, 10, 0)) is False  # 周六


def test_index_spot_ttl_by_market():
    """交易时段 90s（与 spot 一致），非交易时段 base×30。"""
    # CN 盘中 → 90
    assert sess.index_spot_ttl(60, "CN", _cn(2026, 8, 10, 10, 0)) == 90
    # CN 夜间 → 60×30
    assert sess.index_spot_ttl(60, "CN", _cn(2026, 8, 10, 22, 0)) == 60 * 30
    # US 北京时间白天（非交易）→ 放大；夜间（交易）→ 90
    assert sess.index_spot_ttl(60, "US", _cn(2026, 8, 11, 12, 0)) == 60 * 30
    assert sess.index_spot_ttl(60, "US", _cn(2026, 8, 11, 1, 0)) == 90
    # HK 交易时段 → 90
    assert sess.index_spot_ttl(60, "HK", _cn(2026, 8, 10, 10, 0)) == 90


# ===== indices API =====


def _indices_df() -> pd.DataFrame:
    """8 行内部列 mock（INDEXES 顺序）。"""
    return pd.DataFrame(
        [
            {
                "code": s.code,
                "name": s.name,
                "market": s.market,
                "price": float(i + 1000),
                "pct_change": float(i) * 0.1,
                "currency": s.currency,
                "updated_at": "2026-08-15 10:00:00",
            }
            for i, s in enumerate(INDEXES)
        ]
    )


def test_indices_api_ok(monkeypatch):
    """正常数据：返回 8 条 indices，字段契约齐全。"""
    monkeypatch.setattr(MK, "get_indices", lambda: _indices_df())
    out = MK.indices()
    assert set(out) == {"indices"}
    assert len(out["indices"]) == 8
    first = out["indices"][0]
    assert set(first) == {
        "code",
        "name",
        "market",
        "price",
        "pct_change",
        "currency",
        "updated_at",
    }


def test_indices_api_empty_503(monkeypatch):
    """无数据（空 DataFrame）→ 503，语义与 /snapshot 一致。"""
    monkeypatch.setattr(MK, "get_indices", lambda: pd.DataFrame())
    with pytest.raises(HTTPException) as exc:
        MK.indices()
    assert exc.value.status_code == 503


def test_indices_api_source_error_503(monkeypatch):
    """数据源异常 → 503。"""

    def boom():
        raise RuntimeError("指数行情源不可达")

    monkeypatch.setattr(MK, "get_indices", boom)
    with pytest.raises(HTTPException) as exc:
        MK.indices()
    assert exc.value.status_code == 503


def test_indices_api_all_nan_503(monkeypatch):
    """8 行但 price 全 NaN（全部市场无数据）→ 503。"""
    df = _indices_df()
    df["price"] = np.nan
    monkeypatch.setattr(MK, "get_indices", lambda: df)
    with pytest.raises(HTTPException) as exc:
        MK.indices()
    assert exc.value.status_code == 503


def test_indices_api_partial_market_degraded(monkeypatch):
    """部分市场降级：仅 CN 有价 → 200，US/HK 行 price 为 None（前端静默降级）。"""
    df = _indices_df()
    df.loc[df["market"] != "CN", "price"] = np.nan
    monkeypatch.setattr(MK, "get_indices", lambda: df)
    out = MK.indices()
    assert len(out["indices"]) == 8
    cn = [r for r in out["indices"] if r["market"] == "CN"]
    us = [r for r in out["indices"] if r["market"] == "US"]
    assert all(r["price"] is not None for r in cn)
    assert all(r["price"] is None for r in us)


# ===== provider 缓存 / 降级路径（monkeypatch 数据源，不触网） =====


def test_provider_indices_em_primary(monkeypatch):
    """东财主源成功：一次全量 8 只，updated_at 带「最新行情时间」。"""
    from app.core import sources as Q

    prov = Q.AkshareProvider()
    raw = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "上证指数",
                "最新价": 3900.0,
                "涨跌幅": 0.5,
                "最新行情时间": "2026-08-15 10:00:00",
            },
            {
                "代码": "399001",
                "名称": "深证成指",
                "最新价": 14000.0,
                "涨跌幅": -0.3,
                "最新行情时间": "2026-08-15 10:00:00",
            },
            {
                "代码": "399006",
                "名称": "创业板指",
                "最新价": 3600.0,
                "涨跌幅": 1.1,
                "最新行情时间": "2026-08-15 10:00:00",
            },
            {
                "代码": "000300",
                "名称": "沪深300",
                "最新价": 4600.0,
                "涨跌幅": 0.2,
                "最新行情时间": "2026-08-15 10:00:00",
            },
            {
                "代码": "DJIA",
                "名称": "道琼斯",
                "最新价": 53772.0,
                "涨跌幅": -0.13,
                "最新行情时间": "2026-08-15 02:40:00",
            },
            {
                "代码": "NDX",
                "名称": "纳斯达克",
                "最新价": 26689.0,
                "涨跌幅": -0.42,
                "最新行情时间": "2026-08-15 02:40:00",
            },
            {
                "代码": "SPX",
                "名称": "标普500",
                "最新价": 7798.0,
                "涨跌幅": 0.45,
                "最新行情时间": "2026-08-15 02:40:00",
            },
            {
                "代码": "HSI",
                "名称": "恒生指数",
                "最新价": 25116.0,
                "涨跌幅": -1.1,
                "最新行情时间": "2026-08-15 16:00:00",
            },
        ]
    )
    em = prov._indices_from_em_global(raw)
    assert set(em) == {s.code for s in INDEXES}
    assert em["000001.SH"]["price"] == 3900.0
    assert em[".DJI"]["pct"] == -0.13
    assert em["HSI"]["updated_at"] == "2026-08-15 16:00:00"
    # 缺一只（东财无该指数）→ 不报错，仅缺失
    raw2 = raw[raw["代码"] != "HSI"]
    em2 = prov._indices_from_em_global(raw2)
    assert "HSI" not in em2
    assert len(em2) == 7


def test_provider_indices_name_fallback(monkeypatch):
    """代码不匹配时名称兜底（接口代码格式变动防御）。"""
    from app.core import sources as Q

    prov = Q.AkshareProvider()
    raw = pd.DataFrame(
        [{"代码": "X.000001", "名称": "上证指数", "最新价": 123.0, "涨跌幅": 0.1}]
    )
    em = prov._indices_from_em_global(raw)
    assert em["000001.SH"]["price"] == 123.0


def test_provider_indices_sina_us_hq():
    """新浪美股 hq 实时解析（实测字段格式）。"""
    from app.core import sources as Q

    text = (
        'var hq_str_gb_dji="道琼斯,53772.8008,-0.12,2026-08-15 02:40:24,-67.19,53842.80,...";\n'
        'var hq_str_gb_ixic="纳斯达克,26689.6380,-0.42,2026-08-15 02:40:40,-112.64,26802.28,...";\n'
        'var hq_str_gb_inx="标普500,7798.9902,0.45,2026-08-15 02:40:52,34.49,7764.50,...";'
    )
    df = Q.AkshareProvider._indices_from_sina_us_hq(text)
    assert len(df) == 3
    dji = df[df["code"] == ".DJI"].iloc[0]
    assert dji["price"] == 53772.8008
    assert dji["pct_change"] == -0.12
    assert dji["updated_at"] == "2026-08-15 02:40:24"
    assert dji["currency"] == "USD"


def test_provider_indices_sina_cn_hk():
    """新浪 A 股 / 港股指数解析。"""
    from app.core import sources as Q

    prov = Q.AkshareProvider()
    cn = pd.DataFrame(
        [
            {"代码": "sh000001", "名称": "上证指数", "最新价": 3927.0, "涨跌幅": 0.005},
            {
                "代码": "sz399001",
                "名称": "深证成指",
                "最新价": 14354.0,
                "涨跌幅": 0.454,
            },
            {"代码": "sz399006", "名称": "创业板指", "最新价": 3626.0, "涨跌幅": 1.122},
            {"代码": "sh000300", "名称": "沪深300", "最新价": 4665.0, "涨跌幅": 0.041},
        ]
    )
    df = prov._indices_from_sina_cn(cn)
    assert len(df) == 4
    assert df[df["code"] == "000001.SH"].iloc[0]["price"] == 3927.0
    assert df[df["code"] == "000001.SH"].iloc[0]["currency"] == "CNY"

    hk = pd.DataFrame(
        [{"代码": "HSI", "名称": "恒生指数", "最新价": 25116.85, "涨跌幅": -1.101}]
    )
    dfh = prov._indices_from_sina_hk(hk)
    assert len(dfh) == 1
    assert dfh.iloc[0]["price"] == 25116.85
    assert dfh.iloc[0]["currency"] == "HKD"


def test_provider_indices_market_fallback_cache(monkeypatch):
    """市场拉取失败 → 冷却 + 旧缓存兜底；无缓存 → 空行占位不阻塞其他市场。"""
    import app.storage.providers.akshare as AK

    prov = AK.AkshareProvider()
    # 首次：全失败（东财 + 新浪都抛）→ 8 行空值 + 冷却登记
    with (
        patch(
            "app.storage.providers.akshare._ak_retry",
            side_effect=RuntimeError("网络不可达"),
        ),
        patch(
            "app.storage.providers.akshare._ak_call",
            side_effect=RuntimeError("网络不可达"),
        ),
    ):
        df = prov.get_indices()
    assert len(df) == 8
    assert df["price"].isna().all()
    assert all(now := prov._indices_blocked_until[m] > 0 for m in MARKET_ORDER)

    # 冷却中（不重试网络）→ 直接返回缓存
    with (
        patch(
            "app.storage.providers.akshare._ak_retry",
            side_effect=AssertionError("不应重试"),
        ),
        patch(
            "app.storage.providers.akshare._ak_call",
            side_effect=AssertionError("不应重试"),
        ),
    ):
        df2 = prov.get_indices()
    assert len(df2) == 8 and df2["price"].isna().all()
