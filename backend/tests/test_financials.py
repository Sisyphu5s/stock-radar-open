"""T-06 基本面历史库测试：模型建表/仓储 upsert 幂等/抓取函数(mock akshare)/端点。

模式与 test_todos 一致：临时 SQLite（Base.metadata.create_all）+ 直接调用路由函数；
端点内部自建会话经 monkeypatch app.storage.db.SessionLocal 指向临时库，不触碰生产库。
"""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import stocks as stocks_api
from app.storage.db import Base
from app.storage.providers import financials as fin_prov
from app.storage.repos import financials as repo

_TABLES = (
    "financials_balance",
    "financials_income",
    "financials_cash",
    "valuation_history",
)


@pytest.fixture()
def session(tmp_path):
    """独立临时 SQLite（Base.metadata 含四张新表）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fin.db'}", connect_args={"check_same_thread": False}
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
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
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


def test_financial_models_registered():
    """四张表已注册进共享 MetaData（init_db 的 create_all 自动建表，无需手工迁移）。"""
    names = Base.metadata.tables
    for t in _TABLES:
        assert t in names


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


def test_upsert_financials_idempotent(session):
    """同 (code, report_date) 重复 upsert 覆盖不新增；查询报告期倒序。"""
    repo.upsert_financials(
        session,
        "600519.SH",
        "balance",
        [
            {
                "report_date": "2026-03-31",
                "monetary_funds": 100.0,
                "total_assets": 1000.0,
            }
        ],
    )
    repo.upsert_financials(
        session,
        "600519.SH",
        "balance",
        [
            {
                "report_date": "2026-03-31",
                "monetary_funds": 200.0,
                "total_assets": 1000.0,
            }
        ],
    )
    repo.upsert_financials(
        session,
        "600519.SH",
        "balance",
        [
            {
                "report_date": "2026-06-30",
                "monetary_funds": 300.0,
                "total_assets": 1100.0,
            }
        ],
    )
    out = repo.list_financials(session, "600519.SH", "balance", 10)
    assert len(out) == 2  # 同报告期覆盖不新增
    assert out[0].report_date == "2026-06-30"  # 倒序（新在前）
    by_date = {r.report_date: r for r in out}
    assert by_date["2026-03-31"].monetary_funds == 200.0


def test_income_and_latest(session):
    """利润表入库 + latest 查询 + 分表独立 latest_date。"""
    repo.upsert_financials(
        session,
        "600519.SH",
        "income",
        [
            {
                "report_date": "2026-03-31",
                "revenue": 1000.0,
                "operating_cost": 600.0,
                "parent_net_profit": 200.0,
                "gross_margin": 40.0,
                "net_margin": 21.0,
            }
        ],
    )
    row = repo.latest_financials(session, "600519.SH", "income")
    assert row is not None and row.revenue == 1000.0 and row.gross_margin == 40.0
    assert repo.latest_financial_date(session, "600519.SH", "income") == "2026-03-31"
    assert repo.latest_financial_date(session, "600519.SH", "cash") is None  # 空表
    assert repo.latest_financials(session, "600519.SH", "cash") is None


def test_valuation_upsert_and_list(session):
    """估值 upsert 幂等 + 日期倒序 + latest_date。"""
    rows = [
        {
            "date": "2026-08-14",
            "pe": 20.5,
            "pb": 6.2,
            "ps": 9.6,
            "total_mv": 16775.97,
            "float_mv": 16775.97,
        },
        {
            "date": "2026-08-13",
            "pe": 20.48,
            "pb": 6.25,
            "ps": 9.66,
            "total_mv": 16942.23,
            "float_mv": 16942.23,
        },
    ]
    repo.upsert_valuation(session, "600519.SH", rows)
    out = repo.list_valuation(session, "600519.SH", 10)
    assert len(out) == 2
    assert out[0].date == "2026-08-14"  # 倒序
    assert repo.latest_valuation_date(session, "600519.SH") == "2026-08-14"
    assert repo.latest_valuation_date(session, "000001.SZ") is None


# ---------------------------------------------------------------------------
# 抓取函数（mock akshare）
# ---------------------------------------------------------------------------


def test_fetch_income_maps_columns(monkeypatch):
    """利润表：EM 英文列 → 内部字段 + 派生毛利率/净利率（百分数值）。"""
    raw = pd.DataFrame(
        {
            "REPORT_DATE": ["2026-03-31 00:00:00", "2025-12-31 00:00:00"],
            "OPERATE_INCOME": [1000.0, 900.0],
            "OPERATE_COST": [600.0, 540.0],
            "PARENT_NETPROFIT": [200.0, 180.0],
            "NETPROFIT": [210.0, 190.0],
            "BASIC_EPS": [1.6, 1.4],
        }
    )

    def _api(symbol):
        assert symbol == "SH600519"
        return raw

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_profit_sheet_by_report_em=_api)
    )
    out = fin_prov.fetch_financials("600519.SH", "income")
    assert len(out) == 2
    r0 = out[0]
    assert r0["report_date"] == "2026-03-31"
    assert r0["revenue"] == 1000.0
    assert r0["parent_net_profit"] == 200.0
    assert r0["gross_margin"] == 40.0  # (1000-600)/1000*100
    assert r0["net_margin"] == 21.0  # 210/1000*100
    assert r0["eps"] == 1.6


def test_fetch_balance_maps_columns(monkeypatch):
    """资产负债表：EM 英文列 → 内部字段；缺列字段为 None。"""
    raw = pd.DataFrame(
        {
            "REPORT_DATE": ["2025-12-31"],
            "MONETARYFUNDS": [500.0],
            "INVENTORY": [300.0],
            "TOTAL_ASSETS": [2000.0],
            "TOTAL_LIABILITIES": [800.0],
            "TOTAL_PARENT_EQUITY": [1100.0],
        }
    )

    def _api(symbol):
        assert symbol == "SZ000001"
        return raw

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_balance_sheet_by_report_em=_api)
    )
    out = fin_prov.fetch_financials("000001.SZ", "balance")
    assert len(out) == 1
    r = out[0]
    assert r["monetary_funds"] == 500.0
    assert r["total_assets"] == 2000.0
    assert r["parent_equity"] == 1100.0
    assert r["goodwill"] is None  # 接口缺列 → None（降级不报错）


def test_fetch_financials_failure_returns_empty(monkeypatch):
    """抓取失败（网络不可达）→ 降级返回 []，不抛错。"""

    def boom(*a, **k):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setitem(
        sys.modules, "akshare", _fake_ak_module(stock_cash_flow_sheet_by_report_em=boom)
    )
    assert fin_prov.fetch_financials("600519.SH", "cash") == []


def test_fetch_valuation_history(monkeypatch):
    """估值抓取：中文列 → 内部字段；市值 元 → 亿；日期归一。"""
    raw = pd.DataFrame(
        {
            "数据日期": ["2026-08-14", "2026-08-13"],
            "PE(TTM)": [20.5, 20.48],
            "市净率": [6.2, 6.25],
            "市销率": [9.6, 9.66],
            "总市值": [1.677597e12, 1.694223e12],
            "流通市值": [1.677597e12, 1.694223e12],
        }
    )

    def _api(symbol):
        assert symbol == "600519"
        return raw

    monkeypatch.setitem(sys.modules, "akshare", _fake_ak_module(stock_value_em=_api))
    out = fin_prov.fetch_valuation_history("600519")
    assert len(out) == 2
    assert out[0]["date"] == "2026-08-14"
    assert out[0]["pe"] == 20.5
    assert out[0]["total_mv"] == pytest.approx(16775.97)  # 1.677597e12 元 → 亿


# ---------------------------------------------------------------------------
# 端点（mock core.sources 门面）
# ---------------------------------------------------------------------------


def _fake_history_all(code, report_type):
    """三表统一 mock：近期报告期（< REFRESH_FINANCIAL_DAYS，不触发重复拉取）。"""
    if report_type == "balance":
        return [
            {
                "report_date": "2026-06-30",
                "monetary_funds": 300.0,
                "total_assets": 1100.0,
            },
            {
                "report_date": "2026-03-31",
                "monetary_funds": 200.0,
                "total_assets": 1000.0,
            },
        ]
    if report_type == "income":
        return [
            {
                "report_date": "2026-06-30",
                "revenue": 1200.0,
                "parent_net_profit": 250.0,
                "gross_margin": 42.0,
            },
        ]
    return [
        {
            "report_date": "2026-06-30",
            "net_operate_cash": 800.0,
            "net_invest_cash": -300.0,
        },
    ]


def test_financials_history_balance(api_db, monkeypatch):
    """history 端点：库空 → 拉取落库 → 返回报告期倒序数据。"""
    calls = []
    fake = lambda code, report_type: (
        _fake_history_all(code, report_type),
        calls.append(report_type),
    )[0]  # noqa: E731
    monkeypatch.setattr("app.core.sources.get_financial_history", fake)
    out = stocks_api.financials_history("600519.SH", type="balance", limit=10)
    assert out["code"] == "600519.SH" and out["type"] == "balance"
    assert len(out["data"]) == 2
    assert out["data"][0]["report_date"] == "2026-06-30"  # 倒序
    assert out["data"][0]["monetary_funds"] == 300.0
    assert calls == ["balance"]


def test_financials_history_cached_no_refetch(api_db, monkeypatch):
    """库中已有近期数据 → 不再触发拉取（纯库读）。"""
    monkeypatch.setattr("app.core.sources.get_financial_history", _fake_history_all)
    stocks_api.financials_history("600519.SH", type="income", limit=10)
    calls = []

    def boom(code, report_type):
        calls.append(report_type)
        raise AssertionError("库已缓存，不应触发拉取")

    monkeypatch.setattr("app.core.sources.get_financial_history", boom)
    out = stocks_api.financials_history("600519.SH", type="income", limit=10)
    assert calls == []
    assert out["data"][0]["report_date"] == "2026-06-30"


def test_financials_history_valuation(api_db, monkeypatch):
    """估值历史：库空 → 拉取落库 → 返回日期倒序序列。"""
    fake = lambda code: [  # noqa: E731
        {
            "date": "2026-08-14",
            "pe": 20.5,
            "pb": 6.2,
            "ps": 9.6,
            "total_mv": 16775.97,
            "float_mv": 16775.97,
        },
        {
            "date": "2026-08-13",
            "pe": 20.48,
            "pb": 6.25,
            "ps": 9.66,
            "total_mv": 16942.23,
            "float_mv": 16942.23,
        },
    ]
    monkeypatch.setattr("app.core.sources.get_valuation_history", fake)
    out = stocks_api.financials_history("600519.SH", type="valuation", limit=10)
    assert out["type"] == "valuation"
    assert len(out["data"]) == 2
    assert out["data"][0]["date"] == "2026-08-14"
    assert out["data"][0]["total_mv"] == 16775.97


def test_financials_history_invalid_type(api_db):
    """非法 type → 400。"""
    with pytest.raises(HTTPException) as ei:
        stocks_api.financials_history("600519.SH", type="foo")
    assert ei.value.status_code == 400


def test_financials_history_source_failure_uses_cache(api_db, monkeypatch):
    """抓取失败（返回空）→ 端点仍返回 200 与库内数据（降级不阻塞）。"""
    monkeypatch.setattr("app.core.sources.get_financial_history", _fake_history_all)
    stocks_api.financials_history("600519.SH", type="cash", limit=10)
    monkeypatch.setattr("app.core.sources.get_financial_history", lambda c, t: [])
    out = stocks_api.financials_history("600519.SH", type="cash", limit=10)
    assert out["data"][0]["net_operate_cash"] == 800.0


def test_financials_latest(api_db, monkeypatch):
    """latest 端点：三表最近一期合并摘要 + 最新 period。"""
    monkeypatch.setattr("app.core.sources.get_financial_history", _fake_history_all)
    out = stocks_api.financials_latest("600519.SH")
    assert out["code"] == "600519.SH"
    assert out["period"] == "2026-06-30"
    assert out["balance"]["monetary_funds"] == 300.0
    assert out["income"]["gross_margin"] == 42.0
    assert out["cash"]["net_operate_cash"] == 800.0
