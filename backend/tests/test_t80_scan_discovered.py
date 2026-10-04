"""T-80 时间契约重构：scan_discovered_at（首次发现时刻）字段契约测试。

契约（任务卡）：
- triggered_at（不变）= bar 标签时点（日线 = 交易日 15:00），承担去重/排序/筛选。
- as_of（不变，重扫推进）= 数据实际截止时刻（partial → 当前上海时刻，否则 bar_time）。
- scan_discovered_at（新增，不可变）= 该事件首次被扫描发现的时间（上海 naive，去微秒）：
  * 新插入事件 = 本轮扫描的上海时刻；
  * 同 bar 重复扫描（upsert 命中）与「观察」原地刷新只推进 as_of，绝不改写本字段；
  * 旧行（迁移前）为 NULL，输出 null。
- 事件序列化（_event_row / recent_events）输出 scan_discovered_at 键，旧行输出 null。
- quote 输出补 timestamp（服务器上海时刻，秒级 naive ISO，供前端标题栏）。

覆盖：
1. 首次扫描写入：scan_discovered_at 非空且等于本轮扫描时刻（冻结时钟可断言精确值）。
2. 同 bar 重复扫描：事件仍一条，as_of 推进、scan_discovered_at 保留首次值。
3. 旧库迁移：_migrate 对无 scan_discovered_at 列的旧表 ALTER 补列，旧行保持 NULL。
4. 新库 create_all：signal_events 含 scan_discovered_at 列（nullable）。
5. _event_row / recent_events 序列化含 scan_discovered_at 键；旧行（None）输出 null。
6. quote 输出 timestamp：服务器上海时刻，秒级、无微秒、naive ISO。
临时 SQLite，不触碰生产库（scanner/kcache/signals 的 SessionLocal 均指向临时库）。
"""

from __future__ import annotations

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent


class _FrozenClock(datetime):
    """固定 Asia/Shanghai 墙钟（naive）；_FROZEN 可在用例内改以模拟不同时刻。"""

    _FROZEN = datetime(2026, 8, 12, 10, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


@pytest.fixture()
def frozen_shanghai_clock(monkeypatch):
    """冻结 scanner/signals 的 datetime（as_of / scan_discovered_at 取时）与
    kcache._cn_now（_is_partial_daily 判定）。"""
    import app.api.signals as SI
    import app.core.scanning as SC
    import app.storage.klines as KC

    monkeypatch.setattr(SC, "datetime", _FrozenClock)
    monkeypatch.setattr(SI, "datetime", _FrozenClock)
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

    yield Maker
    engine.dispose()


@pytest.fixture()
def temp_engine(tmp_path, monkeypatch):
    """独立临时 DB：database.engine 替换为临时引擎（_migrate 作用于临时库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mig.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    import app.storage.db as DB

    monkeypatch.setattr(DB, "engine", engine)
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# K 线 / 扫描桩（仿 test_signal_as_of.py）
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
# 1) 首次扫描写入：scan_discovered_at 非空 = 本轮扫描时刻
# ---------------------------------------------------------------------------


def test_scan_first_write_discovered_non_null(
    temp_db, monkeypatch, frozen_shanghai_clock
):
    """冻结 2026-08-10 10:00（盘中 partial）：新事件 scan_discovered_at == 10:00，
    as_of == 10:00，triggered_at 仍为 15:00。"""
    from app.core.scanning import scan_once

    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    _patch_scanner(monkeypatch, kline=_daily_kline(last_date="2026-08-10"))
    r = scan_once()
    assert r["events"] == 1
    db = temp_db()
    try:
        ev = db.query(SignalEvent).one()
        assert ev.triggered_at == datetime(2026, 8, 10, 15, 0)
        assert ev.as_of == datetime(2026, 8, 10, 10, 0)
        assert ev.scan_discovered_at == datetime(2026, 8, 10, 10, 0)  # 首次写入非空
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2) 同 bar 重复扫描：事件仍一条，as_of 推进、scan_discovered_at 保留首次值
# ---------------------------------------------------------------------------


def test_scan_repeat_keeps_original_discovered(
    temp_db, monkeypatch, frozen_shanghai_clock
):
    """同一 bar（2026-08-10）盘中两次扫描（10:00 → 11:00）：事件保持 1 条；
    as_of 推进到 11:00，scan_discovered_at 保留首次值 10:00（upsert 命中不覆盖）。"""
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
        assert ev.as_of == datetime(2026, 8, 10, 11, 0)  # 观察刷新推进 as_of
        assert ev.scan_discovered_at == datetime(2026, 8, 10, 10, 0)  # 不可变
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3) 旧库迁移：ALTER 补列，旧行保持 NULL
# ---------------------------------------------------------------------------


def test_migrate_adds_scan_discovered_to_legacy_db(temp_engine):
    """旧库（无 scan_discovered_at 列，但已有 as_of/period，即 T-80 之前的真实库）执行
    _migrate：ALTER 补列，旧数据保留且 scan_discovered_at 为 NULL。"""
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE signal_events (
                    id INTEGER PRIMARY KEY,
                    stock_code VARCHAR(12),
                    signals JSON,
                    status VARCHAR(16),
                    evidence JSON,
                    triggered_at DATETIME,
                    as_of DATETIME,
                    period VARCHAR(16) NOT NULL DEFAULT 'daily'
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO signal_events (stock_code, signals) "
                "VALUES ('600519.SH', '[\"price_up\"]')"
            )
        )
    import app.storage.db as DB

    DB._migrate()
    cols = {c["name"] for c in inspect(temp_engine).get_columns("signal_events")}
    assert "scan_discovered_at" in cols
    with temp_engine.connect() as conn:
        row = conn.execute(
            text("SELECT stock_code, scan_discovered_at FROM signal_events")
        ).one()
        assert row.stock_code == "600519.SH"
        assert row.scan_discovered_at is None  # 旧行保持 NULL


def test_migrate_idempotent_scan_discovered(temp_engine):
    """重复执行 _migrate 不报错，scan_discovered_at 列保持唯一。"""
    Base.metadata.create_all(bind=temp_engine)
    import app.storage.db as DB

    DB._migrate()
    DB._migrate()  # 第二遍：列检查跳过
    cols = {c["name"] for c in inspect(temp_engine).get_columns("signal_events")}
    assert "scan_discovered_at" in cols


# ---------------------------------------------------------------------------
# 4) 新库 create_all 含 scan_discovered_at 列
# ---------------------------------------------------------------------------


def test_create_all_new_db_has_scan_discovered_column(tmp_path):
    """新库 create_all 后 signal_events 表含 scan_discovered_at 列（nullable）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 't80.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("signal_events")}
        assert "scan_discovered_at" in cols
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 5) 序列化：_event_row / recent_events 含 scan_discovered_at，旧行（None）输出 null
# ---------------------------------------------------------------------------


def test_event_row_scan_discovered_serialization(temp_db, monkeypatch):
    """旧行（迁移前 scan_discovered_at 为 NULL）输出 null；有值行输出 ISO；
    as_of 键之后紧跟 scan_discovered_at（契约顺序）。"""
    from app.api.signals import _event_row

    db = temp_db()
    try:
        legacy = SignalEvent(
            stock_code="600000.SH",
            signals=["price_up"],
            status="观察",
            evidence={},
            triggered_at=datetime(2026, 8, 7, 15, 0),
            as_of=None,
            scan_discovered_at=None,  # 旧库迁移后行为
        )
        fresh = SignalEvent(
            stock_code="600519.SH",
            signals=["price_up"],
            status="观察",
            evidence={},
            triggered_at=datetime(2026, 8, 10, 15, 0),
            as_of=datetime(2026, 8, 10, 11, 0),
            scan_discovered_at=datetime(2026, 8, 10, 10, 24),
        )
        db.add_all([legacy, fresh])
        db.commit()
        out_legacy = _event_row(legacy, {}, set())
        out_fresh = _event_row(fresh, {}, set())
        assert out_legacy["scan_discovered_at"] is None
        assert out_fresh["scan_discovered_at"] == "2026-08-10T10:24:00"
        keys = list(out_fresh.keys())
        assert keys[keys.index("as_of") + 1] == "scan_discovered_at"
    finally:
        db.close()


def test_recent_events_contains_scan_discovered(
    temp_db, monkeypatch, frozen_shanghai_clock
):
    """recent_events 输出的 dict 含 scan_discovered_at 键（= 本轮扫描时刻 ISO）。"""
    from app.core.scanning import recent_events, scan_once

    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 24)
    _patch_scanner(monkeypatch, kline=_daily_kline(last_date="2026-08-10"))
    scan_once()
    rows = recent_events(minutes=60 * 24)
    assert len(rows) == 1
    r = rows[0]
    assert "scan_discovered_at" in r
    assert r["triggered_at"] == "2026-08-10T15:00:00"
    assert r["as_of"] == "2026-08-10T10:24:00"
    assert r["scan_discovered_at"] == "2026-08-10T10:24:00"


# ---------------------------------------------------------------------------
# 6) quote 输出 timestamp（服务器上海时刻，秒级）
# ---------------------------------------------------------------------------


def test_quote_contains_timestamp(monkeypatch):
    """quote 输出含 timestamp：服务器上海时刻，秒级、无微秒、naive ISO。"""
    from app.api import stocks as ST

    spot = pd.DataFrame(
        [
            {
                "code": "600519.SH",
                "name": "贵州茅台",
                "price": 1700.0,
                "pct_change": 1.2,
                "volume": 30000.0,
                "amount": 5.1e8,
                "turnover_rate": 0.3,
                "industry": "白酒",
            }
        ]
    )
    monkeypatch.setattr(ST, "get_provider", lambda: types.SimpleNamespace(name="mock"))
    monkeypatch.setattr(ST, "get_spot", lambda: spot)
    monkeypatch.setattr(
        ST,
        "now_cn",
        lambda: datetime(
            2026, 8, 12, 10, 30, 45, 123456, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
    )
    out = ST.quote("600519.SH")
    assert out["timestamp"] == "2026-08-12T10:30:45"  # 微秒被截断 → 秒级
    assert "." not in out["timestamp"]  # naive ISO，无时区/小数秒
