"""指标调优：传统指标网格搜索参数，以预测能力（IC/RankIC）或实操质量（多空/夏普/回撤/胜率）为目标。

具名多参数契约：IndicatorSpec 白名单（覆盖全部可调优指标），每个参数含
key/label/type/default/min/max/step，candidates 为完整具名 params 对象。
例：MACD={fast:12, slow:26, signal:9}；BOLL={window:20, multiplier:2.0}。

（bt-fin 迁移：原 tune.py 全文迁入本模块；
纯计算经 lib.indicators.compute + lib.metrics 直连。）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from itertools import product
import random

import numpy as np
import pandas as pd

from ...lib.metrics import max_drawdown_signed, sharpe_ratio
from ...lib.indicators.compute import (
    bias,
    boll,
    cci,
    cmo,
    ema,
    kdj,
    ma,
    macd,
    mtm,
    psy,
    rsi,
    roc,
    trix,
    volume_ratio,
    wr,
)


@dataclass(frozen=True)
class ParamSpec:
    """单个可调参数契约。type: "int" | "float"。"""

    key: str
    label: str
    type: str  # "int" | "float"
    default: int | float
    min: int | float
    max: int | float
    step: int | float


@dataclass(frozen=True)
class IndicatorSpec:
    """具名多参数指标契约：params 参数定义 + candidates 完整具名参数候选网格。"""

    key: str
    name: str
    params: tuple[ParamSpec, ...]
    candidates: tuple[dict[str, int | float], ...]

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "params": [asdict(p) for p in self.params],
            "defaults": self.defaults(),
            "candidates": [dict(c) for c in self.candidates],
        }


# ---------------------------------------------------------------------------
# IndicatorSpec 白名单（覆盖全部可调优指标；MACD/BOLL 为多参数具名对象）
# ---------------------------------------------------------------------------

SPECS: dict[str, IndicatorSpec] = {
    "ma": IndicatorSpec(
        "ma",
        "MA 均线",
        (ParamSpec("window", "周期", "int", 5, 1, 250, 1),),
        tuple({"window": n} for n in (5, 10, 20, 30, 60, 120)),
    ),
    "ema": IndicatorSpec(
        "ema",
        "EMA 指数均线",
        (ParamSpec("window", "周期", "int", 12, 1, 250, 1),),
        tuple({"window": n} for n in (6, 12, 26, 50)),
    ),
    "rsi": IndicatorSpec(
        "rsi",
        "RSI 相对强弱",
        (ParamSpec("window", "周期", "int", 14, 2, 100, 1),),
        tuple({"window": n} for n in (6, 9, 12, 14, 21)),
    ),
    "macd": IndicatorSpec(
        "macd",
        "MACD",
        (
            ParamSpec("fast", "快线 EMA", "int", 12, 1, 120, 1),
            ParamSpec("slow", "慢线 EMA", "int", 26, 2, 250, 1),
            ParamSpec("signal", "DEA 信号", "int", 9, 1, 60, 1),
        ),
        tuple(
            {"fast": f, "slow": s, "signal": g}
            for (f, s, g) in (
                (6, 13, 5),
                (12, 26, 9),
                (8, 17, 9),
                (5, 35, 5),
                (10, 22, 7),
            )
        ),
    ),
    "kdj": IndicatorSpec(
        "kdj",
        "KDJ",
        (ParamSpec("window", "周期", "int", 9, 2, 100, 1),),
        tuple({"window": n} for n in (5, 9, 14, 21)),
    ),
    "boll": IndicatorSpec(
        "boll",
        "BOLL",
        (
            ParamSpec("window", "周期", "int", 20, 2, 250, 1),
            ParamSpec("multiplier", "带宽倍数", "float", 2.0, 0.1, 5.0, 0.1),
        ),
        tuple(
            {"window": n, "multiplier": k}
            for (n, k) in ((10, 2.0), (20, 2.0), (30, 2.0), (20, 2.5))
        ),
    ),
    "wr": IndicatorSpec(
        "wr",
        "WR 威廉",
        (ParamSpec("window", "周期", "int", 10, 2, 100, 1),),
        tuple({"window": n} for n in (6, 10, 14, 20)),
    ),
    "cci": IndicatorSpec(
        "cci",
        "CCI",
        (ParamSpec("window", "周期", "int", 14, 2, 100, 1),),
        tuple({"window": n} for n in (7, 14, 20, 34)),
    ),
    "roc": IndicatorSpec(
        "roc",
        "ROC",
        (ParamSpec("window", "周期", "int", 12, 1, 100, 1),),
        tuple({"window": n} for n in (6, 12, 24)),
    ),
    "mtm": IndicatorSpec(
        "mtm",
        "MTM",
        (ParamSpec("window", "周期", "int", 12, 1, 100, 1),),
        tuple({"window": n} for n in (6, 12, 24)),
    ),
    "bias": IndicatorSpec(
        "bias",
        "BIAS",
        (ParamSpec("window", "周期", "int", 6, 1, 100, 1),),
        tuple({"window": n} for n in (6, 12, 24)),
    ),
    "psy": IndicatorSpec(
        "psy",
        "PSY",
        (ParamSpec("window", "周期", "int", 12, 2, 100, 1),),
        tuple({"window": n} for n in (6, 12, 24)),
    ),
    "trix": IndicatorSpec(
        "trix",
        "TRIX",
        (ParamSpec("window", "周期", "int", 12, 2, 100, 1),),
        tuple({"window": n} for n in (9, 12, 18)),
    ),
    "cmo": IndicatorSpec(
        "cmo",
        "CMO",
        (ParamSpec("window", "周期", "int", 14, 2, 100, 1),),
        tuple({"window": n} for n in (7, 14, 21)),
    ),
    "volume_ratio": IndicatorSpec(
        "volume_ratio",
        "量比",
        (ParamSpec("window", "周期", "int", 5, 1, 60, 1),),
        tuple({"window": n} for n in (3, 5, 10)),
    ),
}

TARGETS = {
    "ic": "未来收益 IC（方向）",
    "ic_abs": "|IC|（方向无关）",
    "icir": "IC 均值/IC 标准差（稳健性）",
    "rank_ic": "RankIC（Spearman 秩相关）",
    "ls_annual": "多空年化（前20%做多-后20%做空）",
    "sharpe": "多空年化夏普（含空仓日，近似多空收益序列）",
    "max_drawdown": "多空序列最大回撤（越小越好，含空仓日）",
    "win_rate": "信号命中率",
    "stability": "IC 为正比例（方向稳定性）",
    "composite": "综合评分（|IC|+稳健性+稳定性+多空加权）",
}


# ---------------------------------------------------------------------------
# 具名参数工具
# ---------------------------------------------------------------------------


def spec_of(indicator: str) -> IndicatorSpec:
    if indicator not in SPECS:
        raise ValueError(f"未知指标 {indicator}，可选: {list(SPECS)}")
    return SPECS[indicator]


def param_keys(indicator: str) -> tuple[str, ...]:
    return tuple(p.key for p in spec_of(indicator).params)


def param_values(indicator: str, params: dict) -> list:
    """具名参数对象 → 按 spec 顺序的位置参数列表。"""
    return [params[k] for k in param_keys(indicator)]


def param_to_legacy(indicator: str, params: dict) -> str:
    """具名参数 → 旧 param 字符串：单参数 "6"；多参数 "6,13,5" / "20,2.0"（与旧位置数组 str 一致）。"""
    spec = spec_of(indicator)
    parts = []
    for p in spec.params:
        v = params[p.key]
        parts.append(str(int(v)) if p.type == "int" else str(float(v)))
    return parts[0] if len(parts) == 1 else ",".join(parts)


def to_param_obj(indicator: str, val) -> dict | None:
    """把旧数字 / 旧数组 / v2 具名 dict 归一为具名参数对象；非法返回 None（不校验区间）。"""
    spec = SPECS.get(indicator)
    if spec is None:
        return None
    params = spec.params
    keys = tuple(p.key for p in params)
    if isinstance(val, dict) and not isinstance(val, bool):
        if len(val) != len(keys) or any(k not in val for k in keys):
            return None
        raw = [val[k] for k in keys]
    elif isinstance(val, (list, tuple)):
        if len(val) != len(keys):
            return None
        raw = list(val)
    elif isinstance(val, (int, float)) and not isinstance(val, bool):
        if len(keys) != 1:
            return None
        raw = [val]
    else:
        return None
    out: dict = {}
    for p, v in zip(params, raw):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        if p.type == "int":
            if not float(v).is_integer():
                return None
            out[p.key] = int(v)
        else:
            out[p.key] = float(v)
    return out


def validate_params(indicator: str, values) -> dict:
    """严格校验并归一化具名参数：完整键集、数值类型、min/max/step 约束。非法抛 ValueError。"""
    spec = SPECS.get(indicator)
    if spec is None:
        raise ValueError(f"未知指标 {indicator}")
    if not isinstance(values, dict) or isinstance(values, bool):
        raise ValueError(f"{indicator} 参数必须为具名对象")
    keys = tuple(p.key for p in spec.params)
    if set(values) != set(keys):
        raise ValueError(f"{indicator} 需要完整参数 {list(keys)}")
    out: dict = {}
    for p in spec.params:
        v = values[p.key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{p.key} 必须为数值")
        if p.type == "int" and not float(v).is_integer():
            raise ValueError(f"{p.key} 必须为整数")
        fv = float(v)
        if fv < p.min or fv > p.max:
            raise ValueError(f"{p.key}={fv} 超出范围 [{p.min}, {p.max}]")
        steps = (fv - p.min) / p.step
        if abs(steps - round(steps)) > 1e-6:
            raise ValueError(f"{p.key}={fv} 不满足步长 {p.step}")
        out[p.key] = int(v) if p.type == "int" else float(v)
    return out


def _build_tuneable() -> dict:
    """旧式 TUNEABLE 兼容结构（param/name/grid 位置参数形式），由 SPECS 派生。"""
    out: dict = {}
    for key, spec in SPECS.items():
        grid = []
        for cand in spec.candidates:
            vals = param_values(key, cand)
            grid.append(vals[0] if len(vals) == 1 else tuple(vals))
        out[key] = {"param": spec.params[0].key, "name": spec.name, "grid": grid}
    return out


TUNEABLE = _build_tuneable()


# ---------------------------------------------------------------------------
# 计算与调优
# ---------------------------------------------------------------------------


def _compute_values(indicator: str, df: pd.DataFrame, params: dict) -> pd.Series | dict:
    """按具名参数计算指标值。

    MACD 用 DIF+柱体（完整输出），DEA 依赖 signal → signal 真实影响评价得分；
    BOLL 用 %b 带宽位置 → multiplier 真实影响评价得分。
    """
    high, low, close, volume = df["high"], df["low"], df["close"], df["volume"]
    if indicator == "ma":
        return ma(close, int(params["window"]))
    if indicator == "ema":
        return ema(close, int(params["window"]))
    if indicator == "rsi":
        return rsi(close, int(params["window"]))
    if indicator == "macd":
        m = macd(close, int(params["fast"]), int(params["slow"]), int(params["signal"]))
        return m["dif"] + m["hist"]  # hist=(dif-dea)*2，dea 依赖 signal
    if indicator == "kdj":
        return kdj(high, low, close, int(params["window"]))["k"]
    if indicator == "boll":
        b = boll(close, int(params["window"]), float(params["multiplier"]))
        bw = (b["upper"] - b["lower"]).replace(0, np.nan)
        return (close - b["lower"]) / bw  # %b：带宽位置，依赖 window 与 multiplier
    if indicator == "wr":
        return wr(high, low, close, int(params["window"]))
    if indicator == "cci":
        return cci(high, low, close, int(params["window"]))
    if indicator == "roc":
        return roc(close, int(params["window"]))
    if indicator == "mtm":
        return mtm(close, int(params["window"]))["mtm"]
    if indicator == "bias":
        return bias(close, int(params["window"]))
    if indicator == "psy":
        return psy(close, int(params["window"]))
    if indicator == "trix":
        return trix(close, int(params["window"]))["trix"]
    if indicator == "cmo":
        return cmo(close, int(params["window"]))
    if indicator == "volume_ratio":
        return volume_ratio(volume, int(params["window"]))
    raise ValueError(f"未知指标 {indicator}")


def _sample_candidates(
    candidates: tuple[dict[str, int | float], ...],
    algorithm: str,
    rng: random.Random,
) -> list[dict[str, int | float]]:
    """优化算法候选采样：
    - grid: 全量网格（默认，完整覆盖）
    - fast: 快速采样——按 1/3 间隔取候选（候选量大时显著提速，覆盖度降低）
    - random: 随机采样——最多 200 个候选（大网格下探索式搜索）
    """
    if algorithm == "random":
        n = min(len(candidates), 200)
        return rng.sample(list(candidates), n)
    if algorithm == "fast":
        return list(candidates[::3])
    return list(candidates)


# 时序评分窗口参数（_score_series，P2-40）：滚动窗口 20 期、最小起点 30 期、窗口最小有效样本 10
_SCORE_WINDOW = 20
_SCORE_MIN_PERIODS = 30
_SCORE_MIN_VALID = 10


def _score_series(
    v: np.ndarray, fwd: np.ndarray, horizon: int, target: str
) -> dict | None:
    """对单一序列（单指标或组合等权平均）计算预测能力度量与目标 score。

    P2-40 向量化：滚动 20 期窗口矩阵化一次性计算时序 IC / RankIC（Spearman）
    + 方向一致性 + 近似多空收益，无逐日 Python 循环（原 125 组合×~370 天
    np.corrcoef 循环）；窗口 mask/标准差阈值过滤语义与逐日版逐位一致。

    返回键全部 round 到 4 位（与重构前逐位一致）；无有效滚动窗口返回 None
    （调用方跳过该候选）。last_value 由调用方负责（依赖原始 Series 形状）。
    """
    v = np.asarray(v, dtype=np.float64)
    fwd = np.asarray(fwd, dtype=np.float64)
    n = len(fwd)
    t_idx = np.arange(_SCORE_MIN_PERIODS, n - horizon)
    if t_idx.size == 0:
        return None
    vw = np.lib.stride_tricks.sliding_window_view(v, _SCORE_WINDOW)[
        t_idx - _SCORE_WINDOW
    ]
    fw = np.lib.stride_tricks.sliding_window_view(fwd, _SCORE_WINDOW)[
        t_idx - _SCORE_WINDOW
    ]
    mask = np.isfinite(vw) & np.isfinite(fw)
    valid = mask.sum(axis=1)
    idx = np.where(valid > 0)[0]
    if idx.size == 0:
        return None
    mask = mask[idx]
    valid = valid[idx]
    vw = vw[idx]
    fw = fw[idx]
    xw = np.where(mask, vw, np.nan)
    yw = np.where(mask, fw, np.nan)
    xstd = np.nanstd(xw, axis=1)
    ystd = np.nanstd(yw, axis=1)
    keep = (valid >= _SCORE_MIN_VALID) & (xstd >= 1e-12) & (ystd >= 1e-12)
    if not keep.any():
        return None
    mask = mask[keep]
    valid = valid[keep]
    vw = vw[keep]
    xw = xw[keep]
    yw = yw[keep]
    xc = xw - np.nanmean(xw, axis=1, keepdims=True)
    yc = yw - np.nanmean(yw, axis=1, keepdims=True)
    denom = np.sqrt(np.nansum(xc * xc, axis=1) * np.nansum(yc * yc, axis=1))
    ics = np.nansum(xc * yc, axis=1) / denom
    rxv = np.argsort(np.argsort(xw, axis=1), axis=1).astype(np.float64)
    ryv = np.argsort(np.argsort(yw, axis=1), axis=1).astype(np.float64)
    rx = np.where(mask, rxv, np.nan)
    ry = np.where(mask, ryv, np.nan)
    rxc = rx - np.nanmean(rx, axis=1, keepdims=True)
    ryc = ry - np.nanmean(ry, axis=1, keepdims=True)
    rden = np.sqrt(np.nansum(rxc * rxc, axis=1) * np.nansum(ryc * ryc, axis=1))
    rank_ics = np.nansum(rxc * ryc, axis=1) / rden
    sgn = np.nansum(xw * yw > 0, axis=1) / valid
    col_idx = np.broadcast_to(np.arange(_SCORE_WINDOW), mask.shape)
    last_col = np.max(np.where(mask, col_idx, -1), axis=1)
    last_val = vw[np.arange(mask.shape[0]), last_col]
    pct = (mask & (vw <= last_val[:, None])).sum(axis=1) / valid
    fwd_t = fwd[t_idx][idx][keep]
    # 近似多空收益：窗口内最新值分位 → 前20%做多 / 后20%做空 / 其余空仓
    ls_rets = np.where(
        pct >= 0.8,
        fwd_t,
        np.where(pct <= 0.2, -fwd_t, 0.0),
    )
    ic = float(np.mean(ics))
    ic_std = float(np.std(ics))
    icir = float(ic / ic_std) if ic_std > 1e-12 else 0.0
    rank_ic = float(np.mean(rank_ics))
    stability = float((ics > 0).mean())
    win_rate = float(np.mean(sgn))
    yq = fwd_t
    ls_annual = float((np.quantile(yq, 0.8) - np.quantile(yq, 0.2)) * 252 / horizon)
    # 多空年化夏普 / 最大回撤：由近似多空收益序列计算
    ls_arr = ls_rets
    sharpe = sharpe_ratio(ls_arr, periods_per_year=252 / horizon)
    max_drawdown = max_drawdown_signed(ls_arr)
    composite = round(
        0.4 * min(abs(ic) / 0.1, 1.0)
        + 0.3 * stability
        + 0.2 * min(abs(icir) / 2.0, 1.0)
        + 0.1 * max(min(ls_annual / 0.5, 1.0), 0.0),
        4,
    )
    if target == "ic":
        score = ic
    elif target == "ic_abs":
        score = abs(ic)
    elif target == "icir":
        score = icir if icir is not None else -9.0
    elif target == "rank_ic":
        score = rank_ic
    elif target == "stability":
        score = stability
    elif target == "win_rate":
        score = win_rate
    elif target == "ls_annual":
        score = ls_annual
    elif target == "sharpe":
        score = sharpe
    elif target == "max_drawdown":
        score = max_drawdown
    elif target == "composite":
        score = composite
    else:
        score = ic
    # 空仓占比过高的退化保护：横盘窄幅股的多空序列近乎全零，
    # sharpe=0 / 回撤=0 恒得满分 → 调优结果随机；置最差分让真实有仓位的参数胜出
    positions = int((np.abs(ls_arr) > 1e-12).sum())
    position_ratio = positions / max(len(ls_arr), 1)
    if target in ("sharpe", "max_drawdown") and (positions < 5 or position_ratio < 0.1):
        score = -9.0
    if score is None:
        score = -9.0
    return {
        "ic": round(ic, 4),
        "icir": round(icir, 4) if icir is not None else None,
        "rank_ic": round(rank_ic, 4),
        "stability": round(stability, 4),
        "win_rate": round(win_rate, 4),
        "ls_annual": round(ls_annual, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_drawdown, 4),
        "composite": composite,
        "score": round(score, 4),
    }


def tune_indicator(
    indicator: str,
    df: pd.DataFrame,
    horizon: int = 5,
    target: str = "ic",
    algorithm: str = "grid",
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    """优化调优（同步）。algorithm: grid（全量网格）/ fast（1/3 间隔采样）/ random（随机 ≤200）。目标：
      - ic: 最大化未来收益 IC
      - ic_abs: 最大化 |IC|
      - icir: 最大化 IC 均值/IC 标准差（稳健性）
      - rank_ic: 最大化 RankIC（Spearman 秩相关）
      - ls_annual: 最大化多空年化（前20%做多-后20%做空）
      - sharpe: 最大化多空年化夏普（由近似多空收益序列计算）
      - max_drawdown: 最小化多空序列最大回撤
      - win_rate: 最大化方向一致性（信号与未来收益同号比例）
      - stability: 最大化 IC 为正比例
      - composite: 最大化综合评分（|IC|+稳健性+稳定性+多空加权）

    返回含 results/best（新增 params 具名对象、保留 param 字符串）与
    spec/defaults 契约字段。
    """
    spec = spec_of(indicator)  # 未知指标抛 ValueError
    if target not in TARGETS:
        raise ValueError(f"不支持的调优目标 {target}，可选: {list(TARGETS)}")
    if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
        raise ValueError("horizon 必须为整数")
    horizon = int(horizon)
    if not (1 <= horizon <= 250):
        raise ValueError("horizon 必须在 1..250")
    if algorithm not in ("grid", "fast", "random"):
        raise ValueError(f"不支持的优化算法 {algorithm}，可选: grid/fast/random")
    # random 固定种子：同股票/参数/算法组合结果可复现（确定性计算，无探索随机性需求）
    rng = random.Random(42)
    sampled = _sample_candidates(spec.candidates, algorithm, rng)
    close = df["close"].to_numpy()
    fwd = np.full(len(close), np.nan)
    fwd[:-horizon] = close[horizon:] / close[:-horizon] - 1

    results = []
    for cand in sampled:
        if cancel_check:
            cancel_check()
        params = dict(cand)
        try:
            values = _compute_values(indicator, df, params)
            if isinstance(values, dict):
                continue
        except Exception:
            continue
        v = values.reset_index(drop=True).to_numpy()
        scored = _score_series(v, fwd, horizon, target)
        if scored is None:
            continue
        results.append(
            {
                "param": param_to_legacy(indicator, params),
                "params": params,
                "ic": scored["ic"],
                "icir": scored["icir"],
                "rank_ic": scored["rank_ic"],
                "stability": scored["stability"],
                "win_rate": scored["win_rate"],
                "ls_annual": scored["ls_annual"],
                "sharpe": scored["sharpe"],
                "max_drawdown": scored["max_drawdown"],
                "composite": scored["composite"],
                "score": scored["score"],
                "last_value": round(float(values.iloc[-1]), 3)
                if not np.isnan(values.iloc[-1])
                else None,
            }
        )
    results.sort(key=lambda r: -r["score"])
    return {
        "indicator": indicator,
        "name": spec.name,
        "target": target,
        "horizon": horizon,
        "algorithm": algorithm,
        "evaluated": len(results),
        "candidates_total": len(spec.candidates),
        "results": results[:20],
        "best": results[0] if results else None,
        "spec": spec.to_dict(),
        "defaults": spec.defaults(),
    }


def tune_indicators_combined(
    indicators: list[str],
    df: pd.DataFrame,
    horizon: int = 5,
    target: str = "ic",
    algorithm: str = "grid",
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    """整体调优（多指标参数组合联合评分，同步）。

    2~3 个白名单指标各取一个候选参数 → 分别计算指标序列 →
    z-score 标准化 → 等权平均成组合序列 → 与单指标相同的评分逻辑
    （_score_series）评出组合的预测能力/多空质量。algorithm/target/horizon
    语义与 tune_indicator 完全一致。返回结构见契约（combined/indicators/
    results/best/specs/singles/defaults），singles 为各指标独立调优
    （仅内部采样+评分，不调 tune_indicator、不写缓存）。
    """
    if isinstance(indicators, str) or not isinstance(indicators, (list, tuple)):
        raise ValueError(
            f"整体调优需要 2~3 个白名单指标,当前 {len(indicators) if hasattr(indicators, '__len__') else 'N/A'}"
        )
    dedup: list[str] = []
    for ind in indicators:
        if ind not in SPECS:
            raise ValueError(f"整体调优需要 2~3 个白名单指标,当前 {len(indicators)}")
        if ind not in dedup:
            dedup.append(ind)
    if not (2 <= len(dedup) <= 3):
        raise ValueError(f"整体调优需要 2~3 个白名单指标,当前 {len(indicators)}")
    indicators = dedup
    if target not in TARGETS:
        raise ValueError(f"不支持的调优目标 {target}，可选: {list(TARGETS)}")
    if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
        raise ValueError("horizon 必须为整数")
    horizon = int(horizon)
    if not (1 <= horizon <= 250):
        raise ValueError("horizon 必须在 1..250")
    if algorithm not in ("grid", "fast", "random"):
        raise ValueError(f"不支持的优化算法 {algorithm}，可选: grid/fast/random")
    rng = random.Random(42)
    sampled = {
        ind: _sample_candidates(SPECS[ind].candidates, algorithm, rng)
        for ind in indicators
    }
    close = df["close"].to_numpy()
    fwd = np.full(len(close), np.nan)
    fwd[:-horizon] = close[horizon:] / close[:-horizon] - 1

    results = []
    for combo in product(*(sampled[ind] for ind in indicators)):
        if cancel_check:
            cancel_check()
        params_by_indicator = {ind: dict(cand) for ind, cand in zip(indicators, combo)}
        arrs = []
        skip = False
        for ind in indicators:
            params = params_by_indicator[ind]
            try:
                values = _compute_values(ind, df, params)
                if isinstance(values, dict):
                    skip = True
                    break
            except Exception:
                skip = True
                break
            arrs.append(values.reset_index(drop=True).to_numpy())
        if skip:
            continue
        # z-score 标准化（按各自非 NaN 部分）→ 等权平均
        normed = []
        for x in arrs:
            finite = x[np.isfinite(x)]
            if finite.size == 0:
                skip = True
                break
            mean = float(np.mean(finite))
            std = float(np.std(finite))
            if std < 1e-12:
                skip = True
                break
            normed.append((x - mean) / std)
        if skip:
            continue
        m = np.stack(normed)
        fin = np.isfinite(m)
        cnt = fin.sum(axis=0)
        v_comb = np.where(cnt > 0, np.where(fin, m, 0.0).sum(axis=0) / cnt, np.nan)
        scored = _score_series(v_comb, fwd, horizon, target)
        if scored is None:
            continue
        lv = v_comb[-1]
        results.append(
            {
                "params_by_indicator": params_by_indicator,
                "ic": scored["ic"],
                "icir": scored["icir"],
                "rank_ic": scored["rank_ic"],
                "stability": scored["stability"],
                "win_rate": scored["win_rate"],
                "ls_annual": scored["ls_annual"],
                "sharpe": scored["sharpe"],
                "max_drawdown": scored["max_drawdown"],
                "composite": scored["composite"],
                "score": scored["score"],
                "last_value": round(float(lv), 3) if np.isfinite(lv) else None,
            }
        )
    results.sort(key=lambda r: -r["score"])
    # 各指标独立调优（仅内部采样+评分）：取该指标候选里 score 最大的组
    singles: dict = {}
    for ind in indicators:
        best_score = None
        best_ic = None
        for cand in sampled[ind]:
            if cancel_check:
                cancel_check()
            params = dict(cand)
            try:
                values = _compute_values(ind, df, params)
                if isinstance(values, dict):
                    continue
            except Exception:
                continue
            s = _score_series(
                values.reset_index(drop=True).to_numpy(), fwd, horizon, target
            )
            if s is None:
                continue
            if best_score is None or s["score"] > best_score:
                best_score = s["score"]
                best_ic = s["ic"]
        singles[ind] = {"best_ic": best_ic, "best_score": best_score}
    return {
        "combined": True,
        "indicators": indicators,
        "target": target,
        "horizon": horizon,
        "algorithm": algorithm,
        "evaluated": len(results),
        "candidates_total": int(np.prod([len(sampled[ind]) for ind in indicators])),
        "results": results[:20],
        "best": results[0] if results else None,
        "specs": {
            ind: {"name": SPECS[ind].name, "defaults": SPECS[ind].defaults()}
            for ind in indicators
        },
        "singles": singles,
        "defaults": {ind: SPECS[ind].defaults() for ind in indicators},
    }
