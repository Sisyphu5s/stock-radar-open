"""在线指标计算（与 K 线同源缓存，保证长度一致；NaN → None 可序列化）。"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import settings
from ..lib.timex import PERIODS, to_market_naive
from ..storage.appparams import (
    delete_app_params,
    get_app_param,
    get_app_params,
    set_app_params,
)
from ..lib.indicators.compute import compute_all, last_values
from ..storage.klines import cached_kline, get_version

router = APIRouter(prefix="/indicators", tags=["indicators"])

logger = logging.getLogger("stockradar.indicators")

VALID_PERIODS = PERIODS


# ---------------------------------------------------------------------------
# T-114 契约硬化: 响应模型（字段与各端点 return dict 逐一核对，缺字段=响应丢字段）
# ---------------------------------------------------------------------------


class CatalogOverlay(BaseModel):
    """主图叠加 / 副图指标目录项。"""

    key: str
    name: str


class ParamSpecModel(BaseModel):
    """单个可调参数契约（int 保持 int、float 保持 float，逐字节）。"""

    key: str
    label: str
    type: str
    default: int | float
    min: int | float
    max: int | float
    step: int | float


class IndicatorSpecModel(BaseModel):
    """具名多参数指标契约：params 参数定义 + defaults + candidates 完整候选网格。"""

    key: str
    name: str
    params: list[ParamSpecModel]
    defaults: dict[str, int | float]
    candidates: list[dict[str, int | float]]


class StoredParams(BaseModel):
    """已保存的指标参数文档（ind:v2:{indicator} JSON）。"""

    values: dict[str, int | float]
    source: str
    target: str
    horizon: int


class CatalogResponse(BaseModel):
    main_overlays: list[CatalogOverlay]
    sub_indicators: list[CatalogOverlay]
    tuneable: list[str]
    specs: list[IndicatorSpecModel]
    targets: dict[str, str]


class ParamsResponse(BaseModel):
    specs: list[IndicatorSpecModel]
    targets: dict[str, str]
    defaults: dict[str, dict[str, int | float]]
    global_: Optional[dict[str, StoredParams]] = Field(default=None, alias="global")
    legacy: dict[str, str]
    effective: dict[str, dict[str, int | float]]


class SaveParamsResponse(BaseModel):
    ok: bool
    indicator: str
    params: dict[str, int | float]
    stored: StoredParams


class DeleteParamsResponse(BaseModel):
    ok: bool
    indicator: str
    deleted: int
    defaults: dict[str, int | float]


class TuneHistoryRow(BaseModel):
    """GET /tune-history data[] 单条调优记忆。"""

    id: int
    indicator: str
    target: str
    horizon: int
    best_param: str
    best_ic: float
    results: dict[str, Any]
    created_at: Optional[str] = None


class TuneHistoryResponse(BaseModel):
    data: list[TuneHistoryRow]


class TuneGlobalResponse(BaseModel):
    """GET /tune/global：legacy 原始行 + 每指标最新生效参数 + v2 新契约。"""

    data: dict[str, str]
    effective: dict[str, str]
    v2: dict[str, StoredParams]


class TuneGlobalPutResponse(BaseModel):
    ok: bool
    saved: list[str]


class IndicatorsResponse(BaseModel):
    """GET /{code}：指标序列；空 K 线路径不含 effective_params（exclude_unset 丢弃）。"""

    code: str
    dates: list[str]
    indicators: dict[str, list[float | None]]
    latest: dict[str, float | None]
    effective_params: Optional[dict[str, dict[str, int | float]]] = None


class RiskResponse(BaseModel):
    """GET /{code}/risk：风险指标（risk_metrics 样本不足 30 时返回空 dict，指标键可缺失）。"""

    code: str
    periods: Optional[int] = None
    window: Optional[int] = None
    ret_series: Optional[list[float]] = None
    annual_return: Optional[float] = None
    annual_volatility: Optional[float] = None
    ewma_volatility: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    ulcer_index: Optional[float] = None
    downside_deviation: Optional[float] = None
    max_drawdown: Optional[float] = None
    drawdown_recovery_days: Optional[int] = None
    avg_drawdown: Optional[float] = None
    skewness: Optional[float] = None
    kurtosis: Optional[float] = None
    win_rate: Optional[float] = None
    max_loss_streak: Optional[int] = None
    var95_param: Optional[float] = None
    var99_param: Optional[float] = None
    var95_cf: Optional[float] = None
    var99_cf: Optional[float] = None
    tail_risk: Optional[float] = None
    var_pct_budget: Optional[float] = None
    beta: Optional[float] = None
    alpha: Optional[float] = None
    var95: Optional[float] = None
    var99: Optional[float] = None
    cvar95: Optional[float] = None
    cvar99: Optional[float] = None
    explanations: dict[str, str]


class TuneJobResponse(BaseModel):
    """POST /{code}/tune 与 /{code}/tune/combined 提交响应（后台任务）。"""

    job_id: int
    status: str


class LatestResponse(BaseModel):
    code: str
    latest: dict[str, float | None]


def _clean(v):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v


@router.get("/catalog", response_model=CatalogResponse)
def catalog():
    """指标目录（前端选择器用）：主图叠加 / 副图指标 + 具名多参数契约（specs）。"""
    from ..core.indicators.tune import SPECS, TARGETS, TUNEABLE

    return {
        "main_overlays": [
            {"key": "ma5", "name": "MA5"},
            {"key": "ma10", "name": "MA10"},
            {"key": "ma20", "name": "MA20"},
            {"key": "ma30", "name": "MA30"},
            {"key": "ma60", "name": "MA60"},
            {"key": "ma120", "name": "MA120"},
            {"key": "ma250", "name": "MA250"},
            {"key": "ema12", "name": "EMA12"},
            {"key": "ema26", "name": "EMA26"},
            {"key": "expma12", "name": "EXPMA12"},
            {"key": "expma50", "name": "EXPMA50"},
            {"key": "boll", "name": "BOLL"},
            {"key": "sar", "name": "SAR"},
        ],
        "sub_indicators": [
            {"key": "macd", "name": "MACD"},
            {"key": "kdj", "name": "KDJ"},
            {"key": "rsi", "name": "RSI(6,12,24)"},
            {"key": "wr", "name": "WR(10,6)"},
            {"key": "dmi", "name": "DMI(14,6)"},
            {"key": "cci", "name": "CCI(14)"},
            {"key": "roc", "name": "ROC(12)"},
            {"key": "mtm", "name": "MTM(12,6)"},
            {"key": "obv", "name": "OBV"},
            {"key": "bias", "name": "BIAS(6,12,24)"},
            {"key": "psy", "name": "PSY(12)"},
            {"key": "trix", "name": "TRIX(12,9)"},
            {"key": "vr", "name": "VR(26)"},
            {"key": "atr", "name": "ATR(14)"},
            {"key": "cmo", "name": "CMO(14)"},
            {"key": "dpo", "name": "DPO(20)"},
            {"key": "emv", "name": "EMV(14,9)"},
        ],
        "tuneable": list(TUNEABLE),
        "specs": [s.to_dict() for s in SPECS.values()],
        "targets": TARGETS,
    }


@router.get("/params", response_model=ParamsResponse)
def indicator_params():
    """指标参数契约：specs 白名单 + 全局（v2 JSON）+ 生效参数（v2 > legacy > 默认）。"""
    from ..core.indicators.tune import SPECS, TARGETS

    return {
        "specs": [s.to_dict() for s in SPECS.values()],
        "targets": TARGETS,
        "defaults": {ind: s.defaults() for ind, s in SPECS.items()},
        "global": _load_v2_globals(),
        "legacy": get_app_params("ind:"),
        "effective": _effective_all(),
    }


@router.put("/params/{indicator}", response_model=SaveParamsResponse)
def put_indicator_params(indicator: str, payload: dict):
    """保存指标参数为全局默认（AppParam 键 ind:v2:{indicator}，JSON 持久化）。

    payload: {values: {具名参数}, source: manual|global|default, target, horizon}。
    严格校验：指标白名单 / 完整键集 / 数值类型 / min/max/step。
    """
    from ..core.indicators.tune import SPECS, TARGETS, validate_params

    if indicator not in SPECS:
        raise HTTPException(400, f"未知指标 {indicator}，可选: {list(SPECS)}")
    values = payload.get("values")
    try:
        params = validate_params(indicator, values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    source = payload.get("source", "global")
    if source not in ("manual", "global", "default"):
        raise HTTPException(400, "source 必须为 manual/global/default")
    target = payload.get("target", "ic")
    if target not in TARGETS:
        raise HTTPException(400, f"不支持的调优目标 {target}，可选: {list(TARGETS)}")
    horizon = payload.get("horizon", 5)
    if isinstance(horizon, bool) or not isinstance(horizon, (int, str)):
        raise HTTPException(400, "horizon 必须为整数")
    try:
        horizon = int(horizon)
    except (TypeError, ValueError):
        raise HTTPException(400, "horizon 必须为整数")
    if not (1 <= horizon <= 250):
        raise HTTPException(400, "horizon 必须在 1..250")
    doc = {"values": params, "source": source, "target": target, "horizon": horizon}
    set_app_params({f"ind:v2:{indicator}": json.dumps(doc, ensure_ascii=False)})
    return {"ok": True, "indicator": indicator, "params": params, "stored": doc}


@router.delete("/params/{indicator}", response_model=DeleteParamsResponse)
def delete_indicator_params(indicator: str):
    """删除全局参数（恢复系统默认）。"""
    from ..core.indicators.tune import SPECS

    if indicator not in SPECS:
        raise HTTPException(400, f"未知指标 {indicator}，可选: {list(SPECS)}")
    deleted = delete_app_params([f"ind:v2:{indicator}"])
    return {
        "ok": True,
        "indicator": indicator,
        "deleted": deleted,
        "defaults": SPECS[indicator].defaults(),
    }


@router.get("/tune-history", response_model=TuneHistoryResponse)
def tune_history(code: str, indicator: str | None = None):
    """指标调优历史（按股票记忆，只读：新调优不再写入）。"""
    from ..storage.repos.indicators import indicator_tune_history

    rows = indicator_tune_history(code, indicator)
    return {
        "data": [
            {
                "id": r.id,
                "indicator": r.indicator,
                "target": r.target,
                "horizon": r.horizon,
                "best_param": r.best_param,
                "best_ic": r.best_ic,
                "results": r.results,
                "created_at": to_market_naive(r.created_at).isoformat()
                if r.created_at
                else None,
            }
            for r in rows
        ]
    }


def _parse_global_param(value: str):
    """全局参数值字符串 → custom 值：'6' → 6；'12,26,9' → [12, 26, 9]；'20,2.5' → [20, 2.5]。"""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return int(nums[0]) if nums[0].is_integer() else nums[0]
    return [int(n) if n.is_integer() else n for n in nums]


def _effective_global_params() -> dict:
    """AppParam 中 ind:{indicator}:{target} 行 → 每指标取最新（updated_at 最大）一个参数值。"""
    from ..storage.repos.params import latest_indicator_params

    return latest_indicator_params()


def _load_v2_globals() -> dict:
    """AppParam 中 ind:v2:{indicator} 行 → {indicator: {values, source, target, horizon}}（JSON 解析）。"""
    from ..core.indicators.tune import SPECS

    out: dict = {}
    for k, v in get_app_params("ind:v2:").items():
        ind = k.split(":", 2)[-1] if k.count(":") >= 2 else ""
        if ind not in SPECS:
            continue
        try:
            doc = json.loads(v)
        except (TypeError, ValueError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("values"), dict):
            continue
        out[ind] = doc
    return out


def _load_v2_global(indicator: str) -> dict | None:
    """单个指标的 v2 全局参数（ind:v2:{indicator} JSON 解析），无则 None。"""
    from ..core.indicators.tune import SPECS

    if indicator not in SPECS:
        return None
    raw = get_app_param(f"ind:v2:{indicator}")
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return (
        doc if isinstance(doc, dict) and isinstance(doc.get("values"), dict) else None
    )


def _resolve_effective(indicator: str, v2_all: dict, legacy_all: dict) -> dict | None:
    """生效具名参数：v2 global > legacy global；均无返回 None（调用方回退 defaults）。"""
    from ..core.indicators.tune import to_param_obj

    if indicator in v2_all:
        return dict(v2_all[indicator]["values"])
    legacy = legacy_all.get(indicator)
    if legacy:
        parsed = _parse_global_param(legacy)
        obj = to_param_obj(indicator, parsed) if parsed is not None else None
        if obj is not None:
            return obj
    return None


def _effective_all() -> dict:
    """全部可调指标的生效参数：v2 global > legacy global > spec 默认。"""
    from ..core.indicators.tune import SPECS

    v2_all = _load_v2_globals()
    legacy_all = _effective_global_params()
    out: dict = {}
    for ind, spec in SPECS.items():
        obj = _resolve_effective(ind, v2_all, legacy_all)
        out[ind] = obj if obj is not None else spec.defaults()
    return out


@router.get("/tune/global", response_model=TuneGlobalResponse)
def tune_global():
    """指标调优全局参数：{data: {ind:rsi:ic: '6', ...}} + effective（每指标最新一个参数）+ v2（新契约）。"""
    return {
        "data": get_app_params("ind:"),
        "effective": _effective_global_params(),
        "v2": _load_v2_globals(),
    }


@router.put("/tune/global", response_model=TuneGlobalPutResponse)
def tune_global_put(payload: dict):
    """保存/更新指标调优全局参数：{"params": {"ind:rsi:ic": "6", ...}}（upsert AppParam）。

    按 SPECS 严格校验键（ind:{指标}:{目标} 白名单）与值（可解析且参数个数匹配），
    空 params / 非法键一律 400——坏写入会以最新 updated_at 覆盖生效参数，
    静默丢弃已保存的调优结果。
    """
    from ..core.indicators.tune import SPECS, TARGETS, to_param_obj

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(400, "params 必须为 dict")
    if not params:
        raise HTTPException(400, "params 不能为空")
    for k, v in params.items():
        if not isinstance(k, str):
            raise HTTPException(400, "参数键必须为字符串")
        parts = k.split(":")
        if len(parts) != 3 or parts[0] != "ind":
            raise HTTPException(
                400, f"非法参数键 {k}，格式必须为 ind:{{指标}}:{{目标}}"
            )
        ind, tgt = parts[1], parts[2]
        if ind not in SPECS:
            raise HTTPException(400, f"未知指标 {ind}，可选: {list(SPECS)}")
        if tgt not in TARGETS:
            raise HTTPException(400, f"不支持的调优目标 {tgt}，可选: {list(TARGETS)}")
        parsed = _parse_global_param(str(v))
        if parsed is None or to_param_obj(ind, parsed) is None:
            raise HTTPException(400, f"参数值 {v!r} 无法解析为 {ind} 的参数")
    saved = set_app_params(params)
    return {"ok": True, "saved": saved}


@router.get("/{code}", response_model=IndicatorsResponse, response_model_exclude_unset=True)
def indicators(
    code: str,
    days: int = Query(300, ge=1, le=settings.max_indicator_days),
    period: str = "daily",
    fields: str = "ma5,ma10,ma20,ma60,boll,macd,rsi,kdj,volume_ratio",
    custom: str | None = None,
):
    """按股票计算指标序列（与 /stocks/{code}/kline 同源缓存，长度一致）。custom 为 JSON 字符串，透传自定义指标参数。

    未传 custom 时自动合并全局已保存调优参数（AppParam 中 ind:{indicator}:{target} 每指标最新值），
    使调优结果全局生效。
    """
    if period not in VALID_PERIODS:
        raise HTTPException(400, f"指标仅支持 {VALID_PERIODS} 周期")
    from ..storage.cache import indicator_cache

    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    custom_dict = None
    if custom:
        try:
            custom_dict = json.loads(custom)
            if not isinstance(custom_dict, dict):
                custom_dict = None
        except json.JSONDecodeError:
            custom_dict = None
    # 生效参数优先级：manual(请求 custom) > v2 global(ind:v2:) > legacy global(ind:{ind}:{target}) > 默认
    # manual 原始值全部透传 compute_all（含 ma 列表等旧格式），其余指标叠加全局参数
    from ..core.indicators.tune import SPECS, to_param_obj

    v2_all = _load_v2_globals()
    legacy_all = _effective_global_params()
    merged: dict = dict(custom_dict) if custom_dict else {}
    effective: dict = {}
    for ind in SPECS:
        obj = None
        if ind in merged:
            obj = to_param_obj(ind, merged[ind])
        if obj is None and ind in v2_all:
            obj = dict(v2_all[ind]["values"])
        if obj is None and ind in legacy_all:
            parsed = _parse_global_param(legacy_all[ind])
            obj = to_param_obj(ind, parsed) if parsed is not None else None
        if obj is not None:
            effective[ind] = obj
        if ind not in merged and obj is not None:
            merged[ind] = obj
    custom_dict = merged if merged else None
    ver = get_version(code)
    custom_key = json.dumps(custom_dict, sort_keys=True) if custom_dict else ""
    # 字段顺序无关的缓存 key（P2-35）：排序归一，字段乱序请求命中同一缓存
    key = f"v{ver}:ind2:{code}:{period}:{days}:{','.join(sorted(field_list))}:{custom_key}"
    cached = indicator_cache.get(key)
    if cached is not None:
        return cached
    k = cached_kline(code, period, max_rows=days)
    if k is None or k.empty:
        return {"code": code, "dates": [], "indicators": {}, "latest": {}}
    k = k.reset_index(drop=True)
    ind = compute_all(k, field_list, custom_dict)
    ind = {name: [_clean(v) for v in arr] for name, arr in ind.items()}
    result = {
        "code": code,
        "dates": k["date"].tolist(),
        "indicators": ind,
        "latest": {name: _clean(arr[-1]) if arr else None for name, arr in ind.items()},
        "effective_params": effective,
    }
    indicator_cache.set(key, result)
    return result


@router.get("/{code}/risk", response_model=RiskResponse, response_model_exclude_unset=True)
def risk(code: str, days: int | None = None):
    """风险指标（基于日收益序列，Beta 相对沪深300；days=None 用全部数据）。"""
    from fastapi import HTTPException
    import numpy as np
    from ..storage.cache import risk_cache
    from ..core.indicators.risk import RISK_EXPLANATIONS, risk_metrics
    from ..core.sources import get_sina_kline

    k = cached_kline(code, "daily", max_rows=days if days is not None else 5000)
    if k is None or k.empty:
        raise HTTPException(404, f"{code} K 线不可用")
    ver = get_version(code)
    key = f"v{ver}:risk:{code}:{days}"
    cached = risk_cache.get(key)
    if cached is not None:
        return cached
    # 基准：沪深300 日线（新浪指数接口；固定新浪源，复用 quote 单例，避免每次请求重建实例）
    bench = None
    try:
        bk = get_sina_kline("000300.SH", "daily", days=days)
        if bk is not None and len(bk):
            # 按日期对齐基准：每个股票交易日取对应基准收盘，缺失日期记 NaN。
            # 原实现取基准尾部 len(k) 根——停牌/缺口时股票尾部与基准尾部日期不对应，
            # 导致 Beta/Alpha 按位置错位
            bench_by_date = dict(zip(bk["date"].astype(str), bk["close"].to_numpy()))
            bench = np.asarray(
                [bench_by_date.get(d, np.nan) for d in k["date"].astype(str)],
                dtype=np.float64,
            )
    except Exception:
        bench = None
    data = risk_metrics(
        k.reset_index(drop=True)["close"].to_numpy(), bench_close=bench, window=days
    )
    result = {"code": code, **data, "explanations": RISK_EXPLANATIONS}
    risk_cache.set(key, result)
    return result


@router.post("/{code}/tune", response_model=TuneJobResponse)
def tune(code: str, payload: dict):
    """指标调优（后台任务，P1-38b）。payload: {indicator, horizon, target, algorithm?}；
    algorithm: grid（全量网格，默认）/ fast（1/3 间隔采样）/ random（随机 ≤200）。
    参数校验同步执行（400/K 线 404）；调优计算提交任务队列（与 alpha factor_tune
    同机制），返回 {job_id, status='pending'}，结果经 GET /experiments/{job_id}
    轮询：job.result 即原同步契约结果（results/best/spec/defaults/current/global/
    targets/global_saved）。全局参数通过 PUT /params/{indicator} 保存。"""
    from fastapi import HTTPException
    from ..core.indicators.tune import SPECS, TARGETS
    from ..core.tasks.errors import QueueFullError
    from ..core.tasks.runner import submit

    indicator = payload.get("indicator", "")
    if indicator not in SPECS:
        raise HTTPException(400, f"不支持指标 {indicator}")
    k = cached_kline(code, "daily", max_rows=500)
    if k is None or k.empty:
        raise HTTPException(404, f"{code} K 线不可用")
    horizon = payload.get("horizon", 5)
    if isinstance(horizon, bool) or not isinstance(horizon, (int, str)):
        raise HTTPException(400, "horizon 必须为整数")
    try:
        horizon = int(horizon)
    except (TypeError, ValueError):
        raise HTTPException(400, "horizon 必须为整数")
    if not (1 <= horizon <= 250):
        raise HTTPException(400, "horizon 必须在 1..250")
    target = payload.get("target", "ic")
    if target not in TARGETS:
        raise HTTPException(400, f"不支持的调优目标 {target}，可选: {list(TARGETS)}")
    algorithm = payload.get("algorithm", "grid")
    if algorithm not in ("grid", "fast", "random"):
        raise HTTPException(
            400, f"不支持的优化算法 {algorithm}，可选: grid/fast/random"
        )
    try:
        job_id = submit(
            "indicator_tune",
            {
                "code": code,
                "indicator": indicator,
                "horizon": horizon,
                "target": target,
                "algorithm": algorithm,
            },
        )
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job_id, "status": "pending"}


def _run_indicator_tune(job_id: int, params: dict) -> None:
    """指标调优后台任务处理器（注册进 core.tasks.registry，由 runner.submit 分派）。

    网格搜索在 worker 线程执行，不占用请求线程（P1-38b）；kline 在 worker 内重取
    （与请求同源缓存）；缓存命中/未命中均落 job.result，契约与原同步响应一致。
    """
    from ..core.tasks.runner import _start_running, _terminal, _track, _checkpoint
    from ..core.tasks.errors import JobCancelled
    from ..storage.models import utcnow

    try:
        if not _start_running(job_id, 2.0):
            return  # 已被取消
        _track(job_id, 900)
        from ..core.indicators.tune import TARGETS, tune_indicator
        from ..storage.cache import tune_cache
        from ..storage.repos.params import app_param_exists

        indicator = str(params.get("indicator", ""))
        code = str(params.get("code", ""))
        horizon = int(params.get("horizon", 5))
        target = str(params.get("target", "ic"))
        algorithm = str(params.get("algorithm", "grid"))
        k = cached_kline(code, "daily", max_rows=500)
        if k is None or k.empty:
            _terminal(
                job_id,
                status="failed",
                error=f"{code} K 线不可用",
                finished_at=utcnow(),
            )
            return
        ver = get_version(code)
        tkey = f"v{ver}:tune2:{code}:{indicator}:{target}:{horizon}:{algorithm}"
        # 全局记忆标记：ind:{indicator}:{target}（legacy）或 ind:v2:{indicator}（新契约）
        # 是否已保存全局默认参数（缓存命中也要实时计算）
        saved = app_param_exists(f"ind:{indicator}:{target}") or app_param_exists(
            f"ind:v2:{indicator}"
        )
        cached = tune_cache.get(tkey)
        if cached is not None:
            # 缓存命中也要实时计算：current/global 随全局参数变化，不能返回缓存时的旧值
            v2_all = _load_v2_globals()
            legacy_all = _effective_global_params()
            current = _resolve_effective(indicator, v2_all, legacy_all)
            result = {
                **cached,
                "targets": TARGETS,
                "global_saved": saved,
                "current": current if current is not None else cached.get("defaults"),
                "global": v2_all.get(indicator),
            }
        else:
            result = tune_indicator(
                indicator,
                k.reset_index(drop=True),
                horizon=horizon,
                target=target,
                algorithm=algorithm,
                cancel_check=lambda: _checkpoint(job_id),
            )
            v2_all = _load_v2_globals()
            legacy_all = _effective_global_params()
            current = _resolve_effective(indicator, v2_all, legacy_all)
            result["current"] = current if current is not None else result["defaults"]
            result["global"] = v2_all.get(indicator)
            result["targets"] = TARGETS
            result["global_saved"] = saved
            tune_cache.set(tkey, result)
        _terminal(
            job_id, status="done", progress=100.0, result=result, finished_at=utcnow()
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except ValueError as e:
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
    except Exception as e:
        logger.exception("指标调优任务 %s 失败", job_id)
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())


@router.post("/{code}/tune/combined", response_model=TuneJobResponse)
def tune_combined(code: str, payload: dict):
    """整体调优（多指标参数组合联合评分，后台任务，P1-38b）。payload: {indicators:
    list[str], horizon, target, algorithm?}；indicators 为 2~3 个白名单指标。每组合 =
    各指标候选参数 → z-score 标准化等权平均 → 同一评分逻辑。参数校验同步执行（400）；
    计算提交任务队列，返回 {job_id, status='pending'}，结果经 GET /experiments/{job_id}
    轮询：job.result 即原契约结果（缓存键独立于单指标；命中时实时计算 current/global）。"""
    from fastapi import HTTPException
    from ..core.indicators.tune import SPECS, TARGETS
    from ..core.tasks.errors import QueueFullError
    from ..core.tasks.runner import submit

    indicators = payload.get("indicators")
    if not isinstance(indicators, list):
        raise HTTPException(400, "indicators 必须为 list")
    cleaned: list[str] = []
    for ind in indicators:
        if not isinstance(ind, str) or ind not in SPECS:
            raise HTTPException(400, f"不支持指标 {ind}")
        if ind not in cleaned:
            cleaned.append(ind)
    if not (2 <= len(cleaned) <= 3):
        raise HTTPException(400, f"整体调优需要 2~3 个指标,当前 {len(cleaned)}")
    k = cached_kline(code, "daily", max_rows=500)
    if k is None or k.empty:
        raise HTTPException(404, f"{code} K 线不可用")
    horizon = payload.get("horizon", 5)
    if isinstance(horizon, bool) or not isinstance(horizon, (int, str)):
        raise HTTPException(400, "horizon 必须为整数")
    try:
        horizon = int(horizon)
    except (TypeError, ValueError):
        raise HTTPException(400, "horizon 必须为整数")
    if not (1 <= horizon <= 250):
        raise HTTPException(400, "horizon 必须在 1..250")
    target = payload.get("target", "ic")
    if target not in TARGETS:
        raise HTTPException(400, f"不支持的调优目标 {target}，可选: {list(TARGETS)}")
    algorithm = payload.get("algorithm", "grid")
    if algorithm not in ("grid", "fast", "random"):
        raise HTTPException(
            400, f"不支持的优化算法 {algorithm}，可选: grid/fast/random"
        )
    try:
        job_id = submit(
            "indicator_tune_combined",
            {
                "code": code,
                "indicators": cleaned,
                "horizon": horizon,
                "target": target,
                "algorithm": algorithm,
            },
        )
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job_id, "status": "pending"}


def _run_indicator_tune_combined(job_id: int, params: dict) -> None:
    """整体调优后台任务处理器（注册进 core.tasks.registry，由 runner.submit 分派）。"""
    from ..core.tasks.runner import _start_running, _terminal, _track, _checkpoint
    from ..core.tasks.errors import JobCancelled
    from ..storage.models import utcnow

    try:
        if not _start_running(job_id, 2.0):
            return  # 已被取消
        _track(job_id, 900)
        from ..core.indicators.tune import TARGETS, tune_indicators_combined
        from ..storage.cache import tune_cache
        from ..storage.repos.params import app_param_exists

        indicators = [str(x) for x in params.get("indicators", [])]
        code = str(params.get("code", ""))
        horizon = int(params.get("horizon", 5))
        target = str(params.get("target", "ic"))
        algorithm = str(params.get("algorithm", "grid"))
        k = cached_kline(code, "daily", max_rows=500)
        if k is None or k.empty:
            _terminal(
                job_id,
                status="failed",
                error=f"{code} K 线不可用",
                finished_at=utcnow(),
            )
            return
        ver = get_version(code)
        tkey = (
            f"v{ver}:tune2:comb:{code}:{','.join(sorted(indicators))}:"
            f"{target}:{horizon}:{algorithm}"
        )
        # 全局记忆标记：任一指标 ind:{ind}:{target}（legacy）或 ind:v2:{ind}（新契约）
        # 已保存全局默认参数（缓存命中也要实时计算）
        saved = any(
            app_param_exists(f"ind:{ind}:{target}") or app_param_exists(f"ind:v2:{ind}")
            for ind in indicators
        )
        cached = tune_cache.get(tkey)
        if cached is not None:
            # 缓存命中也要实时计算：current/global 随全局参数变化，不能返回缓存时的旧值
            v2_all = _load_v2_globals()
            legacy_all = _effective_global_params()
            current = {
                ind: (
                    _resolve_effective(ind, v2_all, legacy_all)
                    or cached["defaults"][ind]
                )
                for ind in indicators
            }
            result = {
                **cached,
                "targets": TARGETS,
                "global_saved": saved,
                "current": current,
                "global": {ind: v2_all[ind] for ind in indicators if ind in v2_all},
            }
        else:
            result = tune_indicators_combined(
                indicators,
                k.reset_index(drop=True),
                horizon=horizon,
                target=target,
                algorithm=algorithm,
                cancel_check=lambda: _checkpoint(job_id),
            )
            v2_all = _load_v2_globals()
            legacy_all = _effective_global_params()
            current = {
                ind: (
                    _resolve_effective(ind, v2_all, legacy_all)
                    or result["defaults"][ind]
                )
                for ind in indicators
            }
            result["current"] = current
            result["global"] = {ind: v2_all[ind] for ind in indicators if ind in v2_all}
            result["targets"] = TARGETS
            result["global_saved"] = saved
            tune_cache.set(tkey, result)
        _terminal(
            job_id, status="done", progress=100.0, result=result, finished_at=utcnow()
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except ValueError as e:
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
    except Exception as e:
        logger.exception("整体调优任务 %s 失败", job_id)
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())


@router.get("/{code}/latest", response_model=LatestResponse)
def latest(code: str, days: int = 300, period: str = "daily"):
    k = cached_kline(code, period, max_rows=days)
    if k is None or k.empty:
        return {"code": code, "latest": {}}
    return {
        "code": code,
        "latest": {
            k2: _clean(v) for k2, v in last_values(k.reset_index(drop=True)).items()
        },
    }


# 任务类型注册（P1-38b）：indicator_tune / indicator_tune_combined 处理器注册进
# core.tasks.registry，runner.submit 按 job_type 分派（与 alpha factor_tune 同机制）。
# 注册在模块加载时生效（main.py 经 core.plugins 挂载本 router 即 import 本模块）。
from ..core.tasks import registry as _task_registry  # noqa: E402

_task_registry.register("indicator_tune", _run_indicator_tune)
_task_registry.register("indicator_tune_combined", _run_indicator_tune_combined)
