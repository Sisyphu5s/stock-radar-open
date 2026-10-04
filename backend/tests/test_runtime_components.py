"""core/runtime.py 运行时编排器专项测试(T-107):桩组件验证机制,不启动真实后台线程。

覆盖(任务验收 ①-⑤ + 延迟 import 工厂补充):
① 拓扑启动顺序(depends 生效,stop 逆序)
② 单组件启动失败容错(其他组件照常启动,不阻塞)
③ 依赖环检测(环成员跳过 + error 日志,无环组件照常)
④ 配置关闭(runtime_components 关某组件 → 不启动)
⑤ status() 汇总
⑥ 延迟 import 工厂:module_path 指向不存在模块 → 组件级容错;指向存在类 → 正常实例化

铁律:只注册桩组件,不调用 setup_runtime()(真实注册会延迟启动真实后台机制)。
"""

from __future__ import annotations

import logging

import pytest

from app.core.runtime import Component, Runtime


class Stub(Component):
    """桩组件:start/stop 按序记录事件;fail_start=True 时 start 抛异常。"""

    def __init__(
        self,
        name: str,
        events: list[str] | None = None,
        fail_start: bool = False,
        depends: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self.depends = depends
        self._events = events
        self._fail_start = fail_start

    def start(self) -> None:
        if self._fail_start:
            raise RuntimeError(f"{self.name} 启动失败(桩)")
        if self._events is not None:
            self._events.append(f"start:{self.name}")

    def stop(self) -> None:
        if self._events is not None:
            self._events.append(f"stop:{self.name}")

    def status(self) -> dict:
        return {"name": self.name, "running": True}


def test_1_topo_start_stop_order():
    """① 拓扑启动顺序与逆序停止:乱序注册,按 depends 依赖链启动。"""
    events: list[str] = []
    rt = Runtime()
    rt.register("C", instance=Stub("C", events, depends=frozenset({"B"})))
    rt.register("A", instance=Stub("A", events))
    rt.register("B", instance=Stub("B", events, depends=frozenset({"A"})))
    rt.start()
    assert [e for e in events if e.startswith("start:")] == [
        "start:A",
        "start:B",
        "start:C",
    ]
    rt.stop()
    assert [e for e in events if e.startswith("stop:")] == [
        "stop:C",
        "stop:B",
        "stop:A",
    ]


def test_2_start_failure_does_not_block_others():
    """② 组件失败容错:一个 start 抛异常,其他组件照常启动,start() 不抛错。"""
    events: list[str] = []
    rt = Runtime()
    rt.register("Failing", instance=Stub("Failing", events, fail_start=True))
    rt.register("Ok", instance=Stub("Ok", events))
    rt.start()  # 不应抛出
    assert [e for e in events if e.startswith("start:")] == ["start:Ok"]
    st = rt.status()
    assert st["Failing"]["running"] is False
    assert "error" in st["Failing"]
    assert st["Ok"]["running"] is True


def test_3_cycle_detected_and_skipped(caplog):
    """③ 依赖环检测:环成员跳过(error 日志),无环组件照常启动。"""
    events: list[str] = []
    rt = Runtime()
    rt.register("A", instance=Stub("A", events, depends=frozenset({"B"})))
    rt.register("B", instance=Stub("B", events, depends=frozenset({"A"})))
    rt.register("C", instance=Stub("C", events))
    with caplog.at_level(logging.ERROR, logger="stockradar.core.runtime"):
        rt.start()
    assert "依赖环" in caplog.text
    assert [e for e in events if e.startswith("start:")] == ["start:C"]
    st = rt.status()
    assert st["A"]["running"] is False
    assert st["B"]["running"] is False


def test_4_config_disables_component():
    """④ 配置关闭:runtime_components 关某组件 → 不启动;依赖被禁用的组件照常(容错)。"""
    events: list[str] = []
    rt = Runtime(components={"B": False})
    rt.register("A", instance=Stub("A", events))
    rt.register("B", instance=Stub("B", events, depends=frozenset({"A"})))
    rt.register("C", instance=Stub("C", events, depends=frozenset({"B"})))
    rt.start()
    assert [e for e in events if e.startswith("start:")] == ["start:A", "start:C"]
    st = rt.status()
    assert st["B"]["enabled"] is False
    assert st["B"]["running"] is False
    assert st["A"]["enabled"] is True  # 未列出的组件默认启用


def test_5_status_summary():
    """⑤ status() 汇总:全部注册组件含 name/running/enabled;is_running 查询。"""
    rt = Runtime()
    rt.register("A", instance=Stub("A"))
    rt.register("B", instance=Stub("B"))
    rt.start()
    st = rt.status()
    assert set(st) == {"A", "B"}
    assert st["A"] == {"name": "A", "running": True, "enabled": True}
    assert rt.is_running("A") is True
    assert rt.is_running("B") is True


def test_6_delayed_module_path_missing_is_tolerated():
    """⑥a 延迟 import:module_path 指向不存在模块 →
    组件级容错(start 不抛),其他组件照常。"""
    events: list[str] = []
    rt = Runtime()
    rt.register(
        "Ghost",
        module_path="app.core.no_such_module.NoSuchComponent",  # 确定不存在
    )
    rt.register("Ok", instance=Stub("Ok", events))
    rt.start()
    assert [e for e in events if e.startswith("start:")] == ["start:Ok"]
    st = rt.status()
    assert st["Ghost"]["running"] is False
    assert "error" in st["Ghost"]


def test_6b_delayed_module_path_existing_class():
    """⑥b 延迟 import:module_path 指向已存在的 Component 类 → 正常实例化并启动。"""
    rt = Runtime()
    rt.register("Proto", module_path="app.core.runtime.Component")
    rt.start()  # 不抛:实例化成功,start 为空操作
    st = rt.status()
    assert st["Proto"]["running"] is False  # 抽象组件 status 默认 running=False
    assert st["Proto"]["enabled"] is True


def test_factory_registration():
    """factory 工厂分支:start 时才调用工厂得到组件实例。"""
    rt = Runtime()
    rt.register("F", factory=lambda: Stub("F"))
    rt.start()
    assert rt.is_running("F") is True


def test_register_validation():
    """register 校验:重复注册与多来源(工厂+实例)均拒绝。"""
    rt = Runtime()
    rt.register("A", instance=Stub("A"))
    with pytest.raises(ValueError):
        rt.register("A", instance=Stub("A"))
    with pytest.raises(ValueError):
        rt.register("B", factory=lambda: Stub("B"), instance=Stub("B"))
