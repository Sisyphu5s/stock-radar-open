"""设备抽象：numpy | mlx 双后端，同一套张量算子代码。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

import numpy as np

from ...config import settings
from . import native_ops

logger = logging.getLogger("stockradar.alpha.backend")

BACKEND = "numpy"
_T = None  # 张量类型模块（numpy 或 mlx.core）

# 后端初始化互斥锁(P1-56):BACKEND/_T 写入原子化。任务 handler 的 init_backend()
# 调用不在 runner._backend_lock 内(core/tasks/handlers 各文件直接调用),本锁兜底
# 全部路径——无论调用方是否持外部锁,BACKEND/_T 都不会被并发写坏(杜绝计算中途
# 后端翻转、mlx 重复初始化)。
_init_lock = threading.Lock()

# ===== MLX 隔离探测(T-120/P1-63) =====
# 无 Metal 的 headless 环境:mlx 顶层 import 可能进程级 abort(except Exception 拦不住)——
# 探测在子进程执行(abort 只影响子进程),确认可用后才允许主进程 import。
# 显式关闭:环境变量 SR_MLX_OFF=1 或 SR_GP_BACKEND=numpy(与 nn.py 同源开关)。
_PROBE_LOCK = threading.Lock()
_PROBE_RESULT: bool | None = None


def _probe_disabled() -> bool:
    return os.environ.get("SR_MLX_OFF") == "1" or os.environ.get("SR_GP_BACKEND") == "numpy"


def mlx_probe() -> bool:
    """隔离探测 mlx 可用性(子进程执行 import+冒烟,abort 只杀子进程);结果缓存,线程安全。

    通过才允许调用方在主进程 import mlx——部署/CI 无 Metal 环境即使 mlx 已安装,
    也不会再因顶层 import 触发进程级终止(P1-63)。"""
    global _PROBE_RESULT
    with _PROBE_LOCK:
        if _PROBE_RESULT is not None:
            return _PROBE_RESULT
        if _probe_disabled():
            _PROBE_RESULT = False
            return False
        try:
            code = "import mlx.core, mlx.nn; mlx.core.array([0.0, 0.0])"
            r = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                timeout=15,
            )
            _PROBE_RESULT = r.returncode == 0
        except Exception:  # noqa: BLE001 子进程超时/解释器异常 → 视为不可用
            _PROBE_RESULT = False
        if not _PROBE_RESULT:
            logger.warning(
                "mlx 隔离探测未通过，使用 numpy 后端（SR_MLX_OFF=1 可显式跳过探测）"
            )
        return _PROBE_RESULT


def _try_mlx():
    global BACKEND, _T
    # T-120:import 前先隔离探测——无 Metal 环境 import mlx.core 可能 abort,
    # 探测失败直接回退 numpy,主进程不触碰 mlx
    if not mlx_probe():
        _T = np
        return np
    try:
        import mlx.core as mx

        mx.array(np.zeros(2))  # 冒烟测试
        BACKEND = "mlx"
        _T = mx
        logger.info("Alpha 计算后端: mlx (Apple GPU)")
        return mx
    except Exception as e:
        logger.warning("mlx 不可用(%s)，使用 numpy", str(e)[:80])
        _T = np
        return np


def _native_roll(op: str, a, w: int):
    """优先调用原生滚动算子内核（sum/max/min/rank/product/skew/kurt/
    argmax/argmin/decay_linear）。

    返回 None 表示不可用或失败，由调用方走原 Python/numpy 回退路径。
    NaN 位置与回退实现完全一致；数值因前缀和/聚合算法不同存在 ≤1e-9
    相对误差；输出 dtype 还原为输入 dtype，mlx 后端经 from_numpy 转回。
    """
    if not native_ops.native_available():
        return None
    try:
        na = to_numpy(a)
    except Exception:
        return None
    if na.dtype not in (np.float32, np.float64):
        return None  # 非浮点输入保持原实现 dtype 语义
    try:
        out = getattr(native_ops, "rolling_" + op)(na, w)
        if na.dtype != np.float64:
            out = out.astype(na.dtype)
        return from_numpy(out)
    except Exception as e:
        logger.debug("原生 rolling_%s 回退 numpy: %s", op, e)
        return None


def init_backend() -> None:
    global BACKEND, _T
    with _init_lock:
        if settings.gp_backend == "numpy":
            BACKEND, _T = "numpy", np
        elif settings.gp_backend == "mlx":
            _try_mlx()
        else:
            _try_mlx()


def xp() -> np | None:
    return _T


def backend_name() -> str:
    return BACKEND


def to_numpy(arr) -> np.ndarray:
    if BACKEND == "mlx":
        import mlx.core as mx

        if isinstance(arr, mx.array):
            return np.array(arr)
    return np.asarray(arr)


def from_numpy(arr: np.ndarray):
    if BACKEND == "mlx":
        return _T.array(arr)
    return arr


def shift(a, k: int):
    """时间平移：k>0 表示滞后（out[t]=a[t-k]，用过去值）；k<0 表示前移（out[t]=a[t+|k|]，用未来值）。边界填 NaN。"""
    T = a.shape[-1]
    out = _T.full_like(a, np.nan)
    if k > 0:
        out[..., k:] = a[..., : T - k]
    elif k < 0:
        out[..., : T + k] = a[..., -k:]
    else:
        out = a
    return out


def _safe_div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        r = _T.where(
            _T.abs(b) < 1e-12,
            _T.full_like(b, np.nan),
            a / _T.where(_T.abs(b) < 1e-12, 1.0, b),
        )
    return r


def ts_mean(a, w: int):
    """滚动均值。窗口内任一 NaN → 该窗口结果 NaN；前缀不足 w（t<w-1）→ NaN；
    w>T → 全 NaN；w<=1 恒等返回。
    优先原生内核 rolling_mean（O(n) 前缀和）；回退 cumsum+NaN 计数（NaN 置 0 后
    前缀和，配 NaN 计数前缀和，思路同 native_ops._fallback_sum），避免原 cumsum
    技巧中中途 NaN 永久污染后续窗口。ts_std/ts_corr 均由 ts_mean 组合而成，
    NaN 语义自动继承，无需改公式。"""
    if w <= 1:
        return a
    T = a.shape[-1]
    if w > T:
        return _T.full_like(a, np.nan)
    r = _native_roll("mean", a, w)
    if r is not None:
        return r
    # 回退：NaN 置 0 后 cumsum 得 cs；isnan 前缀和得 nnan
    nan_mask = _T.isnan(a)
    cs = _T.cumsum(_T.where(nan_mask, _T.zeros(a.shape, dtype=a.dtype), a), axis=-1)
    nnan = _T.cumsum(nan_mask.astype(cs.dtype), axis=-1)
    # 窗口和/NaN 计数 = 前缀差（前补 w 个 0 对齐长度，参考原实现写法）
    zeros = _T.zeros(a.shape[:-1] + (w,), dtype=cs.dtype)
    shift_cs = _T.concatenate([zeros, cs[..., :-w]], axis=-1)
    shift_nan = _T.concatenate([zeros, nnan[..., :-w]], axis=-1)
    win_sum = cs - shift_cs
    win_nan = nnan - shift_nan
    # 前缀不足 w 的位置输出 NaN；凡窗口含 NaN 的窗口置 NaN
    out = _T.full_like(a, np.nan)
    win_mean = win_sum / w
    out[..., w - 1 :] = _T.where(
        win_nan[..., w - 1 :] > 0,
        _T.full_like(win_sum[..., w - 1 :], np.nan),
        win_mean[..., w - 1 :],
    )
    return out


def ts_std(a, w: int):
    mean = ts_mean(a, w)
    sq = ts_mean(a * a, w)
    with np.errstate(invalid="ignore"):
        var = _T.maximum(sq - mean * mean, 0.0)
    return _T.sqrt(var)


def ts_rank(a, w: int):
    """滚动排名（0-1 分位）。O(T*W) 比较法；NaN 不参与比较（输出 NaN），
    w=1 时单元素窗口无秩可排，返回 NaN。
    优先走原生内核 rolling_rank（NaN/边界语义与下方回退实现逐位一致）。"""
    r = _native_roll("rank", a, w)
    if r is not None:
        return r
    if w <= 1:
        return _T.full_like(a, np.nan)  # w=1 无法排名
    T = a.shape[-1]
    out = _T.full_like(a, np.nan)
    for t in range(w - 1, T):
        win = a[..., t - w + 1 : t + 1]
        x = a[..., t : t + 1]
        n_valid = _T.sum(_T.isfinite(win), axis=-1)
        x_fin = _T.where(_T.isfinite(x), x, _T.full_like(x, np.nan))
        # 仅有限元素参与计数（NaN<=x 恒为 False，天然剔除）
        cnt = _T.sum(win <= x_fin, axis=-1) - 1
        rank = cnt / _T.maximum(n_valid - 1, 1)
        out[..., t] = _T.where(
            (n_valid >= 2) & _T.isfinite(x[..., 0]),
            rank,
            _T.full_like(x[..., 0], np.nan),
        )
    return out


def ts_corr(a, b, w: int):
    ma = ts_mean(a, w)
    mb = ts_mean(b, w)
    cov = ts_mean(a * b, w) - ma * mb
    va = _T.sqrt(_T.maximum(ts_mean(a * a, w) - ma * ma, 0.0))
    vb = _T.sqrt(_T.maximum(ts_mean(b * b, w) - mb * mb, 0.0))
    return _safe_div(cov, va * vb)


def rank(a):
    """横截面排名（0-1）：沿倒数第二维（股票维度）。
    NaN 保持 NaN、不参与排名；按每期有效股票数归一化（仅 1 只有效时返回中性值 0.5）。"""
    if a.ndim < 2:
        return a * 0.0 + 0.5
    finite = _T.isfinite(a)
    # NaN → +inf 排最后：有限值的秩仍为 0..n_valid-1，随后对 NaN 位置掩码为 NaN
    fill = _T.where(finite, a, _T.full_like(a, np.inf))
    sort_idx = _T.argsort(fill, axis=-2)
    ranks = _T.argsort(sort_idx, axis=-2).astype(a.dtype)
    n_valid = _T.sum(finite, axis=-2, keepdims=True)
    ranks = ranks / _T.maximum(n_valid - 1, 1)
    return _T.where(
        finite,
        _T.where(n_valid >= 2, ranks, _T.full_like(a, 0.5)),
        _T.full_like(a, np.nan),
    )


def _rolling_reduce(a, w: int, fn):
    """通用滚动聚合（O(T*W)，w 小可接受）。w<=0 或 w>T 时无有效窗口，返回全 NaN。"""
    T = a.shape[-1]
    if w <= 0:
        return _T.full_like(a, np.nan)
    out = _T.full_like(a, np.nan)
    for t in range(w - 1, T):
        win = a[..., t - w + 1 : t + 1]
        out[..., t] = fn(win, axis=-1)
    return out


def ts_min(a, w: int):
    """滚动最小值。优先原生内核（O(n) 单调队列）；回退 _rolling_reduce（原实现）。"""
    r = _native_roll("min", a, w)
    if r is not None:
        return r
    return _rolling_reduce(a, w, lambda x, axis: _T.min(x, axis=axis))


def ts_max(a, w: int):
    """滚动最大值。优先原生内核（O(n) 单调队列）；回退 _rolling_reduce（原实现）。"""
    r = _native_roll("max", a, w)
    if r is not None:
        return r
    return _rolling_reduce(a, w, lambda x, axis: _T.max(x, axis=axis))


def ts_sum(a, w: int):
    """滚动求和。优先原生内核（O(n) 前缀和）；回退 _rolling_reduce（原实现）。"""
    r = _native_roll("sum", a, w)
    if r is not None:
        return r
    return _rolling_reduce(a, w, lambda x, axis: _T.sum(x, axis=axis))


def ts_product(a, w: int):
    """滚动连乘。优先原生内核；回退 _rolling_reduce（原实现，NaN 传播一致）。"""
    r = _native_roll("product", a, w)
    if r is not None:
        return r
    return _rolling_reduce(a, w, lambda x, axis: _T.prod(x, axis=axis))


def ts_skew(a, w: int):
    """滚动偏度（O(T*W)）。优先原生内核；回退公式与原生逐位一致。"""
    r = _native_roll("skew", a, w)
    if r is not None:
        return r

    def _sk(x, axis):
        m = _T.mean(x, axis=axis, keepdims=True)
        v = _T.mean((x - m) ** 2, axis=axis)
        s = _T.mean((x - m) ** 3, axis=axis)
        return _T.where(_T.abs(v) < 1e-12, _T.zeros_like(s), s / (v**1.5 + 1e-12))

    return _rolling_reduce(a, w, _sk)


def ts_kurt(a, w: int):
    """滚动超额峰度。优先原生内核；回退公式与原生逐位一致。"""
    r = _native_roll("kurt", a, w)
    if r is not None:
        return r

    def _ku(x, axis):
        m = _T.mean(x, axis=axis, keepdims=True)
        v = _T.mean((x - m) ** 2, axis=axis)
        q = _T.mean((x - m) ** 4, axis=axis)
        return _T.where(_T.abs(v) < 1e-12, _T.zeros_like(q), q / (v**2 + 1e-12) - 3.0)

    return _rolling_reduce(a, w, _ku)


def ts_count(a, w: int):
    """滚动窗口内非 NaN 数量。"""
    valid = _T.where(_T.isnan(a), _T.zeros_like(a), _T.ones_like(a))
    return ts_sum(valid, w)


def decay_linear(a, w: int):
    """线性衰减加权均值：权重 w, w-1, ..., 1。优先原生内核；回退原实现。"""
    r = _native_roll("decay_linear", a, w)
    if r is not None:
        return r
    T = a.shape[-1]
    weights = _T.arange(w, 0, -1, dtype=a.dtype)
    out = _T.full_like(a, np.nan)
    for t in range(w - 1, T):
        win = a[..., t - w + 1 : t + 1]
        wsum = _T.sum(win * weights, axis=-1)
        out[..., t] = wsum / (w * (w + 1) / 2)
    return out


def winsorize(a, pct: float = 0.01):
    """横截面缩尾（每期按 pct 分位截断）。"""
    lo = _T.quantile(a, pct, axis=-2, keepdims=True)
    hi = _T.quantile(a, 1 - pct, axis=-2, keepdims=True)
    return _T.clip(a, lo, hi)


def scale(a):
    """横截面标准化（z-score）。"""
    m = _T.mean(a, axis=-2, keepdims=True)
    v = _T.var(a, axis=-2, keepdims=True)
    return (a - m) / (_T.sqrt(v) + 1e-12)


def ts_argmax(a, w: int):
    """滚动窗口内最大值相对位置（0..w-1，从窗口尾向前计数：0=当前，
    w-1=窗口最早元素；NaN 语义同 np.argmax：首个 NaN 恒胜出）。
    优先原生内核；回退 _rolling_reduce（np.argmax + 尾部计数）。"""
    r = _native_roll("argmax", a, w)
    if r is not None:
        return r

    def _f(x, axis):
        return (w - 1) - _T.argmax(x, axis=axis)

    return _rolling_reduce(a, w, _f)


def ts_argmin(a, w: int):
    """滚动窗口内最小值相对位置（0=当前，从窗口尾向前计数；NaN 语义同 np.argmin）。
    优先原生内核；回退 _rolling_reduce。"""
    r = _native_roll("argmin", a, w)
    if r is not None:
        return r

    def _f(x, axis):
        return (w - 1) - _T.argmin(x, axis=axis)

    return _rolling_reduce(a, w, _f)
