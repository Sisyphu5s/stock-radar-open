"""信号时点精准性：多周期下「信号时点 = 触发 bar 在股市数据时间轴上的精确标签」契约。

契约（与 app/lib/timex.py `bar_market_time` docstring 一致）：
- daily/weekly/monthly：该 bar 真实交易日 15:00（Asia/Shanghai naive，当日收盘时点）；
  周/月 bar 必须用组内最后一个真实交易日（禁止未来周五/月末标签）。
- 分钟周期 1/5/15/30/60：该 bar 的精确时分标签，**去除秒与微秒**（市场分钟 bar
  标签粒度到分钟），不因 pandas 索引/输入精度产生 :30:xx 之类偏移。

覆盖：
1. 8 个周期各自断言：分钟周期时点秒/微秒为零且等于 bar 标签；日/周/月 = 交易日 15:00。
2. 周/月重采样跨月边界：构造 2026-07-31(周五) 与 2026-08-03(周一) 场景，
   周 bar 标签取组内最后真实交易日（2026-07-31 15:00），不出现未来日期
   （2026-08-02 / 2026-08-07 / 2026-08-31）。
3. 同一日线 bar 从带时间成分的 date（如 '2026-08-10 09:30'）与纯日期输入
   得到相同 15:00 时点。
4. 非日线查事件表：各周期按 period 列隔离，triggered_at 直读种子值（不再 K 线重算）。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent
from app.lib.timex import PERIODS, bar_market_time


class _FrozenClock(datetime):
    """固定 Asia/Shanghai 墙钟（naive）：2026-08-10 15:00（周一）。"""

    _FROZEN = datetime(2026, 8, 10, 15, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


@pytest.fixture()
def frozen_shanghai_clock(monkeypatch):
    import app.lib.timex as C

    monkeypatch.setattr(C, "datetime", _FrozenClock)
    yield


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB（_period_events 走内存过滤，不触碰生产库）。"""
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

    import app.api.signals as S

    S._sector_codes_cache.clear()

    yield Maker
    engine.dispose()


# ---------------------------------------------------------------------------
# 1) 8 个周期：分钟周期时点秒/微秒为零且等于 bar 标签；日/周/月 = 交易日 15:00
# ---------------------------------------------------------------------------


def test_bar_market_time_all_periods_precision():
    assert PERIODS == ("1", "5", "15", "30", "60", "daily", "weekly", "monthly")
    for p in ("1", "5", "15", "30", "60"):
        # 带秒+微秒噪声的 bar 标签 → 精确时分（秒/微秒清零），等于 bar 标签
        t = bar_market_time("2026-08-10 14:35:30.500", p)
        assert (t.second, t.microsecond) == (0, 0), p
        assert t == datetime(2026, 8, 10, 14, 35), p
        assert t.isoformat() == "2026-08-10T14:35:00", p
        # 纯时分标签输入同样稳定
        assert bar_market_time("2026-08-10 14:35", p) == datetime(
            2026, 8, 10, 14, 35
        ), p
    # 日/周/月 = 真实交易日 15:00 收盘时点
    assert bar_market_time("2026-08-10 09:30", "daily") == datetime(2026, 8, 10, 15, 0)
    assert bar_market_time("2026-08-07", "weekly") == datetime(2026, 8, 7, 15, 0)
    assert bar_market_time("2026-08-31", "monthly") == datetime(2026, 8, 31, 15, 0)


def test_daily_bar_with_time_component_same_close_time():
    """同一日线 bar：带时间成分（如 '2026-08-10 09:30'）与纯日期输入 → 相同 15:00 时点。"""
    with_time = bar_market_time("2026-08-10 09:30", "daily")
    pure_date = bar_market_time("2026-08-10", "daily")
    assert with_time == pure_date == datetime(2026, 8, 10, 15, 0)
    # 分钟周期不受影响：09:30 输入保留 09:30
    assert bar_market_time("2026-08-10 09:30", "60") == datetime(2026, 8, 10, 9, 30)


# ---------------------------------------------------------------------------
# 2) 周/月重采样跨月边界：组内最后真实交易日，不出现未来日期
# ---------------------------------------------------------------------------


def test_resample_weekly_monthly_month_boundary_last_trading_day():
    """2026-07-31(周五) 与 2026-08-03(周一) 场景：周/月 bar 取组内最后真实交易日。

    - 2026-07-27~07-31 是跨月边界周（7 月末→8 月初），该周 bar 标签必须为
      2026-07-31 15:00，而不是未来日期 2026-08-02（W-SUN 周期末标签）。
    - 数据止于 2026-08-03(周一)：最后周 bar 标签 = 2026-08-03（非未来周五
      2026-08-07）；最后月 bar 标签 = 2026-08-03（非未来月末 2026-08-31）。
    """
    from app.core.sources import _resample_period

    dates = pd.bdate_range("2026-07-01", "2026-08-03")
    n = len(dates)
    close = np.linspace(10.0, 11.0, n)
    df = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d").tolist(),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
            "pct_change": np.zeros(n),
        }
    )
    wk = _resample_period(df, "weekly")
    mo = _resample_period(df, "monthly")

    # 不出现未来日期（数据止于 2026-08-03）
    assert (wk["date"] > "2026-08-03").sum() == 0
    assert (mo["date"] > "2026-08-03").sum() == 0
    # 跨月边界周（7 月末）：标签 = 组内最后真实交易日 2026-07-31，绝非 2026-08-02
    assert "2026-07-31" in wk["date"].tolist()
    assert "2026-08-02" not in wk["date"].tolist()
    # 最后周 bar = 2026-08-03（周一），非未来周五 2026-08-07
    assert wk["date"].iloc[-1] == "2026-08-03"
    assert "2026-08-07" not in wk["date"].tolist()
    # 最后月 bar = 2026-08-03，非未来月末 2026-08-31
    assert mo["date"].iloc[-1] == "2026-08-03"
    assert "2026-08-31" not in mo["date"].tolist()

    # bar 时点语义：周/月 bar 日期经 bar_market_time → 该交易日 15:00
    assert bar_market_time(wk["date"].iloc[-1], "weekly") == datetime(2026, 8, 3, 15, 0)
    assert bar_market_time(mo["date"].iloc[-1], "monthly") == datetime(
        2026, 8, 3, 15, 0
    )
    assert bar_market_time("2026-07-31", "weekly") == datetime(2026, 7, 31, 15, 0)


# ---------------------------------------------------------------------------
# 3) 非日线查事件表：triggered_at 直读种子值，period 过滤隔离各周期
# ---------------------------------------------------------------------------


def test_period_events_triggered_at_contract(temp_db, frozen_shanghai_clock):
    """非日线周期改查事件表：triggered_at 直读事件表种子值（不再 K 线重算），
    各周期按 period 列隔离，互不串扰。"""
    from app.api.signals import list_events_page

    db = temp_db()
    base = datetime(2026, 8, 10, 15, 0)
    for i, p in enumerate(PERIODS):
        db.add(
            SignalEvent(
                stock_code="600519.SH",
                signals=["price_up"],
                status="观察",
                evidence={},
                triggered_at=base - timedelta(minutes=i),
                period=p,
            )
        )
    db.commit()
    try:
        for i, p in enumerate(PERIODS):
            r = list_events_page(limit=50, offset=0, period=p, time_range="all", db=db)
            assert r["total"] == 1, p  # 每周期仅命中本周期事件
            expect = (base - timedelta(minutes=i)).isoformat()
            assert r["items"][0]["triggered_at"] == expect, p
    finally:
        db.close()
