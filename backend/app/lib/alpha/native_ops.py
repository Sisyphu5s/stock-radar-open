"""原生滚动窗口算子：ctypes 封装 C 内核（backend/native/librolling.dylib）。

导出 rolling_sum / rolling_mean / rolling_max / rolling_min / rolling_rank /
rolling_product / rolling_skew / rolling_kurt / rolling_argmax / rolling_argmin /
rolling_decay_linear，输入任意维度 numpy 数组（对最后一维做滚动窗口），
返回 float64 ndarray：

  - 原生可用（dylib 加载成功且未设置 SR_NATIVE=0）时走 C 内核；
  - 否则走 numpy 回退实现，语义与 C 逐位一致（sum/mean 同为顺序前缀和，
    max/min 为精确极值，rank 为整数计数 + 一次 IEEE 除法，skew/kurt/
    product/decay_linear 为同式窗口聚合，argmax/argmin 为 np.argmax 语义
    + 尾部计数）。

NaN 语义：
  - sum/mean/max/min：窗口内出现 NaN → 该窗口结果 NaN（与 pandas
    rolling(min_periods=w) 一致）；w <= 0 或 w > n 时输出全 NaN。
  - rank：窗口内有限元素排名（1..k），输出当前元素归一化名次
    (rank-1)/(k-1)；当前元素非有限或有效元素 < 2 → NaN（与
    backend.ts_rank 一致）。
  - product/skew/kurt/decay_linear：窗口内任一 NaN → 结果 NaN。
  - argmax/argmin：窗口内极值相对位置（0=当前，从窗口尾向前计数）；
    窗口含 NaN → 首个 NaN 的位置（与 np.argmax/np.argmin 一致）。
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger("stockradar.alpha.native_ops")

_LIB_PATH = Path(
    os.environ.get(
        "SR_NATIVE_LIB",
        Path(__file__).resolve().parents[3] / "native" / "librolling.dylib",
    )
)

_lib = None
if os.environ.get("SR_NATIVE", "1") != "0":
    try:
        _cand = ctypes.CDLL(str(_LIB_PATH))
        for _name in (
            "rolling_sum",
            "rolling_mean",
            "rolling_max",
            "rolling_min",
            "rolling_rank",
            "rolling_product",
            "rolling_skew",
            "rolling_kurt",
            "rolling_argmax",
            "rolling_argmin",
            "rolling_decay_linear",
        ):
            getattr(_cand, _name).argtypes = [
                ctypes.POINTER(ctypes.c_double),
                ctypes.c_int64,
                ctypes.c_int64,
                ctypes.POINTER(ctypes.c_double),
            ]
            getattr(_cand, _name).restype = ctypes.c_int
        _lib = _cand
        logger.info("原生滚动算子已加载: %s", _LIB_PATH)
    except Exception as e:  # pragma: no cover - 取决于构建环境
        _lib = None
        logger.warning("原生滚动算子不可用(%s)，使用 numpy 回退", str(e)[:120])


def native_available() -> bool:
    """是否走原生路径。SR_NATIVE=0 强制回退 numpy。"""
    if os.environ.get("SR_NATIVE", "1") == "0":
        return False
    return _lib is not None


def _prep(a: np.ndarray, w: int) -> tuple[np.ndarray, int]:
    a = np.asarray(a)
    if a.ndim == 0:
        a = a.reshape(1)
    w = int(w)
    return np.ascontiguousarray(a, dtype=np.float64), w


def _call_c(name: str, a64: np.ndarray, w: int) -> np.ndarray:
    """调用 C 内核。a64 为 float64 C-contiguous；对最后一维滚动。"""
    n = a64.shape[-1]
    out = np.empty_like(a64, dtype=np.float64)
    fn = getattr(_lib, name)
    if a64.ndim == 1:
        rc = fn(
            a64.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_int64(n),
            ctypes.c_int64(w),
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        )
        if rc != 0:
            raise RuntimeError(f"{name} 返回错误码 {rc}")
        return out
    rows = a64.reshape(-1, n)
    orows = out.reshape(-1, n)
    for r in range(rows.shape[0]):
        rc = fn(
            rows[r].ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_int64(n),
            ctypes.c_int64(w),
            orows[r].ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        )
        if rc != 0:
            raise RuntimeError(f"{name} 返回错误码 {rc}")
    return out


# ---------------------------------------------------------------- numpy 回退


def _fallback_sum(a1: np.ndarray, w: int) -> np.ndarray:
    """顺序前缀和（与 C 逐位一致）。窗口内 NaN → NaN。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    if w == 1:
        return a1.copy()  # 避免前缀和 1-ULP 漂移，w=1 精确恒等
    with np.errstate(invalid="ignore"):  # ±inf 混加的已知边界(与 C 一致)
        clean = np.where(np.isnan(a1), 0.0, a1)
        cs = np.cumsum(clean)
        pn = np.cumsum(np.isnan(a1).astype(np.int64))
        s = cs.copy()
        s[w:] = cs[w:] - cs[:-w]  # s[w-1] = cs[w-1] - 0
        bad = pn.copy()
        bad[w:] = pn[w:] - pn[:-w]
        out[w - 1 :] = np.where(bad[w - 1 :] > 0, np.nan, s[w - 1 :])
    return out


def _fallback_mean(a1: np.ndarray, w: int) -> np.ndarray:
    r = _fallback_sum(a1, w)
    if w <= 0 or w > a1.shape[0] or a1.shape[0] == 0:
        return r
    r[w - 1 :] = r[w - 1 :] / w
    return r


def _fallback_maxmin(a1: np.ndarray, w: int, which: str) -> np.ndarray:
    """滑动窗口极值（numpy max/min 传播 NaN，与 C 精确一致）。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    wv = np.lib.stride_tricks.sliding_window_view(a1, w)
    out[w - 1 :] = wv.max(axis=-1) if which == "max" else wv.min(axis=-1)
    return out


def _fallback_rank(a1: np.ndarray, w: int) -> np.ndarray:
    """窗口内对有限元素排名并归一化（与 C 逐位一致）。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    wv = np.lib.stride_tricks.sliding_window_view(a1, w)
    x = a1[w - 1 :]
    k = np.isfinite(wv).sum(axis=-1)  # 有效元素数（含自身）
    x_fin = np.isfinite(x)
    # 与后端 win <= x 逐元素比较一致：-inf 计入、+inf 不计、NaN 恒 False
    non_nan = ~np.isnan(wv)
    le = (np.where(non_nan, wv, np.inf) <= x[:, None]).sum(axis=-1)
    valid = x_fin & (k >= 2)
    out[w - 1 :] = np.where(valid, (le - 1) / np.maximum(k - 1, 1), np.nan)
    return out


def _fallback_product(a1: np.ndarray, w: int) -> np.ndarray:
    """滚动连乘：np.prod 传播 NaN（窗口内任一 NaN → NaN），与 C 一致。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    wv = np.lib.stride_tricks.sliding_window_view(a1, w)
    out[w - 1 :] = np.prod(wv, axis=-1)
    return out


def _fallback_skew(a1: np.ndarray, w: int) -> np.ndarray:
    """滚动偏度：公式与 backend.ts_skew / C 一致（中心矩比，|v|<1e-12 归零）。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    wv = np.lib.stride_tricks.sliding_window_view(a1, w)
    m = np.mean(wv, axis=-1, keepdims=True)
    v = np.mean((wv - m) ** 2, axis=-1)
    s = np.mean((wv - m) ** 3, axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[w - 1 :] = np.where(np.abs(v) < 1e-12, 0.0, s / (v**1.5 + 1e-12))
    return out


def _fallback_kurt(a1: np.ndarray, w: int) -> np.ndarray:
    """滚动超额峰度：公式与 backend.ts_kurt / C 一致。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    wv = np.lib.stride_tricks.sliding_window_view(a1, w)
    m = np.mean(wv, axis=-1, keepdims=True)
    v = np.mean((wv - m) ** 2, axis=-1)
    q = np.mean((wv - m) ** 4, axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[w - 1 :] = np.where(np.abs(v) < 1e-12, 0.0, q / (v**2 + 1e-12) - 3.0)
    return out


def _fallback_argmaxmin(a1: np.ndarray, w: int, which: str) -> np.ndarray:
    """窗口内极值相对位置（0=当前，从窗口尾向前计数）。
    np.argmax/np.argmin 的 NaN 语义（首个 NaN 恒胜出）由 numpy 保证，与 C 一致。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    wv = np.lib.stride_tricks.sliding_window_view(a1, w)
    idx = np.argmax(wv, axis=-1) if which == "max" else np.argmin(wv, axis=-1)
    out[w - 1 :] = (w - 1) - idx
    return out


def _fallback_decay_linear(a1: np.ndarray, w: int) -> np.ndarray:
    """线性衰减加权窗口均值（权重 w..1，分母 w(w+1)/2），与 backend/C 一致。"""
    n = a1.shape[0]
    out = np.full(n, np.nan)
    if n == 0 or w <= 0 or w > n:
        return out
    wv = np.lib.stride_tricks.sliding_window_view(a1, w)
    weights = np.arange(w, 0, -1, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        out[w - 1 :] = (wv * weights).sum(axis=-1) / (w * (w + 1) / 2)
    return out


def _fallback(name: str, a64: np.ndarray, w: int) -> np.ndarray:
    out = np.empty_like(a64, dtype=np.float64)
    rows = a64.reshape(-1, a64.shape[-1])
    orows = out.reshape(-1, a64.shape[-1])
    for r in range(rows.shape[0]):
        if name == "rolling_sum":
            orows[r] = _fallback_sum(rows[r], w)
        elif name == "rolling_mean":
            orows[r] = _fallback_mean(rows[r], w)
        elif name in ("rolling_max", "rolling_min"):
            orows[r] = _fallback_maxmin(rows[r], w, name[8:])
        elif name == "rolling_rank":
            orows[r] = _fallback_rank(rows[r], w)
        elif name == "rolling_product":
            orows[r] = _fallback_product(rows[r], w)
        elif name == "rolling_skew":
            orows[r] = _fallback_skew(rows[r], w)
        elif name == "rolling_kurt":
            orows[r] = _fallback_kurt(rows[r], w)
        elif name in ("rolling_argmax", "rolling_argmin"):
            orows[r] = _fallback_argmaxmin(
                rows[r], w, "max" if name.endswith("argmax") else "min"
            )
        elif name == "rolling_decay_linear":
            orows[r] = _fallback_decay_linear(rows[r], w)
        else:
            raise ValueError(f"未知滚动算子: {name}")
    return out


def _dispatch(name: str, a: np.ndarray, w: int) -> np.ndarray:
    a64, w = _prep(a, w)
    if native_available():
        try:
            return _call_c(name, a64, w)
        except Exception as e:
            logger.debug("原生 %s 失败，回退 numpy: %s", name, e)
    return _fallback(name, a64, w)


def rolling_sum(a: np.ndarray, w: int) -> np.ndarray:
    """滚动求和。窗口内 NaN → NaN；w<=0 或 w>len → 全 NaN。"""
    return _dispatch("rolling_sum", a, w)


def rolling_mean(a: np.ndarray, w: int) -> np.ndarray:
    """滚动均值。窗口内 NaN → NaN；w<=0 或 w>len → 全 NaN。"""
    return _dispatch("rolling_mean", a, w)


def rolling_max(a: np.ndarray, w: int) -> np.ndarray:
    """滚动最大值。窗口内 NaN → NaN；w<=0 或 w>len → 全 NaN。"""
    return _dispatch("rolling_max", a, w)


def rolling_min(a: np.ndarray, w: int) -> np.ndarray:
    """滚动最小值。窗口内 NaN → NaN；w<=0 或 w>len → 全 NaN。"""
    return _dispatch("rolling_min", a, w)


def rolling_rank(a: np.ndarray, w: int) -> np.ndarray:
    """滚动排名（0-1 归一化，语义同 backend.ts_rank）。

    w<=1 → 全 NaN；w>len → 全 NaN；当前元素非有限或窗口有效元素<2 → NaN。
    """
    return _dispatch("rolling_rank", a, w)


def rolling_product(a: np.ndarray, w: int) -> np.ndarray:
    """滚动连乘。窗口内任一 NaN → NaN；w<=0 或 w>len → 全 NaN。"""
    return _dispatch("rolling_product", a, w)


def rolling_skew(a: np.ndarray, w: int) -> np.ndarray:
    """滚动偏度（语义同 backend.ts_skew：中心矩比，|v|<1e-12 归零）。"""
    return _dispatch("rolling_skew", a, w)


def rolling_kurt(a: np.ndarray, w: int) -> np.ndarray:
    """滚动超额峰度（语义同 backend.ts_kurt）。"""
    return _dispatch("rolling_kurt", a, w)


def rolling_argmax(a: np.ndarray, w: int) -> np.ndarray:
    """滚动窗口内最大值相对位置（0=当前，从窗口尾向前计数，语义同 backend.ts_argmax）。"""
    return _dispatch("rolling_argmax", a, w)


def rolling_argmin(a: np.ndarray, w: int) -> np.ndarray:
    """滚动窗口内最小值相对位置（0=当前，从窗口尾向前计数，语义同 backend.ts_argmin）。"""
    return _dispatch("rolling_argmin", a, w)


def rolling_decay_linear(a: np.ndarray, w: int) -> np.ndarray:
    """线性衰减加权窗口均值（权重 w..1，语义同 backend.decay_linear）。"""
    return _dispatch("rolling_decay_linear", a, w)
