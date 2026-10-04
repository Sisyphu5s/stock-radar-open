"""审查修复测试（P0-29 下单并发防护 / P1-31 序列化批量名称 / P1-34 收益基准标注）。

- P0-29：两线程各自 DB session 并发下单，条件写回（WHERE cash=旧值）防资金/持仓双花；
- P1-31：三个列表端点经 stock_names 批量取名称（N+1 修复），查无回退 _stock_name；
- P1-34：experiment 响应带收益基准标注（理论收益），数值口径不变。
临时 SQLite + mock kline，不触碰生产库。
"""

from __future__ import annotations

import threading

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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


def _vshape_kline(n: int = 90) -> pd.DataFrame:
    """V 形收盘价（先跌后涨）→ 保证 macd 出现金叉，满足 MIN_BARS=20。"""
    dates = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    half = n // 2
    close = np.concatenate(
        [np.linspace(100.0, 60.0, half), np.linspace(60.0, 120.0, n - half)]
    )
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
            "amount": 1e6,
        }
    )


@pytest.fixture()
def paper_api(tmp_path, monkeypatch):
    import app.storage.db as DB
    import app.api.paper as P
    import app.core.paper.orders as PO

    engine = create_engine(
        f"sqlite:///{tmp_path / 'paper_api.db'}",
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
    monkeypatch.setattr(P, "_stock_name", lambda code: f"股{code}")

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


# ---------------------------------------------------------------------------
# P0-29：下单并发防护（条件写回，防资金/持仓双花）
# ---------------------------------------------------------------------------


def test_concurrent_buy_no_double_spend(paper_api):
    """两线程同时买同一账户（各自 DB session）：单笔资金够、两笔超限 → 恰好一单成交
    一单拒绝（资金不足），资金/持仓不穿。"""
    from app.api.paper import OrderCreate, place_order
    from app.storage.db import SessionLocal

    client, _ = paper_api
    acc = _create_acc(client, cash=100000.0)
    aid = acc["id"]
    # bar 价 140 × 500 股 ≈ 70017.5（含佣金），单笔够（<100000），两笔并发超限
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker():
        db = SessionLocal()
        try:
            res = place_order(
                aid,
                OrderCreate(
                    code="600519.SH",
                    side="buy",
                    order_type="market",
                    quantity=500,
                ),
                db,
            )
            results.append(
                {"status": res["status"], "reason": res.get("reject_reason")}
            )
        except Exception as e:  # 不应走到 HTTP 异常（并发冲突应转 rejected 而非 5xx）
            results.append({"status": f"error:{type(e).__name__}", "reason": str(e)})
        finally:
            db.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    statuses = sorted(r["status"] for r in results)
    assert statuses == ["filled", "rejected"], f"实际: {results}"
    rej = next(r for r in results if r["status"] == "rejected")
    assert "资金不足" in rej["reason"]

    # 资金不穿：100000 - 140×500 - 佣金 17.5
    acc_after = client.get(f"/api/v1/paper/accounts/{aid}").json()
    assert acc_after["cash"] == pytest.approx(100000 - 140 * 500 - 17.5)
    # 持仓不穿：恰好成交 500 股
    pos = client.get(f"/api/v1/paper/accounts/{aid}/positions").json()["data"]
    assert sum(p["quantity"] for p in pos) == 500
    # 成交流水恰好 1 条
    trades = client.get(f"/api/v1/paper/accounts/{aid}/trades").json()["data"]
    assert len(trades) == 1


def test_concurrent_buy_different_stocks_both_fill(paper_api):
    """两线程并发买不同股票、资金都够 → 两单都成交（互不误伤），持仓/资金一致。"""
    from app.api.paper import OrderCreate, place_order
    from app.storage.db import SessionLocal

    client, _ = paper_api
    acc = _create_acc(client, cash=100000.0)
    aid = acc["id"]
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(code: str):
        db = SessionLocal()
        try:
            res = place_order(
                aid,
                OrderCreate(code=code, side="buy", order_type="market", quantity=100),
                db,
            )
            results.append(res["status"])
        finally:
            db.close()

    ts = [
        threading.Thread(target=worker, args=(c,)) for c in ("600519.SH", "000001.SZ")
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert sorted(results) == ["filled", "filled"], f"实际: {results}"
    pos = client.get(f"/api/v1/paper/accounts/{aid}/positions").json()["data"]
    assert {p["code"] for p in pos} == {"600519.SH", "000001.SZ"}
    assert sum(p["quantity"] for p in pos) == 200
    # 每单 100 股 × 140 = 14000 + 佣金 5
    acc_after = client.get(f"/api/v1/paper/accounts/{aid}").json()
    assert acc_after["cash"] == pytest.approx(100000 - 2 * (14000 + 5.0))


# ---------------------------------------------------------------------------
# P1-31：序列化批量名称（N+1 修复）
# ---------------------------------------------------------------------------


def test_list_endpoints_batch_stock_names(paper_api, monkeypatch):
    """三个列表端点各经 stock_names 批量取名称（每端点一次），名称正确填充。"""
    import app.api.paper as P

    client, _ = paper_api
    acc = _create_acc(client)
    for code in ("600519.SH", "000001.SZ"):
        r = client.post(
            f"/api/v1/paper/accounts/{acc['id']}/orders",
            json={
                "code": code,
                "side": "buy",
                "order_type": "market",
                "quantity": 100,
            },
        )
        assert r.status_code == 200

    calls: list[set] = []

    def fake_stock_names(db, codes):
        calls.append(set(codes))
        return {c: f"名{c}" for c in codes}

    monkeypatch.setattr(P.stock_repo, "stock_names", fake_stock_names)

    orders = client.get(f"/api/v1/paper/accounts/{acc['id']}/orders").json()["data"]
    assert {o["name"] for o in orders} == {"名600519.SH", "名000001.SZ"}
    pos = client.get(f"/api/v1/paper/accounts/{acc['id']}/positions").json()["data"]
    assert {p["name"] for p in pos} == {"名600519.SH", "名000001.SZ"}
    trades = client.get(f"/api/v1/paper/accounts/{acc['id']}/trades").json()["data"]
    assert {t["name"] for t in trades} == {"名600519.SH", "名000001.SZ"}
    # 三个端点各触发一次批量查询（而非每条一条）
    assert len(calls) == 3, f"批量查询次数: {calls}"
    assert all(c == {"600519.SH", "000001.SZ"} for c in calls)


def test_list_orders_fallback_single_name_when_missing(paper_api, monkeypatch):
    """批量查名缺失的代码回退 _stock_name 单查（不丢名称）。"""
    import app.api.paper as P

    client, _ = paper_api
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
    monkeypatch.setattr(P.stock_repo, "stock_names", lambda db, codes: {})
    orders = client.get(f"/api/v1/paper/accounts/{acc['id']}/orders").json()["data"]
    assert orders[0]["name"] == "股600519.SH"  # 回退 fixture 的 _stock_name 值


# ---------------------------------------------------------------------------
# P1-34：历史实验收益基准标注（理论收益，不改数值）
# ---------------------------------------------------------------------------


def test_experiment_response_marks_return_note(paper_api, monkeypatch):
    """experiment 响应带收益基准标注「理论收益」，r5/r10 数值口径不变。"""
    import app.api.paper as P

    client, _ = paper_api
    monkeypatch.setattr(
        P,
        "cached_kline",
        lambda code, period="daily", max_rows=250: _vshape_kline(90),
    )
    r = client.post(
        "/api/v1/paper/experiment",
        json={"code": "600519.SH", "signals": ["macd_golden_cross"], "days": 90},
    )
    assert r.status_code == 200
    body = r.json()
    assert "return_note" in body
    assert "理论收益" in body["return_note"]
    # 数值口径不变：r5/r10 仍为触发日收盘为基准的第 5/10 根 bar 收益
    close = _vshape_kline(90)["close"].to_numpy()
    dates = _vshape_kline(90)["date"].astype(str).tolist()
    idx = {d: i for i, d in enumerate(dates)}
    for res in body["results"]:
        for e in res["details"]:
            assert {"date", "close", "r5", "r10"} <= set(e)
            i = idx[e["date"]]
            assert e["close"] == round(float(close[i]), 4)
