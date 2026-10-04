"""分页端点新增筛选的聚焦测试：实验 status/keyword、信号事件 signal_types。

覆盖：筛选在 count/分页前完成（total 为筛选后完整计数）、跨页分页、组合筛选、
非日线周期内存筛选、time_range/exclude_today 市场日历口径时间过滤（含默认 3d）、
signal_match=all 股票级语义（跨事件满足全部选中信号）、旧端点行为不变。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, SignalEvent, utcnow


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（不触碰生产库）。"""
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

    import app.core.tasks.runner as Q

    monkeypatch.setattr(Q, "SessionLocal", Maker)

    # 清理跨测试残留的模块级状态
    from app.api import signals as S

    S._sector_codes_cache.clear()

    yield Maker
    engine.dispose()


# ---------------------------------------------------------------------------
# 造数辅助
# ---------------------------------------------------------------------------


def _seed_jobs(maker, specs):
    """specs: list of (job_type, status, expression, extra_params)。"""
    db = maker()
    try:
        ids = []
        for i, (jt, st, expr, extra) in enumerate(specs):
            j = ExperimentJob(
                job_type=jt,
                status=st,
                progress=50.0,
                params={"expression": expr, **extra},
            )
            db.add(j)
            db.flush()
            ids.append(j.id)
        db.commit()
        return ids
    finally:
        db.close()


def _seed_events(maker, rows):
    """rows: list of dict(stock_code, signals, ...)。"""
    db = maker()
    try:
        base = utcnow()
        for i, r in enumerate(rows):
            e = SignalEvent(
                stock_code=r["stock_code"],
                signals=r["signals"],
                status=r.get("status", "观察"),
                evidence=r.get("evidence", {"price": 1.0}),
                triggered_at=base + timedelta(seconds=i),
                as_of=base + timedelta(seconds=i),
                scan_discovered_at=base + timedelta(seconds=i),
                period=r.get("period", "daily"),
            )
            db.add(e)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /experiments/page：status 筛选
# ---------------------------------------------------------------------------


def test_experiments_page_status_filter(temp_db):
    from app.api.experiments import experiment_list_page

    _seed_jobs(
        temp_db,
        [
            ("gp_run", "done", "close1", {}),
            ("gp_run", "done", "close2", {}),
            ("gp_run", "running", "close3", {}),
            ("gp_run", "pending", "close4", {}),
            ("evaluate", "done", "close5", {}),
            ("evaluate", "failed", "close6", {}),
        ],
    )

    # active 别名 = pending + running
    r = experiment_list_page(limit=50, offset=0, status="active")
    assert r["total"] == 2
    assert {it["status"] for it in r["items"]} == {"pending", "running"}

    # 单状态精确过滤
    r2 = experiment_list_page(limit=50, offset=0, status="done")
    assert r2["total"] == 3 and all(it["status"] == "done" for it in r2["items"])

    r3 = experiment_list_page(limit=50, offset=0, status="failed")
    assert r3["total"] == 1 and r3["items"][0]["job_type"] == "evaluate"

    # 未知状态 → 空结果（total=0，不是报错）
    r4 = experiment_list_page(limit=50, offset=0, status="bogus")
    assert r4["total"] == 0 and r4["items"] == []


def test_experiments_page_keyword_filter(temp_db):
    from app.api.experiments import experiment_list_page

    _seed_jobs(
        temp_db,
        [
            ("gp_run", "done", "CLOSE10 + Rank(volume)", {"note": "Alpha-Mining"}),
            ("gp_run", "running", "close5 * open", {}),
            ("evaluate", "done", "TURNOVER", {"note": "Sector-Quant"}),
            ("backtest", "failed", "other", {}),
        ],
    )

    # 表达式大小写不敏感（CLOSE10 / close5 均命中）
    r = experiment_list_page(limit=50, offset=0, keyword="close")
    assert r["total"] == 2

    # 序列化 params 中非 expression 键也能命中（大小写不敏感）
    r2 = experiment_list_page(limit=50, offset=0, keyword="sector-quant")
    assert r2["total"] == 1 and r2["items"][0]["job_type"] == "evaluate"
    r2b = experiment_list_page(limit=50, offset=0, keyword="SECTOR-QUANT")
    assert r2b["total"] == 1

    # job id 命中：id=3 且其 params 不含数字 3，保证只由 id 命中
    r3 = experiment_list_page(limit=50, offset=0, keyword="3")
    assert r3["total"] == 1 and r3["items"][0]["id"] == 3

    # 无命中
    r4 = experiment_list_page(limit=50, offset=0, keyword="不存在的关键字")
    assert r4["total"] == 0 and r4["items"] == []


def test_experiments_page_combined_filters_and_cross_page(temp_db):
    from app.api.experiments import experiment_list_page

    _seed_jobs(
        temp_db,
        [
            ("gp_run", "running", "A_close", {}),
            ("gp_run", "pending", "B_close", {}),
            ("gp_run", "done", "C_close", {}),
            ("gp_run", "failed", "D_other", {}),
            ("evaluate", "done", "E_close", {}),
        ],
    )

    # 组合：job_type + status(active) + keyword → total 为筛选后完整计数（非原始 5）
    r = experiment_list_page(
        limit=50, offset=0, job_type="gp_run", status="active", keyword="close"
    )
    assert r["total"] == 2 and {it["id"] for it in r["items"]} == {1, 2}

    # 跨页：在筛选集上分页，has_more 按筛选 total 计算
    r2 = experiment_list_page(limit=1, offset=0, job_type="gp_run", keyword="close")
    assert r2["total"] == 3 and len(r2["items"]) == 1 and r2["has_more"] is True
    assert r2["items"][0]["id"] == 3  # id 降序，筛选集 {1,2,3}

    r3 = experiment_list_page(limit=1, offset=2, job_type="gp_run", keyword="close")
    assert r3["total"] == 3 and len(r3["items"]) == 1 and r3["has_more"] is False
    assert r3["items"][0]["id"] == 1

    r4 = experiment_list_page(limit=1, offset=3, job_type="gp_run", keyword="close")
    assert r4["total"] == 3 and r4["items"] == [] and r4["has_more"] is False

    # 旧参数兼容：仅 job_type 过滤行为不变
    r5 = experiment_list_page(limit=50, offset=0, job_type="evaluate")
    assert r5["total"] == 1 and r5["items"][0]["id"] == 5


# ---------------------------------------------------------------------------
# /signals/events/page：signal_types 筛选（JSON 文本精确匹配，OR）
# ---------------------------------------------------------------------------


def test_events_page_signal_types_filter(temp_db):
    from app.api.signals import list_events_page

    rows = [
        {"stock_code": "600519.SH", "signals": ["volume_surge"]},
        {"stock_code": "000001.SZ", "signals": ["limit_up"]},
        {
            "stock_code": "300750.SZ",
            "signals": ["volume_surge", "price_up"],
        },
        {"stock_code": "601318.SH", "signals": ["price_up"]},
        {
            "stock_code": "600000.SH",
            "signals": ["volume_shrink"],
        },
    ]
    _seed_events(temp_db, rows)
    db = temp_db()

    # OR 单值：含 volume_surge 的事件
    r = list_events_page(limit=50, offset=0, signal_types="volume_surge", db=db)
    assert r["total"] == 2
    assert {it["stock_code"] for it in r["items"]} == {"600519.SH", "300750.SZ"}

    # OR 多值：命中任一信号即保留
    r2 = list_events_page(limit=50, offset=0, signal_types="limit_up,price_up", db=db)
    assert r2["total"] == 3
    assert {it["stock_code"] for it in r2["items"]} == {
        "000001.SZ",
        "300750.SZ",
        "601318.SH",
    }

    # 精确匹配：子串不命中（surge 不是完整信号名）
    r3 = list_events_page(limit=50, offset=0, signal_types="surge", db=db)
    assert r3["total"] == 0

    # 未命中任何信号类型的事件不出现
    r4 = list_events_page(limit=50, offset=0, signal_types="macd_golden_cross", db=db)
    assert r4["total"] == 0
    db.close()


def test_events_page_filters_before_pagination_cross_page(temp_db):
    from app.api.signals import list_events_page

    rows = []
    for i in range(13):
        rows.append(
            {
                "stock_code": "600519.SH",
                "signals": ["volume_surge"] if i % 2 == 0 else ["price_up"],
            }
        )
    for _ in range(12):
        rows.append(
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
            }
        )
    _seed_events(temp_db, rows)
    db = temp_db()

    # signal_types 筛选：total 为筛选后 18（price_up：A 奇数 6 + B 12），跨页不混入
    r = list_events_page(limit=10, offset=0, signal_types="price_up", db=db)
    assert r["total"] == 18 and len(r["items"]) == 10 and r["has_more"] is True
    assert all("price_up" in it["signals"] for it in r["items"])

    r2 = list_events_page(limit=10, offset=10, signal_types="price_up", db=db)
    assert len(r2["items"]) == 8 and r2["has_more"] is False
    assert all("price_up" in it["signals"] for it in r2["items"])

    # volume_surge OR 筛选：13 条 A 中偶数下标 7 条含 volume_surge
    r3 = list_events_page(limit=50, offset=0, signal_types="volume_surge", db=db)
    assert r3["total"] == 7
    assert all("volume_surge" in it["signals"] for it in r3["items"])

    # 组合 + 跨页：code=600519.SH ∩ volume_surge = 7，跨两页
    r4 = list_events_page(
        limit=4, offset=0, code="600519.SH", signal_types="volume_surge", db=db
    )
    assert r4["total"] == 7 and len(r4["items"]) == 4 and r4["has_more"] is True
    r5 = list_events_page(
        limit=4, offset=4, code="600519.SH", signal_types="volume_surge", db=db
    )
    assert len(r5["items"]) == 3 and r5["has_more"] is False
    db.close()


def test_events_page_combined_filters(temp_db):
    from app.api.signals import list_events_page

    rows = [
        {
            "stock_code": "600519.SH",
            "signals": ["volume_surge"],
            "status": "观察",
        },
        {
            "stock_code": "600519.SH",
            "signals": ["price_up"],
            "status": "已忽略",
        },
        {
            "stock_code": "000001.SZ",
            "signals": ["price_up"],
            "status": "观察",
        },
        {
            "stock_code": "300750.SZ",
            "signals": ["volume_surge"],
            "status": "观察",
        },
    ]
    _seed_events(temp_db, rows)
    db = temp_db()

    # code=600519.SH + 含 volume_surge + 观察 → 仅第一条
    r = list_events_page(
        limit=50,
        offset=0,
        code="600519.SH",
        signal_types="volume_surge",
        status="观察",
        db=db,
    )
    assert r["total"] == 1 and r["items"][0]["stock_code"] == "600519.SH"

    # 叠加 status 后的 code 筛选
    r2 = list_events_page(
        limit=50,
        offset=0,
        code="600519.SH",
        signal_types="volume_surge",
        db=db,
    )
    assert r2["total"] == 1
    db.close()


# ---------------------------------------------------------------------------
# 非日线周期：查事件表（period 列过滤），语义与 daily 完全同构
# ---------------------------------------------------------------------------


def _cn_now_naive() -> datetime:
    """Asia/Shanghai 墙钟（naive），用于造自然日边界稳定的种子事件。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def _seed_events_at(maker, rows):
    """rows: list of dict(stock_code, signals, at: naive 时间)。"""
    db = maker()
    try:
        for r in rows:
            db.add(
                SignalEvent(
                    stock_code=r["stock_code"],
                    signals=r["signals"],
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


def _seed_time_spread_events(maker):
    """4 条事件：今天 / 昨天 / 前天 / 5 天前（Asia/Shanghai 相对当前时刻）。"""
    now = _cn_now_naive()
    _seed_events_at(
        maker,
        [
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now,
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=1),
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=2),
            },
            {
                "stock_code": "601318.SH",
                "signals": ["price_up"],
                "at": now - timedelta(days=5),
            },
        ],
    )


def test_events_page_non_daily_queries_event_table(temp_db):
    """非日线周期改查事件表：period 过滤 + 筛选与 daily 完全同构（triggered_at 降序）。"""
    from app.api.signals import list_events_page

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now - timedelta(days=1),
                "period": "60",
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=2),
                "period": "60",
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["volume_surge"],
                "at": now - timedelta(days=2) - timedelta(hours=1),
                "period": "60",
            },
            {
                "stock_code": "601318.SH",
                "signals": ["price_up"],
                "at": now,  # 不同周期：应被 period 过滤排除
                "period": "daily",
            },
        ],
    )
    db = temp_db()
    try:
        # 全部 60 分钟事件按 triggered_at 降序，不混入 daily 事件。
        # time_range="all"：种子含 now-2d-1h 事件，跨午夜后可能落出默认 3d 自然日窗口（时间边界脆弱性）
        r = list_events_page(limit=50, offset=0, period="60", time_range="all", db=db)
        assert r["total"] == 3 and r["period"] == "60"
        assert [it["stock_code"] for it in r["items"]] == [
            "600519.SH",
            "000001.SZ",
            "300750.SZ",
        ]

        # signal_types 过滤（OR）
        r3 = list_events_page(
            limit=50,
            offset=0,
            period="60",
            signal_types="volume_surge",
            time_range="all",
            db=db,
        )
        assert r3["total"] == 1 and r3["items"][0]["stock_code"] == "300750.SZ"

        # 组合命中：period=60 ∩ price_up
        r6 = list_events_page(
            limit=50,
            offset=0,
            period="60",
            signal_types="price_up",
            time_range="all",
            db=db,
        )
        assert r6["total"] == 2

        # 跨页：total 为筛选后完整计数（非日线无计算窗口上限）
        r4 = list_events_page(limit=2, offset=0, period="60", time_range="all", db=db)
        assert r4["total"] == 3 and len(r4["items"]) == 2 and r4["has_more"] is True
        r5 = list_events_page(limit=2, offset=2, period="60", time_range="all", db=db)
        assert r5["total"] == 3 and len(r5["items"]) == 1 and r5["has_more"] is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# time_range / exclude_today：daily 市场日历口径过滤（默认 3d，count/分页前完成）
# ---------------------------------------------------------------------------


def test_events_page_daily_default_time_range_is_3d(temp_db):
    from app.api.signals import list_events_page

    _seed_time_spread_events(temp_db)
    db = temp_db()
    try:
        r = list_events_page(limit=50, offset=0, db=db)
        assert r["total"] == 3  # 默认 time_range=3d：今天/昨天/前天，5 天前被过滤
        assert [it["stock_code"] for it in r["items"]] == [
            "600519.SH",
            "000001.SZ",
            "300750.SZ",
        ]
        assert r["period"] == "daily"
    finally:
        db.close()


def test_events_page_daily_time_range_spans(temp_db):
    from app.api.signals import list_events_page

    _seed_time_spread_events(temp_db)
    db = temp_db()
    try:
        assert (
            list_events_page(limit=50, offset=0, time_range="today", db=db)["total"]
            == 1
        )
        assert (
            list_events_page(limit=50, offset=0, time_range="3d", db=db)["total"] == 3
        )
        assert (
            list_events_page(limit=50, offset=0, time_range="7d", db=db)["total"] == 4
        )
        assert (
            list_events_page(limit=50, offset=0, time_range="30d", db=db)["total"] == 4
        )
        assert (
            list_events_page(limit=50, offset=0, time_range="all", db=db)["total"] == 4
        )
        with pytest.raises(HTTPException) as ei:
            list_events_page(limit=50, offset=0, time_range="bogus", db=db)
        assert ei.value.status_code == 400
    finally:
        db.close()


def test_events_page_daily_time_range_filters_before_pagination(temp_db):
    from app.api.signals import list_events_page

    _seed_time_spread_events(temp_db)
    db = temp_db()
    try:
        # 时间范围在 count/分页前过滤：total 为筛选后完整计数 3
        p1 = list_events_page(limit=2, offset=0, db=db)
        assert p1["total"] == 3 and len(p1["items"]) == 2 and p1["has_more"] is True
        assert [it["stock_code"] for it in p1["items"]] == ["600519.SH", "000001.SZ"]
        p2 = list_events_page(limit=2, offset=2, db=db)
        assert p2["total"] == 3 and len(p2["items"]) == 1 and p2["has_more"] is False
        assert p2["items"][0]["stock_code"] == "300750.SZ"
    finally:
        db.close()


def test_events_page_daily_exclude_today(temp_db):
    from app.api.signals import list_events_page

    _seed_time_spread_events(temp_db)
    db = temp_db()
    try:
        # 默认 3d + exclude_today：排除今天，剩昨天/前天
        r = list_events_page(limit=50, offset=0, exclude_today=True, db=db)
        assert r["total"] == 2
        assert {it["stock_code"] for it in r["items"]} == {"000001.SZ", "300750.SZ"}
        # all + exclude_today：仅排除今天，其余 3 条全保留
        r2 = list_events_page(
            limit=50, offset=0, time_range="all", exclude_today=True, db=db
        )
        assert r2["total"] == 3
        assert "600519.SH" not in {it["stock_code"] for it in r2["items"]}
        # today + exclude_today：空区间
        r3 = list_events_page(
            limit=50, offset=0, time_range="today", exclude_today=True, db=db
        )
        assert r3["total"] == 0 and r3["items"] == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 非日线：time_range / exclude_today 按事件表 triggered_at 过滤
# ---------------------------------------------------------------------------


def test_events_page_non_daily_time_range_filters_triggered_at(temp_db):
    """非日线查事件表：time_range/exclude_today 按事件 triggered_at（市场日历口径）过滤。"""
    from app.api.signals import list_events_page

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now,
                "period": "60",
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=2),
                "period": "60",
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=5),
                "period": "60",
            },
        ],
    )
    db = temp_db()
    try:
        assert (
            list_events_page(
                limit=50, offset=0, period="60", time_range="today", db=db
            )["total"]
            == 1
        )
        assert (
            list_events_page(limit=50, offset=0, period="60", time_range="3d", db=db)[
                "total"
            ]
            == 2
        )
        assert (
            list_events_page(limit=50, offset=0, period="60", time_range="7d", db=db)[
                "total"
            ]
            == 3
        )
        assert (
            list_events_page(limit=50, offset=0, period="60", time_range="all", db=db)[
                "total"
            ]
            == 3
        )
        # exclude_today：today 下为空；all 下排除今天
        r2 = list_events_page(
            limit=50,
            offset=0,
            period="60",
            time_range="today",
            exclude_today=True,
            db=db,
        )
        assert r2["total"] == 0 and r2["items"] == []
        r3 = list_events_page(
            limit=50,
            offset=0,
            period="60",
            time_range="all",
            exclude_today=True,
            db=db,
        )
        assert r3["total"] == 2
        assert "600519.SH" not in {it["stock_code"] for it in r3["items"]}
    finally:
        db.close()


def test_events_page_non_daily_returns_seeded_triggered_at(temp_db):
    """非日线查事件表：triggered_at 直读事件表（不再由 K 线 bar 重算），降序返回。"""
    from app.api.signals import list_events_page

    now = _cn_now_naive().replace(second=0, microsecond=0)
    _seed_events_at(
        temp_db,
        [
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now,
                "period": "60",
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(minutes=5),
                "period": "60",
            },
        ],
    )
    db = temp_db()
    try:
        r = list_events_page(limit=50, offset=0, period="60", time_range="all", db=db)
        assert r["total"] == 2
        assert [it["triggered_at"] for it in r["items"]] == [
            now.isoformat(),
            (now - timedelta(minutes=5)).isoformat(),
        ]
    finally:
        db.close()


def test_events_page_non_daily_period_isolated(temp_db):
    """period 过滤：weekly 查询只返回 period=='weekly' 的事件，不混入其他周期。"""
    from app.api.signals import list_events_page, list_events

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now,
                "period": "weekly",
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=1),
                "period": "weekly",
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=2),
                "period": "daily",
            },
            {
                "stock_code": "601318.SH",
                "signals": ["price_up"],
                "at": now - timedelta(days=3),
                "period": "monthly",
            },
        ],
    )
    db = temp_db()
    try:
        r = list_events_page(limit=50, offset=0, period="weekly", db=db)
        assert r["total"] == 2 and r["period"] == "weekly"
        assert [it["stock_code"] for it in r["items"]] == ["600519.SH", "000001.SZ"]
        # /events 数组端点同语义
        r2 = list_events(limit=50, period="weekly", db=db)
        assert {it["stock_code"] for it in r2["data"]} == {"600519.SH", "000001.SZ"}
    finally:
        db.close()


def test_scan_status_reports_per_period_last_scan(temp_db):
    """/signals/scan/status：periods 返回各周期最近扫描时刻；daily 兼容旧键 meta:last_scan。"""
    from app.api.signals import scan_status
    from app.lib.timex import PERIODS
    from app.storage.klines import set_meta

    db = temp_db()
    try:
        set_meta("meta:last_scan", 1000, db=db)  # 旧键（daily 扫描写）
        set_meta("meta:last_scan:60", 2000, db=db)
        set_meta("meta:last_scan:weekly", 3000, db=db)
        db.commit()
        r = scan_status(db=db)
        # T-54：响应新增 schedule（周期→间隔秒调度表，前端新鲜度阈值自适应）
        assert set(r) == {"last_scan_at", "in_trading_session", "periods", "schedule"}
        # epoch 1000 = 1970-01-01 00:16:40 UTC = 08:16:40 上海（+8）
        assert r["last_scan_at"] == "1970-01-01T08:16:40"
        for p in PERIODS:
            assert p in r["periods"]
        assert r["periods"]["60"] == "1970-01-01T08:33:20"
        assert r["periods"]["weekly"] == "1970-01-01T08:50:00"
        # daily 无专属键 → 回退旧键 meta:last_scan
        assert r["periods"]["daily"] == r["last_scan_at"]
        # 未扫描过的周期 → None
        assert r["periods"]["5"] is None
        # schedule：全部周期键存在且间隔为正秒数（单一事实源 Settings.scan_schedule）
        for p in PERIODS:
            assert p in r["schedule"] and r["schedule"][p] > 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /signals/events/page：signal_match=all（股票级跨事件满足全部选中信号）
# ---------------------------------------------------------------------------


def test_events_page_signal_match_all_cross_events(temp_db):
    """all 语义：同一股票可跨多条事件满足全部选中信号，随后只返回相关事件。"""
    from app.api.signals import list_events_page

    rows = [
        {
            "stock_code": "600519.SH",
            "signals": ["macd_golden_cross"],
        },
        {
            "stock_code": "600519.SH",
            "signals": ["kdj_golden_cross"],
        },
        {
            "stock_code": "000001.SZ",
            "signals": ["macd_golden_cross"],
        },
        {"stock_code": "000001.SZ", "signals": ["price_up"]},
        {
            "stock_code": "300750.SZ",
            "signals": ["macd_golden_cross", "kdj_golden_cross"],
        },
    ]
    _seed_events(temp_db, rows)
    db = temp_db()
    try:
        want = "macd_golden_cross,kdj_golden_cross"
        # 600519 跨两条事件满足两个信号 → 2 条事件；300750 单事件含两者 → 1 条；
        # 000001 缺 kdj → 整只股票排除
        r = list_events_page(
            limit=50, offset=0, signal_types=want, signal_match="all", db=db
        )
        assert r["total"] == 3
        assert {it["stock_code"] for it in r["items"]} == {"600519.SH", "300750.SZ"}
        # 只返回与所选信号相关的事件（600519 两条都在，且每条都含选中信号）
        assert all(
            set(it["signals"]) & {"macd_golden_cross", "kdj_golden_cross"}
            for it in r["items"]
        )
    finally:
        db.close()


def test_events_page_signal_match_any_default_no_regression(temp_db):
    """signal_match 默认 any：命中任一信号即保留（回归旧语义）。"""
    from app.api.signals import list_events_page

    rows = [
        {
            "stock_code": "600519.SH",
            "signals": ["macd_golden_cross"],
        },
        {
            "stock_code": "600519.SH",
            "signals": ["kdj_golden_cross"],
        },
        {
            "stock_code": "000001.SZ",
            "signals": ["macd_golden_cross"],
        },
        {
            "stock_code": "000001.SZ",
            "signals": ["price_up"],
        },
        {
            "stock_code": "300750.SZ",
            "signals": ["macd_golden_cross", "kdj_golden_cross"],
        },
    ]
    _seed_events(temp_db, rows)
    db = temp_db()
    try:
        # 不传 signal_match（默认 any）：5 条中除 000001(price_up) 外全命中
        r = list_events_page(
            limit=50, offset=0, signal_types="macd_golden_cross,kdj_golden_cross", db=db
        )
        assert r["total"] == 4
        # 显式 any 与默认一致
        r2 = list_events_page(
            limit=50,
            offset=0,
            signal_match="any",
            signal_types="macd_golden_cross,kdj_golden_cross",
            db=db,
        )
        assert r2["total"] == 4
        assert {it["stock_code"] for it in r2["items"]} == {
            "600519.SH",
            "000001.SZ",
            "300750.SZ",
        }
    finally:
        db.close()


def test_events_page_signal_match_all_before_pagination(temp_db):
    """all 语义：total 为股票级筛选后完整计数，分页不混入不满足的股票。"""
    from app.api.signals import list_events_page

    rows = []
    for _ in range(10):
        rows.append(
            {
                "stock_code": "600519.SH",
                "signals": ["macd_golden_cross"],
            }
        )
    for _ in range(2):
        rows.append(
            {
                "stock_code": "600519.SH",
                "signals": ["kdj_golden_cross"],
            }
        )
    for _ in range(10):
        rows.append(
            {
                "stock_code": "000001.SZ",
                "signals": ["macd_golden_cross"],
            }
        )
    _seed_events(temp_db, rows)
    db = temp_db()
    try:
        want = "macd_golden_cross,kdj_golden_cross"
        r = list_events_page(
            limit=5, offset=0, signal_types=want, signal_match="all", db=db
        )
        assert r["total"] == 12 and len(r["items"]) == 5 and r["has_more"] is True
        assert all(it["stock_code"] == "600519.SH" for it in r["items"])

        r2 = list_events_page(
            limit=5, offset=10, signal_types=want, signal_match="all", db=db
        )
        assert len(r2["items"]) == 2 and r2["has_more"] is False
        assert all(it["stock_code"] == "600519.SH" for it in r2["items"])
    finally:
        db.close()


def test_events_page_signal_match_invalid_400(temp_db):
    from app.api.signals import list_events_page

    db = temp_db()
    try:
        with pytest.raises(HTTPException) as ei:
            list_events_page(
                limit=50,
                offset=0,
                signal_types="macd_golden_cross",
                signal_match="sometimes",
                db=db,
            )
        assert ei.value.status_code == 400
    finally:
        db.close()


def test_events_page_signal_match_all_with_time_range(temp_db):
    """all 语义与 time_range 组合：时间过滤先于股票级聚合（today 下跨模板失效）。"""
    from app.api.signals import list_events_page

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {
                "stock_code": "600519.SH",
                "signals": ["macd_golden_cross"],
                "at": now,
            },
            {
                "stock_code": "600519.SH",
                "signals": ["kdj_golden_cross"],
                "at": now - timedelta(days=1),
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["macd_golden_cross"],
                "at": now,
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=1),
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["macd_golden_cross", "kdj_golden_cross"],
                "at": now,
            },
        ],
    )
    db = temp_db()
    try:
        want = "macd_golden_cross,kdj_golden_cross"
        # today：600519 的 kdj 事件在昨天被时间过滤 → 股票级不满足 all → 只剩 300750
        r = list_events_page(
            limit=50,
            offset=0,
            signal_types=want,
            signal_match="all",
            time_range="today",
            db=db,
        )
        assert r["total"] == 1 and r["items"][0]["stock_code"] == "300750.SZ"
        # all 时间：600519 跨事件满足 → 2 条事件 + 300750 1 条
        r2 = list_events_page(
            limit=50,
            offset=0,
            signal_types=want,
            signal_match="all",
            time_range="all",
            db=db,
        )
        assert r2["total"] == 3
        assert {it["stock_code"] for it in r2["items"]} == {"600519.SH", "300750.SZ"}
        # any 语义回归：today 下 600519/000001/300750 均因任一选中信号命中而保留
        r3 = list_events_page(
            limit=50,
            offset=0,
            signal_types=want,
            signal_match="any",
            time_range="today",
            db=db,
        )
        assert r3["total"] == 3
        assert {it["stock_code"] for it in r3["items"]} == {
            "600519.SH",
            "000001.SZ",
            "300750.SZ",
        }
    finally:
        db.close()


def test_events_page_non_daily_signal_match_all(temp_db):
    """非日线查事件表：signal_match=all 按股票级满足全部选中信号（与 daily 同构）。"""
    from app.api.signals import list_events_page

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now,
                "period": "60",
            },
            {
                "stock_code": "600519.SH",
                "signals": ["volume_surge"],
                "at": now - timedelta(minutes=1),
                "period": "60",
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(minutes=2),
                "period": "60",
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["price_up", "volume_surge"],
                "at": now - timedelta(minutes=3),
                "period": "60",
            },
            {
                "stock_code": "601318.SH",
                "signals": ["price_up"],
                "at": now - timedelta(minutes=4),
                "period": "60",
            },
            {
                "stock_code": "601318.SH",
                "signals": ["volume_surge"],
                "at": now - timedelta(minutes=5),
                "period": "daily",  # 不同周期：不参与 60 分钟查询
            },
        ],
    )
    db = temp_db()
    try:
        want = "volume_surge,price_up"
        # all：600519（跨事件满足两者）2 条 + 300750（单事件含两者）1 条；
        # 000001 缺 volume_surge、601318 的 60 分钟事件缺 volume_surge → 排除
        r = list_events_page(
            limit=50,
            offset=0,
            period="60",
            signal_types=want,
            signal_match="all",
            db=db,
        )
        assert r["total"] == 3
        assert {it["stock_code"] for it in r["items"]} == {"600519.SH", "300750.SZ"}
        assert all(
            set(it["signals"]) & {"volume_surge", "price_up"} for it in r["items"]
        )

        # any：601318 的 60 分钟 price_up 事件保留 → 5 行
        r2 = list_events_page(
            limit=50,
            offset=0,
            period="60",
            signal_types=want,
            signal_match="any",
            db=db,
        )
        assert r2["total"] == 5
        assert "601318.SH" in {it["stock_code"] for it in r2["items"]}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 旧端点行为不变
# ---------------------------------------------------------------------------


def test_events_old_endpoint_still_array(temp_db):
    from app.api.signals import list_events

    _seed_events(
        temp_db,
        [
            {"stock_code": "600519.SH", "signals": ["volume_surge"]},
            {"stock_code": "600519.SH", "signals": ["price_up"]},
            {"stock_code": "000001.SZ", "signals": ["price_up", "limit_up"]},
            {"stock_code": "000001.SZ", "signals": ["price_up"]},
            {"stock_code": "300750.SZ", "signals": ["volume_surge", "price_up"]},
        ],
    )
    db = temp_db()
    old = list_events(limit=5, db=db)
    assert set(old) == {"data"}
    assert len(old["data"]) == 5
    db.close()


def test_experiments_old_endpoint_still_array(temp_db):
    from app.api.experiments import experiment_list
    from app.core.tasks.runner import list_jobs

    _seed_jobs(temp_db, [("gp_run", "done", "c1", {})] * 4)
    assert set(experiment_list(limit=2)) == {"data"}
    assert len(list_jobs(2)) == 2


# ---------------------------------------------------------------------------
# T-104 P2-45：POST /signals/scan 入参上限（codes ≤ 200 / top_n ≤ 200）
# ---------------------------------------------------------------------------


def test_trigger_scan_top_n_over_cap_400():
    from app.api.signals import trigger_scan

    with pytest.raises(HTTPException) as e:
        trigger_scan({"period": "5", "universe": "top_n", "top_n": 201})
    assert e.value.status_code == 400
    assert "top_n" in e.value.detail


def test_trigger_scan_codes_over_cap_400():
    from app.api.signals import trigger_scan

    codes = [f"600{i:03d}" for i in range(201)]
    with pytest.raises(HTTPException) as e:
        trigger_scan({"period": "5", "universe": "codes", "codes": codes})
    assert e.value.status_code == 400
    assert "codes" in e.value.detail


def test_trigger_scan_caps_inclusive_submit_params(monkeypatch):
    from app.api.signals import trigger_scan
    from app.core.tasks import runner

    calls = []

    def fake_submit(task, params):
        calls.append((task, params))
        return "job-1"

    monkeypatch.setattr(runner, "submit", fake_submit)
    r = trigger_scan(
        {
            "period": "5",
            "universe": "codes",
            "codes": ["600000"] * 200,  # 恰好等于上限 → 放行
        }
    )
    assert r["job_id"] == "job-1"
    assert calls[0][0] == "market_scan"
    assert len(calls[0][1]["codes"]) == 200

    r2 = trigger_scan({"period": "5", "universe": "top_n", "top_n": 200})
    assert r2["job_id"] == "job-1"
    assert calls[1][1]["top_n"] == 200


# ---------------------------------------------------------------------------
# P2-32：result 大 payload 懒加载（列表路径不物化 result 列，done 摘要仍可用）
# ---------------------------------------------------------------------------


def _listen_sql(maker):
    """注册 SQL 采集器，返回 statements 列表（断言列表查询不物化 result 列）。"""
    from sqlalchemy import event

    engine = maker.kw["bind"]
    stmts: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, executemany: stmts.append(
            statement
        ),
    )
    return stmts


def test_list_skips_result_column_for_non_done(temp_db):
    """列表路径（list_jobs）对非 done 任务不触达 result 大列（SQL 不含 result）。"""
    from app.core.tasks.runner import list_jobs

    db = temp_db()
    for st in ("pending", "running"):
        db.add(ExperimentJob(job_type="gp_run", params={}, status=st))
    db.commit()
    db.close()

    stmts = _listen_sql(temp_db)
    items = list_jobs(10)
    assert len(items) == 2
    sel = [s for s in stmts if "experiment_jobs" in s and s.strip().upper().startswith("SELECT")]
    assert sel, "应有列表查询"
    assert all("result" not in s.lower() for s in sel), "列表查询不得物化 result 列"


def test_list_summary_still_computed_from_done_result(temp_db):
    """done 任务摘要仍由 result 计算（列表批量取 result 列，输出契约不变）。"""
    from app.core.tasks.runner import list_jobs

    db = temp_db()
    db.add(
        ExperimentJob(
            job_type="backtest",
            status="done",
            progress=100.0,
            result={
                "backtest": {
                    "modes": [
                        {
                            "mode": "long",
                            "metrics": {
                                "annual_return": 0.12,
                                "sharpe": 1.5,
                                "max_drawdown": -0.2,
                                "win_rate": 0.6,
                            },
                        }
                    ]
                }
            },
            params={"expression": "close"},
        )
    )
    db.commit()
    db.close()

    items = list_jobs(10)
    assert len(items) == 1
    s = items[0]["summary"]
    assert s is not None and s["mode"] == "long" and s["sharpe"] == 1.5


def test_get_job_skips_result_column_for_running(temp_db):
    """get_job 对 running 任务不物化 result 列，返回空 dict（运行期轮询不再大字段反序列化）。"""
    from app.core.tasks.runner import get_job

    db = temp_db()
    j = ExperimentJob(job_type="gp_run", params={}, status="running")
    db.add(j)
    db.commit()
    jid = j.id
    db.close()

    stmts = _listen_sql(temp_db)
    detail = get_job(jid)
    assert detail["status"] == "running"
    assert detail["result"] == {}
    assert all(
        "result" not in s.lower() for s in stmts
    ), "运行中轮询不得物化 result 列"


def test_get_job_returns_full_result_for_done(temp_db):
    """done 任务 get_job 返回完整 result（详情契约不变，前端抽屉消费全量载荷）。"""
    from app.core.tasks.runner import get_job

    db = temp_db()
    j = ExperimentJob(
        job_type="backtest",
        status="done",
        progress=100.0,
        result={"backtest": {"modes": [{"mode": "long", "metrics": {"sharpe": 1.0}}]}},
        params={},
    )
    db.add(j)
    db.commit()
    jid = j.id
    db.close()

    detail = get_job(jid)
    assert detail["status"] == "done"
    assert detail["result"]["backtest"]["modes"][0]["metrics"]["sharpe"] == 1.0


# ---------- T-135 分页稳定序（同日同刻 tie-breaker） ----------


def test_page_filters_tiebreak_stable_order(temp_db, monkeypatch):
    """同日同刻多事件：id 次级键保证 offset 分页跨批无重复/跳过。"""
    from app.storage.repos.signals import page_events

    db = temp_db()
    t = datetime(2026, 8, 23, 10, 0, 0)
    try:
        for i in range(8):
            e = SignalEvent(
                stock_code=f"60000{i}",
                signals=["macd_golden_cross"],
                status="观察",
                evidence={},
                triggered_at=t,  # 全部同刻(不同股票,唯一键不冲突)
                as_of=t,
                period="daily",
            )
            db.add(e)
        db.commit()
    finally:
        db.close()

    seen: list[int] = []
    offset = 0
    for _ in range(4):  # 每批 3 条,拉 4 批覆盖全部 8 条
        rows, total, _stats = page_events(
            db=temp_db(), period="daily", offset=offset, limit=3,
            end=datetime(2026, 8, 24),  # end 为开区间上界,缺省 None 会 SQL 报错
        )
        for r in rows:
            seen.append(r.id)
        offset += len(rows)
        if not rows:
            break
    assert total == 8
    assert len(seen) == len(set(seen)) == 8  # 无重复
    assert seen == sorted(seen, reverse=True)  # 同刻 → id 降序
