"""handlers 共享依赖:runner 符号的动态转发器。

处理器函数体逐字搬移自 runner.py,函数体内引用的 runner 辅助/模块状态
(_update/_terminal/_checkpoint/_GPU_LOCK/顶层 import 的 load_panel/evolve/
train_mlp/SessionLocal/NNModel 等)必须保持「运行时解析 runner 模块当前属性」,
否则测试 monkeypatch(runner 模块属性,如 monkeypatch.setattr(Q, "_update", ...)
Q 即 runner)无法穿透——与迁移前函数体内直接引用 queue(=runner)模块全局名的
语义完全一致。

用法:from ._deps import _dyn; _update = _dyn("_update")
锁/常量/类(测试不 monkeypatch 的)在 handler 文件里直接 from ..runner import,
保证同一对象/常量语义。
"""

from __future__ import annotations

from .. import runner as _runner


def _dyn(name: str):
    """返回运行时从 runner 模块属性解析的转发包装(测试 monkeypatch 穿透)。"""

    def _f(*args, **kwargs):
        return getattr(_runner, name)(*args, **kwargs)

    _f.__name__ = name
    return _f
