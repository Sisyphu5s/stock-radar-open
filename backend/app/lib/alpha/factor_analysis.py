"""因子生命周期分析(T-09):相关性矩阵 / 冗余聚类 / IC 衰减半衰期 / 风格归因。

纯计算模块(lib 层,仅 numpy):不 import storage/core,
输入输出均为 numpy 数组与标量,便于单元测试与跨调用方复用。

口径:
- 相关性:两两 IC 序列在公共有限值上的 Pearson 相关(IC 序列来自 evaluate.ic_series,
  横截面秩相关的时间序列;两因子 IC 高度相关 = 冗余);
- 冗余聚类:单链聚类(union-find),|边| 相关 ≥ threshold 的两因子归为一组——
  单链语义:组内任意两成员存在相关 ≥ threshold 的路径即可合并;
- IC 衰减:各 horizon 独立 forward_returns 后的 IC 均值序列;
  半衰期 = IC(按绝对值)首次跌破峰值一半的 horizon;
- 风格归因:每期横截面 OLS,因子值对 [行业 one-hot, 市值] 回归,
  行业/市值系数(暴露)跨期均值;残差与未来收益的秩相关 = 残差 IC。
"""

from __future__ import annotations

import numpy as np

from app.lib.metrics import spearman_corr


def correlation_matrix(ic_lists: list[np.ndarray]) -> np.ndarray:
    """两两 IC 序列 Pearson 相关(公共有限值对齐)。

    ic_lists: 每因子一条 IC 时间序列((T,) 或 (T,1) 均可,内部压平)。
    返回 (n, n) float64 矩阵:对角线恒 1.0;公共有限值 < 5 或方差为零
    的配对记 NaN(数据不足,前端显示 "—")。
    """
    n = len(ic_lists)
    out = np.full((n, n), np.nan)
    for i in range(n):
        a = np.asarray(ic_lists[i], dtype=np.float64).ravel()
        for j in range(n):
            if i == j:
                out[i, j] = 1.0
                continue
            b = np.asarray(ic_lists[j], dtype=np.float64).ravel()
            mask = np.isfinite(a) & np.isfinite(b)
            if mask.sum() < 5:
                continue
            av, bv = a[mask], b[mask]
            if np.std(av) < 1e-12 or np.std(bv) < 1e-12:
                continue
            out[i, j] = float(np.corrcoef(av, bv)[0, 1])
    return out


def cluster_redundant(corr: np.ndarray, threshold: float) -> list[list[int]]:
    """单链聚类:相关系数 ≥ threshold 的因子归为同组。

    corr: (n, n) 相关矩阵(允许 NaN,视为无边)。threshold ∈ (0, 1]。
    返回分组:list[list[int]],每组为成员下标升序;组间按最小成员下标排序。
    单链语义:组内任意两成员间存在相关 ≥ threshold 的路径(传递闭包)。
    """
    n = corr.shape[0]
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            c = corr[i, j]
            if np.isfinite(c) and c >= threshold:
                _union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(_find(i), []).append(i)
    return [sorted(g) for g in sorted(groups.values(), key=lambda g: min(g))]


def _half_life(horizons: list[int], ic_means: list[float | None]) -> int | None:
    """半衰期:IC(绝对值)首次跌破峰值一半的 horizon;未衰减 / 无有效 IC 返回 None。

    ic_means 与 horizons 等长,None 视为该 horizon 无有效 IC(不参与峰值,
    但参与「首次跌破」的扫描——None 跳过,取后续第一个满足条件的 horizon)。
    """
    valid = [m for m in ic_means if m is not None]
    if not valid:
        return None
    peak = max(abs(m) for m in valid)
    if peak <= 0:
        return None
    for h, m in zip(horizons, ic_means):
        if m is not None and abs(m) < peak / 2.0:
            return h
    return None


def ic_decay(
    expr: str,
    panel: dict[str, np.ndarray],
    horizons: list[int] | None = None,
) -> dict:
    """IC 衰减曲线 + 半衰期。

    expr: 因子表达式字符串;panel: {feature: (S, T)} 面板(须含 close)。
    horizons: 评估的持有期列表,缺省 [1, 3, 5, 10, 20]。
    返回 {horizons, ic_means, half_life}:ic_means 与 horizons 等长,
    无效 horizon(有效期 < 5)记 None;half_life 见 _half_life。
    """
    from .operators import compile_rpn, evaluate_rpn

    if horizons is None:
        horizons = [1, 3, 5, 10, 20]
    rpn = compile_rpn(expr)
    factor = evaluate_rpn(rpn, panel)
    close = np.asarray(panel["close"], dtype=np.float64)

    from .evaluate import forward_returns, ic_series

    means: list[float | None] = []
    for h in horizons:
        fwd = forward_returns(close, h)
        ics = ic_series(factor, fwd)
        valid = ics[np.isfinite(ics)]
        means.append(float(valid.mean()) if len(valid) >= 5 else None)
    return {
        "horizons": list(horizons),
        "ic_means": means,
        "half_life": _half_life(list(horizons), means),
    }


def style_attribution(
    factor: np.ndarray,
    fwd_ret: np.ndarray,
    industry: np.ndarray,
    market_cap: np.ndarray,
    industry_names: dict[int, str] | None = None,
    min_stocks: int = 20,
) -> dict:
    """风格归因:因子值对 [行业 one-hot + 市值] 的横截面多元回归。

    每期 t(有效股票 ≥ min_stocks):X = [行业 one-hot(当期为非 0 编码,0=未知剔除),
    市值],OLS 最小二乘(lstsq)得系数。行业 one-hot 满组(不含截距列,
    避免虚拟变量陷阱——满组哑变量本身携带截距);
    - 行业暴露 = 各行业编码系数的跨期均值(编码 → 行业名经 industry_names,缺省 "行业#N");
    - 市值暴露 = 市值系数跨期均值;
    - 残差 IC = 每期残差与 fwd_ret 的 Spearman 秩相关的均值(IC 同口径)。

    返回 {industry_exposures, size_exposure, residual_ic, n_periods}:
    无任何有效期时各值 None(除 n_periods=0)。
    """
    f = np.asarray(factor, dtype=np.float64)
    r = np.asarray(fwd_ret, dtype=np.float64)
    ind = np.asarray(industry, dtype=np.float64)
    cap = np.asarray(market_cap, dtype=np.float64)
    if not (
        f.ndim == 2
        and r.shape == f.shape
        and ind.shape == f.shape
        and cap.shape == f.shape
    ):
        raise ValueError(
            f"factor/fwd_ret/industry/market_cap 形状不一致: "
            f"{f.shape} vs {r.shape} vs {ind.shape} vs {cap.shape}"
        )
    T = f.shape[1]

    ind_coefs: dict[int, list[float]] = {}
    size_coefs: list[float] = []
    resid_ics: list[float] = []

    for t in range(T):
        base = (
            np.isfinite(f[:, t])
            & np.isfinite(cap[:, t])
            & np.isfinite(r[:, t])
            & np.isfinite(ind[:, t])
            & (ind[:, t] != 0)  # 0 = 未知行业,不参与风格因子
        )
        if base.sum() < min_stocks:
            continue
        y = f[:, t][base]
        codes = ind[:, t][base].astype(int)
        cap_t = cap[:, t][base]
        uni = sorted(set(codes.tolist()))
        # 行业 one-hot 满组 + 市值列(无截距:满组哑变量含截距,再加截距即秩亏,
        # lstsq 最小范数解会把系数摊薄到共线方向,行业暴露失真)
        X = np.column_stack([(codes == c).astype(np.float64) for c in uni] + [cap_t])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        for c, b in zip(uni, beta[: len(uni)]):
            ind_coefs.setdefault(c, []).append(float(b))
        size_coefs.append(float(beta[len(uni)]))
        rv = r[:, t][base]
        if np.std(resid) > 1e-12 and np.std(rv) > 1e-12:
            resid_ics.append(float(spearman_corr(resid, rv)))

    def _mean(vals: list[float]) -> float | None:
        return float(np.mean(vals)) if vals else None

    exposures: dict[str, float | None] = {}
    for code in sorted(ind_coefs):
        label = industry_names.get(code) if industry_names else None
        exposures[label or f"行业#{code}"] = _mean(ind_coefs[code])
    return {
        "industry_exposures": exposures,
        "size_exposure": _mean(size_coefs),
        "residual_ic": _mean(resid_ics),
        "n_periods": len(size_coefs),
    }
