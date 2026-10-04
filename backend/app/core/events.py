"""调用核心事件总线(内存态,不落库,阶段三再持久化),服务 experiments/paper/copilot 等域。

每个 job 维护一个环形缓冲(最多 MAX_RING 条)与一组订阅队列(SSE 长连接);
publish 追加缓冲并广播给全部订阅者(非阻塞 put),subscribe 先重放缓冲再接收实时事件。
订阅队列分两种:同步 queue.Queue(线程消费)与 AsyncSubQueue(事件循环消费,
SSE async 生成器 await get()——publish 经 loop.call_soon_threadsafe 入队,
长连接不再占用 anyio 线程池线程,见 api/experiments._event_stream)。
终态事件(done/failed/cancelled)发布后缓冲不再更新:延迟 _TERMINAL_TTL 秒后由
GC 线程回收(覆盖 SSE 延迟订阅的重放窗口)——终态事件发布时已广播给全部在订阅
者,ring 对它们的唯一价值是重放,TTL 到即回收、不依赖订阅者断开(P1-29);
全部断开时由 unsubscribe 即时清理(快路径)。

线程安全:全局 threading.Lock 保护两个 dict;发布/订阅/快照均为短临界区;
GC 线程仅做轻量扫描与 pop,与锁无嵌套。
事件格式:{"type": event_type, "job_id": job_id, "ts": time.time(), **data}
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time

logger = logging.getLogger("stockradar.core.events")

MAX_RING = 200  # 每任务环形缓冲上限(条)
# 订阅队列容量上限:有界队列防慢消费者(SSE 断连/半开连接)无限积压事件。
# 取值与 MAX_RING 同量级:单订阅者缓冲至多重放+实时事件,Full 时丢弃并靠
# ring 重放兜底(publish 的 Full 分支),不会死锁——重放 put 在上限内完成。
MAX_SUB_QUEUE = 200

# 终态事件类型(任务生命周期终止,ring 不再追加):终态发布后该 job 的
# 缓冲不再有写入,可清理以堵住长跑进程 _jobs 随任务数线性增长的泄漏。
# cancelled 由 delete_job 取消路径发布(queue.py),纳入终态后 ring 才能被回收。
_TERMINAL_TYPES = frozenset({"done", "failed", "cancelled"})

# 终态 ring 延迟清理:终态发布后保留 _TERMINAL_TTL 秒再回收。
# 不能终态发布时立即删——SSE 生成器(api/experiments._event_stream)时序是
# snapshot → yield replay → subscribe,终态事件可能发布在 subscribe 之前,
# 立即删会丢失新订阅的重放。延迟覆盖该窗口,由 GC 线程兜底清理
# (从未被订阅的终态任务 ring 不因此泄漏)。
_TERMINAL_TTL = 60.0
_GC_INTERVAL = 30.0
_gc_started = False

_lock = threading.Lock()
# job_id -> list[dict](环形缓冲,惰性创建:无事件时不建键)
_jobs: dict[int, list[dict]] = {}
# job_id -> list[queue.Queue](同步订阅队列,惰性创建)或 list[AsyncSubQueue](async 订阅)
_subs: dict[int, list["queue.Queue | AsyncSubQueue"]] = {}


def _collect_terminal_rings() -> None:
    """回收已过 TTL 的终态 ring:终态后 ring 不再更新,保留窗口内供 SSE 重放,
    超时后释放内存。不要求无订阅者(P1-29)——终态事件发布时已广播给全部在订阅
    者,ring 对它们的唯一价值是重放(新订阅),残留订阅队列(半开连接等)不阻碍
    回收;新连接由 api/experiments._event_stream 的 DB 终态检查兜底。"""
    now = time.time()
    with _lock:
        for jid, ring in list(_jobs.items()):
            if not ring:
                continue
            last = ring[-1]
            if last.get("type") in _TERMINAL_TYPES and now - last["ts"] > _TERMINAL_TTL:
                _jobs.pop(jid, None)


def _ensure_gc() -> None:
    """惰性启动 GC 线程(首个终态事件发布时);daemon,不阻塞进程退出。

    双检在 _lock 临界区内完成:publish 终态分支可能被多个任务线程并发进入,
    无锁双检存在两个线程同时看到 _gc_started=False 而各起一个 GC 线程的窗口。
    """
    global _gc_started
    with _lock:
        if _gc_started:
            return
        _gc_started = True

    def loop():
        while True:
            time.sleep(_GC_INTERVAL)
            _collect_terminal_rings()

    threading.Thread(target=loop, daemon=True, name="exp-events-gc").start()


def publish(job_id: int, event_type: str, **data) -> None:
    """追加一条事件到该 job 环形缓冲(超 MAX_RING 弹最旧),并广播给全部订阅队列。"""
    event = {"type": event_type, "job_id": job_id, "ts": time.time(), **data}
    with _lock:
        ring = _jobs.get(job_id)
        if ring is None:
            ring = []
            _jobs[job_id] = ring
        ring.append(event)
        if len(ring) > MAX_RING:
            del ring[: len(ring) - MAX_RING]
        for q in list(_subs.get(job_id, ())):
            try:
                q.put_nowait(event)
            except queue.Full:  # 队列已满(极端):丢弃,靠环形缓冲重放兜底
                logger.debug("job %s 订阅队列已满,丢弃事件 %s", job_id, event_type)
    if event_type in _TERMINAL_TYPES:
        # 终态后 ring 不再更新:不立即删除,保留 TTL 窗口供 SSE 延迟订阅重放;
        # GC 线程按 TTL 回收(P1-29:即使订阅连接仍开也不阻碍回收),已订阅者
        # 全部断开时 unsubscribe 即时清理(快路径)。此处只确保 GC 线程已启动。
        _ensure_gc()


class AsyncSubQueue:
    """线程→事件循环订阅队列桥(SSE async 生成器消费实时事件)。

    publish(任务线程) 经 loop.call_soon_threadsafe 在事件循环线程入队,生成器
    await get() 消费——长连接不再占用 anyio 线程池线程(40 个 SSE 连接不再
    耗尽线程池)。队列满时丢弃(与同步订阅队列 queue.Full 语义一致,靠 ring
    重放兜底);事件循环已关闭(连接断开/进程收尾)时丢弃,不向 publish 抛错。
    replay 用于 asubscribe 在事件循环线程内直灌 ring(保证先于实时事件)。
    """

    def __init__(self, maxsize: int = MAX_SUB_QUEUE) -> None:
        self._loop = asyncio.get_running_loop()  # 绑定创建时的事件循环
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    def replay(self, items: list[dict]) -> None:
        """重放 ring(须在事件循环线程调用):直灌,先于实时事件送达。"""
        for event in items:
            try:
                self._q.put_nowait(event)
            except asyncio.QueueFull:  # 重放在上限内完成,超限不再追加
                break

    def put_nowait(self, event: dict) -> None:
        """跨线程入队(任意线程可调,不阻塞):队列满/loop 已关闭 → 丢弃。"""
        try:
            self._loop.call_soon_threadsafe(self._put_from_loop, event)
        except RuntimeError:  # 事件循环已关闭:连接已断,丢弃即可
            pass

    def _put_from_loop(self, event: dict) -> None:
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("async 订阅队列已满,丢弃事件 %s", event.get("type"))

    async def get(self) -> dict:
        return await self._q.get()

    def qsize(self) -> int:
        return self._q.qsize()


def subscribe(job_id: int) -> "queue.Queue":
    """创建订阅队列:先复制该 job 当前环形缓冲全部事件入队(重放),再接收实时事件。"""
    q: "queue.Queue" = queue.Queue(maxsize=MAX_SUB_QUEUE)
    with _lock:
        ring = _jobs.get(job_id)
        if ring:
            for event in ring:
                q.put(event)
        _subs.setdefault(job_id, []).append(q)
    return q


def asubscribe(job_id: int) -> AsyncSubQueue:
    """创建 async 订阅队列(SSE async 生成器用):重放 ring 后接收实时事件。

    须在事件循环线程内调用(asyncio.get_running_loop 绑定循环);unsubscribe
    同样适用(队列实例直接 remove)。
    """
    q = AsyncSubQueue()
    with _lock:
        ring = _jobs.get(job_id)
        if ring:
            q.replay(ring)
        _subs.setdefault(job_id, []).append(q)
    return q


def unsubscribe(job_id: int, q: "queue.Queue") -> None:
    """移除订阅队列;该 job 无剩余订阅时删除键(惰性清理)。"""
    with _lock:
        subs = _subs.get(job_id)
        if subs:
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                _subs.pop(job_id, None)
                # 无剩余订阅者:任务已终态(ring 末条为终态事件)时一并清理
                # 环形缓冲(快路径,不等 TTL;慢路径由 GC 线程按 _TERMINAL_TTL
                # 兜底回收,见 _collect_terminal_rings);任务仍在 running 时不
                # 清理(后续 subscribe 可重放断线前历史)。
                ring = _jobs.get(job_id)
                if ring and ring[-1].get("type") in _TERMINAL_TYPES:
                    _jobs.pop(job_id, None)


def snapshot(job_id: int) -> list[dict]:
    """返回该 job 当前环形缓冲的副本(供 SSE 首连重放)。"""
    with _lock:
        ring = _jobs.get(job_id)
        return list(ring) if ring else []
