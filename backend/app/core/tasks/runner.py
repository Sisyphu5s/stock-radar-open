"""实验任务队列执行器(core/tasks/runner):后台线程执行 GP / 评估等任务,状态入库供轮询。

T-104:由原 experiments/queue.py 整体搬迁而来,行为零变化(状态机/并发
闸门/超时监控/暂停取消/终态写入/进度更新逐字保留)。分派由 9 路 if/elif 改为
查 registry(app.core.tasks.registry),9 个 _run_* 处理器作为默认注册项;
异常/错误文案常量在 core/tasks/errors.py。

T-105:9 个 _run_* 处理器(含 neural 的 _persist_nn_model、paper 的
_prepare_kline/_writeback_paper_result)拆至 core/tasks/handlers/ 独立文件,
本模块保留同名模块属性转发绑定(见文末),注册/分派机制不变,测试
monkeypatch 本模块 _run_* 属性语义不变。
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from sqlalchemy import cast, func, or_, update
from sqlalchemy.types import String as _StrType

from ...config import settings
from ...storage.db import SessionLocal
from ...storage.models import (
    ExperimentJob,
    NNModel,
    PaperProject,
    PaperProjectStock,
    utcnow,
)
from ...lib.timex import to_market_naive
from ...lib.alpha.backend import backend_name, init_backend
from ...core.datasets import load_panel
from ...lib.alpha.evaluate import evaluate_factor, forward_returns, split_panel
from ...lib.alpha.gp import evolve
from ...lib.alpha.nn import train_mlp
from ...lib.alpha.operators import compile_rpn
from ...lib.alpha.regressors import REGRESSORS
from ...storage.klines import cached_kline
from ...lib.metrics import max_drawdown, sharpe_ratio, win_rate
from ...core.paper import experiment as run_paper_experiment
from ...core.paper import validate_signals
from ...core.paper import PAPER_MIN_BARS, PAPER_VALID_PERIODS  # noqa: F401（re-export，handlers 引用）
from ..events import publish as _publish_event
from .errors import JobCancelled, QueueFullError, _CANCELLED_ERR, _timeout_error
from . import registry

logger = logging.getLogger("stockradar.experiments")

_jobs_lock = threading.Lock()

# 后端切换锁：settings.gp_backend 修改 + init_backend() 原子化，避免并发任务互相污染
_backend_lock = threading.Lock()

DEFAULT_TIMEOUT_SEC = 900

# 任务并发闸门：同时执行的重计算任务上限（settings.job_concurrency，默认 2）。
# worker 入口 acquire 阻塞期间任务保持 pending 不流转，拿到闸门后正常启动；
# 排队期间不登记超时、不更新状态。测试可 monkeypatch 为小值 Semaphore 验证排队。
# 配置为 0/负值时按 1 兜底：闸门恒 ≥1，杜绝所有任务永久 pending。
_CONCURRENCY = threading.Semaphore(max(1, settings.job_concurrency))

# GPU 任务互斥锁：neural_train 独占 MLX GPU 显存（长时训练，多个并发会显存超限）。
# mlx 短算子（gp_run 等）不互斥，沿用 _apply_backend 的全局后端切换锁语义。
_GPU_LOCK = threading.Lock()

# 模拟盘实验：支持周期与最小 bar 数——单一事实源 core/paper（PAPER_* 见顶部
# import re-export，handlers/paper 仍从本模块引用，避免跨层依赖 API 模块）


def _parallel_workers() -> int:
    """因子/网格并行度：min(os.cpu_count(), 8)；≤1 时纯串行。仅 numpy 后端启用。"""
    return max(1, min(os.cpu_count() or 1, 8))


def _rpn_feature_names(rpn: list[dict]) -> set[str]:
    """RPN 中用到的特征字段名（__feat__ 指令）。

    P2-15：load_panel 按任务实际特征子集加载面板，表达式类任务从 rpn 收集
    特征字段（+close 供 forward_returns）；异常结构返回空集，由后续求值兜底。
    """
    out: set[str] = set()
    for ins in rpn:
        if isinstance(ins, dict) and ins.get("op") == "__feat__":
            name = ins.get("params", {}).get("name")
            if name:
                out.add(name)
    return out


class _ProgressThrottle:
    """进度节流：至少间隔 min_interval 秒或进度至少跳 min_step，减少 _update 落库次数。"""

    def __init__(self, min_interval: float = 0.2, min_step: float = 2.0):
        self._last_t = time.time()
        self._last_p = -1.0
        self._min_interval = min_interval
        self._min_step = min_step

    def should_update(self, progress: float, force: bool = False) -> bool:
        if force:
            return True
        now = time.time()
        if (
            progress - self._last_p >= self._min_step
            or now - self._last_t >= self._min_interval
        ):
            self._last_t = now
            self._last_p = progress
            return True
        return False


# 运行中任务的超时登记: job_id -> (deadline epoch, timeout_sec)。
# 超时由监控线程判定并置 failed；计算线程无法强杀，_update 的条件更新保证最终状态不被覆盖。
_running_lock = threading.Lock()
_running: dict[int, tuple[float, int]] = {}
# 已被用户取消的任务: job_id。delete_job 取消时登记；可中断循环/回调用 _cancel_check 中止。
_cancelled: set[int] = set()
# 协作式暂停: job_id -> {"origin": 暂停前状态(pending/running), "remaining": 冻结剩余秒数,
#                        "timeout": 超时档位}。暂停期间超时登记被冻结（暂停时间不计入超时），
# worker 在各控制点阻塞，resume 后继续；cancel 可唤醒并终止。
_paused: dict[int, dict] = {}
# 每任务的并发控制对象: job_id -> {"cond": Condition(暂停等待/唤醒), "state": Lock(终态/启动与暂停 CAS 互斥)}。
# state 锁保证「worker 终态写入」与「暂停 CAS」互斥，杜绝完成竞态：
# 暂停先得锁 → worker 终态在控制点阻塞至恢复；worker 先得锁 → 暂停回读终态并拒绝。
_control: dict[int, dict] = {}
_control_lock = threading.Lock()
_timeout_monitor_started = False

# IO 密集任务（dataset_build/market_scan/panel_build）独立并发闸门（P2-31）：
# 与 CPU 重计算任务分闸限流，避免长 IO 阻塞重计算任务的同时 IO 并发无上限。
# 0/负值按 1 兜底（同 _CONCURRENCY 语义）。worker 按 job_type 归入对应闸门。
_IO_JOB_TYPES = frozenset({"dataset_build", "market_scan", "panel_build"})
_IO_CONCURRENCY = threading.Semaphore(max(1, settings.job_io_concurrency))

# 任务队列总在飞上限（pending+running 合计，settings.job_queue_max，默认 100）：
# submit 时非阻塞 acquire，拿不到即抛 QueueFullError（拒绝入队，不创建任务行）；
# worker 退出时 release。每任务一个 daemon 线程，此上限同时约束线程数与 DB 行数
# （P2-31：修复千级任务=千级线程+千级 DB 行）。
_QUEUE_SLOT = threading.Semaphore(max(1, settings.job_queue_max))

# 终态状态（get_job 结果列懒加载判定用；与 api/experiments._TERMINAL_STATUSES 同集合）
_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


# ===== T-122 双模式执行（P1-65 持久任务） =====
# 嵌入模式（默认,SR_TASKS_EMBEDDED=1,测试/单机）:submit 即 spawn daemon 线程,
# 任务随 Web 进程(uvicorn --reload)执行,reload 会中断运行中任务;
# worker 模式(SR_TASKS_EMBEDDED=0,部署脚本设置):任务由独立 worker 进程
# (app.core.tasks.worker_main)轮询认领执行——Web reload 不再中断任务。
def _embedded() -> bool:
    return os.environ.get("SR_TASKS_EMBEDDED", "1") == "1"


# DB 状态桥缓存：worker 控制点查询任务 DB 状态(跨进程取消/暂停检测)带 1s 节流,
# 避免高频控制点逐次 SELECT。
_db_status_cache: dict[int, tuple[float, str | None]] = {}


def _db_status(job_id: int) -> str | None:
    now = time.time()
    hit = _db_status_cache.get(job_id)
    if hit is not None and now - hit[0] < 1.0:
        return hit[1]
    db = SessionLocal()
    try:
        row = db.query(ExperimentJob.status).filter(ExperimentJob.id == job_id).first()
        status = row[0] if row else None
    finally:
        db.close()
    _db_status_cache[job_id] = (now, status)
    if len(_db_status_cache) > 512:
        for k in [k for k, (ts, _) in _db_status_cache.items() if now - ts > 30]:
            _db_status_cache.pop(k, None)
    return status


def _ensure_timeout_monitor():
    global _timeout_monitor_started
    if _timeout_monitor_started:
        return
    _timeout_monitor_started = True

    def loop():
        while True:
            time.sleep(10)
            now = time.time()
            with _running_lock:
                # 暂停期间超时登记已冻结（pause_job 弹出 _running），此处再兜底跳过，
                # 避免与暂停并发竞态时误判超时；T-122:worker 模式再查 DB paused(跨进程冻结)
                expired = [
                    (jid, to)
                    for jid, (dl, to) in _running.items()
                    if now >= dl
                    and jid not in _paused
                    and (not _embedded() and _db_status(jid) != "paused" or _embedded())
                ]
                for jid, _ in expired:
                    _running.pop(jid, None)
            for jid, to in expired:
                _update(
                    jid, status="failed", error=_timeout_error(to), finished_at=utcnow()
                )

    threading.Thread(target=loop, daemon=True, name="exp-timeout-monitor").start()


def _track(job_id: int, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> None:
    """登记超时。若恰在暂停冻结窗口内到达（worker 启动后立即被暂停），
    只记录超时档位，由 resume 恢复登记——暂停时间不计入超时。"""
    with _running_lock:
        if job_id in _paused:
            info = _paused.get(job_id)
            if info is not None:
                info["timeout"] = timeout_sec
            return
        _running[job_id] = (time.time() + timeout_sec, timeout_sec)


def _finish(job_id: int) -> bool:
    """任务结束登记（原子弹出）：True 表示未被超时监控接管，可写最终状态。

    登记已被弹出但任务仍处于暂停冻结（暂停与超时监控的并发竞态窗口）时也返回 True：
    终态写入会在控制点阻塞，恢复后才落库，避免暂停中的任务丢失终态。
    """
    with _running_lock:
        popped = _running.pop(job_id, None)
    if popped is not None:
        return True
    with _control_lock:
        return job_id in _paused


def _apply_backend(backend: str) -> str:
    """按 backend 参数切换计算后端并初始化，返回实际后端名（mlx/numpy）。

    切换过程加锁原子化；任务结束后由 worker 在 _backend_lock 内条件恢复
    （仅当当前值仍为本任务设置值时恢复为任务前值，避免覆盖并发任务的切换）。

    P1-56 全链路锁协议（本段为协调中枢）：
    - 本段与 worker 的 saved/restore 段、api/alpha.py 的 _init_backend_locked
      （API 路径 init_backend 前先持 _backend_lock）共用 _backend_lock——
      settings.gp_backend 的「写 + init_backend」与「读 + init_backend」互斥，
      任何调用方拿到的都是已提交值，杜绝读取切换中间态；
    - lib/alpha/backend.init_backend 内部另有 _init_lock 兜底（handlers 直接
      调用 init_backend 时 BACKEND/_T 写入仍原子），锁序恒为
      _backend_lock → _init_lock，无反向获取，不构成死锁。
    """
    with _backend_lock:
        prev = settings.gp_backend
        if backend == "cpu":
            settings.gp_backend = "numpy"
        elif backend == "gpu":
            settings.gp_backend = "mlx"
        try:
            init_backend()
        except Exception:
            settings.gp_backend = prev
            raise
        if backend == "gpu" and backend_name() != "mlx":
            settings.gp_backend = prev
            raise RuntimeError(
                "GPU 计算不可用（mlx 初始化失败），任务已停止，请改用 CPU 后端重试"
            )
        return backend_name()


def _sanitize_json(obj):
    """递归清洗写入 DB 的 JSON：非有限 float → None，numpy 标量 → 原生类型。

    防御 NaN/Inf 渗入 result JSON（详情端点 json 编码抛错 / 前端收到非法值）；
    仅在 _terminal 落库前对 result 调用一次。
    """
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def _update(job_id: int, **fields) -> int:
    """条件更新：仅当任务仍处于 pending/running 时写入，返回影响行数。

    数据库级 CAS：与 delete_job（取消）并发时，取消提交后的任何后台写入
    （进度/终态/finished_at）都不会回写覆盖取消状态，杜绝取消竞态。
    paused 有意不在此列：暂停期间任何后台写入都被拦截，状态只由
    pause_job/resume_job/delete_job 的显式 CAS 迁移。
    """
    db = SessionLocal()
    try:
        if not fields:
            # 空字段更新无效（`UPDATE ... SET` 语法错误）：返回 0 跳过。
            # 防御纯 phase 更新在 phase 列不可写环境（如测试剥离 phase 的旧包装）下不崩。
            return 0
        res = db.execute(
            update(ExperimentJob)
            .where(ExperimentJob.id == job_id)
            .where(ExperimentJob.status.in_(("pending", "running")))
            .values(**fields)
        )
        db.commit()
        if res.rowcount:
            # 统一进度事件发布：条件更新提交成功后才发（取消/终态后不回发）。
            # progress/phase 为 None 时省略该键；不携带 status（由终态事件表达）。
            data = {}
            progress = fields.get("progress")
            phase = fields.get("phase")
            if progress is not None:
                data["progress"] = progress
            if phase is not None:
                data["phase"] = phase
            if data:
                _publish_event(job_id, "progress", **data)
        return res.rowcount or 0
    finally:
        db.close()


def _set_phase(job_id: int, phase: str, progress: float | None = None) -> None:
    """阶段切换辅助：过协作控制点后更新 phase（progress 非 None 时一并写），
    并单独发布 phase 事件（progress 事件已由 _update 统一发，此处不重复）。

    条件更新未命中（任务已被取消/超时接管，_update 返回 0）时不发布，
    杜绝取消/接管后仍广播幽灵 phase 事件。"""
    _checkpoint(job_id)
    fields = {"phase": phase}
    if progress is not None:
        fields["progress"] = progress
    if not _update(job_id, **fields):
        return
    data = {"phase": phase}
    if progress is not None:
        data["progress"] = progress
    _publish_event(job_id, "phase", **data)


def _cancel_check(job_id: int) -> None:
    """中断点：任务已被取消则抛 JobCancelled。仅在可中断循环/进度回调处调用。

    T-122:DB 桥兜底——worker 模式下 Web 进程 delete_job 只写 DB(内存集合不共享),
    控制点查询 DB cancelled 同样中断(1s 缓存节流)。
    """
    with _running_lock:
        if job_id in _cancelled:
            raise JobCancelled(f"job {job_id} 已被用户取消")
    if not _embedded() and _db_status(job_id) == "cancelled":
        raise JobCancelled(f"job {job_id} 已被用户取消")


# ---------------------------------------------------------------------------
# 协作式暂停：控制点 / 状态互斥 / 超时冻结
# ---------------------------------------------------------------------------


def _control_entry(job_id: int) -> dict:
    """取（或创建）任务的并发控制对象 {"cond", "state"}。"""
    with _control_lock:
        entry = _control.get(job_id)
        if entry is None:
            entry = {"cond": threading.Condition(), "state": threading.Lock()}
            _control[job_id] = entry
        return entry


def _state_lock(job_id: int) -> threading.Lock:
    return _control_entry(job_id)["state"]


def _drop_control(job_id: int) -> None:
    """worker 退出后清理控制对象；任务仍暂停时保留（供 resume 唤醒）。"""
    with _control_lock:
        if job_id in _paused:
            return
        _control.pop(job_id, None)


def _pause_wait(job_id: int) -> None:
    """暂停控制点：任务处于暂停则阻塞，直至恢复或取消。

    T-122:DB 桥——worker 模式暂停由 Web 进程写 DB(内存 _paused 不共享),
    控制点轮询 DB paused(0.3s 间隔)直至恢复;嵌入模式内存 _paused 语义不变。
    """
    cond = _control_entry(job_id)["cond"]
    with cond:
        while job_id in _paused:
            cond.wait()
        if not _embedded():
            while _db_status(job_id) == "paused":
                cond.wait(timeout=0.3)
            # 恢复后重建超时登记(暂停时间不计入超时,近似冻结语义)
            if _db_status(job_id) in ("running", "pending"):
                _track(job_id)


def _checkpoint(job_id: int) -> None:
    """协作控制点：暂停时阻塞（恢复后继续）；已取消则抛 JobCancelled。

    放置于各可中断循环 / 进度回调 / 终态写入前；不可中断的单次底层计算
    可继续到下一个控制点，但暂停期间不会写 done/failed 覆盖 paused。
    """
    _pause_wait(job_id)
    _cancel_check(job_id)


def _mark_paused(
    job_id: int, origin: str, remaining: float | None, timeout_sec: int
) -> None:
    with _control_lock:
        entry = _control.get(job_id)
        if entry is None:
            entry = {"cond": threading.Condition(), "state": threading.Lock()}
            _control[job_id] = entry
        cond = entry["cond"]
    with cond:
        _paused[job_id] = {
            "origin": origin,
            "remaining": remaining,
            "timeout": timeout_sec,
        }


def _unpause(job_id: int) -> None:
    """清除暂停登记并唤醒等待中的 worker（resume 与 cancel 共用）。"""
    entry = _control.get(job_id)
    if entry is None:
        return
    cond = entry["cond"]
    with cond:
        _paused.pop(job_id, None)
        cond.notify_all()


def _freeze_timeout(job_id: int) -> tuple[float | None, int]:
    """暂停冻结超时登记：弹出 _running，返回 (剩余秒数, 超时档位)。
    未登记时剩余为 None。暂停时间不计入超时，resume 时按剩余重建 deadline。"""
    with _running_lock:
        entry = _running.pop(job_id, None)
    if entry is None:
        return None, DEFAULT_TIMEOUT_SEC
    deadline, timeout_sec = entry
    return max(0.0, deadline - time.time()), timeout_sec


def _restore_timeout(job_id: int) -> None:
    """恢复超时登记：按暂停时冻结的剩余时间重建 deadline（暂停不计入超时）。

    未冻结到剩余时间（_running 无条目）时按暂停前状态区分，杜绝无限续期：
    - running 来源：登记已消失 = 已被超时监控线程弹出（或从未登记成功），
      说明任务已超过原期限——拒绝重新计时，按极小剩余（1s）恢复，
      由监控线程立即接管置 failed；
    - pending 来源：从未开始计时（worker 尚未启动），从恢复点重新计时合理。
    """
    info = _paused.get(job_id)
    if info is None:
        return
    remaining = info.get("remaining")
    timeout_sec = info.get("timeout") or DEFAULT_TIMEOUT_SEC
    if remaining is None and info.get("origin") == "running":
        remaining = 1.0  # 曾过期任务：不给续期，按极小剩余恢复
    with _running_lock:
        if remaining is None:
            # 未冻结到剩余时间（pending 或刚启动即暂停）→ 从恢复点重新计时
            _running[job_id] = (time.time() + timeout_sec, timeout_sec)
        else:
            _running[job_id] = (time.time() + remaining, timeout_sec)


def _start_running(job_id: int, progress: float) -> bool:
    """协作启动控制段：若任务已暂停（pending 时暂停），先阻塞至恢复，
    再 pending→running。返回 False 表示任务已被取消/终态，worker 应退出。

    state 锁与 pause_job 的暂停 CAS 互斥，保证「pending 暂停」不会与
    worker 的启动转换竞争丢状态。
    """
    with _state_lock(job_id):
        _checkpoint(job_id)
        return _update(job_id, status="running", progress=progress)


# 多次重启仍中断则置 failed 的上限（T-122 恢复重排队防御：防止环境性故障无限重跑）
MAX_RESTART = 3


def _terminal(job_id: int, **fields) -> None:
    """终态写入（协作式）：先结束超时登记；任务暂停则在控制点阻塞至恢复/取消，
    保证 paused 不被 done/failed 覆盖，恢复后才完成落库。

    未被超时登记（从未开始 / 已被取消或超时接管）时以 CAS 兜底直接尝试，
    条件更新保证不覆盖取消/终态。

    落库前递归清洗 result（非有限 float → None），防止 NaN 渗入 JSON；
    DB 写入异常（SQLite 锁竞争等）重试 2 次，仍失败用独立新 session 强制写 failed，
    避免 _terminal 抛错穿透导致 worker 静默死亡、任务永久卡 running。
    JobCancelled 不在此守卫内——向上传播保持取消语义（禁止兜底 failed 覆盖 cancelled）。
    """
    if "result" in fields and fields["result"] is not None:
        fields["result"] = _sanitize_json(fields["result"])
    committed = False  # 终态写入是否命中条件更新（决定是否发布终态事件）
    try:
        if not _finish(job_id):
            committed = bool(_update(job_id, **fields))
            return
        with _state_lock(job_id):
            _checkpoint(job_id)
            committed = bool(_update(job_id, **fields))
    except JobCancelled:
        raise
    except Exception:
        logger.exception("任务 %s 终态写入失败（DB 异常），重试", job_id)
        for attempt in (1, 2):
            try:
                time.sleep(0.05 * attempt)
                committed = bool(_update(job_id, **fields))
                return
            except Exception:
                logger.warning("任务 %s 终态写入第 %s 次重试失败", job_id, attempt)
        try:
            # 兜底：独立新 session 至少写入一次 failed，避免任务永久卡 running
            committed = bool(
                _update(
                    job_id,
                    status="failed",
                    error="终态写入异常，任务已强制置 failed",
                    finished_at=utcnow(),
                )
            )
        except Exception:
            logger.exception(
                "任务 %s 兜底 failed 写入失败，状态可能残留 running", job_id
            )
    finally:
        # 清理暂停冻结后被 resume 恢复的超时登记残留（worker 已完成，不再需要计时）
        with _running_lock:
            _running.pop(job_id, None)
    # 统一终态事件发布（done/failed）：仅当条件更新命中（终态真实落库）时发布；
    # 取消/超时接管时 _update 返回 0 → committed=False，不再广播幽灵终态事件。
    # JobCancelled 向上传播时不会执行到这里，取消终态由 delete_job 写入 DB
    # 且不在此发布（取消事件阶段三再补）。
    if committed and fields.get("status") in ("done", "failed"):
        _term_data = {"status": fields["status"]}
        if fields.get("progress") is not None:
            _term_data["progress"] = fields["progress"]
        if fields.get("error"):
            _term_data["error"] = fields["error"][:2000]
        _publish_event(job_id, fields["status"], **_term_data)


def has_active_paper_job(project_id: int) -> bool:
    """项目级并发防重：是否存在该项目的运行中（pending/running/paused）paper_experiment 任务。

    params 为 JSON，SQLite 无法可靠按内层字段过滤，故拉取全部活动任务后在内存匹配
    （活动任务量小，成本可忽略）。
    """
    db = SessionLocal()
    try:
        jobs = (
            db.query(ExperimentJob)
            .filter(
                ExperimentJob.job_type == "paper_experiment",
                ExperimentJob.status.in_(("pending", "running", "paused")),
            )
            .all()
        )
        return any((j.params or {}).get("project_id") == project_id for j in jobs)
    finally:
        db.close()


def cancel_paper_jobs(project_id: int) -> int:
    """删除项目前置：取消该项目的所有活动（pending/running/paused）paper_experiment 任务。

    逐个走 delete_job（队列取消逻辑：DB 级 CAS 置 cancelled + 登记中断 + 唤醒暂停），
    返回实际取消数；终态任务不动（历史记录保留，由任务中心独立管理）。
    """
    db = SessionLocal()
    try:
        jobs = (
            db.query(ExperimentJob)
            .filter(
                ExperimentJob.job_type == "paper_experiment",
                ExperimentJob.status.in_(("pending", "running", "paused")),
            )
            .all()
        )
        ids = [j.id for j in jobs if (j.params or {}).get("project_id") == project_id]
    finally:
        db.close()
    for jid in ids:
        delete_job(jid)
    return len(ids)


def recover_stale_jobs(db=None) -> int:
    """启动恢复（T-122/P1-65）：遗留任务不再无条件置 failed。

    - pending/running → 重新排队（pending），params 附 _restart_count 递增；
      超过上限（MAX_RESTART=3，多次重启仍中断 = 环境问题）才置 failed（可人工重跑）；
    - paused 保持暂停（用户显式暂停，重启不丢语义，恢复后由 resume 继续）。

    返回受影响行数；不传 db 时自建 SessionLocal 会话（worker_main 启动用）。
    """
    db = SessionLocal() if db is None else db
    own = db is not None
    try:
        rows = (
            db.query(ExperimentJob)
            .filter(ExperimentJob.status.in_(("pending", "running")))
            .all()
        )
        requeued = 0
        for j in rows:
            params = dict(j.params or {})
            restart = int(params.get("_restart_count", 0)) + 1
            if restart > MAX_RESTART:
                j.status = "failed"
                j.error = f"多次重启后任务仍无法完成（重启 {restart - 1} 次），请检查环境后重跑"
                j.finished_at = utcnow()
            else:
                params["_restart_count"] = restart
                j.params = params
                j.status = "pending"
                j.error = ""  # error 列 NOT NULL，重排队清错误文案
                j.finished_at = None
                requeued += 1
        db.commit()
        return requeued
    finally:
        if not own:
            db.close()


_panel_build_lock = threading.Lock()


def submit_panel_build(dataset_id: int) -> int:
    """提交面板重建任务（冷缓存 miss 后台兜底），返回 job_id。

    由 api 层冷缓存守卫调用（C11a）：任务完成后 load_panel 已落 npz 磁盘
    冷缓存 + 内存热缓存，前端轮询任务 done 后重访原端点即命中 200。

    P2-42 去重：panel_ready 检查与 submit 非原子，双并发冷缓存 miss 会双份
    全量重建+双写 npz。per-dataset 锁包住「查活动任务 + 提交」，已有活动任务
    直接复用其 job_id（与 api/paper._project_lock 同机制；单进程后端有效）。
    """
    with _panel_build_lock:
        existing = _active_panel_build_job(dataset_id)
        if existing is not None:
            return existing
        return submit("panel_build", {"dataset_id": dataset_id})


def _active_panel_build_job(dataset_id: int) -> int | None:
    """是否存在该 dataset 的活动（pending/running/paused）panel_build 任务，返回 job_id。

    params 为 JSON，SQLite 无法可靠按内层字段过滤，拉取活动任务后内存匹配
    （活动任务量小，成本可忽略；与 has_active_paper_job 同口径）。
    """
    db = SessionLocal()
    try:
        jobs = (
            db.query(ExperimentJob)
            .filter(
                ExperimentJob.job_type == "panel_build",
                ExperimentJob.status.in_(("pending", "running", "paused")),
            )
            .all()
        )
        for j in jobs:
            if (j.params or {}).get("dataset_id") == dataset_id:
                return j.id
        return None
    finally:
        db.close()


def submit(job_type: str, params: dict) -> ExperimentJob:
    """提交实验任务。

    嵌入模式（默认）：后台 daemon 线程立即执行（测试/单机）。
    worker 模式（SR_TASKS_EMBEDDED=0，T-122）：只建任务行，由独立 worker 进程
    认领执行——Web reload 不再中断运行中任务（P1-65）。
    P2-31 队列上限（嵌入模式）：先非阻塞 acquire _QUEUE_SLOT，满则抛 QueueFullError
    拒绝入队（不创建任务行）；入队成功后线程退出时 release。DB 写入失败时补偿 release。
    """
    if _embedded():
        _ensure_timeout_monitor()
        if not _QUEUE_SLOT.acquire(blocking=False):
            raise QueueFullError(
                f"任务队列已满（上限 {settings.job_queue_max}），请稍候再试或删除已完成任务"
            )
    db = SessionLocal()
    try:
        job = ExperimentJob(job_type=job_type, params=params)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    except Exception:
        if _embedded():
            _QUEUE_SLOT.release()
        raise
    finally:
        db.close()

    if _embedded():
        # 嵌入模式：本进程 spawn daemon 线程执行（测试/单机，SR_TASKS_EMBEDDED=1）
        try:
            t = threading.Thread(
                target=_run_task, args=(job_id, job_type, params, True), daemon=True,
                name=f"exp-{job_id}",
            )
            t.start()
        except Exception:
            # 线程未能启动（极端）：补偿释放队列槽，避免上限被静默耗尽
            _QUEUE_SLOT.release()
            raise
    # worker 模式：任务由独立 worker 进程认领执行（T-122），本函数只建行
    return job_id


def _run_task(job_id: int, job_type: str, params: dict, release_slot: bool) -> None:
    """单任务执行体（嵌入模式线程 / worker 进程认领后共用，T-122）。

    并发闸门：按 job_type 归入计算闸门或 IO 闸门（P2-31，IO 任务不再绕过）。
    acquire 阻塞期间任务不启动、不计超时；release 兜底在 finally。
    """
    # 并发闸门：按 job_type 归入计算闸门或 IO 闸门（P2-31，IO 任务不再绕过）。
    # acquire 阻塞期间任务保持 pending（不启动、不计超时），拿到闸门后才进入
    # 状态流转；release 兜底在 finally。
    _io_task = job_type in _IO_JOB_TYPES
    gate = _IO_CONCURRENCY if _io_task else _CONCURRENCY
    gate.acquire()
    try:
        # 任务前后端配置延迟到真正开始计算时读取（闸门之后），读取纳入
        # _backend_lock：锁内读取保证拿到的是已提交值，而非并发任务在
        # _apply_backend 切换过程中暴露的中间值（否则恢复时会写回错值）。
        with _backend_lock:
            saved_backend = settings.gp_backend
        # 分派:查 registry(core/tasks/registry)执行对应类型处理器;未知类型
        # 保留原 unknown 分支语义(置 failed,文案不变)。
        if not registry.run(job_type, job_id, params):
            _terminal(
                job_id,
                status="failed",
                error=f"未知任务类型 {job_type}",
                finished_at=utcnow(),
            )
    finally:
        # 恢复任务开始前的全局后端配置（auto = 配置默认），避免互相污染。
        # 条件恢复（选此方案）：仅当任务曾切换过后端（end_backend != saved_backend）
        # 才恢复；快照与恢复整体置于 _backend_lock 内——end_backend 锁内读取避免
        # 读到 _apply_backend 切换中间值，恢复与「设置 + init_backend」原子段互斥，
        # 锁内快照+恢复无缝衔接，等价于原「当前值仍等于结束时值才恢复」的重查语义。
        # 恢复必须先于闸门 release()：否则排队任务 acquire 通过后立即读取
        # settings.gp_backend，会读到本任务设置后的中间值，恢复才随后发生（错位）。
        with _backend_lock:
            end_backend = settings.gp_backend
            if end_backend != saved_backend:
                settings.gp_backend = saved_backend
        gate.release()
        # 队列槽：嵌入模式线程与 submit 的 acquire 一一对应；worker 模式无槽位
        if release_slot:
            _QUEUE_SLOT.release()
        # 条件更新：任务若已被取消，delete_job 已写入取消终态与 finished_at，此处不再覆盖
        _update(job_id, finished_at=utcnow())
        with _running_lock:
            _cancelled.discard(job_id)
        _drop_control(job_id)


def get_job(job_id: int) -> dict | None:
    db = SessionLocal()
    try:
        job = (
            db.query(ExperimentJob)
            .options(defer(ExperimentJob.result))
            .filter(ExperimentJob.id == job_id)
            .first()
        )
        if job is None:
            return None
        status = _display_status(job)
        # P2-32：result 大 payload（backtest modes/回撤曲线数百 KB）懒加载——
        # 仅终态（result 才可能非空）物化该列，pending/running/paused 轮询不触达
        # DB 大字段反复反序列化；非终态恒返回空 dict（与 DB 默认值一致，契约不变）。
        result = job.result if status in _TERMINAL_STATUSES else {}
        return {
            "id": job.id,
            "job_type": job.job_type,
            "status": status,
            "progress": round(job.progress, 1),
            "phase": getattr(job, "phase", ""),
            "params": job.params,
            "result": result,
            "error": job.error,
            "created_at": (
                to_market_naive(job.created_at).isoformat() if job.created_at else None
            ),
            "finished_at": (
                to_market_naive(job.finished_at).isoformat()
                if job.finished_at
                else None
            ),
        }
    finally:
        db.close()


def claim_next() -> tuple[int, str, dict] | None:
    """worker 进程认领最老 pending 任务（T-122）：DB 级 CAS pending→running，
    并发认领（多 worker/重启竞态）仅一个成功。返回 (job_id, job_type, params) 或 None。
    认领即置 running（闸门等待期间显示运行中、进度 0，超时在 _run_task 内登记）。
    """
    db = SessionLocal()
    try:
        row = (
            db.query(ExperimentJob.id, ExperimentJob.job_type, ExperimentJob.params)
            .filter(ExperimentJob.status == "pending")
            .order_by(ExperimentJob.id)
            .first()
        )
        if row is None:
            return None
        job_id, job_type, params = row
        res = db.execute(
            update(ExperimentJob)
            .where(ExperimentJob.id == job_id)
            .where(ExperimentJob.status == "pending")
            .values(status="running")
        )
        db.commit()
        if not res.rowcount:
            return None
        return int(job_id), str(job_type), dict(params or {})
    finally:
        db.close()


def worker_poll_once(max_jobs: int = 4) -> int:
    """worker 进程单轮认领：至多认领 max_jobs 个 pending 任务并异步执行，返回认领数。

    执行线程 daemon（进程退出即终止，终态写入在 finally 兜底）；并发由 _run_task
    内部闸门（重计算/IO 分闸）控制。
    """
    claimed = 0
    for _ in range(max(1, max_jobs)):
        try:
            job = claim_next()
        except Exception as e:  # noqa: BLE001 DB 瞬态错误：跳过本轮
            logger.warning("worker 认领失败: %s", str(e)[:120])
            break
        if job is None:
            break
        job_id, job_type, params = job
        threading.Thread(
            target=_run_task, args=(job_id, job_type, params, False), daemon=True,
            name=f"exp-w-{job_id}",
        ).start()
        claimed += 1
    return claimed


def get_job_params(job_id: int) -> dict | None:
    """按 job_id 取原 params（重跑复用；任务不存在返回 None）。"""
    db = SessionLocal()
    try:
        job = db.get(ExperimentJob, job_id)
        if job is None:
            return None
        return dict(job.params or {})
    finally:
        db.close()


def get_job_status(job_id: int) -> str | None:
    """仅查询任务状态（SSE 心跳用）：只 SELECT status 一列，不反序列化
    params/result 大字段（P1-28 修复：修复前心跳每 15s 全量 get_job 反序列化大 result）。

    任务不存在返回 None；legacy cancelled（failed + 取消标记）在心跳侧按
    failed 视为终态即可，无需归一。
    """
    db = SessionLocal()
    try:
        row = db.query(ExperimentJob.status).filter(ExperimentJob.id == job_id).first()
        return row[0] if row else None
    finally:
        db.close()


def _display_status(job: ExperimentJob) -> str:
    """状态兼容归一：DB 旧数据 status='failed' 且 error 含「已被用户取消」→ 输出 cancelled。

    仅影响输出视图（list/detail/rerun 判定），DB 原样不动；新数据取消直接写 cancelled。
    """
    if job.status == "failed" and job.error and _CANCELLED_ERR in job.error:
        return "cancelled"
    return job.status


_JOB_LABELS = {
    "gp_run": "因子挖掘",
    "evaluate": "因子评估",
    "walk_forward": "Walk-forward 验证",
    "neural_train": "神经网络训练",
    "rl_train": "强化学习训练",
    "backtest": "回测",
    "backtest_opt": "组合优化回测",
    "alpha101_score": "Alpha101全库评分",
    "factor_tune": "因子参数调优",
    "dataset_build": "数据集构建",
    "panel_build": "面板构建",
    "market_scan": "全市场扫描",
    "paper_experiment": "模拟盘实验",
}


def _job_summary(job_type: str, result: dict | None) -> dict | None:
    """任务结果摘要（result 非空；结构异常时返回 None）。调用方只对 done 任务传 result。"""
    if not result:
        return None
    try:
        res = result
        if job_type in ("backtest", "backtest_opt"):
            bt = res.get("backtest") or {}
            modes = bt.get("modes") or []
            if modes:
                m = modes[0].get("metrics") or {}
                mode = modes[0].get("mode")
            elif bt.get("quantiles"):
                # 仅分层回测任务：以最高分位（Q{n_q}）作为摘要
                m = bt["quantiles"][-1].get("metrics") or {}
                mode = bt["quantiles"][-1].get("q")
            else:
                m = bt.get("metrics") or {}
                mode = None
            return {
                k: m.get(k)
                for k in ("annual_return", "sharpe", "max_drawdown", "win_rate")
            } | {"mode": mode}
        if job_type == "evaluate":
            r = res.get("result") or {}
            oos = res.get("oos") or {}
            return {
                "ic": r.get("ic"),
                "rank_ic": r.get("rank_ic"),
                "long_short_annual": r.get("long_short_annual"),
                "stability": r.get("stability"),
                "oos_ic": oos.get("ic"),
            }
        if job_type == "gp_run":
            results = res.get("results") or []
            ics = [x.get("train_ic") for x in results if x.get("train_ic") is not None]
            return {"count": len(results), "max_train_ic": max(ics) if ics else None}
        if job_type == "alpha101_score":
            results = res.get("results") or []
            scored = [x.get("score") for x in results if x.get("score") is not None]
            return {"count": len(results), "max_score": max(scored) if scored else None}
        if job_type == "factor_tune":
            best = res.get("best") or {}
            return {
                "best_ic": best.get("train_ic"),
                "best_value": best.get("param_value"),
                "oos_ic": best.get("oos_ic"),
            }
        if job_type == "dataset_build":
            ds = res.get("dataset") or {}
            return {
                "stock_count": ds.get("stock_count"),
                "row_count": ds.get("row_count"),
            }
        if job_type == "paper_experiment":
            if res.get("multi"):
                # 多股项目：跨股合并所有分股 results 做汇总
                results = [
                    r for st in res.get("stocks", []) for r in st.get("results", [])
                ]
            else:
                results = res.get("results") or []
            wins = [r.get("win_rate") for r in results if r.get("win_rate") is not None]
            return {
                "signal_count": len(results),
                "total_triggers": sum(int(r.get("triggers") or 0) for r in results),
                "avg_win_rate": round(sum(wins) / len(wins), 4) if wins else None,
            }
        if job_type == "rl_train":
            # DQN 训练结果在 meta（handlers/rl_train._run_rl_train 组装）：
            # eval 含验证段仿真指标，train_rewards 为每 episode 平均奖励
            meta = res.get("meta") or {}
            eval_ = meta.get("eval") or {}
            rewards = meta.get("train_rewards") or []
            return {
                "episodes": meta.get("episodes"),
                "avg_train_reward": (
                    round(sum(rewards) / len(rewards), 6) if rewards else None
                ),
                "sharpe": eval_.get("sharpe"),
                "total_return": eval_.get("total_return"),
            }
    except Exception:
        return None
    return None


def _job_to_dict(j: ExperimentJob, result_map: dict[int, dict] | None = None) -> dict:
    """任务列表项序列化。result_map 传入时用预取的 result（P2-32：列表不逐行
    反序列化大 payload）；未传（直接调用）则读 j.result 兜底，兼容非 DB 对象。"""
    result = j.result if result_map is None else result_map.get(j.id)
    return {
        "id": j.id,
        "job_type": j.job_type,
        "label": _JOB_LABELS.get(j.job_type, j.job_type),
        "status": _display_status(j),
        "progress": round(j.progress, 1),
        "phase": getattr(j, "phase", ""),
        "expr": (j.params or {}).get("expression", ""),
        "dataset_id": (j.params or {}).get("dataset_id"),
        "summary": _job_summary(j.job_type, result),
        "error": (j.error or "")[:120],
        "created_at": (
            to_market_naive(j.created_at).isoformat() if j.created_at else None
        ),
        "params": j.params,
    }


def _load_results_map(db, jobs: list[ExperimentJob]) -> dict[int, dict]:
    """批量取页内 done 任务的 result 列（单次查询，P2-32）。

    仅 done 任务需要摘要；pending/running/paused/failed 结果恒空，列表路径不触达
    result 列（避免每任务大 payload 全量反序列化）。
    """
    done_ids = [j.id for j in jobs if j.status == "done"]
    if not done_ids:
        return {}
    rows = (
        db.query(ExperimentJob.id, ExperimentJob.result)
        .filter(ExperimentJob.id.in_(done_ids))
        .all()
    )
    return {rid: res for rid, res in rows}


def list_jobs(limit: int = 20, job_type: str | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        q = db.query(ExperimentJob).options(defer(ExperimentJob.result))
        if job_type:
            q = q.filter(ExperimentJob.job_type == job_type)
        jobs = q.order_by(ExperimentJob.id.desc()).limit(limit).all()
        result_map = _load_results_map(db, jobs)
        return [_job_to_dict(j, result_map) for j in jobs]
    finally:
        db.close()


def list_jobs_page(
    limit: int = 20,
    offset: int = 0,
    job_type: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> tuple[list[dict], int]:
    """分页任务列表：返回 (items, total)。limit clamp [1,200]，offset ≥ 0，id 降序。

    筛选全部在 count/offset 之前完成，total 为筛选后的完整计数：
    - job_type: 任务类型精确过滤
    - status: 单状态精确过滤（pending/running/paused/done/failed/cancelled）；
      别名 active = pending+running
    - keyword: 在任务 id 与 params JSON 文本（含 expression 字段）中大小写不敏感
      模糊搜索；LIKE 通配符（%/_）已转义，避免干扰匹配
    """
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    db = SessionLocal()
    try:
        q = db.query(ExperimentJob).options(defer(ExperimentJob.result))
        if job_type:
            q = q.filter(ExperimentJob.job_type == job_type)
        if status:
            status = status.strip()
            if status == "active":
                q = q.filter(ExperimentJob.status.in_(("pending", "running")))
            else:
                q = q.filter(ExperimentJob.status == status)
        if keyword:
            kw = keyword.strip()
            if kw:
                escaped = (
                    kw.lower()
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                like = f"%{escaped}%"
                q = q.filter(
                    or_(
                        func.lower(cast(ExperimentJob.id, _StrType)).like(
                            like, escape="\\"
                        ),
                        func.lower(cast(ExperimentJob.params, _StrType)).like(
                            like, escape="\\"
                        ),
                    )
                )
        total = q.count()
        jobs = q.order_by(ExperimentJob.id.desc()).offset(offset).limit(limit).all()
        result_map = _load_results_map(db, jobs)
        return [_job_to_dict(j, result_map) for j in jobs], total
    finally:
        db.close()


def delete_job(job_id: int) -> dict | None:
    """删除任务：running/pending/paused 标记 cancelled（保留记录），done/failed/cancelled 直接删行。

    取消用数据库级 CAS（仅当状态仍为 running/pending/paused 才写入 cancelled），并登记
    _cancelled 供计算线程中断；暂停中的线程通过 _unpause 唤醒，在控制点抛
    JobCancelled 终止。任何后续后台写入都被 _update 的条件更新拦截，无法覆盖取消状态。
    返回 None 表示任务不存在。
    """
    db = SessionLocal()
    try:
        job = db.get(ExperimentJob, job_id)
        if job is None:
            return None
        if job.status in ("running", "pending", "paused"):
            res = db.execute(
                update(ExperimentJob)
                .where(ExperimentJob.id == job_id)
                .where(ExperimentJob.status.in_(("running", "pending", "paused")))
                .values(status="cancelled", error=_CANCELLED_ERR, finished_at=utcnow())
            )
            db.commit()
            if res.rowcount:
                if _embedded():
                    with _running_lock:
                        _running.pop(job_id, None)
                        _cancelled.add(job_id)
                    _unpause(job_id)  # 唤醒暂停等待中的 worker（若在等待）
                # 发布 cancelled 终态事件：事件总线（events.py）以终态事件为 ring
                # 回收依据，取消不发布则取消任务 ring 永不清理（_jobs 随取消次数线性增长）。
                # 经 _publish_event 走终态分支（_TERMINAL_TYPES 含 cancelled）。
                _publish_event(
                    job_id, "cancelled", status="cancelled", error=_CANCELLED_ERR
                )
                return {"ok": True, "deleted": False}
            # 并发下计算线程已先行落终态（done/failed/cancelled）→ 按终态任务删除
            job = db.get(ExperimentJob, job_id)
            db.delete(job)
            db.commit()
            return {"ok": True, "deleted": True}
        db.delete(job)
        db.commit()
        return {"ok": True, "deleted": True}
    finally:
        db.close()


def pause_job(job_id: int) -> dict | None:
    """协作式暂停：仅 pending/running 可暂停，返回 None 表示任务不存在。

    暂停是协作式的：worker 在下一个控制点（循环/进度回调/终态写入前）阻塞；
    暂停期间超时登记被冻结（暂停时间不计入超时）；cancel 可唤醒并终止。
    """
    db = SessionLocal()
    try:
        job = db.get(ExperimentJob, job_id)
        if job is None:
            return None
        if job.status not in ("pending", "running"):
            return {
                "ok": False,
                "status": job.status,
                "error": f"仅 pending/running 任务可暂停，当前状态 {job.status}，无法暂停",
            }
        origin = job.status  # CAS 前置读取：commit 后会失效刷新为 paused
        with _state_lock(job_id):
            # state 锁与 worker 的启动/终态写入互斥，保证完成竞态下状态不丢
            res = db.execute(
                update(ExperimentJob)
                .where(ExperimentJob.id == job_id)
                .where(ExperimentJob.status.in_(("pending", "running")))
                .values(status="paused")
            )
            db.commit()
            if not res.rowcount:
                job = db.get(ExperimentJob, job_id)
                return {
                    "ok": False,
                    "status": job.status,
                    "error": f"任务状态已变化（当前 {job.status}），无法暂停",
                }
            remaining, timeout_sec = _freeze_timeout(job_id)
            if _embedded():
                _mark_paused(
                    job_id, origin=origin, remaining=remaining, timeout_sec=timeout_sec
                )
            return {
                "ok": True,
                "status": "paused",
                "cooperative": True,
                "message": "已暂停（协作式）：任务将在当前计算控制点停下，恢复后从断点继续",
            }
    finally:
        db.close()


def resume_job(job_id: int) -> dict | None:
    """恢复协作式暂停的任务：仅 paused 可恢复，返回 None 表示任务不存在。

    pending 来源的任务回到 pending（worker 尚未开始计算），running 来源回到 running；
    超时登记按暂停时冻结的剩余时间重建（暂停时间不计入超时）。
    """
    db = SessionLocal()
    try:
        job = db.get(ExperimentJob, job_id)
        if job is None:
            return None
        if job.status != "paused":
            return {
                "ok": False,
                "status": job.status,
                "error": f"仅 paused 任务可恢复，当前状态 {job.status}，无法恢复",
            }
        info = _paused.get(job_id) or {}
        target = "pending" if info.get("origin") == "pending" else "running"
        res = db.execute(
            update(ExperimentJob)
            .where(ExperimentJob.id == job_id)
            .where(ExperimentJob.status == "paused")
            .values(status=target)
        )
        db.commit()
        if not res.rowcount:
            return {
                "ok": False,
                "status": "paused",
                "error": "任务状态已变化，无法恢复",
            }
        # T-122:worker 模式恢复只写 DB(worker 控制点轮询发现后自行重建超时登记)
        if _embedded():
            _restore_timeout(job_id)
            _unpause(job_id)  # 唤醒在控制点等待的 worker
        return {
            "ok": True,
            "status": target,
            "cooperative": True,
            "message": "已恢复：任务从暂停点继续执行",
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 处理器转发绑定:9 个 _run_* 实现已拆至 core/tasks/handlers/(T-105 bt-handlers),
# 本模块保留同名模块属性转发到 handlers,保证:
# 1. 本模块对 _run_* 的模块属性引用保持可用,外部直接调用零改动;
# 2. 测试 monkeypatch 本模块属性(monkeypatch.setattr(Q, "_run_gp", fake))穿透——
#    _dispatch_ref 运行时 getattr(本模块, "_run_gp") 拿到替换后的处理器。
# 3. handlers 反向依赖 runner 的辅助/模块状态(经 handlers/_deps 动态转发),故
#    import 必须置于本文件全部符号定义之后(模块加载末尾),避免循环 import。
# ---------------------------------------------------------------------------
from sqlalchemy.orm import defer  # noqa: E402  (置于文件末尾,保持白名单记账行号稳定)
from .handlers import (  # noqa: E402
    alpha101,
    backtest,
    backtest_opt,
    dataset,
    evaluate,
    factor_tune,
    gp_run,
    market_scan,
    neural_train,
    panel_build,
    paper,
    rl_train,
    walk_forward,
)

_run_gp = gp_run._run_gp
_run_evaluate = evaluate._run_evaluate
_run_neural_train = neural_train._run_neural_train
_run_rl_train = rl_train._run_rl_train
_run_backtest = backtest._run_backtest
_run_backtest_opt = backtest_opt._run_backtest_opt
_run_alpha101_score = alpha101._run_alpha101_score
_run_factor_tune = factor_tune._run_factor_tune
_run_dataset_build = dataset._run_dataset_build
_run_market_scan = market_scan._run_market_scan
_run_panel_build = panel_build._run_panel_build
_run_paper = paper._run_paper
_run_walk_forward = walk_forward._run_walk_forward
# 随处理器搬走的辅助:保持本模块模块属性 re-export
_persist_nn_model = neural_train._persist_nn_model
_prepare_kline = paper._prepare_kline
_writeback_paper_result = paper._writeback_paper_result


# ---------------------------------------------------------------------------
# 分派注册:13 个任务类型处理器注册进 registry(core/tasks/registry,默认注册项)。
# 注册用「模块属性动态引用」(而非注册时绑定函数对象):测试 monkeypatch 模块
# 属性(如 monkeypatch.setattr(Q, "_run_gp", fake))后,submit 分派仍拿到替换后
# 的处理器——与迁移前 if/elif 链运行时读取模块全局名的语义一致。
# ---------------------------------------------------------------------------
_RUNNERS: dict[str, str] = {
    "gp_run": "_run_gp",
    "evaluate": "_run_evaluate",
    "neural_train": "_run_neural_train",
    "rl_train": "_run_rl_train",
    "backtest": "_run_backtest",
    "backtest_opt": "_run_backtest_opt",
    "alpha101_score": "_run_alpha101_score",
    "factor_tune": "_run_factor_tune",
    "dataset_build": "_run_dataset_build",
    "panel_build": "_run_panel_build",
    "market_scan": "_run_market_scan",
    "paper_experiment": "_run_paper",
    "walk_forward": "_run_walk_forward",
}


def _dispatch_ref(attr: str):
    """注册项:调用时从本模块属性动态解析处理器,支持测试 monkeypatch 穿透。"""

    def _handler(job_id: int, params: dict) -> None:
        return getattr(sys.modules[__name__], attr)(job_id, params)

    return _handler


for _job_type, _attr in _RUNNERS.items():
    registry.register(_job_type, _dispatch_ref(_attr))
