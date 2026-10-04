"""T-42 修复回归：重复命中同一 bar 时点事件不得重置用户「已忽略/已确认」状态。

背景（P0-26）：scanner._apply_hits 内，同一 (code, bar_time, period)
事件已存在（existing_map 命中）时无条件更新 signals/evidence/
triggered_at/as_of 并把 status 重置为「观察」，导致用户已忽略/已确认的事件在下轮
扫描被重置、用户决策丢失。
修复：仅 status=="观察" 的事件刷新字段；「已忽略」「已确认」事件原样保留。

覆盖：① 已忽略事件重复命中 → 状态/证据/信号原样保留（event_count 仍计）；
② 已确认事件同断言；③ 观察事件重复命中 → 正常刷新；④ 无既有事件 → 新建
（status="观察"）。临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock

# fake K 线最后一根 bar（pd.date_range("2024-01-02", periods=400, freq="B") 末位）
# 为 2025-07-14（周一）；daily bar 时点由 bar_market_time 归一到该日 15:00 收盘。
BAR_TIME = datetime(2025, 7, 14, 15, 0)


def _fake_kline_df(n: int = 400) -> pd.DataFrame:
    """400 个工作日日线，最后一天 close 跳升 6% → pct_change=6.0 命中 price_up。

    与 test_scan_period.py 同构，保证 scan_once 对单只股票产出一条命中。
    """
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    close = np.linspace(10, 11, n)
    close[-1] = close[-2] * 1.06  # 最后一天跳升 6%
    pct = np.zeros(n)
    pct[-1] = 6.0
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
            "pct_change": pct,
        }
    )


def _spot_df() -> pd.DataFrame:
    """单只股票快照，价/量对齐 K 线最后 bar → 无新交易判定 → 跳过快照合成。"""
    first_price = float(_fake_kline_df()["close"].iloc[-1])
    return pd.DataFrame(
        {
            "code": ["600100.SH"],
            "name": ["首股"],
            "price": [first_price],
            "pct_change": [0.0],
            "volume": [100.0],
            "amount": [1.0],
            "turnover_rate": [1.0],
        }
    )


def _install(monkeypatch, tmp_path) -> sessionmaker:
    """建临时 SQLite + patch 扫描器外部依赖；返回 Maker。"""
    from app.core import scanning as SC

    engine = create_engine(
        f"sqlite:///{tmp_path / 'scan.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = Maker()
    db.add(Stock(code="600100.SH", name="首股", is_watchlist=False))
    db.commit()
    db.close()

    def fake_fetch(codes, period, **kw):
        return {"600100.SH": _fake_kline_df()}

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(SC, "get_spot", lambda: _spot_df())
    monkeypatch.setattr(SC, "fetch_many", fake_fetch)
    monkeypatch.setattr(SC, "get_provider", lambda: SimpleNamespace(name="mock"))
    return Maker


def _seed_event(db, status: str):
    """预置与扫描去重键 (600100.SH, 2025-07-14 15:00, daily) 一致的事件。

    字段刻意与扫描将生成的值不同，用于证明「保留」或「刷新」确实发生。
    """
    db.add(
        SignalEvent(
            stock_code="600100.SH",
            signals=["volume_surge"],
            status=status,
            evidence={"marker": "original"},
            triggered_at=BAR_TIME,
            period="daily",
        )
    )
    db.commit()


def _assert_preserved(Maker):
    """已忽略/已确认事件重复命中后：全部字段保持种子值不变。"""
    db = Maker()
    try:
        evs = db.query(SignalEvent).all()
        assert len(evs) == 1, "重复命中不得新增事件"
        ev = evs[0]
        assert ev.signals == ["volume_surge"], "信号列表被改写"
        assert ev.evidence == {"marker": "original"}, "证据被改写"
        assert ev.triggered_at == BAR_TIME
        assert ev.as_of is None, "as_of 被改写"
        assert ev.period == "daily"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ① 已忽略：重复命中 → 状态与证据原样保留，event_count 仍计数
# ---------------------------------------------------------------------------


def test_ignored_event_preserved_on_rehit(tmp_path, monkeypatch):
    from app.core import scanning as SC

    Maker = _install(monkeypatch, tmp_path)
    db = Maker()
    _seed_event(db, status="已忽略")
    db.close()

    r = SC.scan_once()
    assert r["events"] == 1, "已忽略事件重复命中仍应计入 event_count"

    _assert_preserved(Maker)
    db = Maker()
    try:
        assert db.query(SignalEvent).one().status == "已忽略"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ② 已确认：同「已忽略」
# ---------------------------------------------------------------------------


def test_confirmed_event_preserved_on_rehit(tmp_path, monkeypatch):
    from app.core import scanning as SC

    Maker = _install(monkeypatch, tmp_path)
    db = Maker()
    _seed_event(db, status="已确认")
    db.close()

    r = SC.scan_once()
    assert r["events"] == 1

    _assert_preserved(Maker)
    db = Maker()
    try:
        assert db.query(SignalEvent).one().status == "已确认"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ③ 观察中：重复命中 → 正常刷新（证据/信号/时间更新，状态仍观察）
# ---------------------------------------------------------------------------


def test_observed_event_refreshed_on_rehit(tmp_path, monkeypatch):
    from app.core import scanning as SC

    Maker = _install(monkeypatch, tmp_path)
    db = Maker()
    _seed_event(db, status="观察")
    db.close()

    r = SC.scan_once()
    assert r["events"] == 1

    db = Maker()
    try:
        evs = db.query(SignalEvent).all()
        assert len(evs) == 1, "重复命中不得新增事件"
        ev = evs[0]
        assert ev.status == "观察"
        assert "price_up" in ev.signals, "观察事件信号未刷新（+6% 日线应命中 price_up）"
        assert ev.evidence.get("price") is not None, "观察事件证据未刷新"
        assert ev.evidence.get("pct_change") == 6.0
        assert ev.triggered_at == BAR_TIME
        assert ev.as_of == BAR_TIME, "观察事件 as_of 未刷新"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ④ 无既有事件：新建路径不变（插入 status="观察"）
# ---------------------------------------------------------------------------


def test_new_event_inserted_as_observed(tmp_path, monkeypatch):
    from app.core import scanning as SC

    Maker = _install(monkeypatch, tmp_path)

    r = SC.scan_once()
    assert r["events"] == 1

    db = Maker()
    try:
        evs = db.query(SignalEvent).all()
        assert len(evs) == 1
        ev = evs[0]
        assert ev.status == "观察"
        assert "price_up" in ev.signals
        assert ev.evidence.get("price") is not None
        assert ev.triggered_at == BAR_TIME
        assert ev.period == "daily"
    finally:
        db.close()
