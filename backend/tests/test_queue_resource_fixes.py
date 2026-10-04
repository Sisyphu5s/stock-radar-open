"""资源/一致性缺陷回归测试：事件环缓冲终态清理 + GPU 锁等待可中断。

- events: 终态事件（done/failed）发布后清理 _jobs 环形缓冲（无订阅者时立即、
  有订阅者时最后一个断开后清理），堵住长跑进程 _jobs 线性增长泄漏；
- queue: GPU 锁等待循环每轮过协作控制点，排队中的 neural 任务可被取消
  （修复前 _GPU_LOCK.acquire() 无限阻塞、不可中断）。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, utcnow
from app.core import events


@pytest.fixture()
def events_clean():
    """清理 events 模块级状态（跨测试隔离）。"""
    with events._lock:
        events._jobs.clear()
        events._subs.clear()
    yield
    with events._lock:
        events._jobs.clear()
        events._subs.clear()


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（worker 与查询走同一临时库）。"""
    import app.core.tasks.runner as Q

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

    monkeypatch.setattr(Q, "SessionLocal", Maker)

    # 清理跨测试残留的模块级状态
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


# ---------------------------------------------------------------------------
# 事件环缓冲：终态后清理（修复 _jobs 永不删除的内存泄漏）
# ---------------------------------------------------------------------------


def test_ring_cleaned_after_terminal_event(events_clean):
    """终态事件后 _jobs 清理：无订阅者经延迟回收（TTL 后 GC 兜底）；有订阅者时
    TTL 窗口内保留重放、TTL 过后即使连接仍开也回收（P1-29，不再依赖订阅者断开）；
    全部断开走 unsubscribe 即时清理（快路径）；运行中任务订阅者断开不删。"""
    # 非终态：ring 保留
    events.publish(1, "progress", progress=10.0)
    assert 1 in events._jobs

    # 无订阅者：终态后 ring 仍保留（SSE 延迟订阅重放窗口），过 TTL 后回收
    events.publish(1, "done", status="done", progress=100.0)
    assert 1 in events._jobs
    events._collect_terminal_rings()
    assert 1 in events._jobs  # 未过 TTL，不回收

    events._TERMINAL_TTL = -1.0  # 模拟 TTL 已过
    events._collect_terminal_rings()
    assert 1 not in events._jobs

    # 有订阅者时：终态 ring 在 TTL 窗口内保留（新订阅重放），TTL 过后即使
    # 订阅连接仍开也被 GC 回收——终态事件发布时已广播给在订阅者，ring 对
    # 它们只有重放价值，不阻碍回收（P1-29）
    events.publish(2, "progress", progress=10.0)
    q = events.subscribe(2)
    events.publish(2, "done", status="done", progress=100.0)
    assert 2 in events._jobs  # TTL 窗口内：订阅者仍连接，ring 保留供重放
    assert q.get(timeout=2)["type"] == "progress"
    assert q.get(timeout=2)["type"] == "done"
    events._TERMINAL_TTL = -1.0  # TTL 已过：即使订阅者仍在，ring 也被回收
    events._collect_terminal_rings()
    assert 2 not in events._jobs
    events.unsubscribe(2, q)  # 断开订阅快路径：_subs 清理，无残留
    assert events._subs.get(2) is None

    # failed 同样清理（无订阅者，TTL 已过）
    events.publish(3, "failed", status="failed", error="boom")
    events._TERMINAL_TTL = -1.0
    events._collect_terminal_rings()
    assert 3 not in events._jobs

    # 运行中任务订阅者全部断开 → ring 保留（可重放断线前历史）
    events.publish(4, "progress", progress=1.0)
    q4 = events.subscribe(4)
    events.unsubscribe(4, q4)
    assert 4 in events._jobs
    # 断开后终态发布 → 无订阅者，TTL 过即回收
    events.publish(4, "failed", status="failed", error="x")
    events._TERMINAL_TTL = -1.0
    events._collect_terminal_rings()
    assert 4 not in events._jobs


# ---------------------------------------------------------------------------
# GPU 锁：等待可中断（修复 acquire 无限阻塞）
# ---------------------------------------------------------------------------


def _install_neural_fakes(monkeypatch):
    """打桩 load_panel / forward_returns / train_mlp（不真实训练）。"""
    import app.core.tasks.runner as Q

    rng = np.random.RandomState(0)
    close = (rng.rand(20, 60) + 10.0).astype("float32")
    vol = (rng.rand(20, 60) + 1.0).astype("float32")

    called = {"train_mlp": False}

    def fake_load_panel(ds_id, features=None):
        return {
            "panel": {"close": close, "volume": vol},
            "dates": [str(i) for i in range(60)],
        }

    def fake_forward_returns(c, horizon=5):
        return c

    def fake_train_mlp(**kw):
        called["train_mlp"] = True
        raise AssertionError("锁未被释放时 train_mlp 不应被调用")

    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(Q, "train_mlp", fake_train_mlp)
    return called


def _spawn_job(maker, params=None) -> int:
    db = maker()
    job = ExperimentJob(job_type="neural_train", params=params or {}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


def test_gpu_lock_wait_interruptible_by_cancel(temp_db, monkeypatch):
    """GPU 锁被占用时，排队等待的 neural 任务可被取消并退出
    （修复前 _GPU_LOCK.acquire() 无限阻塞，取消无效）。"""
    import app.core.tasks.runner as Q

    called = _install_neural_fakes(monkeypatch)
    params = {"dataset_id": 1}
    jid = _spawn_job(temp_db, params)

    Q._GPU_LOCK.acquire()  # 主线程占用 GPU 锁
    exited = threading.Event()
    try:

        def run():
            try:
                Q._run_neural_train(jid, params)
            finally:
                exited.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.3)
        assert not exited.is_set()  # 锁被占用，worker 仍在排队等待

        # 模拟 delete_job 取消：DB CAS 写 cancelled + 登记取消 + 唤醒
        db = temp_db()
        db.execute(
            update(ExperimentJob)
            .where(ExperimentJob.id == jid)
            .values(status="cancelled", error="已被用户取消", finished_at=utcnow())
        )
        db.commit()
        db.close()
        with Q._running_lock:
            Q._running.pop(jid, None)
            Q._cancelled.add(jid)
        Q._unpause(jid)

        # 取消后等待循环在协作控制点抛 JobCancelled → 线程及时退出
        assert exited.wait(timeout=5)
        t.join(timeout=5)
        assert not t.is_alive()

        # 取消状态未被 worker 覆盖；train_mlp 从未被调用（锁一直由主线程持有）
        db = temp_db()
        row = db.get(ExperimentJob, jid)
        db.close()
        assert row.status == "cancelled"
        assert row.error == "已被用户取消"
        assert called["train_mlp"] is False
    finally:
        Q._GPU_LOCK.release()


# ---------------------------------------------------------------------------
# P2-16：alpha101_score 显式传 horizon（不再依赖 _infer_horizon 的 clamp 60）
# ---------------------------------------------------------------------------


def test_alpha101_score_passes_explicit_horizon(temp_db, monkeypatch):
    """alpha101_score 把 params.horizon 显式传给 evaluate_factor。

    P2-16：修复前 evaluate_factor(rpn, panel, fwd) 不传 horizon，内部 _infer_horizon
    推断并 clamp 60，horizon>60 时年化系数失真；修复后显式透传。
    """
    import app.core.tasks.runner as Q

    seen: dict = {}

    def fake_list_alpha101():
        return [{"id": 1, "name": "A1", "formula": "close"}]

    def fake_compile_rpn(f):
        return [{"op": "__feat__", "params": {"name": "close"}}]

    def fake_load_panel(ds_id, features=None):
        return {"panel": {"close": np.zeros((10, 40))}, "dates": ["2026-01-01"] * 40}

    def fake_forward_returns(close, horizon=5):
        return close

    def fake_evaluate_factor(rpn, panel, fwd, horizon=None, **kw):
        seen["horizon"] = horizon
        return {"ic": 0.1, "stability": 0.5}

    from app.lib.alpha import alpha101 as A101
    from app.core import datasets as AD
    from app.lib.alpha import evaluate as AE
    from app.lib.alpha import operators as AO

    monkeypatch.setattr(A101, "list_alpha101", fake_list_alpha101)
    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(AE, "evaluate_factor", fake_evaluate_factor)

    params = {"dataset_id": 1, "horizon": 30}
    jid = _spawn_job(temp_db, params)
    Q._run_alpha101_score(jid, params)

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    assert seen.get("horizon") == 30  # 显式透传，非推断/clamp 值
    assert row.result["results"][0]["score"] is not None


# ---------------------------------------------------------------------------
# P2-51：registry 重复注册告警（防静默覆盖难定位）
# ---------------------------------------------------------------------------


def test_registry_duplicate_register_warns(caplog):
    """同类型处理器重复注册 → warning 告警；覆盖语义保留（最后注册生效）。"""
    import logging

    from app.core.tasks import registry

    name = "__p2_51_dup_test__"

    def h1(job_id, params):
        return None

    def h2(job_id, params):
        return None

    registry.HANDLERS.pop(name, None)
    registry.register(name, h1)
    with caplog.at_level(logging.WARNING, logger="stockradar.core.tasks.registry"):
        registry.register(name, h2)
    assert any("重复注册" in r.message for r in caplog.records)
    assert registry.HANDLERS[name] is h2
    registry.HANDLERS.pop(name, None)
