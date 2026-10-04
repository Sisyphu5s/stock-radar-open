"""多因子合成(T-11):打分秩和 / IC 加权 / 等权信号平均,纯 numpy 可测。

统一处理原则:
- 量纲对齐:除等权平均用横截面 z-score(scale)外,其余方法先把各因子信号
  做横截面 rank(0~1)再组合,避免不同量纲因子直接相加失衡。
- 缺失处理:非有限信号在组合中按 0(中性)参与,不污染其他因子贡献。
- 权重语义:ic_weight 方法的权重 = IC/Σ|IC|(带符号,负 IC 因子自动反向),
  由 ic_weights_from_ic 归一;score/equal 方法权重恒正(或等权)。

合成因子表达式(rpn_expression):生成可在表达式系统编译/求值的线性组合
字符串,供「一键入因子库」(create_factor)使用;equal 方法用 scale 算子,
其余用 rank 算子,保证入库因子与合成结果口径一致。
"""

from __future__ import annotations

import numpy as np

from app.lib.metrics import rank_array


def rank_signal(sig: np.ndarray) -> np.ndarray:
    """横截面 rank 归一 0~1(逐期);非有限值保持 NaN;单有效值期置 0。"""
    x = np.asarray(sig, dtype=np.float64)
    out = np.full_like(x, np.nan)
    for t in range(x.shape[1]):
        col = x[:, t]
        m = np.isfinite(col)
        k = int(m.sum())
        if k <= 1:
            continue
        out[m, t] = rank_array(col[m]) / (k - 1)
    return out


def zscore_signal(sig: np.ndarray) -> np.ndarray:
    """横截面 z-score(逐期):(x - mean)/std;常数截面 → 0(中性)。"""
    x = np.asarray(sig, dtype=np.float64)
    out = np.full_like(x, np.nan)
    for t in range(x.shape[1]):
        col = x[:, t]
        m = np.isfinite(col)
        if not m.any():
            continue
        v = col[m]
        std = float(np.std(v))
        if std < 1e-12:
            out[m, t] = 0.0
        else:
            out[m, t] = (v - float(np.mean(v))) / std
    return out


def _nan0(a: np.ndarray) -> np.ndarray:
    return np.where(np.isfinite(a), a, 0.0)


def combine_by_score(signals: list[np.ndarray]) -> np.ndarray:
    """打分法:各因子信号横截面 rank(0~1)的等权求和。"""
    if not signals:
        raise ValueError("至少需要一个因子信号")
    out = np.zeros_like(np.asarray(signals[0], dtype=np.float64))
    for s in signals:
        out += _nan0(rank_signal(s))
    return out


def combine_by_equal(signals: list[np.ndarray]) -> np.ndarray:
    """等权平均:各因子信号横截面 z-score 后等权平均(信号平均)。"""
    if not signals:
        raise ValueError("至少需要一个因子信号")
    out = np.zeros_like(np.asarray(signals[0], dtype=np.float64))
    for s in signals:
        out += _nan0(zscore_signal(s))
    return out / len(signals)


def combine_by_ic_weight(signals: list[np.ndarray], weights: list[float]) -> np.ndarray:
    """IC 加权:各因子 rank(0~1)按 weights 线性加权(权重可带符号,负 IC 反向)。"""
    if not signals:
        raise ValueError("至少需要一个因子信号")
    if len(weights) != len(signals):
        raise ValueError(
            f"weights({len(weights)}) 与 signals({len(signals)}) 数量不一致"
        )
    out = np.zeros_like(np.asarray(signals[0], dtype=np.float64))
    for s, w in zip(signals, weights):
        out += _nan0(rank_signal(s)) * float(w)
    return out


def ic_weights_from_ic(ics: list[float]) -> list[float]:
    """IC 列表 → 合成权重:IC/Σ|IC|(带符号,负 IC 因子自动反向)。

    - NaN/Inf IC → 权重 0(该因子不参与合成)
    - Σ|IC| ≈ 0(全部无效或全零)→ 等权(中性兜底,不抛错)
    """
    a = np.asarray(ics, dtype=np.float64)
    w = np.where(np.isfinite(a), a, 0.0)
    s = float(np.abs(w).sum())
    n = len(w)
    if s <= 1e-12:
        return [1.0 / n] * n
    return (w / s).tolist()


def rpn_expression(
    factor_exprs: list[str],
    weights: list[float] | None = None,
    method: str = "score",
) -> str:
    """合成因子表达式字符串(表达式系统可编译/求值)。

    - score/ic_weight:rank(f) 的线性组合(权重带符号,负权重 = 反向因子)
    - equal:scale(f)(横截面 z-score)的线性组合
    - weights=None → 等权;|w| < 1e-12 的项跳过(避免 0 权重噪音)
    输出形如 ((0.6 * rank(a)) + (0.4 * rank(b)))。
    """
    op = "rank" if method != "equal" else "scale"
    n = len(factor_exprs)
    if n == 0:
        return ""
    if weights is None:
        weights = [1.0 / n] * n
    if len(weights) != n:
        raise ValueError(f"weights({len(weights)}) 与因子数({n})不一致")
    terms: list[str] = []
    for e, w in zip(factor_exprs, weights):
        wf = float(w)
        if abs(wf) < 1e-12:
            continue
        term = f"{op}({e.strip()})"
        terms.append(term if abs(wf - 1.0) < 1e-12 else f"({_fmt_num(wf)} * {term})")
    if not terms:
        return ""
    out = terms[0]
    for t in terms[1:]:
        out = f"({out} + {t})"
    return out


def _fmt_num(v: float) -> str:
    """权重格式化:整数去小数点,其余保留 6 位去尾零(表达式 token 可解析)。"""
    r = round(v, 6)
    return str(int(r)) if r.is_integer() else f"{r:g}"
