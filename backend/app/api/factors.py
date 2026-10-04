"""/api/v1/factors/* : 因子库与发布流程。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from .alpha import _compute_gate, _panel_or_202
from ..core.factors import (
    advance_step,
    create_factor,
    create_version,
    list_factors,
    published_factors,
    revert_step,
)

router = APIRouter(prefix="/factors", tags=["factors"])


# ===== 响应模型（T-113 契约硬化：P2-29 阶段三，逐路径契约） =====
# 字段与 core/factors 各序列化函数（factor_dict/version_dict/published_factors）返回键
# 逐一核对，缺字段=response_model 裁剪响应=前端数据缺失。
# 数值/布尔列 SQLite 弱类型可能为 NULL（旧行/内存置空），Optional 兜底避免 500。

class FactorRow(BaseModel):
    """因子头部（factor_dict 快照；列表项与创建响应共用）。"""

    id: int
    name: str
    expression: str
    kind: str
    latex: str
    description: str
    status: str
    dataset_id: int
    created_at: Optional[str] = None


class FactorVersionRow(BaseModel):
    """因子版本（version_dict 快照；数值指标列可 NULL，Optional 兜底）。"""

    id: int
    factor_id: int
    version: int
    expression: str
    model_ref: Optional[int] = None
    latex: str
    complexity: Optional[int] = None
    train_ic: Optional[float] = None
    train_rank_ic: Optional[float] = None
    val_ic: Optional[float] = None
    val_rank_ic: Optional[float] = None
    oos_ic: Optional[float] = None
    oos_rank_ic: Optional[float] = None
    return_annual: Optional[float] = None
    turnover: Optional[float] = None
    stability: Optional[float] = None
    oos_verified: Optional[bool] = None
    stability_checked: Optional[bool] = None
    complexity_checked: Optional[bool] = None
    version_released: Optional[bool] = None
    human_approved: Optional[bool] = None
    status: str
    note: Optional[str] = None
    created_at: Optional[str] = None
    approved_at: Optional[str] = None


class FactorListItem(FactorRow):
    """因子库列表项（factor_dict + 版本列表，版本按 version 降序）。"""

    versions: list[FactorVersionRow]


class FactorListResponse(BaseModel):
    data: list[FactorListItem]


class CreateFactorResponse(BaseModel):
    """POST /factors：{factor: 因子头部, version: v1 草稿版本}。"""

    factor: FactorRow
    version: FactorVersionRow


class PublishedFactorRow(BaseModel):
    """GET /factors/published 单行（已发布版本，供市场信号系统引用）。"""

    version_id: int
    factor_id: int
    name: str
    expression: str
    version: int
    oos_ic: Optional[float] = None
    stability: Optional[float] = None
    complexity: Optional[int] = None
    approved_at: Optional[str] = None


class PublishedFactorsResponse(BaseModel):
    data: list[PublishedFactorRow]


class CorrelationCluster(BaseModel):
    """冗余聚类单组（成员为因子名，与 matrix 行列对齐）。"""

    members: list[str]


class FactorCorrelationResponse(BaseModel):
    """POST /factors/analysis/correlation：相关性矩阵 + 冗余聚类。"""

    matrix: list[list[Optional[float]]]
    names: list[str]
    clusters: list[CorrelationCluster]
    threshold: float
    horizon: int


class IcDecayResponse(BaseModel):
    """POST /factors/analysis/ic-decay：IC 衰减曲线 + 半衰期。"""

    horizons: list[int]
    ic_means: list[Optional[float]]
    half_life: Optional[int] = None


class AttributionResponse(BaseModel):
    """POST /factors/analysis/attribution：行业/市值暴露 + 残差 IC。"""

    industry_exposures: dict[str, Optional[float]]
    size_exposure: Optional[float] = None
    residual_ic: Optional[float] = None
    n_periods: int


@router.get("", response_model=FactorListResponse)
def factors():
    return {"data": list_factors()}


@router.post("", response_model=CreateFactorResponse)
def new_factor(payload: dict):
    name = payload.get("name", "未命名因子")
    kind = str(payload.get("kind", "expr") or "expr").strip().lower()
    if kind == "nn":
        return _new_nn_factor(name, payload)
    expression = payload.get("expression", "")
    # expression 为 JSON 数字/对象时 service 层 .strip() 抛 AttributeError → 500，
    # 先做类型校验返回 400
    if not isinstance(expression, str):
        raise HTTPException(400, "expression 必须是字符串")
    expression = expression.strip()
    if not expression:
        raise HTTPException(400, "表达式不能为空")
    try:
        dataset_id = int(payload.get("dataset_id", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "参数 dataset_id 必须是整数")
    return create_factor(
        name,
        expression,
        payload.get("description", ""),
        dataset_id,
        payload.get("metrics"),
    )


def _new_nn_factor(name: str, payload: dict) -> dict:
    """kind='nn' 因子创建：model_id 正整数 + NNModel 存在性校验，服务端生成伪表达式。

    expression 由服务端生成（neural_mlp(model#N)），不入参；重复创建同模型
    命中 service 层表达式去重 → 400。
    """
    from ..storage.repos.models import get_nn_model

    try:
        model_id = int(payload.get("model_id"))
    except (ValueError, TypeError):
        raise HTTPException(400, "参数 model_id 必须是正整数")
    if model_id <= 0:
        raise HTTPException(400, "参数 model_id 必须是正整数")
    model = get_nn_model(model_id)
    if model is None:
        raise HTTPException(400, "模型不存在")
    try:
        dataset_id = int(payload.get("dataset_id", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "参数 dataset_id 必须是整数")
    expression = f"neural_mlp(model#{model_id})"
    try:
        # 训练指标透传：模型 val_ic（验证集 IC，最接近样本外口径）→ oos_ic；
        # 请求体显式 metrics 优先（同键覆盖），其余键原样透传
        metrics = dict(payload.get("metrics") or {})
        if model.val_ic is not None:
            metrics.setdefault("oos_ic", float(model.val_ic))
        return create_factor(
            name,
            expression,
            payload.get("description", ""),
            dataset_id,
            metrics or None,
            kind="nn",
            model_ref=model_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/published", response_model=PublishedFactorsResponse)
def published():
    """已发布因子：市场信号系统的可选因子来源。"""
    return {"data": published_factors()}


@router.post("/{factor_id}/versions", response_model=FactorVersionRow)
def new_version(factor_id: int, payload: dict):
    expression = payload.get("expression", "")
    # 与 new_factor 同款类型校验（非 str 走 service 层会 AttributeError/类型错乱 → 500）
    if not isinstance(expression, str):
        raise HTTPException(400, "expression 必须是字符串")
    expression = expression.strip()
    if not expression:
        raise HTTPException(400, "表达式不能为空")
    return create_version(
        factor_id, expression, payload.get("metrics"), payload.get("note", "")
    )


@router.post("/versions/{version_id}/advance", response_model=FactorVersionRow)
def advance(version_id: int, payload: dict):
    step = payload.get("step", "")
    from ..core.factors import PUBLISH_STEPS

    if step not in [s[0] for s in PUBLISH_STEPS]:
        raise HTTPException(400, f"未知步骤 {step}")
    # approved 正确解析布尔：bool("false") 恒为 True（非空字符串），
    # 前端 JSON 传 "false"/"0" 时会被误判为批准；字符串按字面量解析，其余类型走真值。
    raw_approved = payload.get("approved", True)
    if isinstance(raw_approved, str):
        approved = raw_approved.strip().lower() in ("1", "true", "yes", "on")
    else:
        approved = bool(raw_approved)
    try:
        return advance_step(
            version_id,
            step,
            approved,
            payload.get("note", ""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/versions/{version_id}/revert", response_model=FactorVersionRow)
def revert(version_id: int, payload: dict):
    step = payload.get("step", "")
    from ..core.factors import PUBLISH_STEPS

    if step not in [s[0] for s in PUBLISH_STEPS]:
        raise HTTPException(400, f"未知步骤 {step}")
    try:
        return revert_step(version_id, step)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# 因子生命周期分析(T-09):相关性 / 冗余聚类 / IC 衰减 / 风格归因
# 纯计算在 lib/alpha/factor_analysis.py,端点只做面板加载与参数校验。
# ---------------------------------------------------------------------------

# 相关性端点默认:持有期 5(与全站评估默认一致)、冗余聚类阈值 0.8(相关性 ≥ 0.8 归冗余组)
_CORR_DEFAULT_HORIZON = 5
_CORR_DEFAULT_THRESHOLD = 0.8
_CORR_MAX_FACTORS = 50
_IC_DECAY_HORIZONS = [1, 3, 5, 10, 20]


def _load_panel_or_400(dataset_id: int) -> dict:
    """数据集面板加载:未构建/无数据 → 400。"""
    from ..core.datasets import load_panel

    panel_info = load_panel(dataset_id)
    if panel_info is None:
        raise HTTPException(400, "数据集未构建或无数据，请先在数据集页面构建后重试")
    return panel_info


def _compile_or_400(expression: str):
    from ..lib.alpha.operators import compile_rpn

    try:
        return compile_rpn(expression)
    except Exception as e:
        raise HTTPException(400, f"表达式解析失败: {str(e)[:150]}")


def _int_gt0(payload: dict, key: str) -> int:
    """正整数参数:缺失/非法 → 400。"""
    try:
        v = int(payload.get(key, 0))
    except (ValueError, TypeError):
        raise HTTPException(400, f"参数 {key} 必须是整数")
    if v <= 0:
        raise HTTPException(400, f"参数 {key} 必须是正整数")
    return v


@router.post("/analysis/correlation", response_model=FactorCorrelationResponse)
def factor_correlation(payload: dict):
    """因子相关性矩阵 + 冗余聚类(单链)。

    payload: {factor_ids: [int], dataset_id: int, threshold?: float, horizon?: int}
    对每个因子在其绑定/指定数据集上求 IC 序列(evaluate.ic_series 同口径),
    两两 Pearson 相关 → matrix;单链聚类(相关 ≥ threshold 归组)→ clusters。
    返回 {matrix, names, clusters, threshold}。
    """
    from ..lib.alpha.evaluate import forward_returns, ic_series
    from ..lib.alpha.factor_analysis import cluster_redundant, correlation_matrix
    from ..lib.alpha.operators import evaluate_rpn
    from ..storage.models import Factor

    ids = payload.get("factor_ids")
    if not isinstance(ids, (list, tuple)) or not ids:
        raise HTTPException(400, "factor_ids 必须是非空因子 id 列表")
    if len(ids) > _CORR_MAX_FACTORS:
        raise HTTPException(400, f"因子数量 {len(ids)} 超过上限 {_CORR_MAX_FACTORS}")
    try:
        ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(400, "factor_ids 必须全是整数")
    dataset_id = _int_gt0(payload, "dataset_id")
    try:
        threshold = float(payload.get("threshold", _CORR_DEFAULT_THRESHOLD))
    except (ValueError, TypeError):
        raise HTTPException(400, "参数 threshold 必须是数字")
    if not 0 < threshold <= 1:
        raise HTTPException(400, "threshold 必须在 (0, 1] 区间")
    try:
        horizon = int(payload.get("horizon", _CORR_DEFAULT_HORIZON))
    except (ValueError, TypeError):
        raise HTTPException(400, "参数 horizon 必须是整数")
    if horizon < 1:
        raise HTTPException(400, "horizon 必须 >= 1")

    resp = _panel_or_202(dataset_id)
    if resp is not None:
        return resp
    with _compute_gate():
        panel = _load_panel_or_400(dataset_id)["panel"]
        fwd = forward_returns(panel["close"], horizon)

        from ..storage.db import SessionLocal

        db = SessionLocal()
        try:
            rows = db.query(Factor).filter(Factor.id.in_(ids)).all()
        finally:
            db.close()
        by_id = {f.id: f for f in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise HTTPException(400, f"因子不存在: {missing}")
        factors = [by_id[i] for i in ids]
        for f in factors:
            if f.kind == "nn":
                raise HTTPException(
                    400, f"NN 因子「{f.name}」无表达式，不参与相关性分析"
                )

        ic_lists = []
        for f in factors:
            rpn = _compile_or_400(f.expression)
            vals = evaluate_rpn(rpn, panel)
            ic_lists.append(ic_series(vals, fwd))

        corr = correlation_matrix(ic_lists)
        clusters = cluster_redundant(corr, threshold)
        names = [f.name for f in factors]
        return {
            "matrix": corr.tolist(),
            "names": names,
            "clusters": [{"members": [names[i] for i in g]} for g in clusters],
            "threshold": threshold,
            "horizon": horizon,
        }


@router.post("/analysis/ic-decay", response_model=IcDecayResponse)
def factor_ic_decay(payload: dict):
    """IC 衰减曲线 + 半衰期。

    payload: {expr: str, dataset_id: int, horizons?: [int]}
    各 horizon 独立 forward_returns → IC 均值;半衰期 = |IC| 首次跌破峰值一半的 horizon。
    返回 {horizons, ic_means, half_life}。
    """
    from ..lib.alpha.factor_analysis import ic_decay

    expression = payload.get("expr", "")
    if not isinstance(expression, str) or not expression.strip():
        raise HTTPException(400, "expr 不能为空")
    dataset_id = _int_gt0(payload, "dataset_id")
    horizons = payload.get("horizons")
    if horizons is not None:
        if not isinstance(horizons, (list, tuple)) or not horizons:
            raise HTTPException(400, "horizons 必须是非空整数列表")
        try:
            horizons = [int(h) for h in horizons]
        except (ValueError, TypeError):
            raise HTTPException(400, "horizons 元素必须全是整数")
        if any(h < 1 for h in horizons):
            raise HTTPException(400, "horizons 元素必须 >= 1")
    else:
        horizons = _IC_DECAY_HORIZONS

    resp = _panel_or_202(dataset_id)
    if resp is not None:
        return resp
    with _compute_gate():
        panel = _load_panel_or_400(dataset_id)["panel"]
        try:
            return ic_decay(expression.strip(), panel, horizons=list(horizons))
        except ValueError as e:
            raise HTTPException(400, str(e))


@router.post("/analysis/attribution", response_model=AttributionResponse)
def factor_attribution(payload: dict):
    """风格归因:因子值对 [行业 one-hot + 市值] 的横截面多元回归。

    payload: {expr: str, dataset_id: int}
    返回 {industry_exposures, size_exposure, residual_ic, n_periods}:
    行业/市值暴露 = 回归系数跨期均值;残差 IC = 每期残差与未来收益的秩相关均值。
    数据集须含 industry/market_cap 特征(T-07 估值特征)。
    """
    from ..lib.alpha.evaluate import forward_returns
    from ..lib.alpha.factor_analysis import style_attribution
    from ..lib.alpha.operators import evaluate_rpn

    expression = payload.get("expr", "")
    if not isinstance(expression, str) or not expression.strip():
        raise HTTPException(400, "expr 不能为空")
    dataset_id = _int_gt0(payload, "dataset_id")

    resp = _panel_or_202(dataset_id)
    if resp is not None:
        return resp
    with _compute_gate():
        panel_info = _load_panel_or_400(dataset_id)
        panel = panel_info["panel"]
        missing = [k for k in ("industry", "market_cap") if k not in panel]
        if missing:
            raise HTTPException(
                400,
                f"数据集缺少风格特征 {missing}(需先构建含 T-07 行业/估值特征的面板)",
            )
        rpn = _compile_or_400(expression.strip())
        factor = evaluate_rpn(rpn, panel)
        fwd = forward_returns(panel["close"], _CORR_DEFAULT_HORIZON)

        # 行业编码 → 行业名(与面板构建同款编码表:_industry_code_table 按排序名编码);
        # 快照不可用/网络失败时降级为 "行业#N" 标签,不阻塞分析
        industry_names: dict[int, str] | None = None
        try:
            from ..core.datasets import _industry_code_table
            from ..core.sources import get_spot

            spot = get_spot()
            if spot is not None and not spot.empty:
                industry_names = {v: k for k, v in _industry_code_table(spot).items()}
        except Exception:
            industry_names = None

        return style_attribution(
            factor,
            fwd,
            panel["industry"],
            panel["market_cap"],
            industry_names=industry_names,
        )
