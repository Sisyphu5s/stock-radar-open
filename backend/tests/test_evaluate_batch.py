"""evaluate_batch 批量求值测试：numpy 逐位一致 / mlx 共享上载一致 / 异构批一致。

背景（T2）：mlx 后端原实现每条表达式对每个特征做
np.asarray 下载 + from_numpy 重上载（双重转换，批量 120 条 × 5 特征 = 600 次往返）。
现 evaluate_batch 在 mlx 后端先整体上载一次，全部表达式共享同一批 GPU 张量；
numpy 后端保持零拷贝直传。

约定：
  - numpy 后端：批量 vs 逐条必须**逐位一致**（np.array_equal(equal_nan=True)，
    NaN 位置一致）。
  - mlx 后端：批量 vs 逐条用 1e-5 容差 allclose + NaN 位置一致
    （GPU 浮点路径与逐条上载路径可能产生微小舍入差）。
"""

import numpy as np
import pytest

from app.config import settings
from app.lib.alpha import backend as B
from app.lib.alpha.operators import compile_rpn, evaluate_batch, evaluate_rpn

S, T = 50, 120
FEATURES = ["close", "open", "high", "low", "volume"]

# 12 条异构模板：算子组合/嵌套深度/长度各不相同
_EXPRS = [
    "rank(close) - rank(volume)",
    "ts_mean(close, 5) + delta(close, 2)",
    "ts_corr(close, volume, 5)",
    "ts_delay(close, 1) / close - 1",
    "rank(ts_mean(close, 10)) - 0.5",
    "sign(delta(close, 1)) * rank(volume)",
    "ts_std(close, 5) / ts_mean(close, 20)",
    "max2(close, ts_delay(close, 1)) - min2(close, ts_delay(close, 1))",
    "ts_rank(close, 20) * 2 - 1",
    "rank(open - ts_mean(high, 5))",
    "signed_power(close, 2) - ts_mean(close, 5)",
    "abs(ts_delay(close, 3)) + log(volume)",
]


def _mlx_ok() -> bool:
    try:
        import mlx.core  # noqa: F401

        return True
    except Exception:
        return False


@pytest.fixture
def backend(request):
    """切换全局后端并在用例结束后恢复原设置。"""
    if request.param == "mlx" and not _mlx_ok():
        pytest.skip("mlx 不可用")
    prev = settings.gp_backend
    settings.gp_backend = request.param
    B.init_backend()
    yield request.param
    settings.gp_backend = prev
    B.init_backend()


def _make_panel(seed: int, names=FEATURES) -> dict:
    """(S, T) float32 面板，稀疏注入 NaN（5%）。"""
    rng = np.random.default_rng(seed)
    panel = {}
    for name in names:
        x = rng.standard_normal((S, T)).astype(np.float32)
        x[rng.random((S, T)) < 0.05] = np.nan
        panel[name] = x
    return panel


def _make_rpns(n: int, seed: int) -> list:
    """n 条异构 RPN（长度/算子不同）。"""
    rng = np.random.default_rng(seed)
    rpns = []
    for i in range(n):
        expr = _EXPRS[i % len(_EXPRS)]
        rpns.append(compile_rpn(expr))
    assert len(rpns) == n
    return rpns


@pytest.mark.parametrize("backend", ["numpy"], indirect=True)
def test_numpy_batch_bit_identical(backend):
    """测试 1：numpy 后端，40 条异构表达式批量 vs 逐条逐位一致（NaN 位置一致）。"""
    panel = _make_panel(1)
    rpns = _make_rpns(40, 2)
    batch = evaluate_batch(rpns, panel, padded=True)
    assert len(batch) == len(rpns)
    for rpn, out in zip(rpns, batch):
        ref = evaluate_rpn(rpn, panel)
        assert out.dtype == ref.dtype
        assert out.shape == ref.shape == (S, T)
        assert np.array_equal(out, ref, equal_nan=True), expr_str_of(rpn)


@pytest.mark.parametrize("backend", ["mlx"], indirect=True)
def test_mlx_batch_matches_single(backend):
    """测试 2：mlx 后端，40 条异构表达式批量（共享一次上载）vs 逐条 allclose。"""
    panel = _make_panel(4)
    rpns = _make_rpns(40, 5)
    batch = evaluate_batch(rpns, panel, padded=True)
    assert len(batch) == len(rpns)
    for rpn, out in zip(rpns, batch):
        ref = evaluate_rpn(rpn, panel)
        assert out.shape == ref.shape == (S, T)
        assert np.array_equal(np.isnan(out), np.isnan(ref)), expr_str_of(rpn)
        np.testing.assert_allclose(out, ref, rtol=1e-5, atol=1e-5, equal_nan=True)


@pytest.mark.parametrize("backend", ["mlx"], indirect=True)
def test_mlx_mixed_input_shared_upload(backend):
    """mlx 后端混合输入：部分特征已是 mx.array float32、部分仍为 numpy，
    批量结果与纯 numpy 面板逐条参考一致（已上载特征被直接复用）。"""
    panel = _make_panel(6)
    mixed = {k: (B.from_numpy(v) if k == "close" else v) for k, v in panel.items()}
    rpns = _make_rpns(40, 7)
    batch = evaluate_batch(rpns, mixed, padded=True)
    for rpn, out in zip(rpns, batch):
        ref = evaluate_rpn(rpn, panel)
        assert np.array_equal(np.isnan(out), np.isnan(ref)), expr_str_of(rpn)
        np.testing.assert_allclose(out, ref, rtol=1e-5, atol=1e-5, equal_nan=True)


@pytest.mark.parametrize("backend", ["numpy", "mlx"], indirect=True)
def test_heterogeneous_batch_40(backend):
    """测试 3：批量 40 条异构表达式（长度不同、算子不同）正确求值，与逐条一致。"""
    panel = _make_panel(8)
    rpns = _make_rpns(40, 9)
    lens = {len(r) for r in rpns}
    assert len(lens) >= 3, "模板应保证长度异构"
    batch = evaluate_batch(rpns, panel, padded=True)
    for rpn, out in zip(rpns, batch):
        ref = evaluate_rpn(rpn, panel)
        if backend == "numpy":
            assert np.array_equal(out, ref, equal_nan=True), expr_str_of(rpn)
        else:
            assert np.array_equal(np.isnan(out), np.isnan(ref)), expr_str_of(rpn)
            np.testing.assert_allclose(out, ref, rtol=1e-5, atol=1e-5, equal_nan=True)


@pytest.mark.parametrize("backend", ["numpy", "mlx"], indirect=True)
def test_identical_rpn_lockstep(backend):
    """完全同构 RPN：锁步分支与逐条一致。"""
    panel = _make_panel(3)
    rpn = compile_rpn("rank(ts_mean(close, 5)) - 0.5")
    batch = evaluate_batch([rpn] * 8, panel, padded=True)
    ref = evaluate_rpn(rpn, panel)
    assert len(batch) == 8
    for out in batch:
        if backend == "numpy":
            assert np.array_equal(out, ref, equal_nan=True)
        else:
            np.testing.assert_allclose(out, ref, rtol=1e-5, atol=1e-5, equal_nan=True)


@pytest.mark.parametrize("backend", ["numpy", "mlx"], indirect=True)
def test_padded_false_matches(backend):
    """padded=False 逐条路径与 padded=True 结果一致。"""
    panel = _make_panel(10)
    rpns = _make_rpns(20, 11)
    refs = evaluate_batch(rpns, panel, padded=True)
    outs = evaluate_batch(rpns, panel, padded=False)
    for out, ref in zip(outs, refs):
        if backend == "numpy":
            assert np.array_equal(out, ref, equal_nan=True)
        else:
            assert np.array_equal(np.isnan(out), np.isnan(ref))
            np.testing.assert_allclose(out, ref, rtol=1e-5, atol=1e-5, equal_nan=True)


def expr_str_of(rpn) -> str:
    """测试断言失败时的可读上下文。"""
    return "|".join(ins["op"] for ins in rpn)
