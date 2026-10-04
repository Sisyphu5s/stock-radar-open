"""审计修复第三轮回归测试（P1-4 / P2-3 / P2-4 / P2-7 / P2-8）。

覆盖：
- P1-4: scan_once 进程级互斥锁——锁占用时第二路返回 busy 且不进入扫描逻辑，
        锁释放后正常执行一轮（调度扫描与手动扫描并发不再双写重复事件）
- P2-3: signals IN 子句超 SQLite 变量上限——自选股 >400 只走分批查询路径
        （/events 与 /events/page 均正确，非自选股事件不混入）
- P2-4: factors expression 非 str 400；alpha101/evaluate horizon clamp [1,250]；
        copilot context/fundamental 守卫
- P2-7: scanner 批量 upsert Stock（重复扫描不新增行、价格正确更新）；
        factors.list_factors 批量版本查询内存分组（多因子多版本正确）
- P2-8: kdj_dead_cross 一次计算 kdj（修复前全序列算两遍）
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock


@pytest.fixture(scope="module", autouse=True)
def numpy_backend():
    from app.config import settings
    from app.lib.alpha import backend as B

    settings.gp_backend = "numpy"
    B.init_backend()
    yield


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB：scanner.SessionLocal 与 factors.service.SessionLocal 均指向临时库。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.scanning as SC
    import app.core.factors as FS

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(FS, "SessionLocal", Maker)

    from app.api import signals as S

    S._sector_codes_cache.clear()

    yield Maker
    engine.dispose()


# ---------------------------------------------------------------------------
# K 线 / 扫描桩
# ---------------------------------------------------------------------------


def _daily_kline(
    n: int = 60,
    last_date: str = "2026-08-10",
    last_pct: float = 1.0,
    end_close: float = 11.0,
):
    close = np.linspace(10.0, end_close, n)
    return pd.DataFrame(
        {
            "date": pd.bdate_range(end=last_date, periods=n)
            .strftime("%Y-%m-%d")
            .tolist(),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
            "pct_change": np.r_[np.zeros(n - 1), [last_pct]],
        }
    )


def _patch_scanner(monkeypatch, kline: pd.DataFrame, price: float = 1500.0):
    import types
    import app.core.scanning as SC

    spot = pd.DataFrame(
        {
            "code": ["600519.SH"],
            "name": ["贵州茅台"],
            "price": [price],
            "pct_change": [1.0],
            "volume": [1000.0],
            "amount": [1e8],
            "turnover_rate": [0.5],
        }
    )
    monkeypatch.setattr(SC, "get_spot", lambda: spot.copy())
    monkeypatch.setattr(
        SC,
        "fetch_many",
        lambda codes, period="daily", workers=12, progress_cb=None, start_date=None: {
            c: kline.copy() for c in codes
        },
    )
    monkeypatch.setattr(SC, "get_provider", lambda: types.SimpleNamespace(name="test"))
    return SC


# ---------------------------------------------------------------------------
# P1-4: scan_once 进程级互斥锁
# ---------------------------------------------------------------------------


def test_scan_once_mutex_busy_when_locked(monkeypatch):
    """锁被占用时第二路 scan_once 立即返回 busy，且不进入任何扫描逻辑。"""
    import app.core.scanning as SC

    called: list = []
    monkeypatch.setattr(SC, "get_spot", lambda: called.append(1) or pd.DataFrame())
    SC._scan_lock.acquire()
    try:
        res = SC.scan_once()
    finally:
        SC._scan_lock.release()
    assert res["source"] == "busy"
    assert called == [], "锁占用时不得进入扫描逻辑"


def test_scan_once_mutex_release_runs(monkeypatch):
    """锁释放后正常执行一轮（mock 空快照 → source=empty，证明内部路径可达）。"""
    import app.core.scanning as SC

    monkeypatch.setattr(SC, "get_spot", lambda: pd.DataFrame())
    res = SC.scan_once()
    assert res["source"] == "empty", "锁释放后应正常执行一轮扫描"


def test_scan_once_mutex_serializes_two_threads(temp_db, monkeypatch):
    """两线程并发 scan_once：只有一轮真正执行，另一轮 busy（mock 快速路径计数）。"""
    import threading
    import time
    import app.core.scanning as SC

    entered: list[str] = []

    def slow_get_spot():
        entered.append("spot")
        time.sleep(0.05)
        return pd.DataFrame()  # empty → 正常结束一轮

    monkeypatch.setattr(SC, "get_spot", slow_get_spot)
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(SC.scan_once())

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(results) == 2
    assert sorted(r["source"] for r in results) == ["busy", "empty"], (
        "两线程并发必须只有一轮进入扫描逻辑（互斥），另一路 busy"
    )
    assert len(entered) == 1, "扫描内部逻辑只允许执行一次"


# ---------------------------------------------------------------------------
# P2-3: signals IN 子句分批（自选股 >400 只）
# ---------------------------------------------------------------------------


def _seed_large_watchlist(db: sessionmaker, n_wl: int = 450):
    """n_wl 只自选股 + 2 条自选股事件 + 1 条非自选股事件。"""
    db_ = db()
    try:
        base = datetime(2026, 8, 10, 15, 0)
        for i in range(n_wl):
            db_.add(
                Stock(
                    code=f"{600000 + i:06d}.SH",
                    name=f"s{i}",
                    is_watchlist=True,
                    last_price=1.0,
                    pct_change=0.0,
                    volume=0.0,
                    amount=0.0,
                    turnover_rate=0.0,
                )
            )
        for code in ("600000.SH", "600100.SH"):
            db_.add(
                SignalEvent(
                    stock_code=code,
                    signals=["price_up"],
                    status="观察",
                    evidence={},
                    triggered_at=base,
                )
            )
        # 非自选股事件：watchlist_only 下不应出现
        db_.add(
            SignalEvent(
                stock_code="999999.SZ",
                signals=["price_up"],
                status="观察",
                evidence={},
                triggered_at=base,
            )
        )
        db_.commit()
    finally:
        db_.close()


def test_events_watchlist_large_chunked(temp_db):
    """自选股 450 只（>400 分批阈值）：/events 只返回自选股事件，语义正确。"""
    from app.api.signals import list_events

    _seed_large_watchlist(temp_db)
    db = temp_db()
    try:
        r = list_events(watchlist_only=True, time_range="all", db=db)
        assert {x["stock_code"] for x in r["data"]} == {"600000.SH", "600100.SH"}
        assert all(x["is_watchlist"] for x in r["data"])
    finally:
        db.close()


def test_events_page_watchlist_large_chunked(temp_db):
    """分页端点大自选集合：total/items/has_more 与单次 in_ 语义一致。"""
    from app.api.signals import list_events_page

    _seed_large_watchlist(temp_db)
    db = temp_db()
    try:
        rp = list_events_page(watchlist_only=True, time_range="all", db=db)
        assert rp["total"] == 2
        assert {it["stock_code"] for it in rp["items"]} == {"600000.SH", "600100.SH"}
        assert rp["has_more"] is False

        # 分页 offset 越过全部 → 空页
        rp2 = list_events_page(
            watchlist_only=True, time_range="all", db=db, offset=10, limit=50
        )
        assert rp2["total"] == 2 and rp2["items"] == []

        # any 信号过滤（分块内存路径）
        rp3 = list_events_page(
            watchlist_only=True, time_range="all", db=db, signal_types="volume_surge"
        )
        assert rp3["total"] == 0
    finally:
        db.close()


def test_events_watchlist_small_unaffected(temp_db):
    """自选股 ≤400 只仍走单次 in_ 路径，行为不回归。"""
    from app.api.signals import list_events

    # 120 只（>100 覆盖事件代码 600100，≤400 走单次 in_ 路径）
    _seed_large_watchlist(temp_db, n_wl=120)
    db = temp_db()
    try:
        r = list_events(watchlist_only=True, time_range="all", db=db)
        assert {x["stock_code"] for x in r["data"]} == {"600000.SH", "600100.SH"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# P2-4: 输入校验（非法输入 400 而非 500）
# ---------------------------------------------------------------------------


def test_factors_expression_non_str_400(temp_db):
    from app.api.factors import new_factor, new_version

    # new_factor：expression 为 JSON 数字 → 400（修复前 service .strip() AttributeError → 500）
    with pytest.raises(HTTPException) as ei:
        new_factor({"name": "f", "expression": 123})
    assert ei.value.status_code == 400
    # new_version 同款
    with pytest.raises(HTTPException) as ei2:
        new_version(1, {"expression": {"a": 1}})
    assert ei2.value.status_code == 400
    # 合法字符串不受影响（expression 空仍 400）
    with pytest.raises(HTTPException) as ei3:
        new_factor({"name": "f", "expression": "   "})
    assert ei3.value.status_code == 400


def test_alpha101_evaluate_horizon_clamped(monkeypatch):
    """horizon=0 → clamp 1；horizon=300 → clamp 250；合法值原样传递。"""
    import app.api.alpha as A
    import app.lib.alpha.alpha101 as A101
    import app.core.datasets as DS
    import app.lib.alpha.evaluate as EV

    seen: list[int] = []
    monkeypatch.setattr(
        EV,
        "forward_returns",
        lambda close, horizon: (seen.append(horizon), np.zeros(close.shape))[1],
    )
    monkeypatch.setattr(EV, "evaluate_factor", lambda rpn, data, fwd: {"ic": 0.0})
    monkeypatch.setattr(
        DS, "load_panel", lambda did: {"panel": {"close": np.zeros((10, 50))}}
    )
    monkeypatch.setattr(
        DS, "panel_ready", lambda did, features=None: True
    )  # C11a 热缓存探测
    monkeypatch.setattr(
        A101,
        "get_alpha",
        lambda aid: {
            "name": "x",
            "formula": "ts_mean(close,5)",
            "desc": "",
            "usage": "",
        },
    )

    A.alpha101_evaluate({"alpha_id": 1, "dataset_id": 1, "horizon": 0})
    assert seen == [1], "horizon=0 必须 clamp 到 1（修复前 [:-0] 全切片全零）"

    A.alpha101_evaluate({"alpha_id": 1, "dataset_id": 1, "horizon": 300})
    assert seen == [1, 250], "horizon=300 必须 clamp 到 250"

    A.alpha101_evaluate({"alpha_id": 1, "dataset_id": 1, "horizon": 5})
    assert seen == [1, 250, 5], "合法 horizon 原样传递"


def test_market_chat_context_guards(monkeypatch):
    """context 非 dict → 400；fundamental 非 dict → 忽略不炸（500 修复）。"""
    import app.api.copilot as C

    # context 非 dict → 400
    with pytest.raises(HTTPException) as ei:
        asyncio.run(C.market_chat({"question": "q", "context": "bad"}))
    assert ei.value.status_code == 400

    # fundamental 非 dict（如字符串）：build_market_context 前被剥离，正常出流
    resp = asyncio.run(
        C.market_chat({"question": "q", "context": {"fundamental": "not-a-dict"}})
    )
    assert resp.status_code == 200
    assert resp.media_type == "text/event-stream"

    # fundamental 合法 dict：不受影响
    resp2 = asyncio.run(
        C.market_chat(
            {"question": "q", "context": {"fundamental": {"pe": 10.0, "pb": 1.5}}}
        )
    )
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# P2-7: N+1 消除
# ---------------------------------------------------------------------------


def test_scan_upserts_stock_batch(temp_db, monkeypatch):
    """扫描批量 upsert Stock：重复扫描不新增行、价格正确更新（修复前逐行 db.get N+1）。"""
    from app.storage.models import Stock
    from app.core.scanning import scan_once

    _patch_scanner(
        monkeypatch,
        kline=_daily_kline(last_date="2026-08-10", end_close=11.0),
        price=1500.0,
    )
    assert scan_once()["events"] == 1

    db = temp_db()
    try:
        assert db.query(Stock).count() == 1
        assert db.get(Stock, "600519.SH").last_price == 1500.0
    finally:
        db.close()

    # 第二轮：价格变化 → 更新既有行而非新增
    _patch_scanner(
        monkeypatch,
        kline=_daily_kline(last_date="2026-08-10", end_close=11.0),
        price=1600.0,
    )
    assert scan_once()["events"] == 1
    db = temp_db()
    try:
        assert db.query(Stock).count() == 1, "重复扫描不得新增 Stock 行"
        assert db.get(Stock, "600519.SH").last_price == 1600.0
    finally:
        db.close()


def test_list_factors_versions_batch_grouped(temp_db):
    """list_factors：多因子多版本一次批量查询后内存分组（修复前逐因子 N+1）。"""
    from app.core.factors import create_factor, create_version, list_factors

    f1 = create_factor("f1", "ts_mean(close,5)", "d", 1)
    f1_id = f1["factor"]["id"]
    create_version(f1_id, "delta(close,1)")
    create_factor("f2", "ts_mean(close,10)", "d", 1)

    factors = list_factors()
    by_name = {f["name"]: f for f in factors}
    assert {f["name"] for f in factors} == {"f1", "f2"}
    assert [v["version"] for v in by_name["f1"]["versions"]] == [2, 1], (
        "f1 两版本按 version 降序"
    )
    assert [v["version"] for v in by_name["f2"]["versions"]] == [1]


# ---------------------------------------------------------------------------
# P2-8: kdj_dead_cross 一次计算
# ---------------------------------------------------------------------------


def test_kdj_dead_cross_computes_kdj_once(monkeypatch):
    import app.lib.signals.engine as ENG

    n = 60
    close = np.linspace(10.0, 12.0, n)
    df = pd.DataFrame(
        {
            "date": pd.bdate_range(end="2026-08-10", periods=n).strftime("%Y-%m-%d"),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
        }
    )

    calls: list[int] = []
    orig = ENG.kdj

    def spy(high, low, close_):
        calls.append(1)
        return orig(high, low, close_)

    monkeypatch.setattr(ENG, "kdj", spy)
    ok, msg = ENG._kdj_dead_cross(df, {})
    assert calls == [1], "kdj_dead_cross 必须只计算一次 kdj（修复前算两遍）"
    assert bool(ok) is False, "KDJ 死叉在该单调上涨序列下不命中"
    assert "下穿" in msg

    # golden_cross 同样一次
    calls.clear()
    ENG._kdj_golden_cross(df, {})
    assert calls == [1]


# ---------------------------------------------------------------------------
# P0-22: kdj_dead_cross / trix_golden_cross 链式比较错位修复
# 修复前 `a >= b > c` 链式第二项错配为 昨日D > 今日K（正确应为 今日K < 今日D）；
# trix 的 `x <= y < z` 同理，第二项错配为 昨日MATRIX < 今日TRIX（缺今日 MATRIX）。
# 修复后统一为显式双条件（同 _macd_golden_cross 写法）。
# ---------------------------------------------------------------------------


def _ohlc_df(close: np.ndarray) -> pd.DataFrame:
    """由收盘序列构造 OHLCV df（open/high/low 由 close 平移，测试形态不受影响）。"""
    n = len(close)
    return pd.DataFrame(
        {
            "date": pd.bdate_range(end="2026-08-10", periods=n).strftime("%Y-%m-%d"),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 1000.0),
            "amount": np.full(n, 1e6),
        }
    )


def test_kdj_dead_cross_chain_fix():
    """P0-22: 死叉只认 昨日K≥D 且 今日K<D（链式错位修复）。"""
    import app.lib.signals.engine as ENG

    n = 30
    # 正例：倒V —— 最后两根 冲高→跌到新低 ⇒ 昨日 K>=D 且 今日 K<D → 死叉
    close = np.full(n, 10.0)
    close[-2] = 16.0
    close[-1] = 4.0
    ok, msg = ENG._kdj_dead_cross(_ohlc_df(close), {})
    assert ok is True
    assert "下穿" in msg
    # 反例：V形 —— 最后两根 触底→反弹 ⇒ 昨日 K<D 且 今日 K>=D（金叉日，非死叉）
    close = np.full(n, 10.0)
    close[-2] = 4.0
    close[-1] = 16.0
    ok, msg = ENG._kdj_dead_cross(_ohlc_df(close), {})
    assert ok is False


def test_trix_golden_cross_chain_fix():
    """P0-22: 金叉只认 昨日TRIX≤MATRIX 且 今日TRIX>MATRIX（链式错位修复）。"""
    import app.lib.signals.engine as ENG

    # 正例：长跌后底部四根连涨 ⇒ 最后一根 TRIX 上穿 MATRIX → 金叉
    close = np.concatenate([np.linspace(10.0, 6.0, 26), np.array([6.0, 6.6, 7.3, 8.1])])
    ok, msg = ENG._trix_golden_cross(_ohlc_df(close), {})
    assert ok is True
    assert "TRIX" in msg and "MATRIX" in msg
    # 反例：长涨后顶部回落 ⇒ 昨日 TRIX>MATRIX 且 今日仍高于 MATRIX（非金叉）
    close = np.concatenate([np.linspace(10.0, 14.0, 28), np.array([14.0, 13.7])])
    ok, _ = ENG._trix_golden_cross(_ohlc_df(close), {})
    assert ok is False
