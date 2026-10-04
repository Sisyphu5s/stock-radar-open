"""T-120 安全启动：MLX 隔离探测 + 无 Metal 环境导入安全（P1-63）。

背景：无 Metal 的 headless 环境实测 mlx 顶层 import 会在 Python except 之前
进程级终止（abort），app.main 导入链（runner → lib/alpha/nn）会命中。修复后
mlx 导入经 backend.mlx_probe() 子进程隔离探测门控——探测失败/显式关闭时
主进程完全不触碰 mlx，numpy 路径可用。

本机（Apple Silicon + Metal）mlx 可用，无法直接复现 abort；测试用
SR_MLX_OFF=1 环境变量强制关闭探测，并断言干净子进程可导入 app.main。
"""

from __future__ import annotations

import os
import subprocess
import sys

import numpy as np

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_mlx_probe_disabled_via_env(monkeypatch):
    """SR_MLX_OFF=1 → 探测直接返回 False（不启动子进程）。"""
    from app.lib.alpha import backend

    monkeypatch.setenv("SR_MLX_OFF", "1")
    backend._PROBE_RESULT = None  # 重置缓存
    assert backend.mlx_probe() is False
    backend._PROBE_RESULT = None


def test_mlx_probe_disabled_via_gp_backend_env(monkeypatch):
    """SR_GP_BACKEND=numpy → 探测直接返回 False（与 backend 开关同源）。"""
    from app.lib.alpha import backend

    monkeypatch.delenv("SR_MLX_OFF", raising=False)
    monkeypatch.setenv("SR_GP_BACKEND", "numpy")
    backend._PROBE_RESULT = None
    assert backend.mlx_probe() is False
    backend._PROBE_RESULT = None


def test_try_mlx_falls_back_to_numpy_when_probe_fails(monkeypatch):
    """探测失败 → _try_mlx 回退 numpy，BACKEND 保持 numpy。"""
    from app.lib.alpha import backend

    monkeypatch.setattr(backend, "mlx_probe", lambda: False)
    backend._T = None
    backend.BACKEND = "numpy"
    res = backend._try_mlx()
    assert res is np
    assert backend.BACKEND == "numpy"


def test_nn_module_skips_mlx_import_when_disabled(monkeypatch):
    """SR_MLX_OFF=1 下（重载）导入 nn 模块：_MLX_OK 恒 False，不触发 mlx。"""
    from app.lib.alpha import backend

    monkeypatch.setenv("SR_MLX_OFF", "1")
    backend._PROBE_RESULT = None
    # 清掉可能已加载的 nn 模块缓存，模拟干净进程导入
    for m in [m for m in list(sys.modules) if m == "app.lib.alpha.nn"]:
        del sys.modules[m]
    import app.lib.alpha.nn as nn_mod  # noqa: PLC0415

    assert nn_mod._MLX_OK is False
    assert nn_mod.mx is None and nn_mod.nn is None
    backend._PROBE_RESULT = None


def test_app_main_import_in_clean_process_no_mlx():
    """干净子进程 + SR_MLX_OFF=1：app.main 可导入（P1-63 验收——无 Metal 不再进程级终止）。"""
    code = (
        "import os\n"
        "os.environ['SR_MLX_OFF'] = '1'\n"
        "import sys\n"
        "sys.path.insert(0, {!r})\n"
        "from app.lib.alpha import nn as nn_mod\n"
        "assert nn_mod._MLX_OK is False\n"
        "import app.main\n"
        "print('MAIN_OK')\n"
    ).format(BACKEND_ROOT)
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=90,
        cwd=BACKEND_ROOT,
    )
    assert r.returncode == 0, "stderr: " + r.stderr.decode(errors="replace")[:2000]
    assert b"MAIN_OK" in r.stdout