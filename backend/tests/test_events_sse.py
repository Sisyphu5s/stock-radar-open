"""实验事件总线（环形缓冲）+ SSE 端点 + GPU 互斥锁 + IO 闸门跳过 测试。

SSE 集成用独立 mini app（只挂 experiments router），临时 SQLite，不触碰生产库、
不触发 main.py lifespan；实时推送用线程消费流（TestClient stream 是阻塞读）。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
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
    """独立临时 DB，替换 queue.SessionLocal（API get_job 与 worker 同库）。"""
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


# ---------------------------------------------------------------------------
# 事件总线单元测试
# ---------------------------------------------------------------------------


def test_publish_subscribe_realtime(events_clean):
    """先订阅后发布：实时事件到达（订阅不重放连接后的事件）。"""
    q = events.subscribe(1)
    events.publish(1, "phase", phase="数据准备")
    events.publish(1, "progress", progress=8.0)
    first = q.get(timeout=2)
    e = q.get(timeout=2)
    assert first["type"] == "phase"
    assert e["type"] == "progress"
    assert e["job_id"] == 1
    assert e["progress"] == 8.0
    assert e["ts"] > 0


def test_subscribe_replays_ring(events_clean):
    """先发 5 条再订阅：新订阅应收到全部 5 条重放。"""
    for i in range(5):
        events.publish(10, "progress", progress=float(i))
    q = events.subscribe(10)
    got = [q.get(timeout=2) for _ in range(5)]
    assert [e["progress"] for e in got] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert q.empty()  # 重放后无多余事件


def test_ring_trims_to_max(events_clean):
    """环形裁剪：发 250 条，新订阅只收到最近 MAX_RING 条。"""
    for i in range(250):
        events.publish(20, "progress", progress=float(i))
    q = events.subscribe(20)
    got = [q.get(timeout=2) for _ in range(events.MAX_RING)]
    assert len(got) == events.MAX_RING
    assert got[0]["progress"] == float(250 - events.MAX_RING)  # 最旧被弹出
    assert got[-1]["progress"] == 249.0
    assert q.empty()


def test_unsubscribe_stops_delivery(events_clean):
    """unsubscribe 后不再收到实时事件（重放内容先排空）。"""
    events.publish(30, "progress", progress=1.0)
    q = events.subscribe(30)
    assert q.get(timeout=2)["progress"] == 1.0  # 排空重放
    events.unsubscribe(30, q)
    events.publish(30, "progress", progress=2.0)
    assert q.empty()


def test_asubscribe_replays_ring(events_clean):
    """async 订阅:重放 ring(先于实时事件),unsubscribe 复合同步清理。"""
    for i in range(3):
        events.publish(88, "progress", progress=float(i))

    async def drive():
        q = events.asubscribe(88)
        got = [await asyncio.wait_for(q.get(), timeout=1) for _ in range(3)]
        assert q.qsize() == 0  # 重放后无多余事件
        events.unsubscribe(88, q)
        return [e["progress"] for e in got]

    assert asyncio.run(drive()) == [0.0, 1.0, 2.0]


def test_asubscribe_realtime_across_thread(events_clean):
    """async 订阅跨线程:publish(任务线程)→ 事件循环 await 收到(C10 异步桥)。

    验证 AsyncSubQueue 线程→事件循环桥:实时事件不依赖生成器占线程,
    长连接不再消耗 anyio 线程池线程。
    """

    async def drive():
        q = events.asubscribe(99)
        threading.Thread(
            target=lambda: events.publish(99, "progress", progress=1.0),
            daemon=True,
        ).start()
        e = await asyncio.wait_for(q.get(), timeout=2)
        await asyncio.sleep(0)  # 排空后续回调,避免 loop 关闭后残留
        events.unsubscribe(99, q)
        return e

    e = asyncio.run(drive())
    assert e["type"] == "progress"
    assert e["progress"] == 1.0


# ---------------------------------------------------------------------------
# SSE 端点集成测试
#
# 404 用 TestClient（立即响应）；流内容直接测 _event_stream 生成器——
# 新版 starlette TestClient 的 portal.call 会阻塞到 ASGI 协程结束，SSE
# 长连接永不结束导致流式读取拿不到任何数据，无法经 HTTP 层验证。
# ---------------------------------------------------------------------------


def _client():
    from app.api.experiments import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _make_job(maker) -> int:
    db = maker()
    job = ExperimentJob(job_type="gp_run", params={})
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


def test_sse_404_unknown_job(events_clean, temp_db):
    client = _client()
    resp = client.get("/api/v1/experiments/999999/events")
    assert resp.status_code == 404


def test_event_stream_replay_and_realtime(events_clean, temp_db, monkeypatch):
    """生成器:首连重放已发布事件;连接后再发布能实时收到(事件格式与旧版一致)。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 0.2)
    jid = _make_job(temp_db)

    # 连接前已发布的事件（首连重放）
    events.publish(jid, "phase", phase="数据准备")

    async def drive():
        gen = P._event_stream(jid)
        first = await anext(gen)
        assert "event: replay" in first
        assert "数据准备" in first  # 重放含连接前已发布事件

        # 订阅后队列重放（replay 快照之外，subscribe 时入队的既有事件逐条送达）
        second = await anext(gen)
        assert second.startswith("event: phase")

        # 实时推送（publish 在事件循环线程,经异步桥入队）
        events.publish(jid, "progress", progress=50.0)
        await asyncio.sleep(0)  # 让 call_soon_threadsafe 回调执行入队
        third = await anext(gen)
        assert third.startswith("event: progress")
        assert "50.0" in third
        await gen.aclose()

    asyncio.run(drive())


def test_event_stream_terminal_then_close(events_clean, temp_db, monkeypatch):
    """终态收尾:收到 done 事件后队列空,再一条心跳即关闭连接(不无限挂长连接)。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 0.2)
    jid = _make_job(temp_db)

    async def drive():
        gen = P._event_stream(jid)
        await anext(gen)  # replay
        events.publish(jid, "done", status="done", progress=100.0)
        await asyncio.sleep(0)
        done = await anext(gen)
        assert "event: done" in done
        heartbeat = await anext(gen)  # 队列空 + 已收终态事件 → 最后一条心跳
        assert heartbeat == ": heartbeat\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        await gen.aclose()

    asyncio.run(drive())


def test_event_stream_replay_terminal_closes_immediately(
    events_clean, temp_db, monkeypatch
):
    """首连重放已含终态事件（终态发布在订阅前）→ 置 terminal_done 立即收尾，
    不空等一个 _SSE_GET_TIMEOUT 周期（P1-28 修复：修复前连接拖满超时）。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 5)  # 故意放大：收尾不得依赖该超时
    jid = _make_job(temp_db)
    events.publish(jid, "done", status="done", progress=100.0)

    async def drive():
        t0 = time.time()
        gen = P._event_stream(jid)
        first = await anext(gen)
        assert "event: replay" in first and '"done"' in first
        heartbeat = await anext(gen)  # 重放已含终态 → 直接心跳收尾
        assert heartbeat == ": heartbeat\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        await gen.aclose()
        return time.time() - t0

    assert asyncio.run(drive()) < 2, "终态重放必须立即收尾，不得拖一个 5s 超时周期"


def test_event_stream_replay_db_terminal_closes_immediately(
    events_clean, temp_db, monkeypatch
):
    """ring 已回收（终态发布超 TTL）但 DB 已终态 → 同样立即收尾（走轻量状态查询）。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 5)
    jid = _make_job(temp_db)
    db = temp_db()
    try:
        job = db.get(ExperimentJob, jid)
        job.status = "failed"
        job.error = "timeout"
        db.commit()
    finally:
        db.close()

    async def drive():
        t0 = time.time()
        gen = P._event_stream(jid)
        await anext(gen)  # replay（ring 为空）
        heartbeat = await anext(gen)
        assert heartbeat == ": heartbeat\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        await gen.aclose()
        return time.time() - t0

    assert asyncio.run(drive()) < 2


def test_event_stream_heartbeat_uses_light_status(events_clean, temp_db, monkeypatch):
    """心跳分支只查轻量状态（P1-28）：不得调用全量 get_job 反序列化大 result。"""
    import app.api.experiments as P
    import app.core.tasks.runner as Q

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 0.2)
    jid = _make_job(temp_db)
    db = temp_db()
    try:
        job = db.get(ExperimentJob, jid)
        job.status = "running"  # 模拟进行中：无事件 → 恒心跳
        db.commit()
    finally:
        db.close()

    calls = {"full": 0}
    orig = Q.get_job

    def spy_full(job_id):
        calls["full"] += 1
        return orig(job_id)

    monkeypatch.setattr(Q, "get_job", spy_full)

    async def drive():
        gen = P._event_stream(jid)
        await anext(gen)
        hb = await anext(gen)
        assert hb == ": heartbeat\n\n"
        await gen.aclose()

    asyncio.run(drive())
    assert calls["full"] == 0, "心跳不得触碰全量 get_job（大 result 反序列化）"


def test_event_stream_heartbeat_keeps_alive(events_clean, temp_db, monkeypatch):
    """未终态时队列空 → 纯心跳保活，连接持续。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 0.2)
    jid = _make_job(temp_db)  # pending，无事件

    async def drive():
        gen = P._event_stream(jid)
        await anext(gen)  # replay（快照为空）
        hb = await anext(gen)
        assert hb == ": heartbeat\n\n"
        hb2 = await anext(gen)
        assert hb2 == ": heartbeat\n\n"
        await gen.aclose()

    asyncio.run(drive())


def test_event_stream_close_unsubscribes(events_clean, temp_db, monkeypatch):
    """客户端断开（生成器 close）→ finally 清理订阅，不残留泄漏。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 0.2)
    jid = _make_job(temp_db)
    events.publish(jid, "progress", progress=1.0)

    async def drive():
        gen = P._event_stream(jid)
        await anext(gen)  # replay（生成器暂停在首个 yield，subscribe 尚未执行）
        second = await anext(gen)  # 触发 subscribe（队列重放既有事件送达）
        assert "event: progress" in second
        assert len(events._subs.get(jid, [])) == 1
        await gen.aclose()  # 模拟客户端断开
        assert events._subs.get(jid) is None

    asyncio.run(drive())


def test_event_stream_cancelled_then_close(events_clean, temp_db, monkeypatch):
    """P1-30：收到 cancelled 终态事件后立即收尾（一条心跳即关闭），不得空等
    一个 _SSE_GET_TIMEOUT 周期（修复前事件侧终态判定缺 cancelled）。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 5)  # 收尾不得依赖超时
    jid = _make_job(temp_db)

    async def drive():
        gen = P._event_stream(jid)
        await anext(gen)  # replay
        events.publish(jid, "cancelled", status="cancelled")
        await asyncio.sleep(0)
        t0 = time.time()
        cancelled = await anext(gen)
        assert "event: cancelled" in cancelled
        heartbeat = await anext(gen)  # 已收终态 → 最后一条心跳
        assert heartbeat == ": heartbeat\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        await gen.aclose()
        return time.time() - t0

    assert asyncio.run(drive()) < 2, "cancelled 终态必须立即收尾，不得空等超时周期"


def test_event_stream_replay_cancelled_closes_immediately(
    events_clean, temp_db, monkeypatch
):
    """P1-30：终态（cancelled）发布后新连客户端重放快照即拿到终态并立即收尾，
    不留 15s 空窗。"""
    import app.api.experiments as P

    monkeypatch.setattr(P, "_SSE_GET_TIMEOUT", 5)
    jid = _make_job(temp_db)
    events.publish(jid, "cancelled", status="cancelled")

    async def drive():
        t0 = time.time()
        gen = P._event_stream(jid)
        first = await anext(gen)
        assert "event: replay" in first and '"cancelled"' in first
        heartbeat = await anext(gen)  # 重放已含终态 → 直接心跳收尾
        assert heartbeat == ": heartbeat\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        await gen.aclose()
        return time.time() - t0

    assert asyncio.run(drive()) < 2, "终态重放必须立即收尾，不得拖一个 5s 超时周期"


def test_terminal_ring_gc_with_subscriber(events_clean, monkeypatch):
    """P1-29：终态发布后即使订阅者仍存在（连接未断开、_subs 残留条目），
    TTL 到即由 GC 回收——不再因订阅者存在而永不释放。"""
    events.publish(50, "progress", progress=1.0)
    q = events.subscribe(50)  # 订阅者在线
    events.publish(50, "done", status="done", progress=100.0)
    assert 50 in events._jobs  # TTL 窗口内保留供重放

    # 时间前推超过 TTL 后跑 GC 扫描（模拟 GC 线程下次唤醒）
    future = events._jobs[50][-1]["ts"] + events._TERMINAL_TTL + 1

    class _FakeTime:
        def time(self):
            return future

    monkeypatch.setattr(events, "time", _FakeTime())
    events._collect_terminal_rings()
    assert 50 not in events._jobs  # 订阅者仍在，ring 依然被回收

    events.unsubscribe(50, q)
    events._jobs.pop(50, None)  # 兜底清残留，防止影响其他测试


# ---------------------------------------------------------------------------
# GPU 互斥锁
# ---------------------------------------------------------------------------


def test_gpu_lock_blocks_until_release(events_clean):
    """GPU 锁被持有（主线程）时，新线程 acquire 阻塞，release 后 2 秒内获得。"""
    import app.core.tasks.runner as Q

    lock = Q._GPU_LOCK
    got = threading.Event()

    def waiter():
        lock.acquire()
        try:
            got.set()
        finally:
            lock.release()

    lock.acquire()
    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.1)
    assert not got.is_set()  # 锁被主线程持有，waiter 阻塞
    lock.release()
    assert got.wait(timeout=2)  # release 后 waiter 获得
    t.join(timeout=2)


# ---------------------------------------------------------------------------
# 闸门：IO 密集任务跳过计算闸门
# ---------------------------------------------------------------------------


def test_gate_skipped_for_io_jobs_and_holds_gp(events_clean, temp_db, monkeypatch):
    """闸门被占满（Semaphore(1) 主线程持有）时：
    dataset_build 跳过闸门立即运行完成；gp_run 仍走闸门保持 pending。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    gate = threading.Semaphore(1)
    gate.acquire()  # 主线程占满闸门，不释放
    monkeypatch.setattr(Q, "_CONCURRENCY", gate)

    ran: list[str] = []

    def fake_build(job_id, params):
        if not Q._start_running(job_id, 1.0):
            return
        ran.append("build")
        Q._terminal(
            job_id,
            status="done",
            progress=100.0,
            result={},
            finished_at=utcnow(),
        )

    def fake_gp(job_id, params):
        if not Q._start_running(job_id, 2.0):
            return
        ran.append("gp")
        Q._terminal(
            job_id,
            status="done",
            progress=100.0,
            result={},
            finished_at=utcnow(),
        )

    monkeypatch.setattr(Q, "_run_dataset_build", fake_build)
    monkeypatch.setattr(Q, "_run_gp", fake_gp)

    bid = submit("dataset_build", {"name": "io"})
    gid = submit("gp_run", {})

    # IO 任务不受闸门阻塞：立即进入运行并完成
    row = _wait_status(temp_db, bid, "done", timeout=10)
    assert row.status == "done"
    assert "build" in ran

    # 重计算任务仍走闸门：闸门被占用 → 保持 pending，未进入执行器
    time.sleep(0.5)
    db = temp_db()
    g = db.get(ExperimentJob, gid)
    db.close()
    assert g.status == "pending"
    assert "gp" not in ran

    # 释放闸门 → gp_run 随后完成
    gate.release()
    row = _wait_status(temp_db, gid, "done", timeout=10)
    assert row.status == "done"
