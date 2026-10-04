"""信号事件 as_of（数据实际截止时刻）字段契约测试。

契约（任务卡）：
- triggered_at（不动语义）= bar 标签时点：日线 = 交易日 15:00（bar_market_time 归一）；
  承担去重键/排序/时间范围筛选，全部不变。
- as_of = 数据实际截止时刻：
  * daily 扫描且最后 bar 为盘中部分数据（最后 bar 日期 == 今天(上海)且当前上海时刻 < 15:05，
    复用 kcache._is_partial_daily 判定）→ as_of = 当前上海时刻（去微秒）
  * 否则 as_of = bar_time（即 15:00）
  * 旧行（迁移前 as_of 为 NULL）输出 null
- 序列化：所有返回 dict 含 triggered_at 键处紧随其后补 as_of。
- 排序/去重/筛选/exclude_today 全部仍基于 triggered_at。

覆盖：
1. 收盘后（冻结 2026-08-10 16:00）：as_of == bar_time == 2026-08-10 15:00。
2. 盘中（冻结 2026-08-10 10:24，最后 bar 日期 2026-08-10）：as_of == 10:24，
   triggered_at 仍为 15:00。
3. 同 bar 重复扫描（10:00 → 11:00）：事件仍一条，as_of 推进为 11:00。
4. recent_events / _event_row 序列化输出含 as_of 键；旧行（None）输出 null。
5. create_all 新库 signal_events 表含 as_of 列。
临时 SQLite，不触碰生产库（scanner 与 kcache 的 SessionLocal 均指向临时库）。
"""

from __future__ import annotations

import types
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent


class _FrozenClock(datetime):
    """固定 Asia/Shanghai 墙钟（naive）；_FROZEN 可在用例内改以模拟不同时刻。"""

    _FROZEN = datetime(2026, 8, 10, 15, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


@pytest.fixture()
def frozen_shanghai_clock(monkeypatch):
    """冻结 scanner.datetime（_check_one 的 as_of 取时）与 kcache._cn_now（_is_partial_daily 判定）。"""
    import app.core.scanning as SC
    import app.storage.klines as KC

    monkeypatch.setattr(SC, "datetime", _FrozenClock)
    monkeypatch.setattr(KC, "_cn_now", lambda: _FrozenClock._FROZEN)
    yield


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB：scanner 与 kcache 的 SessionLocal 均替换为临时库（禁触碰生产库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
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
    import app.storage.klines as KC

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(KC, "SessionLocal", Maker)

    from app.api import signals as S

    S._sector_codes_cache.clear()

    yield Maker
    engine.dispose()


# ---------------------------------------------------------------------------
# K 线 / 扫描桩
# ---------------------------------------------------------------------------


def _daily_kline(n: int = 60, last_date: str = "2026-08-10", last_pct: float = 1.0):
    close = np.linspace(10.0, 11.0, n)
    vol = np.full(n, 100.0)
    vol[-1] = 500.0  # 量比 5
    return pd.DataFrame(
        {
            "date": pd.bdate_range(end=last_date, periods=n)
            .strftime("%Y-%m-%d")
            .tolist(),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": vol,
            "amount": vol * 10.0,
            "pct_change": np.r_[np.zeros(n - 1), [last_pct]],
        }
    )


def _patch_scanner(monkeypatch, kline: pd.DataFrame):
    import app.core.scanning as SC

    spot = pd.DataFrame(
        {
            "code": ["600519.SH"],
            "name": ["贵州茅台"],
            "price": [1500.0],
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
# 1) 收盘后：as_of == bar_time == 15:00
# ---------------------------------------------------------------------------


def test_scan_after_close_as_of_equals_bar_time(
    temp_db, monkeypatch, frozen_shanghai_clock
):
    """冻结 2026-08-10 16:00（已收盘）：最后 bar 日期虽为今天，但 16:00 ≥ 15:05 → 非 partial，
    as_of == bar_time == 2026-08-10 15:00。"""
    from app.core.scanning import scan_once

    _FrozenClock._FROZEN = datetime(2026, 8, 10, 16, 0)
    _patch_scanner(monkeypatch, kline=_daily_kline(last_date="2026-08-10"))
    r = scan_once()
    assert r["events"] == 1
    db = temp_db()
    try:
        ev = db.query(SignalEvent).one()
        assert ev.triggered_at == datetime(2026, 8, 10, 15, 0)
        assert ev.as_of == datetime(2026, 8, 10, 15, 0)  # == bar_time
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2) 盘中：as_of = 当前上海时刻，triggered_at 仍为 15:00
# ---------------------------------------------------------------------------


def test_scan_intraday_as_of_now_triggered_at_close(
    temp_db, monkeypatch, frozen_shanghai_clock
):
    """冻结 2026-08-10 10:24，最后 bar 日期 2026-08-10（今天）且 < 15:05 → partial：
    as_of == 10:24，triggered_at 仍为 2026-08-10 15:00。"""
    from app.core.scanning import scan_once

    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 24)
    _patch_scanner(monkeypatch, kline=_daily_kline(last_date="2026-08-10"))
    scan_once()
    db = temp_db()
    try:
        ev = db.query(SignalEvent).one()
        assert ev.triggered_at == datetime(2026, 8, 10, 15, 0)
        assert ev.as_of == datetime(2026, 8, 10, 10, 24)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3) 同 bar 重复扫描：事件仍一条，as_of 推进
# ---------------------------------------------------------------------------


def test_scan_same_bar_repeat_as_of_advances(
    temp_db, monkeypatch, frozen_shanghai_clock
):
    """同一 bar（2026-08-10）盘中两次扫描（10:00 → 11:00）：去重键 triggered_at 未变，
    事件保持 1 条；as_of 从 10:00 推进到 11:00。"""
    from app.core.scanning import scan_once

    _patch_scanner(monkeypatch, kline=_daily_kline(last_date="2026-08-10"))

    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    assert scan_once()["events"] == 1
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 11, 0)
    assert scan_once()["events"] == 1  # 去重键基于 triggered_at，未新增

    db = temp_db()
    try:
        assert db.query(SignalEvent).count() == 1
        ev = db.query(SignalEvent).one()
        assert ev.triggered_at == datetime(2026, 8, 10, 15, 0)
        assert ev.as_of == datetime(2026, 8, 10, 11, 0)  # 更新分支推进 as_of
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4) 序列化：recent_events / _event_row 含 as_of 键，旧行（None）输出 null
# ---------------------------------------------------------------------------


def test_recent_events_serialization_contains_as_of(
    temp_db, monkeypatch, frozen_shanghai_clock
):
    """recent_events 输出的 dict 含 as_of 键（盘中 → 当前时刻 ISO）。"""
    from app.core.scanning import recent_events, scan_once

    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 24)
    _patch_scanner(monkeypatch, kline=_daily_kline(last_date="2026-08-10"))
    scan_once()
    rows = recent_events(minutes=60 * 24)
    assert len(rows) == 1
    r = rows[0]
    assert "as_of" in r
    assert r["triggered_at"] == "2026-08-10T15:00:00"
    assert r["as_of"] == "2026-08-10T10:24:00"


def test_event_row_as_of_none_for_legacy_rows(temp_db, monkeypatch):
    """旧行（迁移前 as_of 为 NULL）序列化输出 null；有值行输出 ISO。"""
    from app.api.signals import _event_row

    db = temp_db()
    try:
        legacy = SignalEvent(
            stock_code="600000.SH",
            signals=["price_up"],
            status="观察",
            evidence={},
            triggered_at=datetime(2026, 8, 7, 15, 0),
            as_of=None,  # 旧库迁移后行为
        )
        fresh = SignalEvent(
            stock_code="600519.SH",
            signals=["price_up"],
            status="观察",
            evidence={},
            triggered_at=datetime(2026, 8, 10, 15, 0),
            as_of=datetime(2026, 8, 10, 10, 24),
        )
        db.add_all([legacy, fresh])
        db.commit()
        out_legacy = _event_row(legacy, {}, set())
        out_fresh = _event_row(fresh, {}, set())
        assert out_legacy["as_of"] is None
        assert out_fresh["as_of"] == "2026-08-10T10:24:00"
        # triggered_at 键之后紧跟 as_of（契约要求紧随其后）
        keys = list(out_fresh.keys())
        assert keys[keys.index("triggered_at") + 1] == "as_of"
        # period 键恒输出（P2-67 契约对齐：与 /market/watchlist/notifications 同源，旧行迁移默认 'daily'）
        assert out_legacy["period"] == "daily"
        assert out_fresh["period"] == "daily"
        # 输出键序与 notifications 对齐：period 紧跟 scan_discovered_at
        assert keys[keys.index("scan_discovered_at") + 1] == "period"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5) create_all 新库含 as_of 列（迁移保证）
# ---------------------------------------------------------------------------


def test_create_all_new_db_has_as_of_column(tmp_path):
    """新库 create_all 后 signal_events 表含 as_of 列（nullable）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'asof.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("signal_events")}
        assert "as_of" in cols
    finally:
        engine.dispose()
