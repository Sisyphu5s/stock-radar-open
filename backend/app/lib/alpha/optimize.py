"""组合优化(T-10):均值方差 / 风险平价 / 最小方差,纯 numpy 可测。

公共约束(全部方法一致,由 _project_weights 统一实施):
- 仅多:权重 >= 0(不做空)
- 单股权重上限:0 <= w_i <= max_weight(max_weight 为 None 时视为无上限,
  等价于 max_weight = 1,即单个股票可全仓)
- 权重和 = 1(满仓)

方法说明:
- mean_variance(returns, cov, risk_aversion):最大化 w'μ - 0.5·λ·w'Σw,
  即最小化 0.5·λ·w'Σw - w'μ(λ=risk_aversion;λ=0 时为纯 μ 最大化线性规划,
  贪心按 μ 降序填满 max_weight;λ 越大越接近最小方差)。
- min_variance(cov):最小化 w'Σw(全局最优,凸二次规划)。
- risk_parity(cov):等边际风险贡献 w_i·(Σw)_i ≡ c(循环坐标下降迭代,
  收敛后投影到约束集——上限约束下的风险平价是近似解,汇报注明)。

数值方法:投影梯度下降 + 回溯线搜索(凸问题,半正定 Q 保证收敛);
约束投影用经典水填充(water-filling,单纯形 ∩ 箱约束)。
"""

from __future__ import annotations

import numpy as np

from app.lib.metrics import annualized_return, sharpe_ratio

# 迭代收敛默认参数(测试可覆盖)
_DEFAULT_ITER = 1000
_DEFAULT_TOL = 1e-9


def _check_weights_max(max_weight: float | None, n: int) -> float | None:
    """校验 max_weight 合法性:None → 无上限;超出可行域(上限×n < 1)抛 ValueError。"""
    if max_weight is None:
        return None
    mw = float(max_weight)
    if not np.isfinite(mw) or mw <= 0:
        raise ValueError(f"max_weight 必须为正数,收到 {max_weight}")
    if mw * n < 1.0 - 1e-12:
        raise ValueError(
            f"max_weight={mw} 与 {n} 只股票不可行(上限×数量 < 1,权重无法凑满)"
        )
    return min(mw, 1.0)


def _project_weights(w: np.ndarray, max_weight: float | None) -> np.ndarray:
    """投影到约束集 {0<=w<=max_weight, sum(w)=1}(水填充,二分求阈值 θ)。

    解 min ‖u - w‖² s.t. 0<=u_i<=M, Σu_i=1:KKT 得 u_i = clamp(w_i - θ, 0, M),
    θ 使 Σ clamp(w_i - θ, 0, M) = 1。f(θ) = Σ clamp(w_i - θ, 0, M) 关于 θ 单调
    递减,二分 80 次收敛(纯 numpy,无先 clip 破坏目标的问题)。
    """
    u = np.asarray(w, dtype=np.float64)
    hi = max_weight if max_weight is not None else 1e300
    # 有效上界:投影点坐标不会超过 max(上限, max(u), 1)(全负 w 时投影点 ~1)
    m_eff = (
        float(max_weight)
        if max_weight is not None
        else max(float(np.max(u)) if u.size else 0.0, 1.0)
    )
    # f(θ_lo) >= 1(至少一个分量 clamp 到 m_eff;可行性由 _check_weights_max 保证)
    theta_lo = float(np.min(u)) - m_eff
    # f(θ_hi) = 0 < 1(全部 clamp 到 0)
    theta_hi = float(np.max(u))
    for _ in range(80):
        mid = 0.5 * (theta_lo + theta_hi)
        s = float(np.clip(u - mid, 0.0, hi).sum())
        if s > 1.0:
            theta_lo = mid
        else:
            theta_hi = mid
    out = np.clip(u - theta_hi, 0.0, hi)
    # 数值兜底:权重和精确归一(浮点累计误差 < 1e-12 级,此处修正避免和偏离 1)
    s = out.sum()
    if s > 1e-15:
        out /= s
    return out


def _proj_grad_min(
    q: np.ndarray,
    lin: np.ndarray,
    max_weight: float | None,
    iters: int = _DEFAULT_ITER,
    tol: float = _DEFAULT_TOL,
) -> np.ndarray:
    """投影梯度下降求解 min 0.5·w'Qw + q'w,约束见 _project_weights。

    方向 d = proj(w - ∇f) - w(可行下降方向,KKT 驻点当且仅当 d=0);
    线搜索沿 d 回溯(凸组合,点恒在约束集内),Armijo 条件
    f(w + αd) ≤ f(w) + 0.5·α·∇f·d。Q 半正定 → 全局最优,单调收敛。
    """
    n = q.shape[0]
    w = _project_weights(np.full(n, 1.0 / n), max_weight)

    def f(x: np.ndarray) -> float:
        return float(0.5 * x @ q @ x + x @ lin)

    for _ in range(iters):
        g = q @ w + lin
        d = _project_weights(w - g, max_weight) - w
        if float(np.max(np.abs(d))) < tol:
            break  # 投影梯度驻点(KKT)
        gd = float(g @ d)
        if gd >= 0:  # 数值扰动导致的非下降方向:减半保守移动
            d *= 0.5
            gd *= 0.5
        fw = f(w)
        alpha = 1.0
        while alpha > 1e-12:
            new_w = w + alpha * d
            if f(new_w) <= fw + 0.5 * alpha * gd:
                break
            alpha *= 0.5
        if alpha <= 1e-12:
            break  # 线搜索失败(数值病态),接受当前点
        w = new_w
    return _project_weights(w, max_weight)


def min_variance(
    cov: np.ndarray,
    max_weight: float | None = None,
    iters: int = _DEFAULT_ITER,
    tol: float = _DEFAULT_TOL,
) -> np.ndarray:
    """最小方差组合:min w'Σw。cov 为对称半正定矩阵(n×n)。"""
    cov = np.asarray(cov, dtype=np.float64)
    if cov.ndim != 2 or cov.shape[0] != cov.shape[1]:
        raise ValueError(f"cov 必须为方阵,收到形状 {cov.shape}")
    n = cov.shape[0]
    if n == 0:
        raise ValueError("cov 不能为空")
    _check_weights_max(max_weight, n)
    return _proj_grad_min(cov, np.zeros(n), max_weight, iters, tol)


def mean_variance(
    returns: np.ndarray,
    cov: np.ndarray,
    risk_aversion: float = 1.0,
    max_weight: float | None = None,
    iters: int = _DEFAULT_ITER,
    tol: float = _DEFAULT_TOL,
) -> np.ndarray:
    """均值方差组合:max w'μ - 0.5·λ·w'Σw(解 min 0.5·λ·w'Σw - w'μ)。

    returns: 期望收益向量 μ(n,)。risk_aversion <= 0 时退化为纯 μ 最大化:
    线性目标,贪心按 μ 降序填满 max_weight(λ=0 时 Q=0,投影梯度无意义)。
    """
    mu = np.asarray(returns, dtype=np.float64).reshape(-1)
    cov = np.asarray(cov, dtype=np.float64)
    if mu.shape[0] != cov.shape[0] or cov.shape[0] != cov.shape[1]:
        raise ValueError(f"returns({mu.shape}) 与 cov({cov.shape}) 维度不一致")
    n = mu.shape[0]
    if n == 0:
        raise ValueError("returns 不能为空")
    mw = _check_weights_max(max_weight, n)
    lam = float(risk_aversion)
    if lam <= 1e-12:
        # 纯 μ 最大化:贪心填充(μ 降序,逐个填满 max_weight 直至权重和=1)
        w = np.zeros(n)
        order = np.argsort(-mu)
        remain = 1.0
        for i in order:
            take = remain if mw is None else min(mw, remain)
            if take <= 1e-15:
                break
            w[i] = take
            remain -= take
        return _project_weights(w, mw)
    return _proj_grad_min(lam * cov, -mu, mw, iters, tol)


def risk_parity(
    cov: np.ndarray,
    max_weight: float | None = None,
    iters: int = 500,
    tol: float = 1e-10,
) -> np.ndarray:
    """风险平价组合:各资产边际风险贡献相等 w_i·(Σw)_i ≡ c。

    循环坐标下降(Spinu 2013 的逐资产解析更新):每轮对每资产解二次方程
    a·w_i² + b·w_i = c(其中 a=Σ_ii, b=Σ_{j≠i} Σ_ij·w_j, c=当前平均边际风险),
    更新后整体归一。收敛后投影到约束集(上限约束为近似,汇报注明)。
    cov 为对角阵时收敛到逆波动率加权 w ∝ 1/σ。
    """
    cov = np.asarray(cov, dtype=np.float64)
    if cov.ndim != 2 or cov.shape[0] != cov.shape[1]:
        raise ValueError(f"cov 必须为方阵,收到形状 {cov.shape}")
    n = cov.shape[0]
    if n == 0:
        raise ValueError("cov 不能为空")
    _check_weights_max(max_weight, n)
    if n == 1:
        return np.array([1.0])
    a = np.diag(cov)
    if np.all(a <= 1e-12):
        raise ValueError("cov 全零方差,无法计算风险平价")
    zero = a <= 1e-12
    if zero.any():
        # 零方差资产(收益恒定):边际风险贡献恒 0,权重置 0;
        # 其余资产在子矩阵上迭代,权重和为 1(重归一),再投影约束集
        keep = ~zero
        sub_w = risk_parity(cov[np.ix_(keep, keep)], max_weight, iters, tol)
        w = np.zeros(n)
        w[keep] = sub_w
        return _project_weights(w, max_weight)
    w = np.full(n, 1.0 / n)
    for _ in range(iters):
        prev = w.copy()
        mrc = w * (cov @ w)  # 边际风险贡献
        c = float(np.mean(mrc))
        for i in range(n):
            b = float(cov[i] @ w - a[i] * w[i])  # Σ_{j≠i} Σ_ij·w_j
            disc = b * b + 4.0 * a[i] * c
            if disc < 0:
                disc = 0.0
            w[i] = (np.sqrt(disc) - b) / (2.0 * a[i])
        w /= w.sum()  # 归一(每次坐标更新破坏权重和)
        if float(np.max(np.abs(w - prev))) < tol:
            break
    return _project_weights(w, max_weight)


def portfolio_metrics(daily_ret: np.ndarray, weights: np.ndarray) -> dict:
    """组合绩效(样本内):按固定权重 w 的日收益序列 → 年化/波动/夏普。

    daily_ret: (S, T) 日收益面板(close 日收益,首列可 NaN);权重非零行参与。
    """
    w = np.asarray(weights, dtype=np.float64)
    ret = np.asarray(daily_ret, dtype=np.float64)
    if w.shape[0] != ret.shape[0]:
        raise ValueError(f"weights({w.shape}) 与面板行数({ret.shape[0]})不一致")
    # NaN 收益(停牌/上市前)按 0 参与——与协方差估计的均值填充同口径
    ret = np.where(np.isfinite(ret), ret, 0.0)
    combo = w @ ret  # (T,) 日收益
    v = combo[np.isfinite(combo)]
    if len(v) < 10:
        return {"annual_return": None, "volatility": None, "sharpe": None}
    ann = annualized_return(v)
    vol = float(np.std(v)) * np.sqrt(252)
    sharpe = sharpe_ratio(v)
    return {
        "annual_return": round(ann, 4),
        "volatility": round(vol, 4),
        "sharpe": round(sharpe, 2),
    }


def turnover_vs_equal(weights: np.ndarray) -> float:
    """相对等权基准的一次性建仓换手:0.5·Σ|w_i - 1/n|(n = 权重向量长度)。

    基准 = 入选股票集的等权(权重为 0 的股票也参与,即从等权组合换到目标权重
    所需单向换手);固定权重组合自身无调仓换手,该值描述建仓成本,与
    backtest._rebuild_weights 的组合级 |Δw| 成本口径一致。
    """
    w = np.asarray(weights, dtype=np.float64)
    n = w.size
    if n == 0:
        return 0.0
    return float(np.abs(w - 1.0 / n).sum() / 2.0)
