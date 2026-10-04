"""SinaProvider 盘中日K当日 bar 实时合成(1 分钟线聚合)契约测试。

契约(任务卡):
- 仅 sina daily + compose_intraday=True 且盘中(上海 09:30 ≤ t < 15:05)
  + 日K最后日期 < 上海今日 → 用当日 1 分钟线聚合当日 bar:
  open=首根 / close=末根 / high=max / low=min / volume=sum / amount=sum,
  聚合后重算 pct_change(close.pct_change().fillna(0)*100,与 kcache merge 同口径)。
- 三道防御(high≥max(o,c) 且 low≤min(o,c);date 单调递增;volume>0 时
  avg=amount/(volume*100) ∈ [low*0.8, high*1.2])+ spot 实时价一致性比对
  (|close - spot price| ≤ 0.01),任一失败静默返回原 df。
- 分钟线拉取失败/空/异常 → 静默返回原 df(不抛错)。
- 周/月透传 compose_intraday 到内部 daily 调用(重采样自动含当日合成 bar)。
- 时间冻结:patch session.now_cn(仿 test_kcache_intraday / test_signal_as_of)。
- 不依赖真实网络(get_kline/get_spot 全部 mock),不触碰生产库。

测试基准日:2026-08-10 周一(昨日) / 2026-08-11 周二(今日)。
"""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.lib import session as sess
from app.storage.providers import SinaProvider
from app.storage.providers.base import _slice_kline

# ---------------------------------------------------------------------------
# 时间冻结基建(仿 test_kcache_intraday.frozen_cn)
# ---------------------------------------------------------------------------


class _FrozenClock:
    """固定 Asia/Shanghai 墙钟(naive);_FROZEN 可在用例内改以模拟盘中/收盘/开盘前。"""

    _FROZEN = datetime(2026, 8, 11, 10, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


@pytest.fixture()
def frozen_cn(monkeypatch):
    """冻结 session.now_cn:合成判定(今日日期/盘中窗口)的取时来源。"""
    monkeypatch.setattr(sess, "now_cn", lambda: _FrozenClock._FROZEN)
    return _FrozenClock


# ---------------------------------------------------------------------------
# 构造桩数据
# ---------------------------------------------------------------------------


def _daily_kline(last_date: str = "2026-08-10") -> pd.DataFrame:
    """构造日线(最后日期=last_date,date 为 YYYY-MM-DD 字符串,升序)。"""
    n = 5
    close = np.linspace(10.0, 10.8, n)
    return pd.DataFrame(
        {
            "date": pd.bdate_range(end=last_date, periods=n)
            .strftime("%Y-%m-%d")
            .tolist(),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 1000.0),
            "amount": np.full(n, 1_000_000.0),
            "pct_change": np.r_[np.zeros(n - 1), [1.0]],
        }
    )


def _minute_kline(today: str = "2026-08-11", amounts=None) -> pd.DataFrame:
    """构造当日 1 分钟线(3 根,date 含时分;volume 单位=手,同 SinaProvider 分钟返回)。"""
    rows = [
        ("09:30", 10.00, 10.10, 9.95, 10.05, 500),
        ("09:31", 10.05, 10.30, 10.02, 10.25, 800),
        ("10:00", 10.25, 10.40, 10.20, 10.35, 1200),
    ]
    amt = amounts or [505_000.0, 824_000.0, 1_242_000.0]
    return pd.DataFrame(
        {
            "date": [f"{today} {t}" for t, *_ in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
            "amount": amt,
            "pct_change": [0.0] * len(rows),
        }
    )


def _spot_df(price: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["600519.SH"],
            "name": ["贵州茅台"],
            "price": [price],
            "pct_change": [1.0],
            "volume": [1000.0],
            "amount": [1e8],
        }
    )


def _make_provider(
    monkeypatch,
    minute_df: pd.DataFrame | None = None,
    minute_exc: Exception | None = None,
    spot_df: pd.DataFrame | None = None,
    spot_exc: Exception | None = None,
    daily_last: str = "2026-08-10",
) -> SinaProvider:
    """构造 SinaProvider 并安装 mock(零网络):

    - period=="1" → 返回构造分钟线 / 空 / 抛异常(合成内拉分钟线走此分支)
    - period=="daily" → 构造日线 + 走真实合成逻辑(_compose_intraday_daily)
    - 其他(weekly/monthly)→ 走真实实现
    - get_spot:未显式给 spot 数据/异常时一律抛异常,防真实网络
    """
    prov = SinaProvider()
    real = SinaProvider.get_kline

    def fake_get_kline(
        self,
        code,
        period,
        start_date=None,
        end_date=None,
        days=None,
        compose_intraday=False,
    ):
        if period == "1":
            if minute_exc is not None:
                raise minute_exc
            if minute_df is None:
                return pd.DataFrame()
            return minute_df.copy()
        if period == "daily":
            out = _daily_kline(last_date=daily_last).copy()
            if compose_intraday:
                out = self._compose_intraday_daily(out, code)
            return _slice_kline(out, start_date, end_date, days)
        return real(
            self,
            code,
            period,
            start_date=start_date,
            end_date=end_date,
            days=days,
            compose_intraday=compose_intraday,
        )

    monkeypatch.setattr(SinaProvider, "get_kline", fake_get_kline)
    if spot_exc is not None:

        def _boom():
            raise spot_exc

        monkeypatch.setattr(prov, "get_spot", _boom)
    if spot_df is not None:
        prov._spot_cache["all"] = spot_df
    if spot_df is None and spot_exc is None:
        # 默认:spot 不可用(防真实网络),校验应跳过
        def _boom():
            raise RuntimeError("no-net")

        monkeypatch.setattr(prov, "get_spot", _boom)
    return prov


# ---------------------------------------------------------------------------
# 1) 聚合正确性:盘中合成一行,OHLCV/amount 正确
# ---------------------------------------------------------------------------


def test_compose_success_aggregates_intraday_row(monkeypatch, frozen_cn):
    """冻结 2026-08-11 10:00(盘中),日K最后日期=08-10(昨日)→ 合成当日 bar:
    open=首根 10.00 / close=末根 10.35 / high=max=10.40 / low=min=9.95 /
    volume=sum=2500 / amount=sum;pct_change 重算;date 单调递增。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    prov = _make_provider(monkeypatch, minute_df=_minute_kline())
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)

    assert len(out) == len(_daily_kline()) + 1
    row = out.iloc[-1]
    assert row["date"] == "2026-08-11"
    assert row["open"] == pytest.approx(10.00)
    assert row["close"] == pytest.approx(10.35)
    assert row["high"] == pytest.approx(10.40)
    assert row["low"] == pytest.approx(9.95)
    assert row["volume"] == pytest.approx(2500.0)
    assert row["amount"] == pytest.approx(505_000 + 824_000 + 1_242_000)
    assert out["date"].is_monotonic_increasing
    prev_close = _daily_kline()["close"].iloc[-1]
    assert out["pct_change"].iloc[-1] == pytest.approx((10.35 / prev_close - 1) * 100)
    # 历史行不被破坏
    assert out.iloc[-2]["date"] == "2026-08-10"
    assert out.iloc[-2]["close"] == pytest.approx(10.8)


# ---------------------------------------------------------------------------
# 2) 边界:非盘中 / 已有当日 / 分钟线空或异常 / 开盘前 → 均不合成
# ---------------------------------------------------------------------------


def test_no_compose_after_close(monkeypatch, frozen_cn):
    """15:30 已收盘(≥ 15:05)→ 不合成。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 15, 30)
    prov = _make_provider(monkeypatch, minute_df=_minute_kline())
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline())
    assert out["date"].iloc[-1] == "2026-08-10"


def test_no_compose_before_open(monkeypatch, frozen_cn):
    """09:00 开盘前(< 09:30)→ 不合成。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 9, 0)
    prov = _make_provider(monkeypatch, minute_df=_minute_kline())
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline())


def test_no_compose_when_daily_has_today(monkeypatch, frozen_cn):
    """日K最后日期=今日 → 不合成(不重复叠加)。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    prov = _make_provider(
        monkeypatch, minute_df=_minute_kline(), daily_last="2026-08-11"
    )
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline(last_date="2026-08-11"))
    assert out["date"].iloc[-1] == "2026-08-11"


def test_no_compose_when_minute_empty(monkeypatch, frozen_cn):
    """今日分钟线为空 → 不合成。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    prov = _make_provider(monkeypatch, minute_df=pd.DataFrame())
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline())


def test_no_compose_when_minute_exception(monkeypatch, frozen_cn):
    """分钟线拉取抛异常 → 不合成且不抛。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    prov = _make_provider(monkeypatch, minute_exc=RuntimeError("boom"))
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline())


# ---------------------------------------------------------------------------
# 3) 防御:OHLC 结构异常 / amount-volume 交叉校验不符 → 拒绝合成
# ---------------------------------------------------------------------------


def test_reject_invalid_ohlc(monkeypatch, frozen_cn):
    """构造 high < close 的异常分钟线(聚合后 high < close)→ 拒绝,返回原 df。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    bad = _minute_kline().copy()
    bad["high"] = bad["close"] - 0.1  # 全部 high < close
    prov = _make_provider(monkeypatch, minute_df=bad)
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline())
    assert out["date"].iloc[-1] == "2026-08-10"


def test_reject_amount_volume_mismatch(monkeypatch, frozen_cn):
    """amount 与 volume 数量级不符(avg=amount/(volume*100) 超出 [low*0.8, high*1.2])
    → 拒绝合成,返回原 df(防单位错乱)。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    bad = _minute_kline(amounts=[1e8, 1e8, 1e8])  # avg=3e8/250000=1200 >> 10.4*1.2
    prov = _make_provider(monkeypatch, minute_df=bad)
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline())
    assert out["date"].iloc[-1] == "2026-08-10"


# ---------------------------------------------------------------------------
# 4) 一致性:spot 实时价比对(不符拒绝 / 失败跳过 / 匹配放行)
# ---------------------------------------------------------------------------


def test_reject_when_spot_price_mismatch(monkeypatch, frozen_cn):
    """spot price=11.0,合成 close=10.35,差 0.65 > 0.01 → 拒绝合成。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    prov = _make_provider(
        monkeypatch, minute_df=_minute_kline(), spot_df=_spot_df(11.0)
    )
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline())
    assert out["date"].iloc[-1] == "2026-08-10"


def test_spot_failure_allows_compose(monkeypatch, frozen_cn):
    """spot 获取失败(抛异常)→ 跳过校验,正常合成(校验不引入主路径故障)。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    prov = _make_provider(
        monkeypatch, minute_df=_minute_kline(), spot_exc=RuntimeError("spot down")
    )
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline()) + 1
    assert out.iloc[-1]["date"] == "2026-08-11"


def test_spot_price_match_allows_compose(monkeypatch, frozen_cn):
    """spot price=10.35 与合成 close 一致(差 ≤ 0.01)→ 校验通过,正常合成。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    prov = _make_provider(
        monkeypatch, minute_df=_minute_kline(), spot_df=_spot_df(10.35)
    )
    out = prov.get_kline("600519.SH", "daily", compose_intraday=True)
    assert len(out) == len(_daily_kline()) + 1
    assert out.iloc[-1]["date"] == "2026-08-11"


# ---------------------------------------------------------------------------
# 5) 周/月透传:compose_intraday 透传到内部 daily 调用
# ---------------------------------------------------------------------------


def _passthrough_provider(monkeypatch, calls: list):
    """weekly/monthly 请求走真实分支,内部 daily 调用被记录 compose_intraday 值。"""
    prov = SinaProvider()
    real = SinaProvider.get_kline

    def fake_get_kline(
        self,
        code,
        period,
        start_date=None,
        end_date=None,
        days=None,
        compose_intraday=False,
    ):
        if period == "daily":
            calls.append(compose_intraday)
            return _daily_kline().copy()
        return real(
            self,
            code,
            period,
            start_date=start_date,
            end_date=end_date,
            days=days,
            compose_intraday=compose_intraday,
        )

    monkeypatch.setattr(SinaProvider, "get_kline", fake_get_kline)
    return prov


def test_weekly_passes_compose_intraday(monkeypatch, frozen_cn):
    """weekly + compose_intraday=True → 内部 daily 调用也带 True。"""
    calls: list[bool] = []
    prov = _passthrough_provider(monkeypatch, calls)
    out = prov.get_kline("600519.SH", "weekly", compose_intraday=True)
    assert calls == [True]
    assert len(out) >= 1
    assert out["date"].iloc[-1] == "2026-08-10"


def test_weekly_default_no_compose(monkeypatch, frozen_cn):
    """weekly 不传 compose_intraday → 内部 daily 默认 False(不合成)。"""
    calls: list[bool] = []
    prov = _passthrough_provider(monkeypatch, calls)
    prov.get_kline("600519.SH", "monthly")
    assert calls == [False]


# ---------------------------------------------------------------------------
# 6) P2-9 盘中增量短路:partial 命中(start_date=今日)只拉 1 分钟线,免日线全量重拉
# ---------------------------------------------------------------------------
# 背景:kcache 单只路径盘中 partial 命中 → _fetch_start 返回 start_date=今日(days=None)
# + compose_intraday=True,期望结果只有今日 bar;新浪日线接口不支持服务端 start_date,
# 旧实现每次全量下载 1600 根再本地切片。修复:命中该组合时只拉当日 1 分钟线聚合
# (三道防御 + spot 校验不变);收盘后/start_date 早于今日/days 指定均回落完整日线。
# 本区块 mock 网络层(SESSION.get)而非 get_kline,让真实短路逻辑生效;按请求
# scale 参数断言"只拉了分钟线"。


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        pass


def _minute_json(today: str = "2026-08-11") -> str:
    """新浪 1 分钟线接口 JSON(3 根,volume 单位=股)。"""
    rows = [
        {
            "day": f"{today} 09:30",
            "open": "10.00",
            "high": "10.10",
            "low": "9.95",
            "close": "10.05",
            "volume": "50000",
        },
        {
            "day": f"{today} 09:31",
            "open": "10.05",
            "high": "10.30",
            "low": "10.02",
            "close": "10.25",
            "volume": "80000",
        },
        {
            "day": f"{today} 10:00",
            "open": "10.25",
            "high": "10.40",
            "low": "10.20",
            "close": "10.35",
            "volume": "120000",
        },
    ]
    return json.dumps(rows)


def _daily_json(last_date: str) -> str:
    """新浪日线接口 JSON(5 根,最后=last_date,volume 单位=股)。"""
    n = 5
    close = np.linspace(10.0, 10.8, n)
    rows = [
        {
            "day": d,
            "open": f"{c - 0.1:.2f}",
            "high": f"{c + 0.2:.2f}",
            "low": f"{c - 0.2:.2f}",
            "close": f"{c:.2f}",
            "volume": f"{1000 * 100}",
        }
        for d, c in zip(
            pd.bdate_range(end=last_date, periods=n).strftime("%Y-%m-%d"), close
        )
    ]
    return json.dumps(rows)


def _network_mock(monkeypatch, calls: list, daily_json: str):
    """mock SESSION.get:记录 (scale, datalen);scale=1 返回分钟线,scale=240 返回
    给定日线 JSON(日线请求若在短路用例中出现会由断言失败暴露)。"""
    from app.core import sources as Q

    def fake_get(url, params=None, timeout=None):
        scale = (params or {}).get("scale")
        calls.append((scale, (params or {}).get("datalen")))
        if scale == 1:
            return _Resp(_minute_json())
        if scale == 240:
            return _Resp(daily_json)
        raise AssertionError(f"未知 scale={scale} 请求")

    monkeypatch.setattr(Q.SESSION, "get", fake_get)


def _boom():
    raise RuntimeError("no-net")


def test_partial_increment_shortcircuits_to_minute_only(monkeypatch, frozen_cn):
    """盘中(start_date=今日 + compose_intraday + days 未指定):只拉 1 分钟线聚合
    今日 bar,日线请求(scale=240)绝不发出。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    calls: list[tuple] = []
    _network_mock(monkeypatch, calls, daily_json=_daily_json("2026-08-10"))

    prov = SinaProvider()
    monkeypatch.setattr(prov, "get_spot", _boom)  # spot 不可用 → 校验跳过

    out = prov.get_kline(
        "600519.SH", "daily", start_date="2026-08-11", compose_intraday=True
    )
    assert calls == [(1, 800)]  # 仅 1 次分钟线请求,无日线请求
    assert len(out) == 1
    assert out.iloc[0]["date"] == "2026-08-11"
    assert out.iloc[0]["close"] == pytest.approx(10.35)  # 末根分钟 close


def test_no_shortcircuit_when_start_date_older(monkeypatch, frozen_cn):
    """start_date 早于今日(长假后回填历史缺口)→ 不短路,完整日线拉取路径保留
    (日线请求发出,且合成今日 bar 仍生效)。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    calls: list[tuple] = []
    _network_mock(monkeypatch, calls, daily_json=_daily_json("2026-08-07"))

    prov = SinaProvider()
    monkeypatch.setattr(prov, "get_spot", _boom)

    out = prov.get_kline(
        "600519.SH", "daily", start_date="2026-08-06", compose_intraday=True
    )
    assert (240, 1600) in calls  # 日线请求被完整调用
    assert (1, 800) in calls  # 分钟线合成仍发生
    assert out["date"].iloc[0] == "2026-08-06"  # 历史缺口行保留
    assert out["date"].iloc[-1] == "2026-08-11"  # 合成今日 bar 仍生效


def test_after_close_partial_increment_full_daily(monkeypatch, frozen_cn):
    """收盘后(≥15:05)start_date=今日:不短路,回落完整日线拉取(真实 bar 覆盖 partial,
    收盘后完整重拉语义不变),且不再拉分钟线合成。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 15, 30)
    calls: list[tuple] = []
    _network_mock(monkeypatch, calls, daily_json=_daily_json("2026-08-11"))

    prov = SinaProvider()
    monkeypatch.setattr(prov, "get_spot", _boom)

    out = prov.get_kline(
        "600519.SH", "daily", start_date="2026-08-11", compose_intraday=True
    )
    assert calls == [(240, 1600)]  # 完整日线拉取,无分钟线请求
    assert out.iloc[-1]["date"] == "2026-08-11"  # 真实 bar


def test_no_shortcircuit_when_days_given(monkeypatch, frozen_cn):
    """days 指定(周/月透传 days=800 / 冷启动 days=800)→ 不短路,完整日线拉取。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    calls: list[tuple] = []
    _network_mock(monkeypatch, calls, daily_json=_daily_json("2026-08-10"))

    prov = SinaProvider()
    monkeypatch.setattr(prov, "get_spot", _boom)

    out = prov.get_kline(
        "600519.SH", "daily", start_date="2026-08-11", days=800, compose_intraday=True
    )
    assert (240, 1600) in calls  # 完整日线拉取(days 路径)
    assert out["date"].iloc[-1] == "2026-08-11"  # 合成今日 bar 仍生效
