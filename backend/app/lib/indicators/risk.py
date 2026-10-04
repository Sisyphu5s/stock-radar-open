"""风险指标纯计算（lib 层）：基于个股历史日收益序列计算风险度量。

（bt-rm 收官：原 core/indicators/risk.py 全文迁入 lib——risk_metrics 为纯
numpy/pandas 计算、无 I/O，唯一实现留在 lib；core/indicators/risk.py 变
re-export。lib/alpha/backtest.py 经 ``..indicators.risk`` 直连本模块，
风控指标不再静默失败。）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..metrics import (
    annualized_return,
    max_drawdown_signed,
    sharpe_ratio,
    win_rate,
)


# 指标简短解释（前端展示用）
RISK_EXPLANATIONS = {
    "var95": "VaR(95%)：95%置信度下单日最大预期损失（历史模拟法）",
    "var99": "VaR(99%)：99%置信度下单日最大预期损失",
    "cvar95": "CVaR(95%)：损失超过 VaR(95%) 后的尾部平均损失（预期缺口）",
    "cvar99": "CVaR(99%)：99% 置信尾部平均损失",
    "var95_param": "参数法 VaR：假设收益服从正态分布计算的 VaR(95%)",
    "tail_risk": "尾部风险比：CVaR(95%)/VaR(95%) 的绝对值比（尾部损失是 VaR 的几倍，>1.3 尾部偏厚）",
    "annual_volatility": "年化波动率：日收益标准差 × √252，衡量整体波动水平",
    "downside_deviation": "下行偏差：仅负收益的波动，衡量亏损侧波动",
    "max_drawdown": "最大回撤：历史峰值到谷底的最大跌幅（负值表示回撤，-0.25=回撤25%）",
    "drawdown_recovery_days": "回撤恢复：最大回撤后恢复到前高的所需交易日",
    "avg_drawdown": "平均回撤：全部回撤区间的平均深度（负值）",
    "sharpe": "夏普比率：(年化收益-无风险)/年化波动，风险调整后收益",
    "sortino": "Sortino：仅用下行偏差替代波动率的夏普变体",
    "beta": "Beta：相对基准（沪深300）的敏感度，>1 波动大于市场",
    "alpha": "Alpha：相对基准的超额收益（年化）",
    "skewness": "偏度：收益分布不对称性（<0 左偏，左尾更厚）",
    "kurtosis": "超额峰度：尾部厚度（>3 厚尾，极端损失概率更高）",
    "win_rate": "胜率：正收益交易日占比",
    "max_loss_streak": "最长连跌：连续下跌的最大天数",
    "var_pct_budget": "风险预算：按参数法（正态假设）VaR(99%) 建议的单笔仓位上限（%）",
    "var95_cf": "Cornish-Fisher VaR(95%)：考虑偏度/峰度修正的正态 VaR（收益非正态时比参数法更准）",
    "var99_cf": "Cornish-Fisher VaR(99%)：偏度/峰度修正",
    "ewma_volatility": "EWMA 年化波动（RiskMetrics λ=0.94）：近期收益权重更高，对近期风险更敏感",
    "calmar": "Calmar 比率：年化收益/最大回撤绝对值——回撤调整后的收益能力",
    "ulcer_index": "Ulcer 指数：回撤深度与持续时间的综合度量（越低越好）",
}


def risk_metrics(
    close: np.ndarray | pd.Series,
    annual: int = 252,
    var_levels: tuple[float, ...] = (0.95, 0.99),
    bench_close: np.ndarray | None = None,
    window: int | None = None,
) -> dict:
    """风险指标（历史模拟法 + 参数法 + 尾部分析）。

    包含：VaR(95/99 历史+参数)、CVaR、尾部风险比、年化波动、下行偏差、
    最大回撤+恢复+平均回撤、夏普、Sortino、Beta/Alpha(相对沪深300)、
    偏度、峰度、胜率、最长连跌、风险预算。
    window: 仅用最近 window 期收盘价计算（None 用全部）。
    max_drawdown/avg_drawdown 为负值（-0.25 表示回撤 25%）。
    tail_risk 为 CVaR95 与 VaR95 的绝对值比（尾部损失是 VaR 的几倍）。
    """
    c = np.asarray(close, dtype=np.float64)
    if window is not None and window > 0:
        c = c[-window:]
        if bench_close is not None:
            bench_close = np.asarray(bench_close, dtype=np.float64)[-window:]
    ret = np.diff(c) / c[:-1]
    ret = ret[np.isfinite(ret) & (c[:-1] > 0)]
    if len(ret) < 30:
        return {}

    # 基础统计
    mean = float(np.mean(ret))
    std = float(np.std(ret, ddof=1))
    vol_ann = std * np.sqrt(annual)
    annual_ret = annualized_return(ret, annual, compound=True)
    sharpe = sharpe_ratio(ret, annual, risk_free=0.02, compound=True, ddof=1)
    downside = ret[ret < 0]
    dd_std = float(np.sqrt(np.mean(downside**2))) if len(downside) else 0.0
    sortino = (
        (annual_ret - 0.02) / (dd_std * np.sqrt(annual)) if dd_std > 1e-12 else 0.0
    )
    skew = float(pd.Series(ret).skew())
    kurt = float(pd.Series(ret).kurt())  # 超额峰度
    win_ratio = win_rate(ret)

    # VaR / CVaR（历史模拟）
    var_cvar: dict = {}
    var95 = cvar95 = None
    for level in var_levels:
        q = float(np.quantile(ret, 1 - level))
        var_cvar[f"var{int(level * 100)}"] = round(q, 6)
        cvar = float(ret[ret <= q].mean()) if (ret <= q).any() else q
        var_cvar[f"cvar{int(level * 100)}"] = round(cvar, 6)
        if abs(level - 0.95) < 1e-9:
            var95, cvar95 = q, cvar

    # 参数法 VaR（正态分布）
    z95, z99 = 1.645, 2.326
    var95_param = -(z95 * std)
    var99_param = -(z99 * std)

    # 更高级：Cornish-Fisher 修正 VaR（偏度 S / 超额峰度 K 修正分位数——
    # 收益非正态时比纯正态假设更贴合实际尾部）
    def _cf_quantile(z: float, s: float, k: float) -> float:
        return (
            z
            + (z * z - 1) * s / 6
            + (z**3 - 3 * z) * k / 24
            - (2 * z**3 - 5 * z) * s * s / 36
        )

    var95_cf = round(-(_cf_quantile(-z95, skew, kurt) * std), 6)
    var99_cf = round(-(_cf_quantile(-z99, skew, kurt) * std), 6)

    # 更高级：EWMA 波动率（RiskMetrics，λ=0.94——近期收益权重指数衰减，对近期风险更敏感）
    lam = 0.94
    w = np.power(lam, np.arange(len(ret) - 1, -1, -1))
    ewma_var = float(np.sum(w * ret**2) / np.sum(w))
    ewma_vol_ann = round(float(np.sqrt(ewma_var * annual)), 4)

    # 最大回撤 + 恢复时间 + 平均回撤
    cum = np.cumprod(1 + ret)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    max_dd = max_drawdown_signed(ret)
    trough_idx = int(np.argmin(dd))
    peak_idx = int(np.argmax(peak[: trough_idx + 1])) if trough_idx > 0 else 0
    recovery = 0
    for i in range(trough_idx, len(cum)):
        if cum[i] >= peak[peak_idx]:
            recovery = i - trough_idx
            break
    else:
        recovery = len(cum) - trough_idx  # 尚未恢复
    # 平均回撤（全部回撤区间）
    in_dd = False
    dd_start = 0
    dd_sizes = []
    for i in range(len(cum)):
        if not in_dd and dd[i] < 0:
            in_dd = True
            dd_start = i
        elif in_dd and (dd[i] >= 0 or i == len(cum) - 1):
            in_dd = False
            if i > dd_start:
                seg = dd[dd_start : i + 1]
                if len(seg) and seg.min() < 0:
                    dd_sizes.append(float(seg.min()))
    avg_dd = float(np.mean(dd_sizes)) if dd_sizes else 0.0

    # 更高级：Calmar 比率（年化收益/最大回撤绝对值）与 Ulcer 指数（回撤深度×持续综合）
    calmar = round(float(annual_ret / abs(max_dd)), 2) if abs(max_dd) > 1e-12 else None
    ulcer = round(float(np.sqrt(np.mean(dd**2)) * 100), 2)

    # Beta / Alpha（相对基准沪深300）
    beta = alpha = None
    if bench_close is not None and len(bench_close) == len(c):
        bref = np.diff(bench_close) / bench_close[:-1]
        n2 = min(len(ret), len(bref))
        # 按日期轴位置配对：ret/bref 同索引即同日期边界，只剔除任一侧非有限的
        # 配对（基准缺口 NaN）。不做独立 finiteness 过滤——那会压缩 bref 长度，
        # 尾部重取造成停牌期错位
        x = ret[-n2:]
        y = bref[-n2:]
        valid = np.isfinite(x) & np.isfinite(y)
        x, y = x[valid], y[valid]
        if len(x) > 30:
            cov = float(np.cov(x, y)[0, 1])
            var_m = float(np.var(y, ddof=1))
            if var_m > 1e-12:
                beta = round(cov / var_m, 3)
                alpha = round((mean - beta * float(np.mean(y))) * annual, 4)

    # 最长连跌
    neg = ret < 0
    max_streak = 0
    cur = 0
    for v in neg:
        cur = cur + 1 if v else 0
        max_streak = max(max_streak, cur)

    # 尾部风险比 + 风险预算（按 VaR99 建议仓位，假设可承受单日 -2%）
    tail_risk = float(cvar95 / var95) if (var95 is not None and var95 < 0) else 0.0
    var_pct_budget = (
        round(min(max(0.02 / abs(var99_param), 0.05), 1.0) * 100, 1)
        if var99_param < 0
        else None
    )

    # 收益序列（供前端分布直方图，最多 300 个，超出均匀抽样）
    if len(ret) > 300:
        step = int(np.ceil(len(ret) / 300))
        ret_series = [round(float(x), 6) for x in ret[::step]]
    else:
        ret_series = [round(float(x), 6) for x in ret]

    return {
        "periods": len(ret),
        "window": len(c),
        "ret_series": ret_series,
        "annual_return": round(annual_ret, 4),
        "annual_volatility": round(vol_ann, 4),
        "ewma_volatility": ewma_vol_ann,
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": calmar,
        "ulcer_index": ulcer,
        "downside_deviation": round(dd_std * np.sqrt(annual), 4),
        "max_drawdown": round(max_dd, 4),
        "drawdown_recovery_days": int(recovery),
        "avg_drawdown": round(avg_dd, 4),
        "skewness": round(skew, 3),
        "kurtosis": round(kurt, 3),
        "win_rate": round(win_ratio, 3),
        "max_loss_streak": int(max_streak),
        "var95_param": round(var95_param, 6),
        "var99_param": round(var99_param, 6),
        "var95_cf": var95_cf,
        "var99_cf": var99_cf,
        "tail_risk": round(tail_risk, 3),
        "var_pct_budget": var_pct_budget,
        "beta": beta,
        "alpha": alpha,
        **var_cvar,
    }
