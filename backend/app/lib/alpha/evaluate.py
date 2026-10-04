"""因子评估：IC / RankIC / 分层收益 / 换手 / 稳定性 / 复杂度。"""

from __future__ import annotations

import numpy as np

# lib 层依赖例外:metrics 为纯计算模块(仅 numpy),归属 lib 待后续卡处理,
# lib 层依赖例外已消除:metrics 已迁 app.lib.metrics(S3 卡)
from app.lib.metrics import rank_array, rank_ic, spearman_corr
from .operators import evaluate_rpn, evaluate_rpn_memo, expr_str


def _as_float(a: np.ndarray) -> np.ndarray:
    """尽量保持原 dtype 引用（不复制）；仅整数/布尔等转 float64。

    面板数据为 float32（dataset 落盘即 float32），旧代码 np.asarray(..., float64) 会对
    全量面板额外产生 2-3 份 float64 副本（每份 ~17MB）。这里保持 float32，相关性计算
    在最终聚合处（标量）才提升精度；float32 路径与 float64 结果差异在 1e-4 相对误差内
    （见 tests/test_memory_bounds.py 精度对比）。
    """
    a = np.asarray(a)
    if a.dtype in (np.float32, np.float64):
        return a
    return np.asarray(a, dtype=np.float64)


def forward_returns(close: np.ndarray, horizon: int = 5) -> np.ndarray:
    """未来 horizon 日收益：close[t+h]/close[t] - 1，边界 NaN。

    入口校验语义：
      - horizon < 1 → ValueError（0/负值是调用 bug，显式暴露；app.api.alpha
        已 clamp 到 [1, 250]，不会触发）；
      - horizon > T-1 → clamp 到 T-1（与 _infer_horizon 的 clamp 先例一致，
        避免 T < horizon 时 [:-h] 空切片产生全 NaN 且语义错误，不打断线上任务）；
      - T <= 1（时间轴不足 2 列）→ ValueError（防 clamp 到 0 后 [:-0] 语义错乱）。
    保持 float32 保留（_as_float）、边界 NaN 结构（尾部 horizon 列全 NaN）。
    close 为 2D (S, T)；T 取 close.shape[-1]。
    """
    close = _as_float(close)
    T = close.shape[-1]
    if horizon < 1:
        raise ValueError(f"horizon 必须 >= 1,收到 {horizon}")
    if T <= 1:
        raise ValueError(f"close 时间轴不足 2 列(T={T})")
    if horizon > T - 1:
        horizon = T - 1
    out = np.full_like(close, np.nan)
    out[:, :-horizon] = close[:, horizon:] / close[:, :-horizon] - 1.0
    return out


def split_panel(
    panel: dict[str, np.ndarray],
    horizon: int,
    train_ratio: float = 0.6,
    val_ratio: float = 0.2,
) -> dict:
    """统一切分 + 前向收益构造器（P2-16）：先按时间轴切分，各段独立 forward_returns。

    切分口径单一事实源：gp/neural_train 用 60/20/20，evaluate/factor_tune 用 80/20
    （val_ratio=0 → 无验证段，样本外紧随训练段）。先 split 再各自 fwd，杜绝
    「完整面板算 fwd 再切片」把后段价格泄入前段标签尾部（P1-31）。

    返回 {train, val, oos, fwd_train, fwd_val, fwd_oos, t1, t2}：
      - val_ratio=0 时 val/fwd_val 为 None，oos 从 t1 起（t2 == t1）；
      - t1/t2 为时间轴列边界（meta split 信息复用）。
    """
    T = next(iter(panel.values())).shape[1]
    t1 = int(T * train_ratio)
    t2 = int(T * (train_ratio + val_ratio)) if val_ratio > 0 else t1

    def cut(i: int, j: int) -> dict:
        return {k: v[:, i:j] for k, v in panel.items()}

    train = cut(0, t1)
    fwd_train = forward_returns(train["close"], horizon)
    oos = cut(t2, T)
    fwd_oos = forward_returns(oos["close"], horizon)
    if val_ratio > 0:
        val = cut(t1, t2)
        fwd_val = forward_returns(val["close"], horizon)
    else:
        val = None
        fwd_val = None
    return {
        "train": train,
        "val": val,
        "oos": oos,
        "fwd_train": fwd_train,
        "fwd_val": fwd_val,
        "fwd_oos": fwd_oos,
        "t1": t1,
        "t2": t2,
    }


def _infer_horizon(fwd_ret: np.ndarray) -> int:
    """从 fwd_ret 尾部连续全 NaN 列推断 horizon（forward_returns 的固有结构）。

    供未显式传 horizon 的旧调用方（gp / api）自动继承年化修正。
    A7：horizon 收敛到 [1, 60]——全 NaN 面板（推断值=整列数）或超宽面板等
    退化输入会推得异常大值，直接当 horizon 会让年化系数 252/h 失真，先 clamp
    再返回；≥1 的下界保证后续 h 日折算不除零/不产生负窗口。
    """
    a = _as_float(fwd_ret)
    if a.ndim != 2 or a.shape[1] == 0:
        return 5
    h = 0
    for t in range(a.shape[1] - 1, -1, -1):
        if np.isfinite(a[:, t]).any():
            break
        h += 1
    h = max(h, 1)
    if h > 60:
        h = 60
    return h


def compute_ic(factor: np.ndarray, fwd_ret: np.ndarray) -> float:
    """IC：横截面 Spearman 秩相关（每期），取均值。"""
    return rank_ic(factor, fwd_ret)


def ic_series(factor: np.ndarray, fwd_ret: np.ndarray) -> np.ndarray:
    """逐期 IC 序列（用于稳定性分析）。

    factor/fwd_ret 保持 float32（面板原生 dtype），仅逐期切片参与秩相关，
    避免对全量面板生成 float64 副本。
    """
    f = _as_float(factor)
    r = _as_float(fwd_ret)
    out = np.full(f.shape[1], np.nan)
    for t in range(f.shape[1]):
        x, y = f[:, t], r[:, t]
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < 5:
            continue
        xv, yv = x[mask], y[mask]
        if np.std(xv) < 1e-12 or np.std(yv) < 1e-12:
            continue
        out[t] = spearman_corr(xv, yv)
    return out


def factor_to_returns(
    factor: np.ndarray,
    fwd_ret: np.ndarray,
    top_pct: float = 0.2,
    horizon: int | None = None,
) -> tuple[float, float, float]:
    """多空分层：Top 20% 平均收益、多空价差年化、月度换手率估计。

    年化：h 日收益先除以 h 折算日收益再乘 252（horizon 缺省时从 fwd_ret 尾部推断）。
    返回 (top_annual, long_short_annual, turnover)。
    """
    f = _as_float(factor)
    r = _as_float(fwd_ret)
    h = horizon or _infer_horizon(r)
    daily_ret = []
    daily_ret_ls = []
    turnover_sum = 0.0
    days = 0
    prev_top: np.ndarray | None = None
    for t in range(f.shape[1]):
        x, y = f[:, t], r[:, t]
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < 10:
            continue
        xv, yv = x[mask], y[mask]
        n_top = max(1, int(len(xv) * top_pct))
        top_idx = np.argsort(xv)[-n_top:]
        daily_ret.append(float(np.nanmean(yv[top_idx])))
        # 多空：Top - Bottom
        bottom_idx = np.argsort(xv)[:n_top]
        daily_ret_ls.append(float(np.nanmean(yv[top_idx]) - np.nanmean(yv[bottom_idx])))
        # 换手率必须基于全局股票身份：top_idx 是 mask 过滤后的局部下标，不同期缺失
        # 股票不同（上市/退市/停牌）时局部下标不指向同一只股票，直接相交会虚高/虚低
        # 换手率。flatnonzero(mask) 把局部下标映射回全局行下标再构建 top_set。
        top_set = set(np.flatnonzero(mask)[top_idx].tolist())
        if prev_top is not None:
            churn = 1.0 - len(top_set & prev_top) / n_top
            turnover_sum += churn
            days += 1
        prev_top = top_set
    if not daily_ret:
        return float("nan"), float("nan"), float("nan")
    ann_top = float(np.mean(daily_ret)) * 252 / h
    ann_ls = float(np.mean(daily_ret_ls)) * 252 / h
    turnover = turnover_sum / days if days else float("nan")
    return ann_top, ann_ls, turnover


def stability(ics: np.ndarray, window: int = 20) -> float:
    """稳定性：IC>0 的期数占比。"""
    valid = ics[np.isfinite(ics)]
    if len(valid) < 10:
        return float("nan")
    return float((valid > 0).mean())


def evaluate_factor(
    rpn: list[dict],
    data: dict[str, np.ndarray],
    fwd_ret: np.ndarray,
    ics_window: int = 20,
    horizon: int | None = None,
    _memo: dict | None = None,
) -> dict:
    """完整评估单因子。horizon 缺省时从 fwd_ret 尾部推断（旧调用方自动继承）。

    _memo: 内部参数——节点级求值缓存（evaluate_rpn_memo），调优网格跨点共享
    子表达式结果（P1-39）；None 时行为与 evaluate_rpn 完全一致。
    """
    vals = evaluate_rpn_memo(rpn, data, _memo)
    ic = compute_ic(vals, fwd_ret)
    rank_ic = ic  # 已为秩相关
    ics = ic_series(vals, fwd_ret)
    top_ann, ls_ann, turnover = factor_to_returns(vals, fwd_ret, horizon=horizon)
    s = stability(ics, ics_window)
    return {
        "expression": expr_str(rpn),
        "complexity": len(rpn),
        "ic": float(ic) if np.isfinite(ic) else None,
        "rank_ic": float(rank_ic) if np.isfinite(rank_ic) else None,
        "top_annual": float(top_ann) if np.isfinite(top_ann) else None,
        "long_short_annual": float(ls_ann) if np.isfinite(ls_ann) else None,
        "turnover": float(turnover) if np.isfinite(turnover) else None,
        "stability": float(s) if np.isfinite(s) else None,
        "ic_positive_ratio": float((ics[np.isfinite(ics)] > 0).mean())
        if np.isfinite(ics).sum() > 0
        else None,
    }


def trim_indices(a: np.ndarray, max_series: int) -> list[int]:
    """trim 抽稀保留的原始索引：先过滤有限值再均匀抽稀（与 evaluate_factor_full 的 trim 一致）。

    供调用方把日期轴与抽稀后的 ic_series/quantiles 对齐。
    """
    a = np.asarray(a)
    idx = np.arange(len(a))[np.isfinite(a)]
    if len(idx) > max_series:
        step = len(idx) // max_series
        idx = idx[::step][:max_series]
    return [int(i) for i in idx]


def evaluate_factor_full(
    rpn: list[dict],
    data: dict[str, np.ndarray],
    fwd_ret: np.ndarray,
    ics_window: int = 20,
    n_quantiles: int = 5,
    max_series: int = 400,
    horizon: int | None = None,
    dates: list[str] | None = None,
) -> dict:
    """完整评估 + 可视化序列（IC 时间序列、分位累计收益）。

    dates: 与 data 的列对齐的日期列表，非 None 时按 ic_series 的抽稀规则输出 "dates"。
    """
    base = evaluate_factor(rpn, data, fwd_ret, ics_window, horizon)
    vals = evaluate_rpn(rpn, data)
    ics = ic_series(vals, fwd_ret)

    def trim(a: np.ndarray) -> list:
        return [round(float(np.asarray(a)[i]), 6) for i in trim_indices(a, max_series)]

    base["ic_series"] = trim(ics)
    base["quantiles"] = quantile_cum_returns(
        vals, fwd_ret, n_quantiles, max_series, horizon
    )
    base["n_quantiles"] = n_quantiles
    if dates is not None:
        base["dates"] = [dates[i] for i in trim_indices(ics, max_series)]
    return base


def quantile_cum_returns(
    factor: np.ndarray,
    fwd_ret: np.ndarray,
    n_quantiles: int = 5,
    max_series: int = 400,
    horizon: int | None = None,
) -> dict[str, list[float]]:
    """按因子分位数分组的组合累计收益（起始 = 1.0），用于分层收益曲线。

    重叠的 h 日收益在累乘前先折算为日等效收益 (1+r)^(1/h)-1。
    factor/fwd_ret 保持 float32（面板原生 dtype），仅逐期切片参与分组；
    最终累计序列为每分位的长度-T 小数组，此处在标量聚合阶段提升为 float64。
    """
    f = _as_float(factor)
    r = _as_float(fwd_ret)
    h = horizon or _infer_horizon(r)
    cum: dict[str, list[float]] = {f"q{i + 1}": [] for i in range(n_quantiles)}
    for t in range(f.shape[1]):
        x, y = f[:, t], r[:, t]
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < max(n_quantiles * 3, 15):
            for k in cum:
                cum[k].append(np.nan)
            continue
        xv, yv = x[mask], y[mask]
        # 等分位数分组（按排序位置）
        order = rank_array(xv)
        n = len(xv)
        for qi in range(n_quantiles):
            lo, hi = qi / n_quantiles, (qi + 1) / n_quantiles
            sel = (order >= n * lo) & (order < n * hi)
            if sel.sum() == 0:
                cum[f"q{qi + 1}"].append(np.nan)
            else:
                cum[f"q{qi + 1}"].append(float(np.nanmean(yv[sel])))

    # 累计（1.0 起点）：h 日收益先折算日等效再累乘
    out: dict[str, list[float]] = {}
    for qi in range(n_quantiles):
        series = np.asarray(cum[f"q{qi + 1}"], dtype=np.float64)
        valid = np.isfinite(series)
        ret = np.zeros_like(series)
        ret[valid] = np.power(1.0 + series[valid], 1.0 / h) - 1.0
        cumret = np.cumprod(1.0 + ret)
        cumret[~valid] = np.nan
        if len(cumret) > max_series:
            step = len(cumret) // max_series
            cumret = cumret[::step][:max_series]
        out[f"q{qi + 1}"] = [
            round(float(v), 6) if np.isfinite(v) else None for v in cumret
        ]
    return out
