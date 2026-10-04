"""T-10 组合优化测试:optimize 数值(等权协方差特例/仅多约束/上限/解析解)、
端点参数校验与正常流程、backtest_opt handler 注册与实验白名单。"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import HTTPException

from app.lib.alpha.optimize import (
    _project_weights,
    mean_variance,
    min_variance,
    portfolio_metrics,
    risk_parity,
    turnover_vs_equal,
)


# ---------------------------------------------------------------------------
# min_variance
# ---------------------------------------------------------------------------


def test_min_variance_identity_cov_equal_weight():
    """等权协方差特例:Σ=I → 最小方差 = 等权。"""
    w = min_variance(np.eye(5))
    assert np.allclose(w, 0.2, atol=1e-6)
    assert np.isclose(w.sum(), 1.0)


def test_min_variance_analytic_2x2():
    """2x2 协方差解析对照:w ∝ Σ⁻¹1(无上限时)。"""
    cov = np.array([[1.0, 0.5], [0.5, 2.0]])
    w = min_variance(cov)
    inv = np.linalg.inv(cov)
    one = np.ones(2)
    sol = inv @ one / (one @ inv @ one)
    assert np.allclose(w, sol, atol=1e-4)


def test_min_variance_long_only_and_sum_one():
    """仅多约束:权重全非负且和为 1(随机对称协方差)。"""
    rng = np.random.default_rng(7)
    a = rng.normal(size=(20, 8))
    cov = a @ a.T + np.eye(20)
    w = min_variance(cov, max_weight=0.2)
    assert w.min() >= -1e-9
    assert np.isclose(w.sum(), 1.0)
    assert w.max() <= 0.2 + 1e-9


def test_min_variance_max_weight_binding():
    """单股权重上限约束:max_weight=0.6 且等权 1/3 可行 → 等权;上限触及时被压制。"""
    # 等权在可行域内 → 即最优
    w = min_variance(np.eye(3), max_weight=0.6)
    assert np.allclose(w, 1 / 3, atol=1e-6)
    # 无上限时全押单只(对角线 1 vs 其它 100),上限 0.8 强制分散
    cov = np.diag([1.0, 100.0, 100.0])
    w2 = min_variance(cov, max_weight=0.8)
    assert w2.max() <= 0.8 + 1e-9
    assert np.isclose(w2.sum(), 1.0)


def test_min_variance_infeasible_max_weight():
    """不可行约束:上限×数量 < 1 → ValueError。"""
    with pytest.raises(ValueError):
        min_variance(np.eye(3), max_weight=0.3)


def test_min_variance_zero_variance_asset():
    """零方差资产(收益恒定):风险平价剔除、最小方差正常(退化 Σ 半正定)。"""
    cov = np.diag([0.0, 1.0, 4.0])
    w = min_variance(cov)  # 半正定可解
    assert np.isclose(w.sum(), 1.0)


# ---------------------------------------------------------------------------
# mean_variance
# ---------------------------------------------------------------------------


def test_mean_variance_analytic_kkT():
    """均值方差解析解(λ=1, Σ=I, 无上限):KKT 得 w = (μ + ν)/λ,Σw=1。"""
    mu = np.array([0.1, 0.2, 0.3])
    w = mean_variance(mu, np.eye(3), risk_aversion=1.0)
    nu = 1.0 / 3 - mu.mean()
    sol = mu + nu  # w = μ + ν(λ=1)
    assert np.allclose(w, sol, atol=1e-4)


def test_mean_variance_zero_risk_aversion_greedy():
    """λ=0:纯 μ 最大化,贪心按 μ 降序填满 max_weight。"""
    w = mean_variance(
        np.array([0.1, 0.2, 0.3]), np.eye(3), risk_aversion=0.0, max_weight=0.5
    )
    assert np.allclose(w, [0.0, 0.5, 0.5], atol=1e-6)


def test_mean_variance_risk_aversion_limits():
    """λ→大 收敛到最小方差(等权 I 协方差);λ=0 全押最大 μ。"""
    mu = np.array([0.05, 0.06, 0.07])
    w_high = mean_variance(mu, np.eye(3), risk_aversion=1e4)
    assert np.allclose(w_high, 1 / 3, atol=1e-3)


def test_mean_variance_infeasible():
    with pytest.raises(ValueError):
        mean_variance(
            np.array([0.1, 0.2]), np.eye(3), risk_aversion=1.0, max_weight=0.4
        )


# ---------------------------------------------------------------------------
# risk_parity
# ---------------------------------------------------------------------------


def test_risk_parity_diagonal_inverse_vol():
    """对角协方差:风险平价 = 逆波动率加权 w ∝ 1/σ。"""
    w = risk_parity(np.diag([1.0, 4.0, 16.0]))
    sol = np.array([1.0, 0.5, 0.25])
    sol = sol / sol.sum()
    assert np.allclose(w, sol, atol=1e-4)


def test_risk_parity_zero_variance():
    """零方差资产剔除:权重置 0,其余资产重归一。"""
    w = risk_parity(np.diag([0.0, 1.0, 4.0]))
    assert w[0] == pytest.approx(0.0, abs=1e-9)
    assert np.isclose(w.sum(), 1.0)
    assert w[1] > w[2]  # 低波动权重大


def test_risk_parity_equality_of_marginal_risk():
    """边际风险贡献相等:w_i·(Σw)_i 各项一致(严格风险平价)。"""
    rng = np.random.default_rng(3)
    a = rng.normal(size=(15, 6))
    cov = a @ a.T + np.eye(15)
    w = risk_parity(cov)
    mrc = w * (cov @ w)
    assert np.allclose(mrc, mrc.mean(), rtol=5e-3)


# ---------------------------------------------------------------------------
# 投影 / 绩效 / 换手
# ---------------------------------------------------------------------------


def test_project_weights_cases():
    assert np.allclose(
        _project_weights(np.array([0.7, 0.7, 0.1]), 0.6), [0.5, 0.5, 0.0], atol=1e-6
    )
    # 负分量:投影回单纯形边界(全押第一只)
    assert np.allclose(
        _project_weights(np.array([-1.0, -2.0]), None), [1.0, 0.0], atol=1e-6
    )
    # 无上限非负
    assert np.allclose(
        _project_weights(np.array([0.3, 0.3, 0.3]), None), [1 / 3] * 3, atol=1e-6
    )


def test_portfolio_metrics_known_returns():
    rng = np.random.default_rng(11)
    ret = rng.normal(0.0005, 0.01, size=(3, 252))
    w = np.array([0.5, 0.3, 0.2])
    m = portfolio_metrics(ret, w)
    combo = w @ np.where(np.isfinite(ret), ret, 0.0)
    assert m["annual_return"] == pytest.approx(float(combo.mean()) * 252, abs=1e-3)
    assert m["volatility"] == pytest.approx(
        float(np.std(combo)) * np.sqrt(252), abs=1e-3
    )
    assert m["sharpe"] == pytest.approx(
        (float(combo.mean()) * 252) / (float(np.std(combo)) * np.sqrt(252)), abs=1e-3
    )


def test_turnover_vs_equal():
    # 等权 → 换手 0
    assert turnover_vs_equal(np.array([1 / 3] * 3)) == pytest.approx(0.0, abs=1e-9)
    # [0.5,0.5,0] vs 等权 1/3:0.5·(|0.5-1/3|·2 + |0-1/3|) = 1/3
    assert turnover_vs_equal(np.array([0.5, 0.5, 0.0])) == pytest.approx(
        1 / 3, abs=1e-6
    )


# ---------------------------------------------------------------------------
# 端点 /alpha/optimize(路由函数直调 + monkeypatch load_panel)
# ---------------------------------------------------------------------------


def _fake_panel(S=40, T=120, seed=1):
    rng = np.random.default_rng(seed)
    close = np.exp(np.cumsum(rng.normal(0.0005, 0.01, size=(S, T)), axis=1)) * 10
    return {
        "panel": {"close": close.astype(np.float32)},
        "dates": [f"2024-01-{i % 28 + 1:02d}" for i in range(T)],
        "codes": [f"{600000 + i}.SH" for i in range(S)],
    }


def test_optimize_endpoint_ok(monkeypatch):
    from app.api.alpha import alpha_optimize
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    res = alpha_optimize(
        {
            "expression": "rank(close)",
            "dataset_id": 1,
            "horizon": 5,
            "method": "min_var",
            "max_weight": 0.2,
            "top_n": 20,
        }
    )
    assert len(res["weights"]) == 20
    assert res["weights"][0]["code"].endswith(".SH")
    assert abs(sum(x["weight"] for x in res["weights"]) - 1.0) < 1e-4
    assert all(x["weight"] <= 0.2 + 1e-6 for x in res["weights"])
    assert res["perf"]["annual_return"] is not None
    assert 0.0 <= res["turnover"] <= 1.0


def test_optimize_endpoint_mv_risk_parity(monkeypatch):
    from app.api.alpha import alpha_optimize
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel(seed=2))
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    for method in ("mv", "risk_parity", "min_var"):
        res = alpha_optimize(
            {
                "expression": "rank(close)",
                "dataset_id": 1,
                "method": method,
                "max_weight": 0.1,
            }
        )
        assert res["method"] == method
        assert abs(sum(x["weight"] for x in res["weights"]) - 1.0) < 1e-4


def test_optimize_endpoint_guards(monkeypatch):
    from app.api.alpha import alpha_optimize
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    with pytest.raises(HTTPException) as e:
        alpha_optimize({"dataset_id": 1})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        alpha_optimize({"expression": "rank(close)", "dataset_id": 0})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        alpha_optimize(
            {
                "expression": "rank(close)",
                "dataset_id": 1,
                "method": "nope",
                "max_weight": 0.2,
            }
        )
    assert e.value.status_code == 400
    # 不可行:max_weight×top_n < 1
    with pytest.raises(HTTPException) as e:
        alpha_optimize(
            {
                "expression": "rank(close)",
                "dataset_id": 1,
                "method": "mv",
                "max_weight": 0.01,
            }
        )
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# handler 注册 / 实验白名单
# ---------------------------------------------------------------------------


def test_backtest_opt_registered():
    # 必须先 import runner(模块末尾触发 registry.register)
    from app.core.tasks import registry, runner  # noqa: F401

    assert "backtest_opt" in registry.HANDLERS
    assert callable(registry.HANDLERS["backtest_opt"])


def test_backtest_opt_in_experiment_whitelist(monkeypatch):
    from app.api import experiments

    seen = {}

    def fake_submit(job_type, params):
        seen["job_type"] = job_type
        return 1

    monkeypatch.setattr(experiments, "submit", fake_submit)
    res = experiments.create_experiment(
        {
            "job_type": "backtest_opt",
            "params": {"expression": "rank(close)", "dataset_id": 1},
        }
    )
    assert seen["job_type"] == "backtest_opt"
    assert res["status"] == "pending"
    with pytest.raises(HTTPException) as e:
        experiments.create_experiment({"job_type": "unknown_type", "params": {}})
    assert e.value.status_code == 400


def test_backtest_opt_label_and_runners():
    from app.core.tasks import runner

    assert runner._JOB_LABELS["backtest_opt"] == "组合优化回测"
    assert runner._RUNNERS["backtest_opt"] == "_run_backtest_opt"
    assert callable(runner._run_backtest_opt)
