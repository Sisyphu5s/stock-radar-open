"""/api/v1/experiments/* : 实验任务队列。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Any, Optional

from ..core import events
from ..core.tasks.errors import QueueFullError
from ..core.tasks.runner import (
    delete_job,
    get_job,
    get_job_params,
    get_job_status,
    has_active_paper_job,
    list_jobs,
    list_jobs_page,
    pause_job,
    resume_job,
    submit,
)

router = APIRouter(prefix="/experiments", tags=["experiments"])


# ===== 响应模型（T-113 契约硬化：P2-29 阶段三，逐路径契约） =====
# 字段与各端点 return dict 逐一核对，缺字段=response_model 裁剪响应=前端数据缺失。
# 详情/分页端点声明了前端宽类型消费但本端点不返回的契约字段（results/meta/…、
# period/stats）：这些字段仅用于 R1 对账（前端 api 层宽类型并集），经
# response_model_exclude_unset 剔除，不改变实际响应字节。

class JobListItem(BaseModel):
    """任务列表项（GET /experiments 与 /experiments/page 共用，_job_to_dict 快照）。

    expr/dataset_id 为任务 params 透传的任意 JSON 值（非强类型），用 Any 保持字节不变。
    """

    id: int
    job_type: str
    label: str
    status: str
    progress: float
    phase: str
    expr: Any
    dataset_id: Any
    summary: Optional[dict[str, Any]] = None
    error: str = ""
    created_at: Optional[str] = None
    params: dict[str, Any]


class JobDetail(BaseModel):
    """任务详情（GET /experiments/{job_id}，get_job）：含完整 result 与 finished_at。

    本端点实际不含 label/expr/dataset_id/summary（前端以列表快照合并，见 TaskDetailDrawer）；
    results/meta/backtest/oos/scan 为前端 JobResultView 判别联合的宽类型字段——本端点
    不返回，经 exclude_unset 剔除输出，仅声明以供契约对账。
    """

    id: int
    job_type: str
    status: str
    progress: float
    phase: str
    params: dict[str, Any]
    result: dict[str, Any]
    error: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    results: Optional[list[Any]] = None
    meta: Optional[dict[str, Any]] = None
    backtest: Optional[dict[str, Any]] = None
    oos: Optional[dict[str, Any]] = None
    scan: Optional[dict[str, Any]] = None
    label: Optional[str] = None
    expr: Any = None
    dataset_id: Any = None
    summary: Optional[dict[str, Any]] = None


class ExperimentPageStats(BaseModel):
    """分页筛选口径聚合统计（与信号事件分页同型）。

    实验分页端点不返回 stats，仅供契约对账（前端共享 PageResult 宽类型）。
    """

    today_new: int
    stock_count: int


class ExperimentPageResponse(BaseModel):
    """GET /experiments/page：{items/total/limit/offset/has_more}。

    period/stats 为前端共享 PageResult 宽类型字段，实验端点不返回——exclude_unset 剔除。
    """

    items: list[JobListItem]
    total: int
    limit: int
    offset: int
    has_more: bool
    period: Optional[str] = None
    stats: Optional[ExperimentPageStats] = None


class CreateExperimentResponse(BaseModel):
    """POST /experiments 与 POST /experiments/{id}/rerun 共用：{job_id, status=pending}。"""

    job_id: int
    status: str


class ExperimentListResponse(BaseModel):
    data: list[JobListItem]


class ExperimentDeleteResponse(BaseModel):
    ok: bool
    deleted: bool


class JobControlResponse(BaseModel):
    """POST /experiments/{id}/pause|resume：成功/失败两态字段并集（pause_job/resume_job）。"""

    ok: bool
    status: str
    cooperative: Optional[bool] = None
    message: Optional[str] = None
    error: Optional[str] = None

# SSE 心跳间隔（秒）：队列空时按此时长阻塞等待，超时发心跳保活。
# 测试可 monkeypatch 为小值以缩短终态收尾等待。
_SSE_GET_TIMEOUT = 15

# 终态事件类型集合（ring/SSE 事件流侧判定）：单一事实源在 events._TERMINAL_TYPES，
# 含 cancelled（delete_job 取消路径发布，queue.py）。前端 useJobEvents/useJobFlow
# 以相同终态集合收尾，本常量变更需同步前端（供前端同步）。
_TERMINAL_TYPES = events._TERMINAL_TYPES

# 终态状态（DB 侧；含 cancelled；legacy cancelled 存为 failed 已在集合内）
_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


@router.post("", response_model=CreateExperimentResponse)
def create_experiment(payload: dict):
    job_type = payload.get("job_type", "gp_run")
    if job_type not in (
        "gp_run",
        "evaluate",
        "neural_train",
        "rl_train",
        "backtest",
        "backtest_opt",
        "alpha101_score",
        "factor_tune",
        "dataset_build",
        "panel_build",
        "market_scan",
        "paper_experiment",
        "walk_forward",
    ):
        raise HTTPException(400, f"未知任务类型 {job_type}")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise HTTPException(400, "参数 params 必须是对象")
    try:
        job_id = submit(job_type, params)
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job_id, "status": "pending"}


# 注意：/page 必须先于 /{job_id} 注册，否则 "page" 会被当作 job_id 捕获
@router.get("/page", response_model=ExperimentPageResponse, response_model_exclude_unset=True)
def experiment_list_page(
    limit: int = 20,
    offset: int = 0,
    job_type: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
):
    """分页实验任务列表（向后兼容新增，旧数组 /experiments 端点保留）。

    筛选与排序（id 降序）和 /experiments 一致，筛选全部在 count/offset 前完成：
    - job_type: 任务类型精确过滤
    - status: 单状态精确过滤（pending/running/paused/done/failed）；别名 active = pending+running
    - keyword: 在任务 id、表达式与序列化 params 中大小写不敏感模糊搜索
    返回 items/total/limit/offset/has_more；total 为筛选后完整计数。
    """
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    items, total = list_jobs_page(limit, offset, job_type, status, keyword)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.get("/{job_id}", response_model=JobDetail, response_model_exclude_unset=True)
def experiment_status(job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job


async def _event_stream(job_id: int):
    """SSE 异步生成器：首连重放环形缓冲 → 实时推送 → 心跳保活 → 终态收尾。

    事件格式与旧版 sync 生成器逐字节一致（event: xxx\ndata: {json}\n\n）；
    订阅走 events.asubscribe（AsyncSubQueue 线程→事件循环桥），实时事件经
    loop.call_soon_threadsafe 入队，生成器 await 消费——长连接不再占用
    anyio 线程池线程（40 个 SSE 连接不再耗尽线程池）。

    终态收尾：收到终态事件（done/failed/cancelled，_TERMINAL_TYPES）或 DB 状态已
    终态且队列空时，再发一次心跳后关闭长连接，避免无限挂连接；客户端断开时生成器
    被取消，finally 中 unsubscribe 清理订阅。
    P1-28：重放内含终态事件（终态发布在订阅前）→ 置 terminal_done 立即收尾，
    不再空等一个 _SSE_GET_TIMEOUT；心跳分支只查轻量状态（get_job_status），
    避免每 15s 全量反序列化大 result。
    P1-30：事件侧终态判定统一走 _TERMINAL_TYPES（含 cancelled）——cancelled 事件
    到达即收尾，不得空等超时周期。
    """
    snap = events.snapshot(job_id)
    yield f"event: replay\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
    q = events.asubscribe(job_id)
    try:
        # 重放末条为终态事件（ring 终态后不再追加）或 DB 已终态 → 直接收尾
        terminal_done = bool(snap) and snap[-1].get("type") in _TERMINAL_TYPES
        if not terminal_done:
            st = get_job_status(job_id)
            terminal_done = st is not None and st in _TERMINAL_STATUSES
        while True:
            if terminal_done:
                yield ": heartbeat\n\n"
                return
            try:
                e = await asyncio.wait_for(q.get(), timeout=_SSE_GET_TIMEOUT)
                yield (
                    f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                )
                if e.get("type") in _TERMINAL_TYPES:
                    terminal_done = True
            except asyncio.TimeoutError:
                # 队列空：终态（终态事件已发或 DB 已终态，如超时置 failed）→
                # 最后一条心跳后关闭连接；未终态 → 纯心跳保活。
                if terminal_done:
                    yield ": heartbeat\n\n"
                    return
                st = get_job_status(job_id)
                if st is None or st in _TERMINAL_STATUSES:
                    yield ": heartbeat\n\n"
                    return
                yield ": heartbeat\n\n"
    finally:
        events.unsubscribe(job_id, q)


@router.get("/{job_id}/events")
async def experiment_events(job_id: int):
    """SSE 事件流：首连重放 + 实时进度/阶段/终态事件推送（内存态，不落库）。"""
    if get_job(job_id) is None:
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        _event_stream(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("", response_model=ExperimentListResponse)
def experiment_list(limit: int = 20, job_type: str | None = None):
    limit = max(1, min(200, limit))
    return {"data": list_jobs(limit, job_type)}


@router.delete("/{job_id}", response_model=ExperimentDeleteResponse)
def experiment_delete(job_id: int):
    res = delete_job(job_id)
    if res is None:
        raise HTTPException(404, "任务不存在")
    return res


@router.post("/{job_id}/pause", response_model=JobControlResponse)
def experiment_pause(job_id: int):
    """协作式暂停：仅 pending/running 可暂停。

    任务在下一个计算控制点（可中断循环 / 进度回调 / 终态写入前）停下；
    不可中断的单次底层计算会继续到下一个控制点。暂停期间超时不计时，
    终态不会覆盖 paused；DELETE 可随时取消。
    """
    res = pause_job(job_id)
    if res is None:
        raise HTTPException(404, "任务不存在")
    if not res.get("ok"):
        raise HTTPException(409, res.get("error", "任务无法暂停"))
    return res


@router.post("/{job_id}/rerun", response_model=CreateExperimentResponse)
def experiment_rerun(job_id: int):
    """重跑任务：仅终态（done/failed/cancelled）可重跑，复用原 params 重新提交。

    非终态（pending/running/paused）→ 409；任务不存在 → 404。
    paper_experiment 重跑直连 submit 会绕过 paper.run_project 的查重，
    此处补项目级并发防重（与 run_project 同口径）：项目已有活动任务 → 409。
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job["status"] not in ("done", "failed", "cancelled"):
        raise HTTPException(
            409, f"仅终态任务可重跑，当前状态 {job['status']}，无法重跑"
        )
    params = get_job_params(job_id)
    if job["job_type"] == "paper_experiment":
        pid = (params or {}).get("project_id")
        if pid is not None and has_active_paper_job(pid):
            raise HTTPException(409, f"项目 {pid} 已有运行中的模拟盘实验，请稍候再重跑")
    try:
        new_id = submit(job["job_type"], params)
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": new_id, "status": "pending"}


@router.post("/{job_id}/resume", response_model=JobControlResponse)
def experiment_resume(job_id: int):
    """恢复协作式暂停的任务：仅 paused 可恢复。

    恢复后任务从暂停点继续；pending 来源的任务回到 pending 并继续启动计算，
    running 来源回到 running 直接续跑。
    """
    res = resume_job(job_id)
    if res is None:
        raise HTTPException(404, "任务不存在")
    if not res.get("ok"):
        raise HTTPException(409, res.get("error", "任务无法恢复"))
    return res
