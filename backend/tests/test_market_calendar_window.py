"""市场日历口径时间窗口的聚焦测试。

背景：recent_events 原为「当前时刻 - N 小时」滑动窗口，
事件可见性随查询时刻漂移（昨日收盘事件今日 10:00 可见、16:00 被排除）。
改造后窗口锚定最近交易日（跳过周末）零点，不随当前时刻滑动。

（/market/radar 端点已删除——前端重定向 /signals，无消费者；
radar.min_trigger_hours 日历口径逻辑随之移除，仅保留其底层
calendar_window_start / recent_events 契约测试。）

覆盖：
1. calendar_window_start：跳过周末回溯 N 个工作日；周六查询锚定周五。
2. recent_events：周一/深夜/周二凌晨查询窗口一致；周末查询按最近交易日边界。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock


class _MutableClock(datetime):
    """可变冻结 Asia/Shanghai 墙钟（naive）：测试内通过 _now 切换时刻。

    冻结 scanner 模块内的 datetime.now(ZoneInfo("Asia/Shanghai"))；
    fromisoformat 等其它类方法由基类继承，行为不受影响。
    """

    _now = datetime(2026, 8, 10, 15, 0)  # 2026-08-10 为周一

    @classmethod
    def now(cls, tz=None):
        return cls._now


@pytest.fixture()
def frozen_clock(monkeypatch):
    import app.core.scanning as SC

    monkeypatch.setattr(SC, "datetime", _MutableClock)
    yield _MutableClock
    _MutableClock._now = datetime(2026, 8, 10, 15, 0)  # 复位，避免污染其它测试


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 scanner.SessionLocal（recent_events 读取走临时库）。"""
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

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    yield Maker
    engine.dispose()


def _seed_stock(db, code: str, name: str):
    db.add(Stock(code=code, name=name, is_watchlist=False))
    db.commit()


def _seed_event(db, code: str, triggered_at: datetime):
    db.add(
        SignalEvent(
            stock_code=code,
            signals=["volume_surge"],
            status="观察",
            evidence={},
            triggered_at=triggered_at,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# calendar_window_start：跳过周末的工作日窗口起点
# ---------------------------------------------------------------------------


def test_calendar_window_start_skips_weekends(frozen_clock):
    from app.core.scanning import calendar_window_start

    frozen_clock._now = datetime(2026, 8, 10, 15, 0)  # 周一
    assert calendar_window_start(1) == datetime(2026, 8, 10)  # 今日零点
    assert calendar_window_start(3) == datetime(2026, 8, 6)  # 跳过 8/8、8/9 周末
    assert calendar_window_start(7) == datetime(2026, 7, 31)  # 7 个工作日（跨周末）


def test_calendar_window_start_weekend_anchors_last_trading_day(frozen_clock):
    from app.core.scanning import calendar_window_start

    frozen_clock._now = datetime(2026, 8, 9, 14, 0)  # 周六
    assert calendar_window_start(1) == datetime(2026, 8, 7)  # 最近交易日=周五
    assert calendar_window_start(3) == datetime(2026, 8, 5)  # 五、四、三

    frozen_clock._now = datetime(2026, 8, 9, 22, 0)  # 周六深夜：边界不随时刻滑动
    assert calendar_window_start(1) == datetime(2026, 8, 7)
    assert calendar_window_start(3) == datetime(2026, 8, 5)


# ---------------------------------------------------------------------------
# recent_events：窗口锚定最近交易日边界，不随当前时刻滑动
# ---------------------------------------------------------------------------


def test_recent_events_window_stable_across_time(frozen_clock, temp_db):
    from app.core.scanning import recent_events

    db = temp_db()
    try:
        _seed_stock(db, "600519.SH", "贵州茅台")
        t_mon = datetime(2026, 8, 10, 15, 0)  # 周一收盘
        t_fri = datetime(2026, 8, 7, 15, 0)  # 周五收盘
        t_thu = datetime(2026, 8, 6, 15, 0)  # 周四收盘
        t_wed = datetime(2026, 8, 5, 15, 0)  # 周三收盘（3 工作日窗口外）
        for t in (t_mon, t_fri, t_thu, t_wed):
            _seed_event(db, "600519.SH", t)

        # 周一 15:00：窗口 8/6 零点起 → 周一/周五/周四，周三收盘事件出窗
        frozen_clock._now = datetime(2026, 8, 10, 15, 0)
        got = {e["triggered_at"] for e in recent_events(minutes=60 * 24)}
        assert got == {
            "2026-08-10T15:00:00",
            "2026-08-07T15:00:00",
            "2026-08-06T15:00:00",
        }, "旧口径在 16:00 会把周五收盘事件排除——新口径不应随时刻滑动"

        # 同一天深夜 23:00：结果必须与 15:00 完全一致（边界不随当前时刻滑动）
        frozen_clock._now = datetime(2026, 8, 10, 23, 0)
        assert {e["triggered_at"] for e in recent_events(minutes=60 * 24)} == got

        # 周二凌晨 02:00：窗口按交易日推进为「周二/周一/周五」（起点 8/7 零点），
        # 周一收盘事件仍可见，周四事件出窗——边界以交易日为界，跨天后整体推进
        frozen_clock._now = datetime(2026, 8, 11, 2, 0)
        assert {e["triggered_at"] for e in recent_events(minutes=60 * 24)} == {
            "2026-08-10T15:00:00",
            "2026-08-07T15:00:00",
        }
    finally:
        db.close()


def test_recent_events_weekend_anchors_last_trading_day(frozen_clock, temp_db):
    from app.core.scanning import recent_events

    db = temp_db()
    try:
        _seed_stock(db, "600519.SH", "贵州茅台")
        t_fri = datetime(2026, 8, 7, 15, 0)
        t_thu = datetime(2026, 8, 6, 15, 0)
        t_wed = datetime(2026, 8, 5, 15, 0)
        t_old = datetime(2026, 8, 4, 15, 0)  # 周二：4 个交易日前，应出窗
        for t in (t_fri, t_thu, t_wed, t_old):
            _seed_event(db, "600519.SH", t)

        # 周六查询：窗口按最近 3 个交易日（五/四/三）计算，周五收盘事件仍可见
        frozen_clock._now = datetime(2026, 8, 9, 14, 0)
        got = {e["triggered_at"] for e in recent_events(minutes=60 * 24)}
        assert got == {
            "2026-08-07T15:00:00",
            "2026-08-06T15:00:00",
            "2026-08-05T15:00:00",
        }, "非交易时段事件应按最近交易日边界计算"
    finally:
        db.close()
