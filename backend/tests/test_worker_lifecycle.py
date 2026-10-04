"""T-122 持久任务执行与恢复（P1-65）：worker 模式双轨 + 恢复语义 + DB 桥协作控制。

覆盖：
- 恢复语义：running/pending → pending 重排队（_restart_count 递增），超上限置 failed，
  paused 保持暂停（不再无条件置 failed）
- 认领 CAS：并发认领仅一个成功
- worker 模式 submit 不 spawn 执行线程（SR_TASKS_EMBEDDED=0）
- worker_poll_once 认领并执行 pending 任务至 done（注册临时 handler）
- DB 桥：DB 置 cancelled → _cancel_check 抛 JobCancelled；DB 置 paused → _pause_wait 阻塞
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, utcnow


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 runner.SessionLocal（与 test_job_rerun 同模式）。"""
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
    monkeypatch.setenv("SR_TASKS_EMBEDDED", "0")

    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()
    Q._db_status_cache.clear()

    yield Maker
    engine.dispose()


def _job_status(maker, job_id: int) -> str | None:
    db = maker()
    try:
        row = db.get(ExperimentJob, job_id)
        return row.status if row else None
    finally:
        db.close()


def test_recover_requeues_and_counts(temp_db, monkeypatch):
    """running/pending → pending 重排队且 _restart_count 递增；paused 保持。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    try:
        a = ExperimentJob(job_type="evaluate", params={}, status="running")
        b = ExperimentJob(job_type="evaluate", params={}, status="pending")
        c = ExperimentJob(job_type="evaluate", params={}, status="paused")
        db.add_all([a, b, c])
        db.commit()
        ids = (a.id, b.id, c.id)
    finally:
        db.close()

    assert Q.recover_stale_jobs() == 2
    assert _job_status(temp_db, ids[0]) == "pending"
    assert _job_status(temp_db, ids[1]) == "pending"
    assert _job_status(temp_db, ids[2]) == "paused"  # 暂停语义不丢
    db = temp_db()
    try:
        a = db.get(ExperimentJob, ids[0])
        assert a.params["_restart_count"] == 1
    finally:
        db.close()


def test_recover_abandons_after_max_restart(temp_db, monkeypatch):
    """重启计数超上限（多次重启仍中断=环境问题）→ 置 failed，防无限重跑。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    try:
        j = ExperimentJob(
            job_type="evaluate", params={"_restart_count": Q.MAX_RESTART}, status="running"
        )
        db.add(j)
        db.commit()
        jid = j.id
    finally:
        db.close()

    Q.recover_stale_jobs()
    assert _job_status(temp_db, jid) == "failed"


def test_claim_next_single_winner(temp_db, monkeypatch):
    """并发认领 CAS：两个调用仅一个成功。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    try:
        j = ExperimentJob(job_type="evaluate", params={}, status="pending")
        db.add(j)
        db.commit()
        jid = j.id
    finally:
        db.close()

    first = Q.claim_next()
    second = Q.claim_next()
    assert first is not None and first[0] == jid
    assert second is None  # 已认领为 running，CAS 拒绝
    assert _job_status(temp_db, jid) == "running"


def test_submit_worker_mode_no_embedded_thread(temp_db, monkeypatch):
    """worker 模式 submit 只建行，不 spawn 执行线程（任务由 worker 进程认领）。"""
    import app.core.tasks.runner as Q

    jid = Q.submit("evaluate", {"expression": "close"})
    assert _job_status(temp_db, jid) == "pending"


def test_worker_poll_once_executes_to_done(temp_db, monkeypatch):
    """worker_poll_once 认领并执行 pending 任务至 done。"""
    import app.core.tasks.runner as Q
    from app.core.tasks import registry

    calls: list[int] = []

    def fake_run(job_type, job_id, _params):
        calls.append((job_type, job_id))
        Q._terminal(job_id, status="done", result={"ok": True}, finished_at=utcnow())

    monkeypatch.setattr(registry, "run", fake_run)
    jid = Q.submit("evaluate", {"expression": "close"})
    claimed = Q.worker_poll_once(4)
    assert claimed == 1
    deadline = time.time() + 5
    while time.time() < deadline and _job_status(temp_db, jid) != "done":
        time.sleep(0.05)
    assert _job_status(temp_db, jid) == "done"
    assert calls == [("evaluate", jid)]


def test_cancel_check_db_bridge(temp_db, monkeypatch):
    """DB 桥：Web 进程 delete_job 只写 DB（embedded=0 无内存集合），控制点仍中断。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.errors import JobCancelled

    db = temp_db()
    try:
        j = ExperimentJob(job_type="evaluate", params={}, status="running")
        db.add(j)
        db.commit()
        jid = j.id
    finally:
        db.close()

    Q.delete_job(jid)  # 模拟 Web 进程取消（embedded=0 仅写 DB）
    Q._db_status_cache.clear()
    try:
        Q._cancel_check(jid)
        raise AssertionError("应抛 JobCancelled")
    except JobCancelled:
        pass


def test_pause_wait_db_bridge(temp_db, monkeypatch):
    """DB 桥：DB 置 paused → 控制点阻塞；恢复后退出等待。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    try:
        j = ExperimentJob(job_type="evaluate", params={}, status="running")
        db.add(j)
        db.commit()
        jid = j.id
    finally:
        db.close()

    Q.pause_job(jid)  # DB 置 paused（embedded=0 不做内存登记）
    Q._db_status_cache.clear()
    entered = threading.Event()
    exited = threading.Event()

    def waiter():
        entered.set()
        Q._pause_wait(jid)
        exited.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    assert entered.wait(2)
    time.sleep(0.5)  # 让 waiter 进入 DB 轮询等待
    assert not exited.is_set()  # 仍被暂停阻塞
    Q.resume_job(jid)  # DB 置回 running
    Q._db_status_cache.clear()
    assert exited.wait(3)
    t.join(timeout=1)