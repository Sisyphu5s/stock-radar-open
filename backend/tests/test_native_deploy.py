"""原生内核部署链路测试：build.sh 产出 dylib、ctypes 加载全部符号、health 暴露 native 状态。

不触碰生产库：health 通过函数直调（import app.main 无副作用，DB/扫描均挂在
lifespan 内，不走 TestClient 生命周期）。
"""

import ctypes
import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

from app.lib.alpha import native_ops

NATIVE_DIR = Path(__file__).resolve().parents[1] / "native"
# build.sh 已归入 scripts/native/(2026-08-15 脚本统一大文件夹),自定位 cd 到本目录
BUILD_SH = Path(__file__).resolve().parents[2] / "scripts" / "native" / "build.sh"
DYLIB = NATIVE_DIR / "librolling.dylib"

ALL_OPS = [
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
]


@pytest.fixture(scope="module")
def built_lib():
    """运行 build.sh 产出 dylib（多架构或本机架构均可）。"""
    if shutil.which("clang") is None:
        pytest.skip("clang 不可用，无法构建 native 内核")
    subprocess.run(["bash", str(BUILD_SH)], check=True)
    assert DYLIB.exists(), "build.sh 未产出 librolling.dylib"
    return DYLIB


def test_build_produces_loadable_lib(built_lib):
    """构建产物可被 ctypes 加载，native_available() 为 True。"""
    importlib.reload(native_ops)  # 重新走顶层加载逻辑
    assert native_ops.native_available() is True


def test_lib_exports_all_symbols(built_lib):
    """dylib 导出全部 11 个算子符号，调用约定可注册并可用。"""
    lib = ctypes.CDLL(str(built_lib))
    for name in ALL_OPS:
        assert hasattr(lib, name), f"dylib 缺少符号 {name}"
        fn = getattr(lib, name)
        fn.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_double),
        ]
        fn.restype = ctypes.c_int
    # 原生结果冒烟：一个窗口调用返回 0
    import numpy as np

    a = np.array([1.0, 2.0, 3.0, 4.0])
    out = np.empty(4)
    rc = lib.rolling_sum(
        a.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        ctypes.c_int64(4),
        ctypes.c_int64(2),
        out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
    )
    assert rc == 0
    np.testing.assert_allclose(out, [np.nan, 3.0, 5.0, 7.0])
    # native_ops 模块内注册完整（argtypes 4 元组 + c_int 返回值）
    assert native_ops.native_available()
    for name in ALL_OPS:
        fn = getattr(native_ops._lib, name)
        assert len(fn.argtypes) == 4
        assert fn.restype == ctypes.c_int


def test_health_exposes_native_available():
    """health 响应包含 native_available: bool（与 native_ops 状态一致）。"""
    from app.main import health

    body = health()
    assert "native_available" in body
    assert isinstance(body["native_available"], bool)
    assert body["native_available"] == native_ops.native_available()
