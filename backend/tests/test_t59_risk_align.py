"""T-59 风险指标 Beta/Alpha 按日期对齐 + VaR 预算文案口径统一。

- risk.py：基准含停牌缺口（NaN）时，原实现先独立过滤 bref 再尾部重取，
  压缩长度造成错位；修复后按日期轴位置配对、只剔除任一侧非有限的配对
- indicators.py /risk：原实现取基准尾部 len(k) 根（按位置对齐），
  基准与个股日期轴不一致时错位；修复后按日期映射基准
- var_pct_budget 文案：实现用参数法（正态）VaR(99%)，文案改为同口径
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 1) risk_metrics：基准含 NaN 缺口时 Beta/Alpha 仍按位置正确配对
# ---------------------------------------------------------------------------


def test_beta_aligns_across_benchmark_nan_gaps():
    """个股收益 == 基准收益（理论 beta=1），基准中间有停牌缺口 NaN：
    修复后配对过滤，beta≈1；修复前 bref 独立过滤后尾部错位。"""
    from app.core.indicators.risk import risk_metrics

    rng = np.random.default_rng(0)
    rets = rng.normal(0.0005, 0.01, 300)
    close = np.cumprod(1 + rets) * 100
    bench_close = np.cumprod(1 + rets) * 100
    bench_close[50] = np.nan
    bench_close[51] = np.nan  # 缺口使 bref 的 49/50/51 三个位置为 NaN

    r = risk_metrics(close, bench_close=bench_close)
    assert r["beta"] is not None
    assert r["beta"] == pytest.approx(1.0, abs=1e-4)
    assert abs(r["alpha"]) < 0.01


def test_beta_unchanged_without_gaps():
    """无缺口时结果与修复前一致（基线不回归）。"""
    from app.core.indicators.risk import risk_metrics

    rng = np.random.default_rng(0)
    rets = rng.normal(0.0005, 0.01, 300)
    close = np.cumprod(1 + rets) * 100
    r = risk_metrics(close, bench_close=np.cumprod(1 + rets) * 100)
    assert r["beta"] == pytest.approx(1.0, abs=1e-4)


# ---------------------------------------------------------------------------
# 2) API /risk 端点：基准按日期映射（基准日期轴更长时不错位）
# ---------------------------------------------------------------------------


def test_risk_endpoint_aligns_benchmark_by_date(monkeypatch):
    """基准 kline 比个股多 10 个交易日（尾部不对应个股）：
    修复后按日期映射 → beta≈1；修复前取基准尾部 300 根 → 错位 beta≠1。"""
    import app.api.indicators as I
    from app.storage import cache as CACHE

    n = 300
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0003, 0.01, n)
    stock_close = np.cumprod(1 + rets) * 100
    dates_stock = pd.date_range("2024-01-01", periods=n).astype(str)
    stock_df = pd.DataFrame({"date": dates_stock, "close": stock_close})
    # 基准：同样日期上收盘价 == 个股收盘价（理论 beta=1），但多 10 个尾部交易日
    extra = np.cumprod(1 + rng.normal(0.005, 0.02, 10)) * stock_close[-1]
    bench_close = np.concatenate([stock_close, extra])
    bench_df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n + 10).astype(str),
            "close": bench_close,
        }
    )

    import app.core.sources as Q

    monkeypatch.setattr(I, "cached_kline", lambda code, period, **kw: stock_df)
    monkeypatch.setattr(I, "get_version", lambda *a, **k: 0)
    monkeypatch.setattr(Q, "get_sina_kline", lambda *a, **k: bench_df)
    CACHE.risk_cache.clear()

    r = I.risk("600519.SH")
    assert r["beta"] is not None
    assert r["beta"] == pytest.approx(1.0, abs=1e-4), (
        f"按日期对齐后 beta 应≈1，实际 {r['beta']}（位置对齐会因尾部多出的交易日错位）"
    )
    assert abs(r["alpha"]) < 0.01


# ---------------------------------------------------------------------------
# 3) VaR 预算文案口径统一（参数法）
# ---------------------------------------------------------------------------


def test_var_budget_explanation_matches_parametric():
    from app.core.indicators.risk import RISK_EXPLANATIONS

    # 实现用 var99_param（参数法/正态假设）算预算，文案必须同口径，不能读成历史法 VaR99
    assert "参数法" in RISK_EXPLANATIONS["var_pct_budget"]
