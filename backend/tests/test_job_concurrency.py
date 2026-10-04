"""任务并发闸门（Semaphore）测试：worker 入口 acquire 阻塞期间任务保持 pending。

临时 SQLite，不触碰生产库；假执行器验证排队语义：并发 2 时第 3 个任务
保持 pending，直到有任务结束释放闸门。
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, utcnow


def _wait_status(maker, jid: int, status: str, timeout: float = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = maker()
        row = db.get(ExperimentJob, jid)
        db.close()
        if row is not None and row.status == status:
            return row
        time.sleep(0.01)
    raise AssertionError(f"等待任务 {jid} 状态 {status} 超时")


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（计算线程/删除走同一临时库）。"""
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
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


def test_semaphore_gate_holds_third_job_pending(temp_db, monkeypatch):
    """闸门生效：并发 2 时，第 3 个任务保持 pending，直到前序任务结束才启动。

    注：market_scan/dataset_build 为 IO 密集任务已退出全局闸门（阶段二），
    此处用仍走闸门的计算任务 gp_run 验证闸门语义。
    """
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    entered: list[int] = []
    release = threading.Event()

    def fake_gp(job_id, params):
        # 假执行器：进入（已拿闸门）→ 启动 running → 阻塞 → 完成
        entered.append(job_id)
        if not Q._start_running(job_id, 1.0):
            return
        release.wait(timeout=15)
        Q._terminal(
            job_id,
            status="done",
            progress=100.0,
            result={"scan": {}},
            finished_at=utcnow(),
        )

    monkeypatch.setattr(Q, "_CONCURRENCY", threading.Semaphore(2))
    monkeypatch.setattr(Q, "_run_gp", fake_gp)

    ids = [submit("gp_run", {"full_universe": False}) for _ in range(3)]

    # 等前 2 个进入执行器（拿到闸门并启动）
    deadline = time.time() + 10
    while time.time() < deadline:
        db = temp_db()
        rows = {r.id: r.status for r in db.query(ExperimentJob).all()}
        db.close()
        if sorted(rows.values()) == ["pending", "running", "running"]:
            break
        time.sleep(0.01)
    assert sorted(rows.values()) == ["pending", "running", "running"]
    pending_ids = [i for i, s in rows.items() if s == "pending"]
    assert len(pending_ids) == 1
    assert pending_ids[0] not in entered  # 排队任务未进入执行器（未拿到闸门）
    assert len(entered) == 2

    # 放行：排队任务随后拿到闸门并完成
    release.set()
    deadline = time.time() + 15
    while time.time() < deadline:
        db = temp_db()
        st = {r.id: r.status for r in db.query(ExperimentJob).all()}
        db.close()
        if len(st) == 3 and all(s == "done" for s in st.values()):
            break
        time.sleep(0.02)
    assert len(entered) == 3
    assert all(s == "done" for s in st.values())


def test_concurrency_zero_config_does_not_block(temp_db, monkeypatch):
    """job_concurrency 配置为 0 时闸门按 1 兜底：任务正常提交并完成，不会永久 pending。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    def fake_scan(job_id, params):
        if not Q._start_running(job_id, 1.0):
            return
        Q._terminal(
            job_id,
            status="done",
            progress=100.0,
            result={"scan": {}},
            finished_at=utcnow(),
        )

    monkeypatch.setattr(Q, "_CONCURRENCY", threading.Semaphore(max(1, 0)))
    monkeypatch.setattr(Q, "_run_market_scan", fake_scan)

    jid = submit("market_scan", {"full_universe": False})
    row = _wait_status(temp_db, jid, "done")
    assert row.status == "done"


def test_semaphore_single_slot_serializes_all(temp_db, monkeypatch):
    """闸门为 1（单并发）时任务严格串行：任意时刻至多 1 个 running。

    注：market_scan/dataset_build 已退出全局闸门（IO 密集，阶段二），
    此处用仍走闸门的计算任务 factor_tune 验证串行语义。
    """
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def fake_tune(job_id, params):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        if not Q._start_running(job_id, 1.0):
            with lock:
                active["n"] -= 1
            return
        time.sleep(0.1)
        Q._terminal(
            job_id,
            status="done",
            progress=100.0,
            result={},
            finished_at=utcnow(),
        )
        with lock:
            active["n"] -= 1

    monkeypatch.setattr(Q, "_CONCURRENCY", threading.Semaphore(1))
    monkeypatch.setattr(Q, "_run_factor_tune", fake_tune)

    ids = [submit("factor_tune", {"name": f"d{i}"}) for i in range(3)]
    deadline = time.time() + 20
    while time.time() < deadline:
        db = temp_db()
        st = {r.id: r.status for r in db.query(ExperimentJob).all()}
        db.close()
        if len(st) == 3 and all(s == "done" for s in st.values()):
            break
        time.sleep(0.02)
    assert all(s == "done" for s in st.values())
    assert active["max"] == 1  # 从未并发


def test_queue_full_rejects_submit_without_row(temp_db, monkeypatch):
    """P2-31 队列上限：_QUEUE_SLOT 耗尽时 submit 抛 QueueFullError 拒绝入队，
    且不创建任务行（千级任务=千级 DB 行的根因之一被切断）。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.errors import QueueFullError
    from app.core.tasks.runner import submit

    monkeypatch.setattr(Q, "_QUEUE_SLOT", threading.Semaphore(1))
    Q._QUEUE_SLOT.acquire()  # 模拟队列已满
    try:
        with pytest.raises(QueueFullError):
            submit("gp_run", {"full_universe": False})
    finally:
        Q._QUEUE_SLOT.release()

    db = temp_db()
    try:
        assert db.query(ExperimentJob).all() == []  # 拒绝入队，零任务行
    finally:
        db.close()


def test_io_gate_serializes_io_tasks(temp_db, monkeypatch):
    """P2-31 IO 闸门：IO 闸门为 1 时，第 2 个 IO 任务（market_scan）保持 pending，
    直到前序任务结束释放闸门（IO 任务不再无限并发绕过限流）。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    entered: list[int] = []
    release = threading.Event()

    def fake_scan(job_id, params):
        entered.append(job_id)
        if not Q._start_running(job_id, 1.0):
            return
        release.wait(timeout=15)
        Q._terminal(
            job_id,
            status="done",
            progress=100.0,
            result={"scan": {}},
            finished_at=utcnow(),
        )

    monkeypatch.setattr(Q, "_IO_CONCURRENCY", threading.Semaphore(1))
    monkeypatch.setattr(Q, "_run_market_scan", fake_scan)

    ids = [submit("market_scan", {"full_universe": False}) for _ in range(2)]

    deadline = time.time() + 10
    while time.time() < deadline:
        db = temp_db()
        st = {r.id: r.status for r in db.query(ExperimentJob).all()}
        db.close()
        if sorted(st.values()) == ["pending", "running"]:
            break
        time.sleep(0.01)
    assert sorted(st.values()) == ["pending", "running"]
    assert len(entered) == 1  # 排队中的 IO 任务未进入执行器

    release.set()
    deadline = time.time() + 15
    while time.time() < deadline:
        db = temp_db()
        st = {r.id: r.status for r in db.query(ExperimentJob).all()}
        db.close()
        if len(st) == 2 and all(s == "done" for s in st.values()):
            break
        time.sleep(0.02)
    assert len(entered) == 2
    assert all(s == "done" for s in st.values())
