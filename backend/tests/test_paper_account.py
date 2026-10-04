"""T-01 模拟盘账户测试：撮合 / 绩效纯函数 + 账户端点（临时 SQLite + mock kline）。

覆盖（docs/TODO.md T-01）：
- 撮合语义：市价按 bar 价成交、限价触发判定（买 ≤ / 卖 ≥）、未触发挂起、
  资金/持仓不足拒绝、卖出已实现盈亏与清仓；
- 绩效：重放成交流水 → 净值曲线（收益/回撤/夏普/胜率/平仓口径）；
- 端点：账户 CRUD / 下单撮合 / 撤单 / 持仓估值 / 成交记录 / 绩效查询。
临时 SQLite，不触碰生产库；cached_kline 以 mock 注入合成日线。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.paper.account import (
    LOT_SIZE,
    execute_order,
    equity_curve,
    fee_for,
    resolve_bar_price,
    validate_order,
)
from app.storage.db import Base


def _mk_kline(closes: list[float], dates: list[str] | None = None) -> pd.DataFrame:
    n = len(closes)
    if dates is None:
        dates = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d").tolist()
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000.0] * n,
            "amount": [1e6] * n,
        }
    )


_KLINE = _mk_kline(
    [100.0, 110.0, 120.0, 130.0, 140.0],
    ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
)


# ---------------------------------------------------------------------------
# 撮合 / 费用纯函数
# ---------------------------------------------------------------------------


def test_fee_commission_floor_and_stamp():
    # 小额买入：佣金取最低 5 元，无印花税
    assert fee_for("buy", 10000) == 5.0
    # 大额买入：佣金按比例，无印花税
    assert fee_for("buy", 2_000_000) == round(2_000_000 * 0.00025, 2)
    # 卖出：佣金 + 印花税
    assert fee_for("sell", 2_000_000) == round(
        2_000_000 * 0.00025 + 2_000_000 * 0.0005, 2
    )


def test_validate_order_rules():
    with pytest.raises(ValueError):
        validate_order("hold", "market", None, 100)
    with pytest.raises(ValueError):
        validate_order("buy", "fill", None, 100)
    with pytest.raises(ValueError):
        validate_order("buy", "market", None, 50)  # 非一手整数倍
    with pytest.raises(ValueError):
        validate_order("buy", "market", None, 0)
    with pytest.raises(ValueError):
        validate_order("buy", "limit", None, 100)  # 限价缺价格
    validate_order("buy", "market", None, 200)  # 合法
    validate_order("sell", "limit", 10.5, 100)  # 合法


def test_resolve_bar_price_default_latest_and_explicit():
    df = _mk_kline([100.0, 110.0, 120.0], ["2026-01-02", "2026-01-03", "2026-01-06"])
    assert resolve_bar_price(df) == (120.0, "2026-01-06")
    assert resolve_bar_price(df, "2026-01-03") == (110.0, "2026-01-03")
    with pytest.raises(ValueError):
        resolve_bar_price(df, "2026-01-04")
    with pytest.raises(ValueError):
        resolve_bar_price(_mk_kline([]), None)


def test_execute_market_buy_updates_cost_basis():
    res, cash, positions, realized = execute_order(
        100000.0,
        {},
        {
            "code": "600519.SH",
            "side": "buy",
            "order_type": "market",
            "price": None,
            "quantity": 100,
        },
        100.0,
    )
    assert res["status"] == "filled"
    assert realized is None
    assert cash == 100000 - 10000 - 5.0
    assert positions["600519.SH"]["quantity"] == 100
    assert positions["600519.SH"]["avg_cost"] == 10005.0 / 100  # 费用摊薄入成本


def test_execute_limit_buy_trigger_and_pending():
    order = {
        "code": "600519.SH",
        "side": "buy",
        "order_type": "limit",
        "price": 105.0,
        "quantity": 100,
    }
    res, cash, pos, _ = execute_order(100000.0, {}, order, 110.0)
    assert res["status"] == "pending"  # bar 价 110 > 委托价 105 未触发
    assert cash == 100000 and pos == {}
    res, _, pos, _ = execute_order(100000.0, {}, order, 100.0)
    assert res["status"] == "filled"
    assert pos["600519.SH"]["quantity"] == 100


def test_execute_limit_sell_trigger_rule():
    positions = {"600519.SH": {"quantity": 100, "avg_cost": 100.0}}
    order = {
        "code": "600519.SH",
        "side": "sell",
        "order_type": "limit",
        "price": 115.0,
        "quantity": 100,
    }
    res, _, _, _ = execute_order(0.0, positions, order, 110.0)
    assert res["status"] == "pending"
    res, cash, pos, realized = execute_order(0.0, positions, order, 120.0)
    assert res["status"] == "filled"
    assert realized == round((120 - 100) * 100 - fee_for("sell", 12000), 6)
    assert pos.get("600519.SH") is None  # 清仓不保留零持仓
    assert cash == 12000 - fee_for("sell", 12000)


def test_execute_reject_insufficient_cash_and_position():
    res, cash, pos, _ = execute_order(
        500.0,
        {},
        {
            "code": "600519.SH",
            "side": "buy",
            "order_type": "market",
            "price": None,
            "quantity": 100,
        },
        100.0,
    )
    assert res["status"] == "rejected"
    assert "资金不足" in res["reason"]
    assert cash == 500 and pos == {}
    res, _, _, _ = execute_order(
        100000.0,
        {"600519.SH": {"quantity": 100, "avg_cost": 100.0}},
        {
            "code": "600519.SH",
            "side": "sell",
            "order_type": "market",
            "price": None,
            "quantity": 300,
        },
        120.0,
    )
    assert res["status"] == "rejected"
    assert "持仓不足" in res["reason"]


# ---------------------------------------------------------------------------
# 绩效纯函数
# ---------------------------------------------------------------------------


def test_equity_curve_and_metrics():
    trades = [
        {
            "code": "600519.SH",
            "side": "buy",
            "price": 100.0,
            "quantity": 100,
            "fee": 5.0,
            "bar_date": "2026-01-02",
        },
        {
            "code": "600519.SH",
            "side": "sell",
            "price": 130.0,
            "quantity": 100,
            "fee": 11.5,
            "bar_date": "2026-01-05",
        },
    ]
    out = equity_curve(
        100000.0, trades, lambda c: dict(zip(_KLINE["date"], _KLINE["close"]))
    )
    curve = out["curve"]
    assert [p["date"] for p in curve] == ["2026-01-02", "2026-01-05"]
    # 首日买入后按当日收盘估值（≈ 初始资金 - 费用），末日后清仓只剩现金
    assert curve[0]["equity"] == pytest.approx(100000 - 5.0)
    final = 100000 - 5.0 + (130 - 100) * 100 - 11.5
    assert curve[-1]["equity"] == pytest.approx(final)
    m = out["metrics"]
    assert m["total_return"] == pytest.approx(final / 100000 - 1)
    assert m["trade_count"] == 1
    assert m["winning_trades"] == 1
    assert m["win_rate"] == 1.0
    assert m["initial_cash"] == 100000.0
    assert m["final_equity"] == pytest.approx(final)
    assert m["max_drawdown"] == 0.0  # 单边上涨无回撤
    assert m["sharpe"] is not None
    assert m["days"] == len(curve)


def test_equity_curve_empty_and_losing_trade():
    assert equity_curve(100000.0, [], lambda c: {})["metrics"]["trade_count"] == 0
    trades = [
        {
            "code": "600519.SH",
            "side": "buy",
            "price": 100.0,
            "quantity": 100,
            "fee": 5.0,
            "bar_date": "2026-01-02",
        },
        {
            "code": "600519.SH",
            "side": "sell",
            "price": 95.0,
            "quantity": 100,
            "fee": 9.75,
            "bar_date": "2026-01-05",
        },
    ]
    out = equity_curve(
        100000.0, trades, lambda c: dict(zip(_KLINE["date"], _KLINE["close"]))
    )
    assert out["metrics"]["win_rate"] == 0.0
    assert out["metrics"]["winning_trades"] == 0
    assert out["metrics"]["total_return"] < 0


def test_equity_curve_stale_close_forward_carry():
    """停牌/非交易日向前沿用最近收盘：持仓股当日无 bar 时按最近收盘估值（P2-39 机制保真）。"""
    trades = [
        {
            "code": "600519.SH",
            "side": "buy",
            "price": 100.0,
            "quantity": 100,
            "fee": 5.0,
            "bar_date": "2026-01-02",
        },
        {
            "code": "000001.SZ",
            "side": "buy",
            "price": 20.0,
            "quantity": 100,
            "fee": 5.0,
            "bar_date": "2026-01-05",
        },
    ]
    lookup = {
        "600519.SH": {"2026-01-02": 100.0, "2026-01-04": 110.0},
        "000001.SZ": {"2026-01-05": 20.0},
    }
    out = equity_curve(100000.0, trades, lambda c: lookup[c])
    curve = out["curve"]
    assert [p["date"] for p in curve] == ["2026-01-02", "2026-01-04", "2026-01-05"]
    # 01-04 A 按当日收盘 110；01-05 A 当日无 bar（停牌）沿用 01-04 收盘 110
    assert curve[0]["equity"] == pytest.approx(100000 - 5.0)
    assert curve[1]["equity"] == pytest.approx(100000 - 10000 - 5 + 100 * 110)
    assert curve[2]["equity"] == pytest.approx(
        100000 - 10000 - 5 - 2000 - 5 + 100 * 110 + 100 * 20
    )


def test_equity_curve_integrity_defensive_sell():
    """数据完整性防御（P2-53）：持仓缺失/不足的卖出被跳过——realized 不虚高、qty 不为负。"""
    closes = lambda c: dict(zip(_KLINE["date"], _KLINE["close"]))
    # 无对应买入的裸卖出：现金不虚增、不计入已平仓
    orphan = [
        {
            "code": "600519.SH",
            "side": "sell",
            "price": 130.0,
            "quantity": 100,
            "fee": 11.5,
            "bar_date": "2026-01-05",
        }
    ]
    out = equity_curve(100000.0, orphan, closes)
    assert out["metrics"]["trade_count"] == 0
    assert out["metrics"]["win_rate"] is None
    assert out["curve"][-1]["equity"] == pytest.approx(100000.0)
    # 卖出量超过持仓量：整笔跳过，持仓保留、qty 不为负
    oversell = [
        {
            "code": "600519.SH",
            "side": "buy",
            "price": 100.0,
            "quantity": 100,
            "fee": 5.0,
            "bar_date": "2026-01-02",
        },
        {
            "code": "600519.SH",
            "side": "sell",
            "price": 130.0,
            "quantity": 300,
            "fee": 11.5,
            "bar_date": "2026-01-05",
        },
    ]
    out = equity_curve(100000.0, oversell, closes)
    assert out["metrics"]["trade_count"] == 0
    assert out["curve"][-1]["equity"] == pytest.approx(100000 - 5.0 - 10000.0 + 110 * 100)


# ---------------------------------------------------------------------------
# 端点层：临时 SQLite + mock kline
# ---------------------------------------------------------------------------


@pytest.fixture()
def account_api(tmp_path, monkeypatch):
    import app.storage.db as DB
    import app.api.paper as P
    import app.core.paper.orders as PO

    engine = create_engine(
        f"sqlite:///{tmp_path / 'paper_account.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(DB, "SessionLocal", Maker)

    def _kline(
        code, period="daily", max_rows=400, refresh_if_stale=True, start_date=None
    ):
        return _KLINE.copy()

    monkeypatch.setattr(P, "cached_kline", _kline)
    monkeypatch.setattr(
        PO, "cached_kline", _kline
    )  # 下单取 K 线编排在 core.paper.orders
    monkeypatch.setattr(P, "_stock_name", lambda code: "贵州茅台")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.paper import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), Maker


def _create_acc(client, cash=100000.0):
    r = client.post(
        "/api/v1/paper/accounts", json={"name": "测试账户", "initial_cash": cash}
    )
    assert r.status_code == 200
    return r.json()


def test_account_crud(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    assert acc["name"] == "测试账户"
    assert acc["initial_cash"] == 100000.0
    assert acc["cash"] == 100000.0
    assert acc["id"] >= 1

    assert client.get("/api/v1/paper/accounts").json()["data"][0]["id"] == acc["id"]
    assert client.get(f"/api/v1/paper/accounts/{acc['id']}").json()["cash"] == 100000.0
    assert client.get("/api/v1/paper/accounts/999").status_code == 404

    r = client.delete(f"/api/v1/paper/accounts/{acc['id']}")
    assert r.status_code == 204
    assert client.get("/api/v1/paper/accounts").json()["data"] == []


def test_market_buy_fills_and_updates_position(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
    )
    assert r.status_code == 200
    o = r.json()
    assert o["status"] == "filled"
    assert o["filled_qty"] == 100
    assert o["filled_avg_price"] == 140.0  # bar_date 缺省 → 最新 bar 收盘价
    assert o["bar_date"] == "2026-01-08"

    pos = client.get(f"/api/v1/paper/accounts/{acc['id']}/positions").json()["data"]
    assert len(pos) == 1
    assert pos[0]["code"] == "600519.SH"
    assert pos[0]["quantity"] == 100
    assert pos[0]["close"] == 140.0  # 最新 bar 估值
    assert pos[0]["market_value"] == 14000.0

    acc_after = client.get(f"/api/v1/paper/accounts/{acc['id']}").json()
    assert acc_after["cash"] == pytest.approx(100000 - 14000 - 5.0)

    trades = client.get(f"/api/v1/paper/accounts/{acc['id']}/trades").json()["data"]
    assert len(trades) == 1
    assert trades[0]["side"] == "buy"
    assert trades[0]["amount"] == 14000.0


def test_limit_not_triggered_pending_then_cancel(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "buy",
            "order_type": "limit",
            "price": 90.0,
            "quantity": 100,
        },
    )
    o = r.json()
    assert o["status"] == "pending"  # 最新 bar 140 > 90 未触发

    acc_after = client.get(f"/api/v1/paper/accounts/{acc['id']}").json()
    assert acc_after["cash"] == 100000.0  # 挂起不动资金
    assert (
        client.get(f"/api/v1/paper/accounts/{acc['id']}/positions").json()["data"] == []
    )

    # 撤单成功
    r = client.delete(f"/api/v1/paper/accounts/{acc['id']}/orders/{o['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "canceled"
    # 重复撤单 400
    r = client.delete(f"/api/v1/paper/accounts/{acc['id']}/orders/{o['id']}")
    assert r.status_code == 400
    assert "仅挂起" in r.json()["detail"]


def test_limit_buy_triggered_at_floor(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "buy",
            "order_type": "limit",
            "price": 140.0,
            "quantity": 100,
            "bar_date": "2026-01-08",
        },
    )
    assert r.json()["status"] == "filled"
    assert r.json()["filled_avg_price"] == 140.0
    assert r.json()["bar_date"] == "2026-01-08"


def test_sell_insufficient_position_rejected(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "sell",
            "order_type": "market",
            "quantity": 100,
        },
    )
    o = r.json()
    assert o["status"] == "rejected"
    assert "持仓不足" in o["reject_reason"]


def test_validation_errors(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    # 非一手整数倍
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "buy",
            "order_type": "market",
            "quantity": 50,
        },
    )
    assert r.status_code == 400
    # 非法方向
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "hold",
            "order_type": "market",
            "quantity": 100,
        },
    )
    assert r.status_code == 400
    # 非日线周期
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
            "period": "5",
        },
    )
    assert r.status_code == 400
    # 不存在的 bar 时点
    r = client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
            "bar_date": "2026-05-01",
        },
    )
    assert r.status_code == 422


def test_performance_endpoint_buy_sell(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
            "bar_date": "2026-01-02",
        },
    )
    client.post(
        f"/api/v1/paper/accounts/{acc['id']}/orders",
        json={
            "code": "600519.SH",
            "side": "sell",
            "order_type": "market",
            "quantity": 100,
            "bar_date": "2026-01-07",
        },
    )
    out = client.get(f"/api/v1/paper/accounts/{acc['id']}/performance").json()
    curve = out["curve"]
    assert curve and curve[0]["date"] == "2026-01-02"
    assert curve[-1]["date"] == "2026-01-07"
    m = out["metrics"]
    assert m["trade_count"] == 1
    assert m["winning_trades"] == 1
    assert m["win_rate"] == 1.0
    assert m["total_return"] > 0
    assert m["final_equity"] == pytest.approx(curve[-1]["equity"])


def test_performance_endpoint_empty_account(account_api):
    client, _ = account_api
    acc = _create_acc(client)
    out = client.get(f"/api/v1/paper/accounts/{acc['id']}/performance").json()
    assert out["curve"] == []
    assert out["metrics"]["trade_count"] == 0
    assert out["metrics"]["total_return"] is None
    assert out["metrics"]["final_equity"] == 100000.0
