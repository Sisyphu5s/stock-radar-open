"""原生滚动算子测试：C 内核 vs numpy 回退逐位一致 + pandas 参考 + backend 集成 + 性能冒烟。

约定：
  - “逐位一致” = np.array_equal(..., equal_nan=True)（C 与 numpy 回退同为
    顺序前缀和/精确极值/整数计数，浮点结果逐位相同）。
  - pandas 参考：sum/mean/max/min 用 s.rolling(w).*()（默认 min_periods=w，
    窗口内 NaN → NaN），前缀和求和顺序不同，用紧容差 allclose + NaN 位置一致。
  - rank 语义（window 内对有限元素排名并归一化）与 pandas rolling.rank 不同，
    用显式 numpy 参考实现。
  - 新算子 product/skew/kurt/decay_linear：C 为顺序累加、numpy 回退为向量化
    聚合，用 1e-12 紧容差 allclose（NaN 位置必须一致）；argmax/argmin 为
    整数位置，要求逐位一致。
"""

import os

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.lib.alpha import backend as B
from app.lib.alpha import native_ops

OPS = ["sum", "mean", "max", "min", "rank"]
FLOAT_OPS = ["product", "skew", "kurt", "decay_linear"]  # 1e-12 紧容差
INT_OPS = ["argmax", "argmin"]  # 整数位置，逐位一致
NEW_OPS = FLOAT_OPS + INT_OPS


@pytest.fixture(scope="module", autouse=True)
def numpy_backend():
    settings.gp_backend = "numpy"
    B.init_backend()
    yield


@pytest.fixture
def native_disabled(monkeypatch):
    """强制 native_ops 走 numpy 回退。"""
    monkeypatch.setattr(native_ops, "_lib", None)


GOLDEN = [
    np.array([1.0, np.nan, 3.0, 4.0, 5.0]),  # NaN 居中
    np.array([-1.0, -2.0, -3.0, -4.0]),  # 全负值
    np.array([1.0, 1.0, 1.0, 2.0, 2.0]),  # 并列值
    np.array([1.0, np.inf, 3.0, np.nan, -np.inf, 5.0]),  # ±inf + NaN
    np.array([np.nan, np.nan, np.nan]),  # 全 NaN
    np.array([0.5]),  # 单元素
    np.array([3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]),  # 常规
]


def _rank_reference(a, w):
    """backend.ts_rank 的显式参考实现（任意维度，末轴）。"""
    T = a.shape[-1]
    out = np.full_like(a, np.nan, dtype=np.float64)
    for t in range(w - 1, T):
        win = a[..., t - w + 1 : t + 1]
        x = a[..., t]
        n_valid = np.isfinite(win).sum(axis=-1)
        cnt = (win <= x[..., None]).sum(axis=-1) - 1
        rank = cnt / np.maximum(n_valid - 1, 1)
        out[..., t] = np.where((n_valid >= 2) & np.isfinite(x), rank, np.nan)
    return out


def _native_and_fallback(op, a, w):
    """返回 (C 结果, numpy 回退结果)，二者必须逐位一致。"""
    native = getattr(native_ops, "rolling_" + op)(a, w)
    lib = native_ops._lib
    native_ops._lib = None
    try:
        fallback = getattr(native_ops, "rolling_" + op)(a, w)
    finally:
        native_ops._lib = lib
    return native, fallback


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("w", [1, 2, 5, 0, -1])
def test_golden_c_matches_fallback(op, w):
    """黄金样例（NaN / w=1 / w<=0 / 负值 / 并列 / ±inf）：C 与 numpy 回退逐位一致。"""
    for a in GOLDEN:
        native, fallback = _native_and_fallback(op, a, w)
        assert native.shape == a.shape
        assert native.dtype == np.float64
        assert np.array_equal(native, fallback, equal_nan=True), (op, w, a)


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
@pytest.mark.parametrize("op", OPS)
def test_golden_values(op):
    """黄金样例数值正确性（w>=n 全 NaN；w=1 恒等/rank 全 NaN）。"""
    for a in GOLDEN:
        n = a.shape[0]
        # w >= n → 全 NaN
        assert np.isnan(getattr(native_ops, "rolling_" + op)(a, n + 3)).all()
        # w=1
        r1 = getattr(native_ops, "rolling_" + op)(a, 1)
        if op == "rank":
            assert np.isnan(r1).all()
        else:
            assert np.array_equal(r1, a, equal_nan=True)
    # 语义抽查：NaN 在窗口内 → 该窗口结果 NaN
    a = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
    assert np.isnan(native_ops.rolling_sum(a, 2)[2])
    assert np.isnan(native_ops.rolling_max(a, 2)[1])
    assert np.isnan(native_ops.rolling_mean(a, 2)[2])
    assert np.isnan(native_ops.rolling_min(a, 2)[1])
    # rank：窗口内非有限元素不参与；当前元素有限但窗口含 NaN → 仍输出
    r = native_ops.rolling_rank(np.array([1.0, np.nan, 3.0]), 3)
    assert r[2] == 1.0


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
@pytest.mark.parametrize("op", OPS)
def test_random_bit_identical(op):
    """随机数组（固定 seed）多组：C 与 numpy 回退逐位一致。"""
    rng = np.random.default_rng(2026)
    for _ in range(20):
        n = int(rng.integers(2, 80))
        a = rng.standard_normal(n)
        a[rng.random(n) < 0.25] = np.nan
        w = int(rng.integers(1, 12))
        native, fallback = _native_and_fallback(op, a, w)
        assert np.array_equal(native, fallback, equal_nan=True), (op, n, w)


@pytest.mark.parametrize("op", ["sum", "mean", "max", "min"])
@pytest.mark.parametrize("w", [1, 3, 5])
def test_pandas_reference(op, w):
    """对比 pandas rolling（min_periods=w）：NaN 位置一致 + 紧容差。"""
    rng = np.random.default_rng(7)
    for _ in range(10):
        n = int(rng.integers(4, 60))
        a = rng.standard_normal(n)
        a[rng.random(n) < 0.2] = np.nan
        native, fallback = _native_and_fallback(op, a, w)
        ref = getattr(pd.Series(a).rolling(w), op)().to_numpy(dtype=np.float64)
        assert np.array_equal(np.isnan(native), np.isnan(ref)), (op, w)
        np.testing.assert_allclose(native, ref, rtol=1e-9, atol=1e-9, equal_nan=True)


@pytest.mark.parametrize("w", [2, 4])
def test_rank_reference(w):
    """rank 与 backend.ts_rank 显式参考逐位一致（含 NaN / ±inf）。"""
    rng = np.random.default_rng(11)
    cases = list(GOLDEN)
    for _ in range(10):
        n = int(rng.integers(4, 60))
        a = rng.standard_normal(n)
        a[rng.random(n) < 0.2] = np.nan
        cases.append(a)
    for a in cases:
        native, fallback = _native_and_fallback("rank", a, w)
        ref = _rank_reference(a, w)
        assert np.array_equal(native, ref, equal_nan=True), (w, a)
        assert np.array_equal(fallback, ref, equal_nan=True), (w, a)


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
@pytest.mark.parametrize("op", OPS)
def test_multidim_last_axis(op):
    """多维输入按最后一维滚动，C 与 numpy 回退逐位一致。"""
    rng = np.random.default_rng(3)
    a = rng.standard_normal((4, 6, 17))
    a.ravel()[rng.random(a.size) < 0.15] = np.nan
    native, fallback = _native_and_fallback(op, a, 5)
    assert native.shape == a.shape
    assert np.array_equal(native, fallback, equal_nan=True)


def test_sr_native_env_switch(monkeypatch):
    """SR_NATIVE=0 强制回退开关。"""
    assert native_ops.native_available() == (native_ops._lib is not None)
    monkeypatch.setenv("SR_NATIVE", "0")
    assert native_ops.native_available() is False
    a = np.array([1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(native_ops, "_lib", None)
    assert np.array_equal(
        native_ops.rolling_sum(a, 2), [np.nan, 3.0, 5.0, 7.0], equal_nan=True
    )


def test_backend_native_matches_fallback():
    """backend.ts_sum/max/min/rank：原生路径与强制回退路径语义一致（float64 逐位）。"""
    rng = np.random.default_rng(99)
    a = rng.standard_normal((3, 40))
    a.ravel()[rng.random(a.size) < 0.2] = np.nan
    for op, fn in [
        ("sum", B.ts_sum),
        ("max", B.ts_max),
        ("min", B.ts_min),
        ("rank", B.ts_rank),
    ]:
        native = B.to_numpy(fn(a, 6))
        lib = native_ops._lib
        native_ops._lib = None
        try:
            fallback = B.to_numpy(fn(a, 6))
        finally:
            native_ops._lib = lib
        # NaN 位置必须一致；数值 float64 下逐位一致（rank/max/min 精确）
        assert np.array_equal(np.isnan(native), np.isnan(fallback)), op
        if op == "sum":
            np.testing.assert_allclose(
                native, fallback, rtol=1e-12, atol=1e-12, equal_nan=True
            )
        else:
            assert np.array_equal(native, fallback, equal_nan=True), op


def test_backend_dtype_preserved():
    """float32 输入经原生路径后 dtype 与回退一致（float32）。"""
    rng = np.random.default_rng(5)
    a = rng.standard_normal((3, 50)).astype(np.float32)
    native = B.to_numpy(B.ts_sum(a, 5))
    lib = native_ops._lib
    native_ops._lib = None
    try:
        fallback = B.to_numpy(B.ts_sum(a, 5))
    finally:
        native_ops._lib = lib
    assert native.dtype == fallback.dtype == np.float32
    assert np.array_equal(np.isnan(native), np.isnan(fallback))
    np.testing.assert_allclose(native, fallback, rtol=1e-5, atol=1e-5)


def test_backend_edge_windows():
    """边界 w 语义与原有防御一致。"""
    x = np.random.default_rng(1).standard_normal((3, 100))
    assert np.isnan(B.ts_sum(x, 0)).all()
    assert np.isnan(B.ts_max(x, 0)).all()
    assert np.isnan(B.ts_rank(x, 1)).all()
    assert np.isnan(B.ts_sum(x, 5000)).all()  # w > T
    assert np.isnan(B.ts_rank(x, 5000)).all()


def test_perf_smoke():
    """性能冒烟：T=5000, w=20，记录耗时；宽松上限防回归。"""
    rng = np.random.default_rng(0)
    a = rng.standard_normal(5000)
    results = {}
    for op in OPS:
        fn = getattr(native_ops, "rolling_" + op)
        fn(a, 20)  # 预热
        t0 = _now()
        for _ in range(20):
            fn(a, 20)
        dt_native = (_now() - t0) / 20
        lib = native_ops._lib
        native_ops._lib = None
        try:
            fn(a, 20)
            t0 = _now()
            for _ in range(20):
                fn(a, 20)
            dt_fallback = (_now() - t0) / 20
        finally:
            native_ops._lib = lib
        results[op] = (dt_native, dt_fallback)
        # 宽松上限：单次 < 50ms
        assert dt_native < 0.05, (op, dt_native)
        assert dt_fallback < 0.05, (op, dt_fallback)
    print("\n[perf] T=5000, w=20, 单次耗时(ms) native / fallback:")
    for op, (dn, df) in results.items():
        print(f"  {op:6s}: {dn * 1e3:8.3f} / {df * 1e3:8.3f}")


def _now():
    import time

    return time.perf_counter()


# ---------------------------------------------------------------------------
# 新算子：product / skew / kurt / argmax / argmin / decay_linear
# ---------------------------------------------------------------------------


def _assert_new_op(native, fallback, op, ctx):
    """浮点算子 1e-12 紧容差 + NaN 位置一致；整数位置算子逐位一致。"""
    assert native.shape == fallback.shape, (op, ctx)
    assert np.array_equal(np.isnan(native), np.isnan(fallback)), (op, ctx)
    if op in INT_OPS:
        assert np.array_equal(native, fallback, equal_nan=True), (op, ctx)
    else:
        np.testing.assert_allclose(
            native, fallback, rtol=1e-12, atol=1e-12, equal_nan=True
        )


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
@pytest.mark.parametrize("op", NEW_OPS)
@pytest.mark.parametrize("w", [1, 2, 5, 0, -1])
def test_new_ops_golden(op, w):
    """黄金样例（NaN/±inf/单元素/w=1/w<=0）：C 与 numpy 回退一致。"""
    for a in GOLDEN:
        native, fallback = _native_and_fallback(op, a, w)
        assert native.dtype == np.float64
        _assert_new_op(native, fallback, op, (w, a))


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
@pytest.mark.parametrize("op", NEW_OPS)
def test_new_ops_random(op):
    """随机数组 20 组（含 NaN/±inf，固定 seed）：C 与 numpy 回退一致。"""
    rng = np.random.default_rng(2026)
    for _ in range(20):
        n = int(rng.integers(2, 80))
        a = rng.standard_normal(n)
        a[rng.random(n) < 0.2] = np.nan
        a[rng.random(n) < 0.05] = np.inf
        a[rng.random(n) < 0.05] = -np.inf
        w = int(rng.integers(1, 12))
        native, fallback = _native_and_fallback(op, a, w)
        _assert_new_op(native, fallback, op, (n, w))


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
@pytest.mark.parametrize("op", NEW_OPS)
def test_new_ops_multidim_last_axis(op):
    """多维输入按最后一维滚动，C 与 numpy 回退一致。"""
    rng = np.random.default_rng(3)
    a = rng.standard_normal((4, 6, 17))
    a.ravel()[rng.random(a.size) < 0.15] = np.nan
    native, fallback = _native_and_fallback(op, a, 5)
    assert native.shape == a.shape
    _assert_new_op(native, fallback, op, "multidim")


def test_new_ops_w_out_of_bounds():
    """w<=0 或 w>n → 全 NaN（与 backend._rolling_reduce 防御一致）。"""
    a = np.array([1.0, 2.0, 3.0])
    for op in NEW_OPS:
        fn = getattr(native_ops, "rolling_" + op)
        assert np.isnan(fn(a, 0)).all(), op
        assert np.isnan(fn(a, -3)).all(), op
        assert np.isnan(fn(a, 7)).all(), op


def test_argmax_argmin_convention():
    """位置约定：0=当前（窗口最新），w-1=最早；首个 NaN 恒胜出（np 语义）。"""
    a = np.array([1.0, 3.0, 2.0])  # 最大值 3 位于 1 天前
    assert native_ops.rolling_argmax(a, 3)[2] == 1.0
    assert native_ops.rolling_argmin(a, 3)[2] == 2.0  # 最小值 1 位于最早
    a2 = np.array([2.0, np.nan, 1.0])  # 首个 NaN 恒胜出
    assert native_ops.rolling_argmax(a2, 3)[2] == 1.0
    assert native_ops.rolling_argmin(a2, 3)[2] == 1.0
    a3 = np.array([9.0, 9.0, 1.0])  # 并列最大值取最先出现（最早）
    assert native_ops.rolling_argmax(a3, 3)[2] == 2.0
    assert native_ops.rolling_argmin(a3, 3)[2] == 0.0


@pytest.mark.parametrize(
    "op,fn",
    [
        ("product", B.ts_product),
        ("skew", B.ts_skew),
        ("kurt", B.ts_kurt),
        ("argmax", B.ts_argmax),
        ("argmin", B.ts_argmin),
        ("decay_linear", B.decay_linear),
    ],
)
def test_backend_new_ops_native_matches_fallback(op, fn):
    """backend 新算子：原生路径与强制回退路径一致（float64）。"""
    rng = np.random.default_rng(2027)
    a = rng.standard_normal((3, 60))
    a.ravel()[rng.random(a.size) < 0.2] = np.nan
    native = B.to_numpy(fn(a, 7))
    lib = native_ops._lib
    native_ops._lib = None
    try:
        fallback = B.to_numpy(fn(a, 7))
    finally:
        native_ops._lib = lib
    _assert_new_op(native, fallback, op, "backend")


def test_new_ops_match_backend_fallback():
    """native_ops 原生结果 == backend 显式回退公式（公式一致性锚点）。"""
    rng = np.random.default_rng(13)
    a = rng.standard_normal(70)
    a[rng.random(70) < 0.15] = np.nan
    a[rng.random(70) < 0.05] = np.inf
    w = 9
    lib = native_ops._lib
    native_ops._lib = None
    try:
        ref = {
            "product": B.ts_product(a, w),
            "skew": B.ts_skew(a, w),
            "kurt": B.ts_kurt(a, w),
            "argmax": B.ts_argmax(a, w),
            "argmin": B.ts_argmin(a, w),
            "decay_linear": B.decay_linear(a, w),
        }
    finally:
        native_ops._lib = lib
    for op in NEW_OPS:
        native = getattr(native_ops, "rolling_" + op)(a, w)
        _assert_new_op(native, B.to_numpy(ref[op]), op, "backend-ref")


@pytest.mark.skipif(
    not native_ops.native_available(), reason="未构建 native/librolling.dylib"
)
def test_new_ops_perf_smoke():
    """性能冒烟：T=5000, w=20，原生单次 <100ms（宽松上限，防严重回归）。"""
    rng = np.random.default_rng(0)
    a = rng.standard_normal(5000)
    print("\n[perf-new] T=5000, w=20, 单次耗时(ms) native:")
    for op in NEW_OPS:
        fn = getattr(native_ops, "rolling_" + op)
        fn(a, 20)  # 预热
        t0 = _now()
        for _ in range(10):
            fn(a, 20)
        dt = (_now() - t0) / 10
        print(f"  {op:14s}: {dt * 1e3:8.3f}")
        assert dt < 0.1, (op, dt)
