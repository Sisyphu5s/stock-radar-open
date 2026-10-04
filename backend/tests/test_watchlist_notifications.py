"""自选股通知端点（/market/watchlist/notifications）与自选列表扩展的聚焦测试。

覆盖：
1. bootstrap（after_id 为空）：不返回任何旧消息，仅返回当前自选股事件最大 id 游标。
2. 增量拉取：只返回关注股票（is_watchlist）的事件，非关注股票事件绝不出现。
3. 增量顺序：按事件 id 升序返回，且严格 id > after_id。
4. limit/cursor：limit 钳制返回条数，latest_id = 本次返回最大 id，下一轮从 latest_id 继续，
   被截断的事件不丢失；无新事件时 latest_id 保持 after_id 不变。
5. GET /market/watchlist 扩展返回 last_price/pct_change/industry/updated_at（保留 code/name）。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock, utcnow
from app.api.market import get_watchlist, watchlist_notifications


@pytest.fixture()
def temp_db(tmp_path):
    """独立临时 DB（直接传 db 给路由函数，不触碰生产库）。"""
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
    yield Maker
    engine.dispose()


def _seed(db, stocks, events):
    """stocks: [(code, name, is_watchlist)]；events: [(stock_code, signals)]。"""
    for code, name, is_wl in stocks:
        db.add(
            Stock(
                code=code,
                name=name,
                is_watchlist=is_wl,
                industry="白酒" if is_wl else "其他",
                last_price=10.0,
                pct_change=1.2,
                updated_at=utcnow(),
            )
        )
    for i, (code, signals) in enumerate(events):
        db.add(
            SignalEvent(
                stock_code=code,
                signals=signals,
                status="观察",
                evidence={},
                triggered_at=utcnow(),
                as_of=utcnow(),
                scan_discovered_at=utcnow(),
            )
        )
    db.commit()


# ---------------------------------------------------------------------------
# bootstrap：不返回旧消息，仅返回游标
# ---------------------------------------------------------------------------


def test_bootstrap_returns_only_cursor(temp_db):
    db = temp_db()
    try:
        _seed(
            db,
            [
                ("600519.SH", "贵州茅台", True),
                ("000001.SZ", "平安银行", True),
                ("300750.SZ", "宁德时代", False),
            ],
            [
                ("600519.SH", ["volume_surge"]),
                ("000001.SZ", ["limit_up"]),
                ("000001.SZ", ["volume_surge"]),
                ("300750.SZ", ["volume_surge"]),
            ],
        )  # 非关注，id=4
        r = watchlist_notifications(after_id=None, db=db)
        assert r["bootstrap"] is True
        assert r["data"] == []  # 不返回任何旧消息
        assert r["latest_id"] == 3  # 关注股票事件最大 id（非关注 id=4 不计入）
    finally:
        db.close()


def test_bootstrap_empty_watchlist_cursor_zero(temp_db):
    db = temp_db()
    try:
        _seed(db, [], [("600519.SH", ["volume_surge"])])
        r = watchlist_notifications(after_id=None, db=db)
        assert r["bootstrap"] is True and r["data"] == [] and r["latest_id"] == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 不推非关注 + 增量顺序
# ---------------------------------------------------------------------------


def test_incremental_excludes_non_watchlist_ordered_asc(temp_db):
    db = temp_db()
    try:
        _seed(
            db,
            [("600519.SH", "贵州茅台", True), ("300750.SZ", "宁德时代", False)],
            [
                ("600519.SH", ["volume_surge"]),  # id=1 关注
                ("300750.SZ", ["limit_up"]),  # id=2 非关注
                ("600519.SH", ["price_up", "volume_shrink"]),
            ],
        )  # id=3 关注
        r = watchlist_notifications(after_id=0, db=db)
        assert r["bootstrap"] is False
        assert [e["id"] for e in r["data"]] == [1, 3]  # id 升序，跳过 id=2
        assert all(e["stock_code"] == "600519.SH" for e in r["data"])
        assert r["latest_id"] == 3
        # 事件字段齐全：id/股票/signals/status/triggered_at
        e1 = r["data"][0]
        assert e1["stock_name"] == "贵州茅台"
        assert e1["signals"] == ["volume_surge"]
        assert e1["status"] == "观察"
        assert e1["triggered_at"]
    finally:
        db.close()


def test_incremental_only_after_cursor(temp_db):
    db = temp_db()
    try:
        _seed(
            db,
            [("600519.SH", "贵州茅台", True)],
            [
                ("600519.SH", ["price_up"]),
                ("600519.SH", ["volume_surge"]),
                ("600519.SH", ["limit_up"]),
            ],
        )
        r = watchlist_notifications(after_id=1, db=db)
        assert [e["id"] for e in r["data"]] == [2, 3]
        assert r["latest_id"] == 3
        # after_id 超过全部事件：空且游标不动
        r2 = watchlist_notifications(after_id=3, db=db)
        assert r2["data"] == [] and r2["latest_id"] == 3
    finally:
        db.close()


# ---------------------------------------------------------------------------
# limit/cursor：截断后从 latest_id 续拉，不丢事件
# ---------------------------------------------------------------------------


def test_limit_clamps_and_cursor_resumes(temp_db):
    db = temp_db()
    try:
        stocks = [
            ("600519.SH", "贵州茅台", True),
            ("000001.SZ", "平安银行", True),
            ("300750.SZ", "宁德时代", True),
        ]
        events = [
            (c, ["price_up"])
            for i, c in enumerate(
                ["600519.SH"] * 6 + ["000001.SZ"] * 4 + ["300750.SZ"] * 2
            )
        ]  # 12 条关注事件
        _seed(db, stocks, events)
        assert db.query(SignalEvent).count() == 12

        # limit clamp：超过上限按上限钳制（上限 200，此处 12 条全部返回）
        r = watchlist_notifications(after_id=0, limit=9999, db=db)
        assert len(r["data"]) == 12 and r["latest_id"] == 12

        # 常规 limit=5：latest_id = 本次最大 id，续拉不丢
        r1 = watchlist_notifications(after_id=0, limit=5, db=db)
        assert [e["id"] for e in r1["data"]] == [1, 2, 3, 4, 5]
        assert r1["latest_id"] == 5

        r2 = watchlist_notifications(after_id=r1["latest_id"], limit=5, db=db)
        assert [e["id"] for e in r2["data"]] == [6, 7, 8, 9, 10]
        assert r2["latest_id"] == 10

        r3 = watchlist_notifications(after_id=r2["latest_id"], limit=5, db=db)
        assert [e["id"] for e in r3["data"]] == [11, 12]
        assert r3["latest_id"] == 12

        # 全部拉完：空 + 游标不变
        r4 = watchlist_notifications(after_id=12, limit=5, db=db)
        assert r4["data"] == [] and r4["latest_id"] == 12
    finally:
        db.close()


def test_limit_min_clamp_one(temp_db):
    db = temp_db()
    try:
        _seed(
            db,
            [("600519.SH", "贵州茅台", True)],
            [("600519.SH", ["price_up"])] * 3,
        )
        r = watchlist_notifications(after_id=0, limit=0, db=db)
        assert len(r["data"]) == 1 and r["latest_id"] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /market/watchlist 扩展字段
# ---------------------------------------------------------------------------


def test_get_watchlist_extended_fields(temp_db):
    db = temp_db()
    try:
        _seed(
            db, [("600519.SH", "贵州茅台", True), ("000001.SZ", "平安银行", False)], []
        )
        r = get_watchlist(db=db)
        assert len(r["data"]) == 1
        rec = r["data"][0]
        assert rec["code"] == "600519.SH" and rec["name"] == "贵州茅台"
        assert rec["last_price"] == 10.0
        assert rec["pct_change"] == 1.2
        assert rec["industry"] == "白酒"
        assert rec["updated_at"]
    finally:
        db.close()
