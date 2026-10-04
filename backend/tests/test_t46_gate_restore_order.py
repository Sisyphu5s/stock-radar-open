"""T-46 回归测试:并发闸门 release 与后端条件恢复的顺序 + 快照锁。

背景(问题已确认,backend/app/core/tasks/runner.py submit 的 worker):
- 原 finally 中 _CONCURRENCY.release() 先于「后端条件恢复」执行 → 排队任务
  acquire 通过后立即读 settings.gp_backend,读到的是上一任务设置后的中间值,
  恢复随后才发生 → 排队任务拿到错误后端配置,恢复被覆盖/错位(问题 1);
- end_backend 快照读取未纳入 _backend_lock(该锁保护 gp_backend 修改 +
  init_backend() 原子段)→ 可能读到切换中间值(问题 2)。

修复语义(本测试锁定):
- 后端条件恢复整体先于 _CONCURRENCY.release();
- end_backend 快照与恢复同处 _backend_lock 内(锁内快照+恢复无缝衔接,
  等价于原「当前值仍等于结束时值才恢复」的重查语义);
- saved_backend 读取亦在 _backend_lock 内(拿到已提交值而非并发切换中间值)。

测试策略(并发时序难以稳定复现,采用「记录型替身 + 事件驱动确定性编排」):
把 _CONCURRENCY / _backend_lock / settings 换成记录器,用闸门=1 严格串行化 +
threading.Event 编排,直接断言事件序与排队任务读到的值——修复前这些断言必失败:
- 单任务:断言「后端恢复写」先于「gate_release」,且恢复段事件序为
  [lock_acq, read(end), write(restore), lock_rel];
- 双任务(闸门=1,确定性行为复现):A 切后端为 mlx 后阻塞,B 排队;
  放行 A 后断言 B 拿到闸门时读到的后端为恢复后的 numpy(修复前读到 mlx)。
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, utcnow


class OrderRecorder:
    """线程安全事件序记录器:log() 追加 (event, value) 元组。"""

    def __init__(self):
        self.events: list[tuple[str, object]] = []
        self._lock = threading.Lock()

    def log(self, event_name: str, value: object) -> None:
        with self._lock:
            self.events.append((event_name, value))

    def first_idx(self, event_name: str, value: object = None) -> int:
        with self._lock:
            for i, (e, v) in enumerate(self.events):
                if e == event_name and (value is None or v == value):
                    return i
        return -1

    def count(self, event_name: str) -> int:
        with self._lock:
            return sum(1 for e, _ in self.events if e == event_name)


class RecordingSettings:
    """替换 Q.settings:gp_backend 的读写全部入事件序。"""

    def __init__(self, initial: str, order: OrderRecorder):
        self._backend = initial
        self._order = order
        self.job_concurrency = 2  # 模块导入期已建 _CONCURRENCY,此处仅为占位

    @property
    def gp_backend(self) -> str:
        self._order.log("backend_read", self._backend)
        return self._backend

    @gp_backend.setter
    def gp_backend(self, value: str) -> None:
        self._backend = value
        self._order.log("backend_write", value)


class RecordingGate:
    """替换 Q._CONCURRENCY:acquire/release 入事件序,语义同 Semaphore。"""

    def __init__(self, permits: int, order: OrderRecorder):
        self._sem = threading.Semaphore(permits)
        self._order = order

    def acquire(self) -> None:
        self._sem.acquire()
        self._order.log("gate_acquire", None)

    def release(self) -> None:
        self._order.log("gate_release", None)
        self._sem.release()


class RecordingLock:
    """替换 Q._backend_lock:上下文管理器,加锁/解锁入事件序。"""

    def __init__(self, order: OrderRecorder):
        self._lock = threading.Lock()
        self._order = order

    def __enter__(self) -> "RecordingLock":
        self._lock.acquire()
        self._order.log("backend_lock_acq", None)
        return self

    def __exit__(self, *exc) -> bool:
        self._order.log("backend_lock_rel", None)
        self._lock.release()
        return False


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB,替换 queue.SessionLocal(worker 与查询走同一临时库)。"""
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


def _install_recorders(monkeypatch, permits: int = 1):
    """安装记录型替身(闸门/后端锁/settings),返回 (order, gate, settings)。"""
    import app.core.tasks.runner as Q

    order = OrderRecorder()
    gate = RecordingGate(permits, order)
    settings = RecordingSettings("numpy", order)
    monkeypatch.setattr(Q, "_CONCURRENCY", gate)
    monkeypatch.setattr(Q, "_backend_lock", RecordingLock(order))
    monkeypatch.setattr(Q, "settings", settings)
    return order, gate, settings


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


def _wait_events(order, event_name: str, count: int = 1, timeout: float = 10):
    """等待事件序中 event_name 出现 count 次(worker 线程异步落日志后主线程才断言)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if order.count(event_name) >= count:
            return
        time.sleep(0.01)
    raise AssertionError(f"等待事件 {event_name}×{count} 超时")


def test_restore_before_release_and_end_snapshot_under_lock(temp_db, monkeypatch):
    """单任务事件序断言(修复前必失败):

    1) 后端恢复写先于 gate_release——修复前 release 先于恢复,排队任务读到中间值;
    2) 恢复段事件序为 [lock_acq, read(end), write(restore), lock_rel]——
       end_backend 快照与恢复同在 _backend_lock 内(修复前快照在锁外读取)。
    """
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    order, _, _ = _install_recorders(monkeypatch)

    def fake_gp(job_id, params):
        # 模拟任务切换后端(gp_run 内部 _apply_backend("gpu") → "mlx")
        Q.settings.gp_backend = "mlx"
        if not Q._start_running(job_id, 1.0):
            return
        Q._terminal(
            job_id, status="done", progress=100.0, result={}, finished_at=utcnow()
        )

    monkeypatch.setattr(Q, "_run_gp", fake_gp)

    jid = submit("gp_run", {"full_universe": False})
    _wait_status(temp_db, jid, "done")
    _wait_events(order, "gate_release")

    restore_idx = order.first_idx("backend_write", "numpy")
    gate_rel_idx = order.first_idx("gate_release")
    assert restore_idx != -1, "应存在一次后端恢复写(numpy)"
    assert gate_rel_idx != -1, "应存在 gate_release"
    # 问题 1:恢复先于 release
    assert restore_idx < gate_rel_idx, (
        f"恢复(#{restore_idx})应先于 release(#{gate_rel_idx}),事件序: {order.events}"
    )
    # 问题 2:恢复段 = [lock_acq, read(end), write, lock_rel],快照与恢复同锁
    assert order.events[restore_idx - 2] == ("backend_lock_acq", None)
    assert order.events[restore_idx - 1][0] == "backend_read"
    assert order.events[restore_idx + 1] == ("backend_lock_rel", None)


def test_queued_task_reads_restored_backend_after_gate(temp_db, monkeypatch):
    """行为断言(修复核心,确定性复现):闸门=1 时排队任务 B 在拿到闸门后读到的
    后端必须是 A 恢复后的稳定值 numpy,而非 A 设置后的中间值 mlx。

    编排:A 拿到闸门 → 切后端为 mlx → 阻塞;此时提交 B(B 在闸门处排队)。
    放行 A:A 的 finally 先恢复 numpy 再释放闸门 → B 才 acquire 并读后端。
    修复前:A 先 release 后恢复 → B 在 acquire 后读到 mlx,本断言失败。
    """
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    order, _, _ = _install_recorders(monkeypatch, permits=1)

    a_switched = threading.Event()
    a_release = threading.Event()
    b_saw: dict[str, str] = {}
    # 闸门=1 + 提交顺序保证:A 的 worker 先创建、先拿到闸门,故 fake_gp
    # 首次调用必为 A(worker 线程在 submit 返回前即启动,不能按 job_id 字典分发)
    a_called = {"done": False}

    def fake_gp(job_id, params):
        if not a_called["done"]:
            a_called["done"] = True
            Q.settings.gp_backend = "mlx"  # A 模拟 _apply_backend 切 GPU
            a_switched.set()
            a_release.wait(timeout=15)
        else:
            # B:刚拿到闸门即读后端——修复后应为 A 已恢复的 numpy
            b_saw["backend"] = Q.settings.gp_backend
        if not Q._start_running(job_id, 1.0):
            return
        Q._terminal(
            job_id, status="done", progress=100.0, result={}, finished_at=utcnow()
        )

    monkeypatch.setattr(Q, "_run_gp", fake_gp)

    id_a = submit("gp_run", {"full_universe": False})  # A:先创建,先拿闸门
    id_b = submit("gp_run", {"full_universe": False})  # B:在闸门处排队

    # A 已切换后端并阻塞;B 因闸门=1 仍 pending(未拿到闸门)
    assert a_switched.wait(timeout=10), "A 未在预期时间内切换后端"
    db = temp_db()
    b_row = db.get(ExperimentJob, id_b)
    db.close()
    assert b_row.status == "pending", "B 应在 A 阻塞期间保持 pending(排队于闸门)"

    # 放行 A:A 的 finally 先恢复后端、后释放闸门 → B 才能 acquire 并读到稳定值
    a_release.set()
    _wait_status(temp_db, id_a, "done")
    _wait_status(temp_db, id_b, "done")
    _wait_events(order, "gate_release", count=2)

    assert b_saw["backend"] == "numpy", (
        f"排队任务读到中间值 {b_saw['backend']!r}(应为恢复后的 numpy);"
        f"事件序: {order.events}"
    )
    restore_idx = order.first_idx("backend_write", "numpy")
    gate_rel_idx = order.first_idx("gate_release")
    assert restore_idx != -1 and gate_rel_idx != -1
    assert restore_idx < gate_rel_idx, (
        f"恢复(#{restore_idx})应先于首次 release(#{gate_rel_idx}),"
        f"事件序: {order.events}"
    )
