"""T-51（修 P1-18）signals API 三处筛选/口径问题的聚焦测试。

覆盖：
1. /events sector 前置过滤（主路径）：limit 前 SQL 过滤，不再 limit 后内存筛截断
   ——目标板块事件被其它板块新事件挤出 limit 时，修复后仍全量返回。
2. /events sector + watchlist 大集合分批路径：>400 只自选股先与板块交集再分批。
3. /events sector 空集 → 空数据。
4. stock_timeline：days 为「最大事件条数」行数语义（回归固化契约，非自然日天数）。
time_range 口径本次选「文档明示」方案（见 signals.py list_events docstring），
未做对齐改造，故无日历口径断言。临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock


@pytest.fixture()
def temp_db(tmp_path):
    """独立临时 DB，直调端点函数（db 参数注入），不触碰生产库。"""
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

    # 清理跨测试残留的板块→代码集合缓存（含 TTL），避免旧数据污染断言
    from app.api import signals as S

    S._sector_codes_cache.clear()

    yield Maker
    engine.dispose()


def _cn_now_naive() -> datetime:
    """Asia/Shanghai 墙钟（naive），造自然日边界稳定的种子事件。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def _seed_events_at(maker, rows):
    """rows: list of dict(stock_code, at, ...)。批量单 commit。"""
    db = maker()
    try:
        for r in rows:
            db.add(
                SignalEvent(
                    stock_code=r["stock_code"],
                    signals=r.get("signals", ["volume_surge"]),
                    status=r.get("status", "观察"),
                    evidence=r.get("evidence", {"price": 1.0}),
                    triggered_at=r["at"],
                    as_of=r["at"],
                    scan_discovered_at=r["at"],
                    period=r.get("period", "daily"),
                )
            )
        db.commit()
    finally:
        db.close()


def _seed_stocks(maker, specs):
    """specs: list of (code, is_watchlist)。批量单 commit。"""
    db = maker()
    try:
        for code, wl in specs:
            db.add(
                Stock(
                    code=code,
                    name=f"股票{code.split('.')[0]}",
                    is_watchlist=wl,
                )
            )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /events sector 前置过滤（主路径）：limit 前 SQL 过滤，不截断
# ---------------------------------------------------------------------------


def test_events_sector_filter_precedes_sql_limit(temp_db):
    """200 条沪主板 + 10 条更新的创业板事件：sector=沪主板 + limit=200。

    修复前 SQL 取最新 200 条被 10 条创业板挤出 10 条最旧沪主板，内存筛后仅 190；
    修复后 sector 前置 SQL 过滤，200 条沪主板全量返回。
    """
    from app.api.signals import list_events

    now = _cn_now_naive()
    rows = []
    for i in range(200):  # 沪主板（较旧）
        rows.append(
            {
                "stock_code": "600519.SH",
                "at": now - timedelta(hours=2) + timedelta(seconds=i),
            }
        )
    for j in range(10):  # 创业板（较新，挤占 limit）
        rows.append(
            {
                "stock_code": "300750.SZ",
                "at": now - timedelta(minutes=30) + timedelta(seconds=j),
            }
        )
    _seed_events_at(temp_db, rows)
    db = temp_db()
    try:
        # 目标板块不被截断：200 条沪主板全部命中（修复前仅 190）
        r = list_events(limit=200, sector="沪主板", time_range="all", db=db)
        assert len(r["data"]) == 200
        assert all(it["sector"] == "沪主板" for it in r["data"])
        # 窄板块事件全返回（10 < 200，不截断、不混入其它板块）
        r2 = list_events(limit=200, sector="创业板", time_range="all", db=db)
        assert len(r2["data"]) == 10
        assert all(it["sector"] == "创业板" for it in r2["data"])
        # 无 sector 对照：最新 200 条 = 10 创业板 + 190 沪主板（证明挤出确实发生）
        r3 = list_events(limit=200, time_range="all", db=db)
        assert len(r3["data"]) == 200
        assert sum(1 for it in r3["data"] if it["sector"] == "创业板") == 10
        assert sum(1 for it in r3["data"] if it["sector"] == "沪主板") == 190
    finally:
        db.close()


def test_events_sector_filter_watchlist_batches(temp_db):
    """watchlist_only + sector：>400 只自选股先与板块交集再分批查询。

    401 只创业板自选股（各 1 条事件）+ 10 条更新的非自选沪主板事件：
    修复前分批取最新 200 条混入沪主板、内存筛后不足 200；修复后 200 条全创业板。
    """
    from app.api.signals import list_events

    now = _cn_now_naive()
    cyb = [f"300{i:03d}.SZ" for i in range(1, 402)]  # 401 只创业板自选股
    _seed_stocks(temp_db, [(c, True) for c in cyb])
    rows = [
        {"stock_code": c, "at": now - timedelta(hours=1) + timedelta(seconds=i)}
        for i, c in enumerate(cyb)
    ]
    rows += [
        {
            "stock_code": "600519.SH",
            "at": now - timedelta(minutes=30) + timedelta(seconds=j),
        }
        for j in range(10)
    ]
    _seed_events_at(temp_db, rows)
    db = temp_db()
    try:
        r = list_events(
            limit=200, sector="创业板", watchlist_only=True, time_range="all", db=db
        )
        assert len(r["data"]) == 200  # 401 条创业板事件取最新 200，不混入沪主板
        assert all(it["sector"] == "创业板" for it in r["data"])
        assert all(it["is_watchlist"] for it in r["data"])
    finally:
        db.close()


def test_events_sector_empty_returns_empty(temp_db):
    """sector 命中的板块在事件表无对应代码 → 空数据（与 /events/page 一致）。"""
    from app.api.signals import list_events

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [{"stock_code": "600519.SH", "at": now - timedelta(hours=1)}],
    )
    db = temp_db()
    try:
        r = list_events(limit=200, sector="创业板", time_range="all", db=db)
        assert r == {"data": []}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# stock_timeline：days 为「最大事件条数」行数语义（非自然日天数）
# ---------------------------------------------------------------------------


def test_stock_timeline_days_is_row_limit(temp_db):
    """10 条事件集中于 1 小时内：days=3 → 返回最近 3 条（行数语义）。

    若是 3 天自然日窗口则 10 条全返回——行数契约被固化；days=0 回退默认 30。
    """
    from app.api.signals import stock_timeline

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {
                "stock_code": "600519.SH",
                "at": now - timedelta(hours=1) + timedelta(minutes=i),
            }
            for i in range(10)
        ],
    )
    db = temp_db()
    try:
        r = stock_timeline("600519.SH", days=3, db=db)
        assert len(r["data"]) == 3
        # 最近 3 条 = 最新 3 条（triggered_at 降序）
        assert [e["triggered_at"] for e in r["data"]] == sorted(
            (e["triggered_at"] for e in r["data"]), reverse=True
        )
        # 0 → 回退默认 30，全量 10 条
        r2 = stock_timeline("600519.SH", days=0, db=db)
        assert len(r2["data"]) == 10
        # 默认参数（不传 days）契约不变
        r3 = stock_timeline("600519.SH", db=db)
        assert len(r3["data"]) == 10
        # 负值同样 clamp 到默认 30
        r4 = stock_timeline("600519.SH", days=-5, db=db)
        assert len(r4["data"]) == 10
    finally:
        db.close()
