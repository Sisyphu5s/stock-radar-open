"""分页端点 + 任务取消竞态加固的聚焦测试（临时 SQLite，不触碰生产库）。"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, SignalEvent, Stock, utcnow

# ---------------------------------------------------------------------------
# 公共夹具：独立临时 DB，替换 queue.SessionLocal（计算线程/删除走同一临时库）
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
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
    monkeypatch.setattr(Q, "init_backend", lambda: None)

    # 清理跨测试残留的模块级状态
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()

    from app.api import signals as S

    S._sector_codes_cache.clear()

    yield Maker

    engine.dispose()


def _seed_jobs(maker, n: int, job_type: str = "gp_run") -> list[int]:
    db = maker()
    try:
        ids = []
        for i in range(n):
            j = ExperimentJob(
                job_type=job_type,
                params={"expression": f"close{i}"},
                status="done" if i % 3 else "running",
                progress=50.0,
            )
            db.add(j)
            db.flush()
            ids.append(j.id)
        db.commit()
        return ids
    finally:
        db.close()


def _seed_events(
    maker,
    n: int,
    sector_codes: list[str] | None = None,
    status: str = "观察",
) -> list[int]:
    """造 n 条按 stock_code 轮转、triggered_at 严格递增的事件。"""
    from datetime import timedelta

    codes = sector_codes or ["600519.SH", "000001.SZ", "300750.SZ"]
    db = maker()
    try:
        ids = []
        base = utcnow()
        for i in range(n):
            e = SignalEvent(
                stock_code=codes[i % len(codes)],
                signals=["volume_surge"],
                status=status,
                evidence={"price": 10.0},
                triggered_at=base + timedelta(seconds=i),
                as_of=base + timedelta(seconds=i),
                scan_discovered_at=base + timedelta(seconds=i),
            )
            db.add(e)
            db.flush()
            ids.append(e.id)
        db.commit()
        return ids
    finally:
        db.close()


def _seed_stocks(maker, codes: list[str], watchlist: bool = False):
    db = maker()
    try:
        for c in codes:
            db.add(Stock(code=c, name=f"股票{c[:6]}", is_watchlist=watchlist))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /signals/events/page 分页契约
# ---------------------------------------------------------------------------


def test_signals_events_page_contract_and_paging(temp_db):
    from app.api.signals import list_events_page

    _seed_events(temp_db, 25)
    db = temp_db()

    p1 = list_events_page(limit=10, offset=0, db=db)
    assert set(p1) == {
        "items",
        "total",
        "limit",
        "offset",
        "has_more",
        "period",
        "stats",
    }
    assert len(p1["items"]) == 10 and p1["total"] == 25
    assert p1["limit"] == 10 and p1["offset"] == 0 and p1["has_more"] is True
    assert p1["period"] == "daily"

    # 排序：triggered_at 降序（先插入的 id 小 → 时间旧 → 应排最后）
    ts = [it["triggered_at"] for it in p1["items"]]
    assert ts == sorted(ts, reverse=True)
    assert p1["items"][0]["id"] == 25  # 最新一条在前

    p2 = list_events_page(limit=10, offset=10, db=db)
    assert len(p2["items"]) == 10 and p2["offset"] == 10 and p2["has_more"] is True
    assert p2["items"][0]["id"] == 15

    p3 = list_events_page(limit=10, offset=20, db=db)
    assert len(p3["items"]) == 5 and p3["has_more"] is False

    # 每页条目结构与旧端点 data[] 一致
    assert {
        "id",
        "stock_code",
        "stock_name",
        "signals",
        "status",
        "evidence",
        "is_watchlist",
        "sector",
        "triggered_at",
        "as_of",
        "scan_discovered_at",
    } <= set(p1["items"][0])
    db.close()


def test_signals_events_page_filters(temp_db):
    from app.api.signals import list_events_page

    codes = ["600519.SH", "601318.SH", "000001.SZ", "300750.SZ", "688981.SH"]
    _seed_stocks(temp_db, codes)
    _seed_events(temp_db, 20, sector_codes=codes)
    db = temp_db()

    # sector 过滤：total 为该板块完整计数，条目全为该板块
    r = list_events_page(limit=50, offset=0, sector="沪主板", db=db)
    assert r["total"] == 8 and len(r["items"]) == 8  # 600519/601318 各 4 条
    assert all(it["sector"] == "沪主板" for it in r["items"])

    r2 = list_events_page(limit=50, offset=0, sector="创业板,科创板", db=db)
    assert r2["total"] == 8 and all(
        it["sector"] in ("创业板", "科创板") for it in r2["items"]
    )

    # status / code 过滤与排序一致
    r3 = list_events_page(limit=50, offset=0, status="已忽略", db=db)
    assert r3["total"] == 0 and r3["items"] == []

    r4 = list_events_page(limit=50, offset=0, code="600519.SH", db=db)
    assert r4["total"] == 4 and all(
        it["stock_code"] == "600519.SH" for it in r4["items"]
    )

    # watchlist_only
    r6 = list_events_page(limit=50, offset=0, watchlist_only=True, db=db)
    assert r6["total"] == 0  # 无自选股
    db.close()


def test_signals_events_old_endpoint_still_array(temp_db):
    from app.api.signals import list_events

    _seed_events(temp_db, 12)
    db = temp_db()
    old = list_events(limit=5, db=db)
    assert set(old) == {"data"}
    assert len(old["data"]) == 5
    db.close()


def test_page_events_match_all_keep_over_999(temp_db):
    """P1-36：signal_match=all 的 keep 超 SQLite 变量上限（999）仍正常分页。"""
    from app.api.signals import list_events_page

    # 1000 只股票各 1 条 volume_surge 事件 → all 语义 keep 含全部 1000 只，
    # 单次 IN 会超 SQLite 变量上限；分批后语义不变（total 完整、分页不截断）
    codes = [f"{6000 + i:04d}.SH" for i in range(1000)]
    _seed_events(temp_db, 1000, sector_codes=codes)
    db = temp_db()

    p1 = list_events_page(
        limit=50, offset=0, signal_types="volume_surge", signal_match="all", db=db
    )
    assert p1["total"] == 1000
    assert len(p1["items"]) == 50 and p1["has_more"] is True
    assert p1["stats"]["stock_count"] == 1000
    assert p1["items"][0]["id"] == 1000  # 最新（triggered_at 最晚）在前

    last = list_events_page(
        limit=50, offset=950, signal_types="volume_surge", signal_match="all", db=db
    )
    assert len(last["items"]) == 50 and last["has_more"] is False
    tail = list_events_page(
        limit=50, offset=990, signal_types="volume_surge", signal_match="all", db=db
    )
    assert len(tail["items"]) == 10
    db.close()


# ---------------------------------------------------------------------------
# /experiments/page 分页契约
# ---------------------------------------------------------------------------


def test_experiments_page_contract(temp_db):
    from app.api.experiments import experiment_list_page

    _seed_jobs(temp_db, 7, job_type="gp_run")
    _seed_jobs(temp_db, 3, job_type="evaluate")

    p1 = experiment_list_page(limit=3, offset=0)
    assert set(p1) == {"items", "total", "limit", "offset", "has_more"}
    assert len(p1["items"]) == 3 and p1["total"] == 10
    assert p1["has_more"] is True
    assert p1["items"][0]["id"] == 10  # id 降序

    p2 = experiment_list_page(limit=3, offset=9)
    assert len(p2["items"]) == 1 and p2["has_more"] is False

    # job_type 筛选（与旧端点一致）
    p3 = experiment_list_page(limit=50, offset=0, job_type="evaluate")
    assert p3["total"] == 3 and all(it["job_type"] == "evaluate" for it in p3["items"])

    # limit/offset clamp
    p4 = experiment_list_page(limit=0, offset=-5)
    assert p4["limit"] == 1 and p4["offset"] == 0


def test_experiments_old_endpoint_still_array(temp_db):
    from app.api.experiments import experiment_list
    from app.core.tasks.runner import list_jobs

    _seed_jobs(temp_db, 4)
    assert set(experiment_list(limit=2)) == {"data"}
    assert len(list_jobs(2)) == 2


# ---------------------------------------------------------------------------
# 取消竞态：取消后后台写入不得覆盖取消状态
# ---------------------------------------------------------------------------


def test_cancel_blocks_late_terminal_write(temp_db):
    """核心竞态：worker 已完成计算、终态写入之前用户取消 → 结果不得覆盖取消状态。"""
    from app.core.tasks.runner import (
        JobCancelled,
        _cancel_check,
        _cancelled,
        _finish,
        _update,
        delete_job,
    )

    db = temp_db()
    job = ExperimentJob(job_type="evaluate", params={}, status="running", progress=30.0)
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    res = delete_job(jid)
    assert res == {"ok": True, "deleted": False}

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert (
        row.status == "cancelled"
        and row.error == "已被用户取消"
        and row.finished_at is not None
    )

    # 后台晚到的终态/进度/finished_at 写入全部被条件更新拦截
    assert (
        _update(
            jid,
            status="done",
            progress=100.0,
            result={"results": [1]},
            finished_at=utcnow(),
        )
        == 0
    )
    assert _update(jid, progress=66.0) == 0
    assert _update(jid, finished_at=utcnow()) == 0

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled"
    assert row.error == "已被用户取消"
    assert row.result == {}  # 结果未被写入
    assert row.progress == 30.0  # 进度未被覆盖

    # 内存取消登记：中断点抛错、_finish 不认领
    with pytest.raises(JobCancelled):
        _cancel_check(jid)
    assert jid in _cancelled
    assert _finish(jid) is False

    # 取消后再次 delete → 任务已 cancelled，直接删行
    res2 = delete_job(jid)
    assert res2 == {"ok": True, "deleted": True}
    db = temp_db()
    assert db.get(ExperimentJob, jid) is None
    db.close()


def test_cancel_before_start_blocks_running_transition(temp_db):
    """pending 状态被取消 → worker 的 pending→running 转换被拦截并中止。"""
    from app.core.tasks.runner import _update, delete_job

    db = temp_db()
    job = ExperimentJob(job_type="gp_run", params={}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert delete_job(jid) == {"ok": True, "deleted": False}
    # worker 启动后尝试置 running → 条件更新不匹配（已 cancelled），返回 0
    assert _update(jid, status="running", progress=2.0) == 0

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled" and row.error == "已被用户取消"


def test_uncancelled_done_write_still_works(temp_db):
    """未取消任务：终态写入正常生效（防止加固过度阻塞正常完成）。"""
    from app.core.tasks.runner import _update, delete_job

    db = temp_db()
    job = ExperimentJob(job_type="evaluate", params={}, status="running")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert (
        _update(
            jid, status="done", progress=100.0, result={"ok": 1}, finished_at=utcnow()
        )
        == 1
    )
    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done" and row.result == {"ok": 1}

    # done 任务 delete → 直接删行
    assert delete_job(jid) == {"ok": True, "deleted": True}
    db = temp_db()
    assert db.get(ExperimentJob, jid) is None
    db.close()


def test_cancel_race_loop_abort(temp_db, monkeypatch):
    """可中断循环：取消后循环提前中止，终态保持取消，_cancelled 被清理。

    走 submit 生产入口（worker finally 负责 _cancelled 清理）；_run_alpha101_score
    内部是函数级 import，故打桩需命中源模块。
    """
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import delete_job, submit

    calls = []
    N = 30

    def fake_list_alpha101():
        return [{"id": i, "name": f"f{i}", "formula": f"close{i}"} for i in range(N)]

    def fake_compile_rpn(f):
        return f

    def fake_load_panel(ds_id, features=None):
        import numpy as np

        return {"panel": {"close": np.zeros((5, 40))}, "dates": ["2026-01-01"] * 40}

    def fake_forward_returns(close, horizon=5):
        return close

    def fake_evaluate_factor(rpn, panel, fwd, horizon=5):
        calls.append(rpn)
        time.sleep(0.02)
        return {"ic": 0.1, "stability": 0.5}

    from app.lib.alpha import alpha101 as A101
    from app.core import datasets as AD
    from app.lib.alpha import evaluate as AE
    from app.lib.alpha import operators as AO

    monkeypatch.setattr(A101, "list_alpha101", fake_list_alpha101)
    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(AE, "evaluate_factor", fake_evaluate_factor)

    jid = submit("alpha101_score", {"dataset_id": 1, "limit": N})

    # 等任务进入 running（循环已开始）再取消
    deadline = time.time() + 5
    while time.time() < deadline:
        db = temp_db()
        row = db.get(ExperimentJob, jid)
        db.close()
        if row is not None and row.status == "running":
            break
        time.sleep(0.01)
    assert row.status == "running"

    assert delete_job(jid) == {"ok": True, "deleted": False}

    # 等 worker finally 清理取消登记（即计算线程已完全退出）
    deadline = time.time() + 10
    while time.time() < deadline:
        with Q._running_lock:
            gone = jid not in Q._cancelled
        if gone:
            break
        time.sleep(0.01)
    assert gone

    # 循环被取消检查点中止：未跑完全部因子
    assert len(calls) < N

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled"
    assert row.error == "已被用户取消"
    assert row.result == {}  # 未写入 done 结果
    assert row.progress < 100.0


def test_cancel_pending_worker_exits_immediately(temp_db, monkeypatch):
    """取消发生在 worker 真正计算前：_run_* 秒退，不触碰底层计算。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import _run_gp, delete_job

    # 若取消检查失效导致继续执行，立即失败暴露
    monkeypatch.setattr(
        Q,
        "load_panel",
        lambda *a: (_ for _ in ()).throw(AssertionError("不应执行计算")),
    )
    monkeypatch.setattr(
        Q,
        "evolve",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应执行进化")),
    )

    db = temp_db()
    job = ExperimentJob(job_type="gp_run", params={}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert delete_job(jid) == {"ok": True, "deleted": False}

    t = threading.Thread(
        target=lambda: _run_gp(jid, {"backend": "cpu", "dataset_id": 1})
    )
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled" and row.error == "已被用户取消"
    assert row.result == {}


# ---------------------------------------------------------------------------
# 路由注册：/experiments/page 必须先于 /{job_id}，否则被捕获为 job_id
# ---------------------------------------------------------------------------


def test_experiments_page_route_ordering():
    from app.api.experiments import router as er

    paths = [r.path for r in er.routes]
    assert paths.index("/experiments/page") < paths.index("/experiments/{job_id}")


def test_signals_events_page_route_registered():
    from app.api.signals import router as sr

    assert "/signals/events/page" in [r.path for r in sr.routes]
