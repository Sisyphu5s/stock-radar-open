"""forward_returns 入口校验/clamp 测试（T-44 / P0-28）。

参考 tests/test_memory_bounds.py 对 forward_returns 的既有断言风格
（test_forward_returns_keeps_float32：float32 保留 + 与 float64 路径 allclose）。
"""

import numpy as np
import pytest

from app.lib.alpha import evaluate as AE


def test_forward_returns_normal():
    """close(5,10), h=3：前 7 列 == close[:,3:]/close[:,:-3]-1，后 3 列 NaN。"""
    rng = np.random.RandomState(42)
    close = rng.uniform(10, 30, (5, 10))
    out = AE.forward_returns(close, 3)
    assert out.shape == close.shape
    np.testing.assert_allclose(
        out[:, :-3], close[:, 3:] / close[:, :-3] - 1.0, rtol=1e-6, atol=1e-8
    )
    assert np.isnan(out[:, -3:]).all()
    # float32 保留（_as_float）
    out32 = AE.forward_returns(close.astype(np.float32), 3)
    assert out32.dtype == np.float32
    np.testing.assert_allclose(out32, out, rtol=1e-4, atol=1e-6)


def test_forward_returns_clamps_horizon_to_T_minus_1():
    """h=50 且 T=10 → 等价 h=9（clamp 到 T-1，不产生空切片全 NaN）。"""
    rng = np.random.RandomState(7)
    close = rng.uniform(10, 30, (5, 10))
    out50 = AE.forward_returns(close, 50)
    out9 = AE.forward_returns(close, 9)
    np.testing.assert_allclose(out50, out9, rtol=1e-12, atol=1e-12)
    assert np.isnan(out50[:, -9:]).all()
    assert np.isfinite(out50[:, :-9]).all()


def test_forward_returns_raises_on_bad_inputs():
    """h=0 / h=-1 / T=1 → ValueError（显式暴露调用 bug，而非静默错乱）。"""
    rng = np.random.RandomState(3)
    close = rng.uniform(10, 30, (5, 10))
    with pytest.raises(ValueError, match="horizon"):
        AE.forward_returns(close, 0)
    with pytest.raises(ValueError, match="horizon"):
        AE.forward_returns(close, -1)
    with pytest.raises(ValueError, match="时间轴不足 2 列"):
        AE.forward_returns(np.ones((5, 1)), 5)


# ---------------------------------------------------------------------------
# split_panel：统一四 runner 切分口径 + 无跨段泄漏（P2-16 / P1-31）
# ---------------------------------------------------------------------------


def _panel(S=20, T=100, seed=0):
    rng = np.random.RandomState(seed)
    close = np.cumprod(1.0 + rng.randn(S, T) * 0.01, axis=1).astype(np.float32)
    return {
        "close": close,
        "volume": rng.rand(S, T).astype(np.float32),
        "open": rng.rand(S, T).astype(np.float32),
    }


def test_split_panel_matches_runner_configs():
    """P2-16: split_panel 统一四 runner 切分口径（60/20/20、80/20 无验证段）。"""
    p = _panel(T=100)
    # gp / neural_train：60/20/20
    sp = AE.split_panel(p, 5, 0.6, 0.2)
    assert sp["train"]["close"].shape == (20, 60)
    assert sp["val"]["close"].shape == (20, 20)
    assert sp["oos"]["close"].shape == (20, 20)
    assert sp["t1"] == 60 and sp["t2"] == 80
    # evaluate / factor_tune：80/20 无验证段
    sp2 = AE.split_panel(p, 5, 0.8, 0.0)
    assert sp2["train"]["close"].shape == (20, 80)
    assert sp2["oos"]["close"].shape == (20, 20)
    assert sp2["val"] is None and sp2["fwd_val"] is None
    assert sp2["t1"] == 80 and sp2["t2"] == 80


def test_split_panel_fwd_no_cross_segment_leak():
    """P1-31: 各段 forward_returns 在段内独立计算——段尾部 horizon 列全 NaN。

    修复前「完整面板 fwd 再切片」会借用后段价格算出实值（如 train 段尾
    h 列用 val 价格），泄漏判据即尾部 NaN + 与段内独立计算逐位一致。
    """
    p = _panel(T=100)
    sp = AE.split_panel(p, 5, 0.6, 0.2)
    # 段内独立 forward_returns 逐位一致
    np.testing.assert_allclose(
        sp["fwd_train"], AE.forward_returns(p["close"][:, :60], 5), equal_nan=True
    )
    np.testing.assert_allclose(
        sp["fwd_val"], AE.forward_returns(p["close"][:, 60:80], 5), equal_nan=True
    )
    np.testing.assert_allclose(
        sp["fwd_oos"], AE.forward_returns(p["close"][:, 80:], 5), equal_nan=True
    )
    # 泄漏判据：段边界（尾部 horizon 列）无前向收益
    assert np.isnan(sp["fwd_train"][:, -5:]).all()
    assert np.isnan(sp["fwd_val"][:, -5:]).all()
    assert np.isnan(sp["fwd_oos"][:, -5:]).all()
    # 修复前 fwd_train 末列有值（fwd_full[:, 59] = close[64]/close[59]-1）
    leaky = AE.forward_returns(p["close"], 5)[:, :60]
    assert not np.isnan(leaky[:, -1]).all()  # 旧语义确实泄漏，测试判据有效


def test_split_panel_cuts_all_features():
    """切分保持面板全部特征键（train/val/oos 键集合与 panel 一致）。"""
    p = _panel()
    sp = AE.split_panel(p, 5, 0.6, 0.2)
    for seg in (sp["train"], sp["val"], sp["oos"]):
        assert set(seg) == set(p)
    for key, arr in sp["train"].items():
        assert arr.shape[1] == 60
    assert sp["train"]["close"].dtype == np.float32
