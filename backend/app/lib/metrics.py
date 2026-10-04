"""绩效与秩相关纯函数：年化收益 / 夏普 / 最大回撤 / 胜率 / Spearman 秩相关。

口径说明（各调用方原口径存在差异，函数以参数区分，替换时保证调用方输出不变）：
- 年化收益：simple（均值×期数）或 compound（(1+均值)^期数-1）。
- 夏普：均值/标准差×√期数（期数 = 252 或 252/horizon），可含无风险利率与 ddof。
- 回撤：max_drawdown（正数幅度）或 max_drawdown_signed（负数口径）。
- win_rate 为收益序列正收益占比；注意 tune.py 的「方向一致性」另算，不适用本函数。
"""

from __future__ import annotations

import numpy as np


def annualized_return(
    returns, periods_per_year: int = 252, compound: bool = False
) -> float:
    """年化收益：simple = mean×期数；compound = (1+mean)^期数-1。"""
    v = np.asarray(returns, dtype=np.float64)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return float("nan")
    mean = float(np.mean(v))
    if compound:
        return (1 + mean) ** periods_per_year - 1 if mean > -1 else -1.0
    return mean * periods_per_year


def sharpe_ratio(
    returns,
    periods_per_year: float = 252,
    risk_free: float = 0.0,
    compound: bool = False,
    ddof: int = 0,
) -> float:
    """夏普比率：(年化收益 - 无风险) / 年化波动；年化波动 <= 1e-12 时返回 0.0。"""
    v = np.asarray(returns, dtype=np.float64)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return float("nan")
    ann = annualized_return(v, periods_per_year, compound)
    vol = float(np.std(v, ddof=ddof)) * np.sqrt(periods_per_year)
    if vol <= 1e-12:
        return 0.0
    return (ann - risk_free) / vol


def max_drawdown(returns) -> float:
    """最大回撤（正数幅度 0~1）：max((peak-cum)/peak)；净值峰值非正时返回 0.0。"""
    v = np.asarray(returns, dtype=np.float64)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return 0.0
    cum = np.cumprod(1 + v)
    peak = np.maximum.accumulate(cum)
    if peak[-1] <= 0:
        return 0.0
    return float(np.max((peak - cum) / peak))


def max_drawdown_signed(returns) -> float:
    """最大回撤（负数口径）：min((cum-peak)/peak)。"""
    v = np.asarray(returns, dtype=np.float64)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return 0.0
    cum = np.cumprod(1 + v)
    peak = np.maximum.accumulate(cum)
    # 除零防护：净值峰值归零后（cum 一旦为 0，连乘恢复不了正值）peak 恒为 0，
    # 回撤口径无意义；与 max_drawdown 的 peak[-1] <= 0 守卫语义一致。
    if peak[-1] <= 0:
        return 0.0
    return float(((cum - peak) / peak).min())


def win_rate(returns) -> float:
    """胜率：正收益占比。"""
    v = np.asarray(returns, dtype=np.float64)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return float("nan")
    return float((v > 0).mean())


def rank_array(x) -> np.ndarray:
    """秩变换：argsort(argsort)，float64（并列值按位置先后取秩）。"""
    return np.argsort(np.argsort(np.asarray(x))).astype(np.float64)


def spearman_corr(x, y) -> float:
    """Spearman 秩相关系数（numpy 实现，避免引入 scipy 依赖）。"""
    rx = rank_array(x)
    ry = rank_array(y)
    return float(np.corrcoef(rx, ry)[0, 1])


def rank_ic(
    factor: np.ndarray, fwd_ret: np.ndarray, min_count: int = 5, min_std: float = 1e-12
) -> float:
    """横截面 RankIC：每期 Spearman 秩相关取均值；无有效期返回 NaN。"""
    f = np.asarray(factor, dtype=np.float64)
    r = np.asarray(fwd_ret, dtype=np.float64)
    if f.shape != r.shape:
        raise ValueError(f"形状不一致 {f.shape} vs {r.shape}")
    ics = []
    for t in range(f.shape[1]):
        x, y = f[:, t], r[:, t]
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < min_count:
            continue
        xv, yv = x[mask], y[mask]
        if np.std(xv) < min_std or np.std(yv) < min_std:
            continue
        ics.append(spearman_corr(xv, yv))
    if not ics:
        return float("nan")
    return float(np.mean(ics))
