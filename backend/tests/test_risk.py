"""风险指标高级方法测试：Cornish-Fisher VaR / EWMA 波动 / Calmar / Ulcer。"""

from __future__ import annotations

import numpy as np

from app.core.indicators.risk import RISK_EXPLANATIONS, risk_metrics


def _synth_close(n: int = 400, seed: int = 7) -> np.ndarray:
    """带偏度/峰度的合成收盘价序列（随机游走 + 偶发大跌制造厚尾）。"""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.02, n)
    # 偶发大跌（左偏厚尾）
    for i in range(5):
        rets[rng.integers(30, n)] -= 0.08
    return np.cumprod(1 + rets) * 100


def test_risk_advanced_metrics_present():
    r = risk_metrics(_synth_close())
    assert r, "数据应足够计算"
    # 新高级指标全部存在且为数值
    for k in ("ewma_volatility", "var95_cf", "var99_cf", "ulcer_index"):
        assert k in r and r[k] is not None, f"缺少 {k}"
    # Calmar：回撤非零时应存在
    assert "calmar" in r
    # 解释文案覆盖新指标
    for k in ("ewma_volatility", "var95_cf", "calmar", "ulcer_index"):
        assert k in RISK_EXPLANATIONS, f"缺少解释 {k}"


def test_ewma_volatility_sensitive_to_recent():
    """EWMA 波动对近期波动更敏感：近期放大后 EWMA 波动应高于普通年化波动。"""
    rng = np.random.default_rng(3)
    rets = np.concatenate([rng.normal(0.0002, 0.01, 300), rng.normal(0.0, 0.05, 100)])
    close = np.cumprod(1 + rets) * 100
    r = risk_metrics(close)
    assert r["ewma_volatility"] > r["annual_volatility"], (
        f"近期波动放大时 EWMA({r['ewma_volatility']}) 应大于普通年化({r['annual_volatility']})"
    )


def test_cornish_fisher_var_differs_from_normal():
    """厚尾分布下 99% 层 Cornish-Fisher VaR 应比正态参数法更保守（绝对值更大）。"""
    rng = np.random.default_rng(11)
    rets = rng.normal(0.0, 0.02, 500)
    for _ in range(12):
        rets[rng.integers(50, 500)] -= 0.1  # 强左偏厚尾
    close = np.cumprod(1 + rets) * 100
    r = risk_metrics(close)
    assert r["kurtosis"] > 0, "合成数据应有厚尾"
    assert abs(r["var99_cf"]) > abs(r["var99_param"]), (
        f"厚尾下 99% CF VaR({r['var99_cf']}) 应比参数法({r['var99_param']}) 更保守"
    )


def test_calmar_and_ulcer_reasonable():
    r = risk_metrics(_synth_close())
    # Calmar 有界（回撤非零时）
    assert r["calmar"] is None or -20 < r["calmar"] < 20
    # Ulcer 指数非负
    assert r["ulcer_index"] >= 0
