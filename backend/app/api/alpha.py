"""/api/v1/alpha/* : 因子研究：算子集、GP 配置等。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.tasks.errors import QueueFullError
from ..lib.alpha import backend as B
from ..lib.alpha.operators import (
    DEFAULT_FEATURES,
    DEFAULT_OP_SET,
    EDITABLE_OPS,
    OPERATORS,
    TS_PARAM_GRID,
)
from ..storage.db import get_db

router = APIRouter(prefix="/alpha", tags=["alpha"])


# ---------------------------------------------------------------------------
# T-114 契约硬化: 响应模型（字段与各端点 return dict 逐一核对，缺字段=响应丢字段）
# ---------------------------------------------------------------------------


class OperatorRow(BaseModel):
    """GET /operators data[] 单算子。"""

    name: str
    arity: int
    display: str
    category: str


class OperatorsResponse(BaseModel):
    data: list[OperatorRow]
    default_set: list[str]
    features: list[str]
    param_grid: dict[str, list[int | float]]
    editable_ops: dict[str, dict[str, list[int | float]]]


class ExplainStep(BaseModel):
    """表达式求值步骤（__feat__/__const__ 步骤无 params 键）。"""

    step: int
    op: str
    display: str
    inputs: list[str]
    note: str
    params: Optional[dict[str, Any]] = None


class ExplainResponse(BaseModel):
    expression: str
    steps: list[ExplainStep]


class Alpha101Item(BaseModel):
    """Alpha101 单因子元数据（evaluable=False 时含 skip_reason）。"""

    id: int
    name: str
    category: str
    formula: str
    desc: str
    formula_expr: str
    params_desc: str
    usage: str
    evaluable: bool
    latex: Optional[str] = None
    skip_reason: Optional[str] = None


class Alpha101ListResponse(BaseModel):
    data: list[Alpha101Item]
    categories: list[str]
    total: int


class Alpha101DetailResponse(BaseModel):
    alpha: Alpha101Item
    same_category: list[Alpha101Item]


class ToLibraryResponse(BaseModel):
    """POST /alpha101/{id}/to-library：成功含 factor/version，重复含 message。"""

    ok: bool
    duplicate: bool
    factor: Optional[dict[str, Any]] = None
    version: Optional[dict[str, Any]] = None
    message: Optional[str] = None


class EvalMetrics(BaseModel):
    """evaluate_factor 单段评估指标。"""

    expression: str
    complexity: int
    ic: Optional[float] = None
    rank_ic: Optional[float] = None
    top_annual: Optional[float] = None
    long_short_annual: Optional[float] = None
    turnover: Optional[float] = None
    stability: Optional[float] = None
    ic_positive_ratio: Optional[float] = None


class Alpha101EvaluateResponse(BaseModel):
    alpha: Alpha101Item
    train: EvalMetrics
    oos: EvalMetrics
    meta: dict[str, int]


class JobPendingResponse(BaseModel):
    """后台任务提交响应（alpha101/score 等）。"""

    job_id: int
    status: str


class TuneParamMeta(BaseModel):
    """collect_tunable_params 单条可调参数。"""

    name: str
    op: str
    key: str
    index: int
    default: Optional[int | float] = None
    is_int: bool
    count: int


class TuneParamRequest(BaseModel):
    """factor_tune 任务结果 param 契约字段（POST /alpha/tune 本身不返回，供对账）。"""

    name: str
    min: int | float
    max: int | float
    step: int | float
    is_int: Optional[bool] = None


class TuneGridItem(BaseModel):
    """factor_tune 任务结果 grid[]/best 单点（GET /experiments/{job_id}.result 契约）。"""

    param_value: int | float
    train_ic: Optional[float] = None
    rank_ic: Optional[float] = None
    icir: Optional[float] = None
    ls_annual: Optional[float] = None
    stability: Optional[float] = None
    oos_ic: Optional[float] = None
    turnover: Optional[float] = None
    ls_sharpe: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_rate: Optional[float] = None
    error: Optional[str] = None


class TuneRunMeta(BaseModel):
    """factor_tune 任务结果 meta（split 为 train/oos 时间轴列边界）。"""

    stocks: Optional[int] = None
    days: Optional[int] = None
    split: Optional[dict[str, int]] = None


class AlphaTuneResponse(BaseModel):
    """POST /alpha/tune：probe 与提交两种返回形态的并集（字段全部可选）。

    probe=true → {expression, probe, available_params, param_meta, target, horizon}；
    提交 → {job_id, status, available_params}。param/grid/best/best_expression/meta
    为任务结果（GET /experiments/{job_id}.result）契约字段，前端统一类型声明覆盖，
    此处一并声明以满足契约对账；response_model_exclude_unset 保证实际响应不补 null。
    """

    expression: Optional[str] = None
    probe: Optional[bool] = None
    job_id: Optional[int] = None
    status: Optional[str] = None
    available_params: Optional[list[str]] = None
    param_meta: Optional[list[TuneParamMeta]] = None
    target: Optional[str] = None
    horizon: Optional[int] = None
    param: Optional[TuneParamRequest] = None
    grid: Optional[list[TuneGridItem]] = None
    best: Optional[TuneGridItem] = None
    best_expression: Optional[str] = None
    meta: Optional[TuneRunMeta] = None


class TuneGlobalResponse(BaseModel):
    data: dict[str, str]


class TuneGlobalPutResponse(BaseModel):
    ok: bool
    saved: list[str]


class BackendResponse(BaseModel):
    backend: str


class LatexResponse(BaseModel):
    expression: str
    latex: str


class LatexBatchResponse(BaseModel):
    data: list[LatexResponse]


class NNModelRow(BaseModel):
    """GET /nn-models data[] 单条模型产物。"""

    id: int
    checkpoint: str
    architecture: dict[str, Any]
    dataset_id: int
    horizon: int
    epochs: int
    train_ic: Optional[float] = None
    val_ic: Optional[float] = None
    train_loss: Optional[list[int | float]] = None
    val_loss: Optional[list[int | float]] = None
    created_at: Optional[str] = None


class NNModelsResponse(BaseModel):
    data: list[NNModelRow]


class PublishStep(BaseModel):
    key: str
    name: str
    desc: str


class PublishStepsResponse(BaseModel):
    data: list[PublishStep]


class OptimizeWeightItem(BaseModel):
    code: str
    weight: float


class OptimizePerf(BaseModel):
    annual_return: Optional[float] = None
    volatility: Optional[float] = None
    sharpe: Optional[float] = None


class OptimizeResponse(BaseModel):
    method: str
    max_weight: float
    top_n: int
    weights: list[OptimizeWeightItem]
    perf: OptimizePerf
    turnover: float


class CombineWeightItem(BaseModel):
    name: str
    weight: float


class CombineSignalPoint(BaseModel):
    code: str
    value: float


class CombineFactorRef(BaseModel):
    """save_to_library 的 factor 结果（成功 id 非空；重复时 message 带原因）。"""

    id: Optional[int] = None
    name: str
    duplicate: bool
    message: Optional[str] = None


class CombineResponse(BaseModel):
    method: str
    ic: Optional[float] = None
    ic_series: list[Optional[float]]
    ic_positive_ratio: Optional[float] = None
    weights: list[CombineWeightItem]
    combined_signal: list[CombineSignalPoint]
    expression: str
    factor: Optional[CombineFactorRef] = None


def _int_param(payload: dict, key: str, default: int) -> int:
    """请求体整数参数：非法值统一返回 400（避免 ValueError 500）。"""
    try:
        return int(payload.get(key, default))
    except (ValueError, TypeError):
        raise HTTPException(400, f"参数 {key} 必须是整数")


def _float_param(payload: dict, key: str, default: float) -> float:
    """请求体浮点参数：非法值统一返回 400（P1-57，避免 float() ValueError 500）。"""
    try:
        return float(payload.get(key, default))
    except (ValueError, TypeError):
        raise HTTPException(400, f"参数 {key} 必须是数字")


def _init_backend_locked() -> None:
    """后端初始化纳入 runner._backend_lock（P1-56）。

    API 路径与运行中任务的 _apply_backend 切换/恢复共享 settings.gp_backend 与
    BACKEND/_T：不持锁的 init_backend() 会读到任务切换过程的中间值，或在任务
    恢复前后翻转计算后端（计算中途后端切换 / mlx 重复初始化）。持锁后与任务的
    「设置 + init_backend + 恢复」原子段互斥，拿到的一律是已提交值。
    """
    from ..core.tasks.runner import _backend_lock

    with _backend_lock:
        B.init_backend()


@contextmanager
def _compute_gate():
    """P2-38: 重计算同步端点占用任务并发闸门（runner._CONCURRENCY）。

    数十秒 numpy 的重计算若直接跑在共享 anyio 线程池上，并发一多（≥40）会
    饿死行情/搜索等轻量端点；复用后台任务的并发闸门后，同步重计算与后台任务
    同额限流，且闸门硬上限保证线程池永不被重计算占满。

    调用约定：在 `_panel_or_202` 的 202 早退分支之后、进入面板加载/求值前
    acquire，compute 结束（含 return/异常）经 finally release。
    """
    from ..core.tasks.runner import _CONCURRENCY

    _CONCURRENCY.acquire()
    try:
        yield
    finally:
        _CONCURRENCY.release()


def _panel_or_202(dataset_id: int, features: list[str] | None = None):
    """面板冷缓存守卫（C11a）：热/冷缓存命中返回 None（端点走原 load_panel 同步路径）；
    冷缓存 miss → 提交 panel_build 后台重建任务，返回 202 + {task_id, status, message}。

    任务完成后 load_panel 已落 npz + 内存热缓存，前端轮询任务 done 后重访本端点
    即返回 200 热数据；任务执行中同参数重复请求由 runner 并发闸门/队列天然排队，
    不另造防重。
    """
    from ..core.datasets import panel_ready
    from ..core.tasks.runner import submit_panel_build

    if panel_ready(dataset_id, features):
        return None
    try:
        job_id = submit_panel_build(dataset_id)
    except QueueFullError as e:
        # P2-31 队列满：拒绝入队返回 429（与通用错误格式 {detail} 一致，前端读 detail）
        return JSONResponse(status_code=429, content={"detail": str(e)})
    return JSONResponse(
        status_code=202,
        content={
            "task_id": job_id,
            "status": "queued",
            "message": (
                f"数据集面板冷缓存未命中，已提交后台构建任务（#{job_id}），"
                "任务完成后请重试本请求获取结果"
            ),
        },
    )


@router.get("/operators", response_model=OperatorsResponse)
def operators():
    return {
        "data": [
            {
                "name": k,
                "arity": v["arity"],
                "display": v["display"],
                "category": (
                    "时间序列"
                    if k.startswith("ts_") or k in ("delta",)
                    else "横截面"
                    if k == "rank"
                    else "算术"
                ),
            }
            for k, v in sorted(OPERATORS.items())
        ],
        "default_set": DEFAULT_OP_SET,
        "features": DEFAULT_FEATURES,
        "param_grid": TS_PARAM_GRID,
        "editable_ops": EDITABLE_OPS,
    }


@router.get("/explain", response_model=ExplainResponse, response_model_exclude_unset=True)
def explain(expr: str):
    """表达式计算过程逐步说明。"""
    from ..lib.alpha.operators import compile_rpn, explain_rpn

    try:
        return {"expression": expr, "steps": explain_rpn(compile_rpn(expr))}
    except Exception as e:
        raise HTTPException(400, f"表达式解析失败: {str(e)[:120]}")


@router.get("/alpha101", response_model=Alpha101ListResponse, response_model_exclude_unset=True)
def alpha101_list(category: str | None = None):
    """WorldQuant Alpha101 经典因子库。"""
    from ..lib.alpha.alpha101 import list_alpha101

    # P2-36: list_alpha101() 只构建一次元数据（列表+分类集合同源），避免每请求
    # 重复构建 101 因子元数据；分类集合取自全量（category 过滤只作用于 data）。
    all_items = list_alpha101()
    items = (
        [a for a in all_items if a["category"] == category] if category else all_items
    )
    cats = sorted({a["category"] for a in all_items})
    return {"data": items, "categories": cats, "total": len(items)}


@router.get("/alpha101/{alpha_id}", response_model=Alpha101DetailResponse, response_model_exclude_unset=True)
def alpha101_detail(alpha_id: int):
    """Alpha101 单因子详情 + 同类别因子列表。"""
    from ..lib.alpha.alpha101 import get_alpha, list_alpha101

    a = get_alpha(alpha_id)
    if a is None:
        raise HTTPException(404, "Alpha 因子不存在")
    same_category = [x for x in list_alpha101() if x["category"] == a["category"]]
    return {"alpha": a, "same_category": same_category}


def _alpha101_score_metrics(db: Session, alpha_id: int) -> dict | None:
    """最新 alpha101_score 任务结果中该因子的评分 → create_factor metrics。

    指标键按因子版本列语义映射：ic_mean → oos_ic、stability → stability；
    无评分记录（从未评分/该因子无评分）返回 None，create_factor 不写指标，
    创建行为与旧契约一致（停在 draft，不推进发布状态机）。
    """
    from ..storage.models import ExperimentJob

    job = (
        db.query(ExperimentJob)
        .filter(
            ExperimentJob.job_type == "alpha101_score",
            ExperimentJob.status == "done",
        )
        .order_by(ExperimentJob.id.desc())
        .first()
    )
    if job is None or not job.result:
        return None
    for r in job.result.get("results") or []:
        if r.get("id") == alpha_id:
            metrics = {}
            if r.get("ic_mean") is not None:
                metrics["oos_ic"] = float(r["ic_mean"])
            if r.get("stability") is not None:
                metrics["stability"] = float(r["stability"])
            return metrics or None
    return None


@router.post(
    "/alpha101/{alpha_id}/to-library",
    response_model=ToLibraryResponse,
    response_model_exclude_unset=True,
)
def alpha101_to_library(alpha_id: int, payload: dict, db: Session = Depends(get_db)):
    """将 Alpha101 因子导入本地因子库（expression 已存在时返回 duplicate 标记）。

    最近一次 alpha101_score 任务对该因子的 ic_mean/stability 自动透传为
    版本指标（oos_ic/stability），未评分则不写指标；创建停在 draft。
    """
    from ..lib.alpha.alpha101 import get_alpha
    from ..core.factors import create_factor

    a = get_alpha(alpha_id)
    if a is None:
        raise HTTPException(404, "Alpha 因子不存在")
    try:
        result = create_factor(
            name=a["name"],
            expression=a["formula"],
            description=f"{a['desc']}\n使用方向: {a['usage']}",
            dataset_id=_int_param(payload, "dataset_id", 0),
            metrics=_alpha101_score_metrics(db, alpha_id),
        )
        return {"ok": True, "duplicate": False, **result}
    except ValueError as e:
        msg = str(e)
        # 仅表达式重复（create_factor 消息含"已存在/重复"）标记 duplicate；
        # 其它校验类 ValueError 属参数错误，按 400 返回而非误标重复
        if "已存在" in msg or "重复" in msg:
            return {"ok": False, "duplicate": True, "message": msg}
        raise HTTPException(400, f"创建因子失败: {msg[:200]}")


@router.post(
    "/alpha101/evaluate",
    response_model=Alpha101EvaluateResponse,
    response_model_exclude_unset=True,
)
def alpha101_evaluate(payload: dict):
    """评估单个 Alpha101 因子（训练/样本外 IC）。

    面板冷缓存未命中 → 202 + 后台构建任务（任务完成后重试本端点即 200 热数据）。
    """
    from ..lib.alpha.alpha101 import get_alpha
    from ..core.datasets import load_panel
    from ..lib.alpha.evaluate import evaluate_factor, forward_returns
    from ..lib.alpha.operators import compile_rpn

    a = get_alpha(_int_param(payload, "alpha_id", 0))
    if a is None:
        raise HTTPException(404, "Alpha 因子不存在")
    ds = _int_param(payload, "dataset_id", 0)
    if ds <= 0:
        raise HTTPException(400, "dataset_id 无效")
    resp = _panel_or_202(ds)
    if resp is not None:
        return resp
    panel_info = load_panel(ds)
    if panel_info is None:
        raise HTTPException(400, "数据集未构建或无数据，请先在数据集页面构建后重试")
    panel = panel_info["panel"]
    try:
        rpn = compile_rpn(a["formula"])
    except Exception as e:
        raise HTTPException(400, f"{a['name']} 公式含未支持算子: {str(e)[:150]}")
    # horizon=0 → forward_returns 的 [:-0] 全切片产生全零矩阵且语义错误；
    # clamp 到 [1, 250]（对齐 indicators/tune.py 的 horizon 边界）
    horizon = max(1, min(250, _int_param(payload, "horizon", 5)))
    fwd = forward_returns(panel["close"], horizon=horizon)
    n_days = panel["close"].shape[1]
    t2 = int(n_days * 0.8)
    train = evaluate_factor(rpn, {k: v[:, :t2] for k, v in panel.items()}, fwd[:, :t2])
    oos = evaluate_factor(rpn, {k: v[:, t2:] for k, v in panel.items()}, fwd[:, t2:])
    return {
        "alpha": a,
        "train": train,
        "oos": oos,
        "meta": {"stocks": panel["close"].shape[0], "days": n_days},
    }


@router.post("/alpha101/score", response_model=JobPendingResponse)
def alpha101_score(payload: dict):
    """Alpha101 因子库批量评分（后台任务，逐因子评估计入任务管理器）。

    返回 {job_id, status}，结果含 results（每因子 ic_mean/stability/score）与 meta。
    """
    from ..core.tasks.runner import submit

    ds = _int_param(payload, "dataset_id", 0)
    if ds <= 0:
        raise HTTPException(400, "dataset_id 无效")
    try:
        job_id = submit(
            "alpha101_score",
            {
                "dataset_id": ds,
                "horizon": _int_param(payload, "horizon", 5),
                "limit": _int_param(payload, "limit", 0),
            },
        )
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job_id, "status": "pending"}


@router.post("/tune", response_model=AlphaTuneResponse, response_model_exclude_unset=True)
def tune(payload: dict):
    """因子参数网格搜索调优。

    对表达式中的可调参数（如 ts_mean 的 window、ts_delay/delta 的 delay、
    signed_power 的 power、winsorize 的 pct）做网格搜索：
    每个网格点替换参数后重新求值，训练段 0-80% 计算 IC/ICIR/多空年化/稳定性，
    20% 样本外计算 OOS IC；按 target 排序返回 grid + best。

    payload: {expression, dataset_id, horizon=5, target="ic", param={name,min,max,step},
              probe?=true 时只返回可用参数列表不跑网格}
    同步执行（网格 ≤ 50 点，面板走 panel_cache，速度可控）。
    """
    import numpy as np
    from ..core.datasets import load_panel
    from ..lib.alpha.evaluate import (
        evaluate_factor,
        factor_to_returns,
        forward_returns,
        ic_series,
        stability as stab_fn,
    )
    from ..lib.alpha.operators import (
        collect_tunable_params,
        compile_rpn,
        evaluate_rpn,
        expr_str,
        set_tunable_param,
    )

    expression = str(payload.get("expression", "")).strip()
    if not expression:
        raise HTTPException(400, "expression 不能为空")
    try:
        rpn = compile_rpn(expression)
    except Exception as e:
        raise HTTPException(400, f"表达式解析失败: {str(e)[:150]}")

    available = collect_tunable_params(rpn)
    available_names = [a["name"] for a in available]
    target = str(payload.get("target", "ic"))
    if target not in (
        "ic",
        "ic_abs",
        "icir",
        "rank_ic",
        "ls_annual",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "turnover",
        "stability",
        "composite",
    ):
        raise HTTPException(400, f"target 不受支持: {target}")
    horizon = _int_param(payload, "horizon", 5)

    if bool(payload.get("probe", False)):
        return {
            "expression": expression,
            "probe": True,
            "available_params": available_names,
            "param_meta": available,
            "target": target,
            "horizon": horizon,
        }

    param = payload.get("param")
    if not isinstance(param, dict) or not str(param.get("name", "")).strip():
        raise HTTPException(
            400, "param.name 必填（可用参数: " + ", ".join(available_names) + ")"
        )
    name = str(param["name"]).strip()
    if name not in available_names:
        raise HTTPException(
            400,
            f"参数 {name} 不在可调参数列表中（可用: "
            + ", ".join(available_names)
            + ")",
        )
    meta = next(a for a in available if a["name"] == name)
    try:
        pmin, pmax, pstep = (
            float(param.get("min")),
            float(param.get("max")),
            float(param.get("step")),
        )
    except (TypeError, ValueError):
        raise HTTPException(400, "param.min/max/step 必须是数字")
    if not pstep > 0 or pmin > pmax:
        raise HTTPException(400, "param 范围非法（需 min <= max 且 step > 0）")
    is_int = bool(meta["is_int"]) or bool(param.get("is_int", False))

    values: list[float] = []
    v = pmin
    while v <= pmax + pstep * 1e-9:
        values.append(float(round(v)) if is_int else round(v, 6))
        v += pstep
        if len(values) > 100:
            break
    if not values:
        raise HTTPException(400, "网格为空（min/max/step 未产生任何取值）")
    if len(values) > 50:
        raise HTTPException(
            400, f"网格点 {len(values)} 超过上限 50，请增大 step 或收窄范围"
        )

    # 异步后台任务：网格搜索在任务管理器可见（factor_tune）
    from ..core.tasks.runner import submit

    ds = _int_param(payload, "dataset_id", 0)
    if ds <= 0:
        raise HTTPException(400, "dataset_id 无效")
    try:
        job_id = submit(
            "factor_tune",
            {
                "expression": expression,
                "dataset_id": ds,
                "horizon": horizon,
                "target": target,
                "param": {
                    "name": name,
                    "min": pmin,
                    "max": pmax,
                    "step": pstep,
                    "is_int": is_int,
                },
            },
        )
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job_id, "status": "pending", "available_params": available_names}


@router.get("/tune/global", response_model=TuneGlobalResponse)
def tune_global():
    """全局调优参数（键值对，调优结果可一键保存为全局默认）。"""
    from ..storage.appparams import get_app_params

    return {"data": get_app_params()}


@router.put("/tune/global", response_model=TuneGlobalPutResponse)
def tune_global_put(payload: dict):
    """保存/更新全局调优参数：{"params": {"ts_mean.window": "15", ...}}。"""
    from ..storage.appparams import set_app_params

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(400, "params 必须为 dict")
    saved = set_app_params(params)
    return {"ok": True, "saved": saved}


@router.get("/backend", response_model=BackendResponse)
def backend_status():
    _init_backend_locked()
    return {"backend": B.backend_name()}


@router.get("/latex", response_model=LatexResponse)
def latex(expr: str):
    """因子表达式 → LaTeX（前端 KaTeX 渲染）。"""
    from ..lib.alpha.latex import cached_latex

    return {"expression": expr, "latex": cached_latex(expr)}


@router.post("/latex", response_model=LatexBatchResponse)
def latex_batch(payload: dict):
    exprs = payload.get("expressions", [])
    from ..lib.alpha.latex import cached_latex

    return {"data": [{"expression": e, "latex": cached_latex(e)} for e in exprs]}


@router.get("/nn-models", response_model=NNModelsResponse)
def nn_models():
    """神经网络模型列表（训练产物，按 id 降序，供 nn 因子管理与回测引用）。"""
    from ..storage.repos.models import list_nn_models
    from ..lib.timex import to_market_naive

    rows = list_nn_models()
    return {
        "data": [
            {
                "id": m.id,
                "checkpoint": m.checkpoint,
                "architecture": m.architecture,
                "dataset_id": m.dataset_id,
                "horizon": m.horizon,
                "epochs": m.epochs,
                "train_ic": m.train_ic,
                "val_ic": m.val_ic,
                "train_loss": m.train_loss,
                "val_loss": m.val_loss,
                "created_at": (
                    to_market_naive(m.created_at).isoformat() if m.created_at else None
                ),
            }
            for m in rows
        ]
    }


@router.get("/publish-steps", response_model=PublishStepsResponse)
def publish_steps():
    from ..core.factors import PUBLISH_STEPS

    return {"data": [{"key": k, "name": n, "desc": d} for k, n, d in PUBLISH_STEPS]}


# ---------------------------------------------------------------------------
# T-10 组合优化 / T-11 多因子合成(均同步执行:面板走 panel_cache,速度可控)
# ---------------------------------------------------------------------------


def _rpn_feature_names(rpn: list[dict]) -> set[str]:
    """RPN 用到的特征字段(与 runner._rpn_feature_names 同规则,供 load_panel 子集加载)。"""
    return {
        ins["params"]["name"]
        for ins in rpn
        if isinstance(ins, dict)
        and ins.get("op") == "__feat__"
        and ins.get("params", {}).get("name")
    }


def _daily_returns(close: np.ndarray) -> np.ndarray:
    """close 面板 → 日收益面板(S,T),首列 NaN。"""
    import numpy as np

    ret = np.full_like(close, np.nan, dtype=np.float64)
    ret[:, 1:] = close[:, 1:] / close[:, :-1] - 1.0
    return ret


def _top_idx_by_signal(factor: np.ndarray, top_n: int) -> np.ndarray:
    """最后一个有效截面(有效股票 >= top_n)按信号值降序取 top_n 的全局下标。"""
    import numpy as np

    n_t = factor.shape[1]
    for t in range(n_t - 1, -1, -1):
        x = factor[:, t]
        mask = np.isfinite(x)
        if mask.sum() >= top_n:
            order = np.argsort(x[mask])
            idx = np.where(mask)[0]
            return idx[order[-top_n:]]
    return np.array([], dtype=np.int64)


def _estimate_mu_cov(daily_ret: np.ndarray, top_idx: np.ndarray, horizon: int):
    """top 股票日收益 → (期望收益 μ, 协方差 Σ)。

    窗口 = 最近 max(60, horizon*21) 交易日(约 horizon 个月);
    NaN(停牌/上市前)按该股有效收益均值填充(均值填充不改变方差估计期望)。
    """
    import numpy as np

    win = max(60, int(horizon) * 21)
    start = max(0, daily_ret.shape[1] - win)
    sub = daily_ret[np.asarray(top_idx), start:]
    means = np.nanmean(sub, axis=1)
    means = np.where(np.isfinite(means), means, 0.0)  # 全 NaN 行(新股)按 0
    filled = np.where(np.isfinite(sub), sub, means[:, None])
    cov = np.cov(filled)
    return means, cov


def _weights_output(codes, top_idx: np.ndarray, w) -> list[dict]:
    return [
        {"code": str(codes[i]) if codes else f"#{i}", "weight": round(float(wi), 6)}
        for i, wi in zip(top_idx, w)
    ]


@router.post("/optimize", response_model=OptimizeResponse)
def alpha_optimize(payload: dict):
    """组合优化:表达式信号选 top N → 收益协方差 → 均值方差/风险平价/最小方差。

    payload: {expression, dataset_id, horizon=5, method='mv'|'risk_parity'|'min_var',
              max_weight=0.1, risk_aversion=1.0(仅 mv), top_n=20}
    返回: {weights:[{code,weight}], perf:{annual_return,volatility,sharpe},
           turnover, method, max_weight, top_n}(同步执行)。
    """
    import numpy as np
    from ..core.datasets import load_panel
    from ..lib.alpha.operators import compile_rpn, evaluate_rpn
    from ..lib.alpha.optimize import (
        mean_variance,
        min_variance,
        portfolio_metrics,
        risk_parity,
        turnover_vs_equal,
    )

    _init_backend_locked()  # rank/ts 算子依赖后端(mlx/numpy),与 runner handler 同规则
    expression = str(payload.get("expression", "")).strip()
    if not expression:
        raise HTTPException(400, "expression 不能为空")
    try:
        rpn = compile_rpn(expression)
    except Exception as e:
        raise HTTPException(400, f"表达式解析失败: {str(e)[:150]}")
    ds = _int_param(payload, "dataset_id", 0)
    if ds <= 0:
        raise HTTPException(400, "dataset_id 无效")
    need = sorted(_rpn_feature_names(rpn) | {"close"})
    resp = _panel_or_202(ds, features=need)
    if resp is not None:
        return resp
    with _compute_gate():
        panel_info = load_panel(ds, features=need)
        if panel_info is None:
            raise HTTPException(400, "数据集未构建或无数据，请先构建数据集后重试")
        panel = panel_info["panel"]
        codes = panel_info.get("codes") or []
        factor = evaluate_rpn(rpn, panel)
        close = panel["close"]
        n_s, n_t = close.shape

        method = str(payload.get("method", "mv"))
        if method not in ("mv", "risk_parity", "min_var"):
            raise HTTPException(400, f"method 不受支持: {method}")
        top_n = max(2, min(200, _int_param(payload, "top_n", 20)))
        max_weight = _float_param(payload, "max_weight", 0.1)
        if not np.isfinite(max_weight) or max_weight <= 0:
            raise HTTPException(400, "max_weight 必须为正数")
        if max_weight * top_n < 1.0:
            raise HTTPException(
                400,
                f"max_weight={max_weight} 与 top_n={top_n} 不可行(上限×数量需 >= 1)",
            )

        top_idx = _top_idx_by_signal(factor, top_n)
        if len(top_idx) == 0:
            raise HTTPException(
                400, f"数据集信号有效股票数不足 {top_n}，请换数据集或调小 top_n"
            )
        horizon = max(1, min(250, _int_param(payload, "horizon", 5)))
        daily_ret = _daily_returns(close)
        mu, cov = _estimate_mu_cov(daily_ret, top_idx, horizon)

        if method == "mv":
            risk_aversion = _float_param(payload, "risk_aversion", 1.0)
            w = mean_variance(
                mu, cov, risk_aversion=risk_aversion, max_weight=max_weight
            )
        elif method == "risk_parity":
            w = risk_parity(cov, max_weight=max_weight)
        else:
            w = min_variance(cov, max_weight=max_weight)

        perf = portfolio_metrics(daily_ret[np.asarray(top_idx)], w)
        turnover = turnover_vs_equal(w)
        return {
            "method": method,
            "max_weight": round(float(max_weight), 6),
            "top_n": int(len(top_idx)),
            "weights": _weights_output(codes, top_idx, w),
            "perf": perf,
            "turnover": round(turnover, 6),
        }


def _combine_factor_name() -> str:
    from datetime import datetime

    return f"合成因子 {datetime.now().strftime('%H:%M:%S')}"


@router.post("/combine", response_model=CombineResponse, response_model_exclude_unset=True)
def alpha_combine(payload: dict, db: Session = Depends(get_db)):
    """多因子合成:打分秩和 / IC 加权 / 等权信号平均,可一键入因子库。

    payload: {factor_ids|exprs, dataset_id, horizon=5, method='score'|'ic_weight'|'equal',
              save_to_library=false, factor_name?}
    返回: {method, ic, ic_series, ic_positive_ratio, weights:[{name,weight}],
           combined_signal:[{code,value}], expression, factor?}(同步执行)。
    """
    import numpy as np
    from ..core.datasets import load_panel
    from ..lib.alpha.combine import (
        combine_by_equal,
        combine_by_ic_weight,
        combine_by_score,
        ic_weights_from_ic,
        rpn_expression,
    )
    from ..lib.alpha.evaluate import forward_returns, ic_series
    from ..lib.alpha.operators import compile_rpn, evaluate_rpn
    from ..lib.metrics import rank_ic

    _init_backend_locked()  # rank/ts 算子依赖后端(mlx/numpy),与 runner handler 同规则
    factor_ids = payload.get("factor_ids")
    exprs = payload.get("exprs")
    if factor_ids and exprs:
        raise HTTPException(400, "factor_ids 与 exprs 只能二选一")
    method = str(payload.get("method", "score"))
    if method not in ("score", "ic_weight", "equal"):
        raise HTTPException(400, f"method 不受支持: {method}")
    ds = _int_param(payload, "dataset_id", 0)
    if ds <= 0:
        raise HTTPException(400, "dataset_id 无效")

    names: list[str] = []
    expressions: list[str] = []
    if factor_ids:
        from ..storage.models import Factor

        ids = [int(x) for x in factor_ids if str(x).strip().lstrip("-").isdigit()]
        if not ids:
            raise HTTPException(400, "factor_ids 无效")
        factors = db.query(Factor).filter(Factor.id.in_(ids)).all()
        if len(factors) != len(set(ids)):
            raise HTTPException(400, "部分因子不存在(因子库)")
        by_id = {f.id: f for f in factors}
        for fid in ids:
            f = by_id[fid]
            if f.kind != "expr" or not (f.expression or "").strip():
                raise HTTPException(400, f"因子「{f.name}」非表达式因子,无法参与合成")
            names.append(f.name)
            expressions.append(f.expression.strip())
    else:
        raw = exprs or []
        if not isinstance(raw, list) or not raw:
            raise HTTPException(400, "exprs 不能为空(表达式列表)")
        for e in raw:
            e = str(e).strip()
            if not e:
                raise HTTPException(400, "exprs 含空表达式")
            expressions.append(e)
            names.append(e)

    rpns: list[list[dict]] = []
    need: set[str] = {"close"}
    for e in expressions:
        try:
            rpn = compile_rpn(e)
        except Exception as ex:
            raise HTTPException(400, f"表达式解析失败: {e} -> {str(ex)[:120]}")
        rpns.append(rpn)
        need |= _rpn_feature_names(rpn)
    resp = _panel_or_202(ds, features=sorted(need))
    if resp is not None:
        return resp
    with _compute_gate():
        panel_info = load_panel(ds, features=sorted(need))
        if panel_info is None:
            raise HTTPException(400, "数据集未构建或无数据，请先构建数据集后重试")
        panel = panel_info["panel"]
        codes = panel_info.get("codes") or []
        signals = [evaluate_rpn(r, panel) for r in rpns]
        close = panel["close"]
        horizon = max(1, min(250, _int_param(payload, "horizon", 5)))
        fwd = forward_returns(close, horizon)

        ics = [rank_ic(s, fwd) for s in signals]
        if method == "ic_weight":
            weights = ic_weights_from_ic(ics)
            combined = combine_by_ic_weight(signals, weights)
        else:
            weights = [1.0 / len(signals)] * len(signals)
            combined = (
                combine_by_score(signals)
                if method == "score"
                else combine_by_equal(signals)
            )

        combined_ic = rank_ic(combined, fwd)
        ic_seq = ic_series(combined, fwd)
        ics_valid = ic_seq[np.isfinite(ic_seq)]

        # 最近有效截面(有效信号 >= 2)按合成信号值降序取前 30
        sig_points: list[dict] = []
        for t in range(close.shape[1] - 1, -1, -1):
            x = combined[:, t]
            mask = np.isfinite(x)
            if mask.sum() >= 2:
                order = np.argsort(-x[mask])
                idx = np.where(mask)[0][order[:30]]
                sig_points = [
                    {
                        "code": str(codes[i]) if codes else f"#{i}",
                        "value": round(float(x[i]), 6),
                    }
                    for i in idx
                ]
                break

        expression = rpn_expression(expressions, weights, method)
        result: dict = {
            "method": method,
            "ic": float(combined_ic) if np.isfinite(combined_ic) else None,
            "ic_series": [
                round(float(v), 6) if np.isfinite(v) else None for v in ic_seq
            ],
            "ic_positive_ratio": round(float((ics_valid > 0).mean()), 4)
            if len(ics_valid) > 0
            else None,
            "weights": [
                {"name": names[i], "weight": round(float(weights[i]), 6)}
                for i in range(len(names))
            ],
            "combined_signal": sig_points,
            "expression": expression,
            "factor": None,
        }

        if bool(payload.get("save_to_library", False)):
            from ..core.factors import create_factor

            name = (
                str(payload.get("factor_name") or "").strip() or _combine_factor_name()
            )
            try:
                r = create_factor(
                    name=name,
                    expression=expression,
                    description=f"多因子合成(方法: {method}) · 合成IC={result['ic']}",
                    dataset_id=ds,
                    metrics={
                        "train_ic": result["ic"],
                        "oos_ic": result["ic"],
                        "stability": result["ic_positive_ratio"],
                        "complexity": len(compile_rpn(expression)) if expression else 0,
                    },
                )
                result["factor"] = {
                    "id": r["factor"]["id"],
                    "name": r["factor"]["name"],
                    "duplicate": False,
                }
            except ValueError as e:
                result["factor"] = {
                    "id": None,
                    "name": name,
                    "duplicate": True,
                    "message": str(e),
                }
        return result
