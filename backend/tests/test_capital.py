"""T-08 资金类历史测试：模型建表/仓储 upsert 幂等/抓取列映射(mock akshare)/端点。

模式与 test_financials 一致：临时 SQLite（Base.metadata.create_all）+ 直接调用路由
函数；端点内部自建会话经 monkeypatch app.storage.db.SessionLocal 指向临时库。
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import stocks as stocks_api
from app.storage.db import Base
from app.storage.providers import capital as cap_prov
from app.storage.repos import capital as repo

_TABLES = (
    "capital_moneyflow",
    "capital_lhb",
    "capital_margin",
    "capital_northbound",
)


@pytest.fixture()
def session(tmp_path):
    """独立临时 SQLite（Base.metadata 含四张新表）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cap.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Maker()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def api_db(tmp_path, monkeypatch):
    """端点自建会话 → 临时库（patch app.storage.db.SessionLocal）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api_cap.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("app.storage.db.SessionLocal", Maker)
    yield Maker
    engine.dispose()


def _fake_ak_module(**fns):
    """构造伪 akshare 模块（fetch_* 函数内 import akshare as ak）。"""
    m = types.ModuleType("akshare")
    for k, v in fns.items():
        setattr(m, k, v)
    return m


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


def test_capital_models_registered():
    """四张表已注册进共享 MetaData（init_db 的 create_all 自动建表，无需手工迁移）。"""
    names = Base.metadata.tables
    for t in _TABLES:
        assert t in names


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


def test_moneyflow_upsert_idempotent(session):
    """同 (code, date) 重复 upsert 覆盖不新增；查询日期倒序。"""
    repo.upsert_capital(
        session,
        "600519.SH",
        "moneyflow",
        [
            {"date": "2026-08-14", "main_net": 1e8, "super_net": 5e7},
            {"date": "2026-08-13", "main_net": -2e7, "large_net": 3e6},
        ],
    )
    repo.upsert_capital(
        session,
        "600519.SH",
        "moneyflow",
        [{"date": "2026-08-14", "main_net": 9e7, "super_net": 4e7}],
    )
    out = repo.list_capital(session, "600519.SH", "moneyflow", 10)
    assert len(out) == 2  # 同日期覆盖不新增
    assert out[0].date == "2026-08-14"  # 倒序（新在前）
    assert out[0].main_net == 9e7  # 被第二次覆盖
    assert repo.latest_capital_date(session, "600519.SH", "moneyflow") == "2026-08-14"
    assert repo.latest_capital_date(session, "000001.SZ", "moneyflow") is None


def test_lhb_upsert_triple_key(session):
    """龙虎榜同日多次上榜（不同 reason）为两行；同 (code, date, reason) 覆盖不新增。"""
    repo.upsert_capital(
        session,
        "600519.SH",
        "lhb",
        [
            {"date": "2026-08-13", "reason": "日涨幅偏离值达7%", "net_amount": 1e8},
            {
                "date": "2026-08-13",
                "reason": "连续三日涨幅偏离值累计达20%",
                "net_amount": 2e8,
            },
        ],
    )
    repo.upsert_capital(
        session,
        "600519.SH",
        "lhb",
        [{"date": "2026-08-13", "reason": "日涨幅偏离值达7%", "net_amount": 5e7}],
    )
    out = repo.list_capital(session, "600519.SH", "lhb", 10)
    assert len(out) == 2
    by_reason = {r.reason: r for r in out}
    assert by_reason["日涨幅偏离值达7%"].net_amount == 5e7  # 覆盖
    assert by_reason["连续三日涨幅偏离值累计达20%"].net_amount == 2e8


def test_margin_northbound_upsert(session):
    """两融/北向 upsert 幂等 + latest 查询。"""
    repo.upsert_capital(
        session,
        "600519.SH",
        "margin",
        [
            {
                "date": "2026-08-14",
                "margin_balance": 2e10,
                "short_balance": 3e8,
                "net_buy": 1e7,
            },
            {"date": "2026-08-13", "margin_balance": 1.9e10, "net_buy": 0.0},
        ],
    )
    repo.upsert_capital(
        session,
        "600519.SH",
        "northbound",
        [
            {"date": "2024-08-15", "hold_shares": 8e7, "hold_ratio": 6.3},
            {"date": "2024-08-14", "hold_shares": 7.9e7, "hold_ratio": 6.2},
        ],
    )
    assert len(repo.list_capital(session, "600519.SH", "margin", 10)) == 2
    assert len(repo.list_capital(session, "600519.SH", "northbound", 10)) == 2
    latest = repo.latest_capital(session, "600519.SH", "northbound")
    assert latest is not None and latest.hold_ratio == 6.3
    assert repo.latest_capital(session, "600519.SH", "northbound").date == "2024-08-15"


# ---------------------------------------------------------------------------
# 抓取函数（mock akshare）
# ---------------------------------------------------------------------------


def test_fetch_moneyflow_maps_columns(monkeypatch):
    """资金流：中文列 → 内部字段；日期归一；未知列不报错。"""
    raw = pd.DataFrame(
        {
            "日期": ["2026-08-14", "2026-08-13"],
            "主力净流入-净额": [1e8, -2e7],
            "超大单净流入-净额": [5e7, 1e7],
            "大单净流入-净额": [3e7, -1e7],
            "中单净流入-净额": [-2e7, 5e6],
            "小单净流入-净额": [-6e7, 1.5e7],
            "收盘价": [1500.0, 1490.0],
            "涨跌幅": [1.2, -0.8],
        }
    )

    def _api(stock, market):
        assert stock == "600519" and market == "sh"
        return raw

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_individual_fund_flow=_api)
    )
    out = cap_prov.fetch_moneyflow("600519.SH")
    assert len(out) == 2
    r = out[0]
    assert r["date"] == "2026-08-14"
    assert r["main_net"] == 1e8
    assert r["super_net"] == 5e7
    assert r["small_net"] == -6e7
    assert "收盘价" not in r  # 非映射列不落库


def test_fetch_moneyflow_failure_returns_empty(monkeypatch):
    """资金流抓取失败（网络不可达）→ 降级返回 []，不抛错。"""

    def boom(*a, **k):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_individual_fund_flow=boom)
    )
    assert cap_prov.fetch_moneyflow("600519.SH") == []


def test_fetch_lhb_filters_code(monkeypatch):
    """龙虎榜：日期区间全市场 → 仅保留目标代码行；summary 回落上榜原因。"""
    raw = pd.DataFrame(
        {
            "代码": ["600519", "000001", "600519"],
            "名称": ["贵州茅台", "平安银行", "贵州茅台"],
            "上榜日": ["2026-08-13", "2026-08-13", "2026-08-12"],
            "上榜原因": [
                "日涨幅偏离值达7%",
                "日涨幅偏离值达7%",
                "连续三日涨幅偏离值累计达20%",
            ],
            "解读": ["主力资金净买入", "", ""],
            "龙虎榜买入额": [1e8, 2e8, 3e8],
            "龙虎榜卖出额": [5e7, 1e8, 1e8],
            "龙虎榜净买额": [5e7, 1e8, 2e8],
            "龙虎榜成交额": [1.5e8, 3e8, 4e8],
            "换手率": [1.2, 3.4, 2.1],
        }
    )

    def _api(start_date, end_date):
        assert start_date == "20260801" and end_date == "20260814"
        return raw

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_lhb_detail_em=_api)
    )
    out = cap_prov.fetch_lhb("600519.SH", "2026-08-01", "2026-08-14")
    assert len(out) == 2  # 过滤掉 000001
    r0, r1 = out
    assert r0["date"] == "2026-08-13" and r0["net_amount"] == 5e7
    assert r0["summary"] == "主力资金净买入"  # 解读优先
    assert r1["summary"] == "连续三日涨幅偏离值累计达20%"  # 无解读回落上榜原因
    assert r1["buy_amount"] == 3e8


def test_fetch_margin_daily_and_net_buy(monkeypatch):
    """两融：逐日拉取（仅工作日）→ 过滤代码 → 融资净买入=余额环比。"""
    import datetime

    # 2026-08-14 是周五,08-13 周四,08-12 周三(窗口内均为工作日)
    def _sse(date):
        assert date in ("20260814", "20260813", "20260812")
        if date == "20260814":
            return pd.DataFrame(
                {
                    "信用交易日期": ["2026-08-14"],
                    "标的证券代码": ["600519"],
                    "标的证券简称": ["贵州茅台"],
                    "融资余额": [2e10],
                    "融资买入额": [1e8],
                    "融资偿还额": [9e7],
                }
            )
        if date == "20260813":
            return pd.DataFrame(
                {
                    "信用交易日期": ["2026-08-13"],
                    "标的证券代码": ["600519"],
                    "标的证券简称": ["贵州茅台"],
                    "融资余额": [1.9e10],
                    "融资买入额": [1.2e8],
                    "融资偿还额": [1e8],
                }
            )
        return pd.DataFrame(columns=["信用交易日期", "标的证券代码"])  # 无该股

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_margin_detail_sse=_sse)
    )
    # 窗口 08-12 ~ 08-14(3 个工作日)
    out = cap_prov.fetch_margin("600519.SH", "2026-08-12", "2026-08-14")
    assert len(out) == 2  # 08-12 无该股被过滤
    assert out[0]["date"] == "2026-08-13" and out[0]["margin_balance"] == 1.9e10
    assert out[1]["date"] == "2026-08-14"
    assert out[1]["net_buy"] == 1e9  # 2e10 - 1.9e10
    assert out[0]["net_buy"] is None  # 首日无前值
    assert out[0]["short_balance"] is None  # 沪市无融券余额


def test_fetch_margin_szse_short_balance(monkeypatch):
    """深市两融：含融券余额列；非交易日(周末)跳过不请求。"""

    def _szse(date):
        assert date == "20260814"  # 08-15/16 为周末不请求
        return pd.DataFrame(
            {
                "证券代码": ["000001"],
                "证券简称": ["平安银行"],
                "融资买入额": [1e8],
                "融资余额": [2.5e10],
                "融券卖出量": [1e5],
                "融券余量": [2e6],
                "融券余额": [3e8],
                "融资融券余额": [2.53e10],
            }
        )

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_margin_detail_szse=_szse)
    )
    out = cap_prov.fetch_margin("000001.SZ", "2026-08-14", "2026-08-16")
    assert len(out) == 1
    assert out[0]["short_balance"] == 3e8
    assert out[0]["margin_balance"] == 2.5e10
    assert out[0]["net_buy"] is None


def test_fetch_northbound_maps_columns(monkeypatch):
    """北向持股：中文列 → 内部字段；失败返回 None。"""
    raw = pd.DataFrame(
        {
            "持股日期": ["2024-08-15", "2024-08-14"],
            "持股数量": [8e7, 7.9e7],
            "持股数量占A股百分比": [6.3, 6.2],
            "持股市值": [1.2e11, 1.18e11],
        }
    )

    def _api(symbol):
        assert symbol == "600519"
        return raw

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_hsgt_individual_em=_api)
    )
    out = cap_prov.fetch_northbound("600519.SH")
    assert out is not None and len(out) == 2
    assert out[0]["date"] == "2024-08-15" and out[0]["hold_shares"] == 8e7
    assert out[0]["hold_ratio"] == 6.3
    assert "持股市值" not in out[0]  # 非映射列不落库

    # 网络失败 → None
    def boom(*a, **k):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_hsgt_individual_em=boom)
    )
    assert cap_prov.fetch_northbound("600519.SH") is None


# ---------------------------------------------------------------------------
# 端点（mock core.sources 门面）
# ---------------------------------------------------------------------------


def _fake_moneyflow(code):
    return [
        {
            "date": "2026-08-14",
            "main_net": 1e8,
            "super_net": 5e7,
            "large_net": 3e7,
            "medium_net": -2e7,
            "small_net": -6e7,
        },
        {
            "date": "2026-08-13",
            "main_net": -2e7,
            "super_net": 1e7,
            "large_net": -1e7,
            "medium_net": 5e6,
            "small_net": 1.5e7,
        },
    ]


def test_capital_history_moneyflow(api_db, monkeypatch):
    """history 端点：库空 → 拉取落库 → 返回日期倒序数据。"""
    calls = []
    fake = lambda code, ctype, start_date="", end_date="": (
        _fake_moneyflow(code) if ctype == "moneyflow" else [],
        calls.append((code, ctype)),
    )[0]  # noqa: E731
    monkeypatch.setattr("app.core.sources.get_capital_data", fake)
    out = stocks_api.capital_history("600519.SH", type="moneyflow", limit=10)
    assert out["code"] == "600519.SH" and out["type"] == "moneyflow"
    assert len(out["data"]) == 2
    assert out["data"][0]["date"] == "2026-08-14"  # 倒序
    assert out["data"][0]["main_net"] == 1e8
    assert calls == [("600519.SH", "moneyflow")]


def test_capital_history_cached_no_refetch(api_db, monkeypatch):
    """库中已有数据 → moneyflow 不再触发拉取（纯库读）。"""
    fake = lambda code, ctype, start_date="", end_date="": _fake_moneyflow(code)
    monkeypatch.setattr("app.core.sources.get_capital_data", fake)
    stocks_api.capital_history("600519.SH", type="moneyflow", limit=10)
    calls = []

    def boom(code, ctype, start_date="", end_date=""):
        calls.append(ctype)
        raise AssertionError("库已缓存，不应触发拉取")

    monkeypatch.setattr("app.core.sources.get_capital_data", boom)
    out = stocks_api.capital_history("600519.SH", type="moneyflow", limit=10)
    assert calls == []
    assert out["data"][0]["main_net"] == 1e8


def test_capital_history_margin_incremental(api_db, monkeypatch):
    """两融：库最新日期早于今天 → 增量拉取 (last+1, today]，落库后返回。"""
    from app.storage.repos.capital import upsert_capital

    # 固定“今天”，避免窗口断言随着 CI 日期变化。
    monkeypatch.setattr(
        "app.core.fundamentals.now_cn",
        lambda: datetime(2026, 8, 14),
    )

    upsert_capital(
        None,
        "600519.SH",
        "margin",
        [{"date": "2026-08-13", "margin_balance": 1.7e10, "net_buy": 1e8}],
    )
    seen = {}

    def fake(code, ctype, start_date="", end_date=""):
        seen["start"], seen["end"] = start_date, end_date
        return [{"date": "2026-08-14", "margin_balance": 1.71e10, "net_buy": 1e8}]

    monkeypatch.setattr("app.core.sources.get_capital_data", fake)
    out = stocks_api.capital_history("600519.SH", type="margin", limit=10)
    assert seen["start"] == "2026-08-14" and seen["end"] >= "2026-08-14"
    assert out["data"][0]["date"] == "2026-08-14"
    assert out["data"][1]["date"] == "2026-08-13"


def test_capital_history_lhb_initial_window(api_db, monkeypatch):
    """龙虎榜：库空 → 初始窗口 (today-window, today] 拉取；latest 日期当日不再重复拉。"""
    from app.lib.session import now_cn

    today = now_cn().date().isoformat()
    seen = {}

    def fake(code, ctype, start_date="", end_date=""):
        seen["start"], seen["end"] = start_date, end_date
        return [
            {
                "date": today,
                "reason": "日涨幅偏离值达7%",
                "buy_amount": 1e8,
                "sell_amount": 5e7,
                "net_amount": 5e7,
                "deal_amount": 1.5e8,
                "turnover_rate": 1.2,
                "summary": "2家机构买入",
            }
        ]

    monkeypatch.setattr("app.core.sources.get_capital_data", fake)
    out = stocks_api.capital_history("000506.SZ", type="lhb", limit=10)
    assert len(out["data"]) == 1
    assert out["data"][0]["net_amount"] == 5e7
    assert out["data"][0]["reason"] == "日涨幅偏离值达7%"
    # 第二次请求：库最新日期 == 今天(数据已当日拉取) → 不再触发网络
    calls = []

    def boom(code, ctype, start_date="", end_date=""):
        calls.append(1)
        raise AssertionError("当日已拉取，不应重复")

    monkeypatch.setattr("app.core.sources.get_capital_data", boom)
    stocks_api.capital_history("000506.SZ", type="lhb", limit=10)
    assert calls == []


def test_capital_history_invalid_type(api_db):
    """非法 type → 400。"""
    with pytest.raises(HTTPException) as ei:
        stocks_api.capital_history("600519.SH", type="foo")
    assert ei.value.status_code == 400


def test_capital_history_source_failure_uses_cache(api_db, monkeypatch):
    """抓取失败（返回空）→ 端点仍返回 200 与库内数据（降级不阻塞）。"""
    fake = lambda code, ctype, start_date="", end_date="": (
        _fake_moneyflow(code) if ctype == "moneyflow" else []
    )
    monkeypatch.setattr("app.core.sources.get_capital_data", fake)
    stocks_api.capital_history("600519.SH", type="moneyflow", limit=10)
    monkeypatch.setattr(
        "app.core.sources.get_capital_data",
        lambda code, ctype, start_date="", end_date="": [],
    )
    out = stocks_api.capital_history("600519.SH", type="moneyflow", limit=10)
    assert out["data"][0]["main_net"] == 1e8


def test_capital_history_northbound(api_db, monkeypatch):
    """北向：库空 → 拉取落库 → 返回停披露前历史序列。"""
    fake = lambda code, ctype, start_date="", end_date="": [
        {"date": "2024-08-15", "hold_shares": 8e7, "hold_ratio": 6.3},
        {"date": "2024-08-14", "hold_shares": 7.9e7, "hold_ratio": 6.2},
    ]
    monkeypatch.setattr("app.core.sources.get_capital_data", fake)
    out = stocks_api.capital_history("600519.SH", type="northbound", limit=10)
    assert out["type"] == "northbound"
    assert out["data"][0]["date"] == "2024-08-15"
    assert out["data"][0]["hold_ratio"] == 6.3


def test_capital_history_northbound_unavailable_503(api_db, monkeypatch):
    """北向接口失效（门面返回 None）→ 表留空 + 503 说明。"""
    monkeypatch.setattr(
        "app.core.sources.get_capital_data",
        lambda code, ctype, start_date="", end_date="": None,
    )
    with pytest.raises(HTTPException) as ei:
        stocks_api.capital_history("600519.SH", type="northbound", limit=10)
    assert ei.value.status_code == 503
    assert "北向" in ei.value.detail
    # 表确实留空（无任何落库）
    assert stocks_api.capital_latest("600519.SH")["northbound"] is None


def test_capital_latest(api_db, monkeypatch):
    """latest 端点：各类型最新一行合并摘要。"""
    fake = lambda code, ctype, start_date="", end_date="": _fake_moneyflow(code)
    monkeypatch.setattr("app.core.sources.get_capital_data", fake)
    stocks_api.capital_history("600519.SH", type="moneyflow", limit=10)
    out = stocks_api.capital_latest("600519.SH")
    assert out["code"] == "600519.SH"
    assert out["moneyflow"]["date"] == "2026-08-14"
    assert out["lhb"] is None
    assert out["margin"] is None
    assert out["northbound"] is None
