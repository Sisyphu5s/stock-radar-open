"""运行时编排器(核心层,bt-runtime 领地):组件注册表 + 拓扑启停 + 状态汇总。

统一管理服务启动期的后台机制(源恢复/扫描调度/预热/任务超时巡检/事件 GC/
后端初始化/任务重启恢复),由 main.py lifespan 以单点 `setup_runtime().start()`
启动,替代原硬编码的逐个 try/except 启动调用。

设计要点(机制 > 特判):
- 组件注册表数据驱动:setup_runtime() 集中登记 (name, depends, factory|module_path),
  可变性(启停)由 Settings.runtime_components 表达(SR_RUNTIME_* 环境变量,空=全开)。
- 延迟 import 工厂:core.sources / core.scanning 属 bt-scan 并行领地,本卡不 import
  未存在模块——注册表接受 "module.path.ClassName" 字符串,start() 时才 import 并
  实例化;模块缺失 = 该组件启动失败 = 组件级容错(warning,不阻塞服务),待 bt-scan
  合并后自动生效,无需改本文件。
- 拓扑启停:按 depends 做 Kahn 拓扑排序;依赖环 → error 日志 + 跳过环成员;
  start 逐个执行,单组件失败仅 warning 不阻塞其他(与旧 main.py 容错语义一致);
  stop 逆序执行。
- 惰性组件:TaskMonitor(_ensure_timeout_monitor)/EventsGC(_ensure_gc) 原为首次
  使用时惰性启动线程,组件化后保持惰性(组件 start 空操作),status() 反映底层
  线程标志的真实运行状态。

Component 协议与 bt-scan 卡共同遵守(冻结):
    class Component:
        name: str
        depends: frozenset[str] = frozenset()
        def start(self) -> None: ...
        def stop(self) -> None: ...
        def status(self) -> dict:  # {"name": str, "running": bool, **自定义}
"""

from __future__ import annotations

import importlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..config import settings

logger = logging.getLogger("stockradar.core.runtime")


class Component:
    """后台机制组件协议(bt-scan 并行卡共同遵守,冻结)。"""

    name: str
    depends: frozenset[str] = frozenset()

    def start(self) -> None:
        """启动组件。抛异常 = 启动失败,由 Runtime 组件级容错捕获(不阻塞其他组件)。"""

    def stop(self) -> None:
        """停止组件(逆拓扑序调用)。"""

    def status(self) -> dict:
        """状态快照:必须含 name/running,可带组件自定义字段。"""
        return {"name": self.name, "running": False}


class _CallableComponent(Component):
    """普通组件包装:start 调用给定函数(一次性启动动作,无后台机制)。"""

    def __init__(
        self,
        name: str,
        start_fn: Callable[[], None],
        status_fn: Callable[[], dict] | None = None,
        depends: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self.depends = depends
        self._start_fn = start_fn
        self._status_fn = status_fn
        self._started = False

    def start(self) -> None:
        self._start_fn()
        self._started = True

    def stop(self) -> None:
        # 一次性启动动作无对应停机逻辑(daemon 线程随进程退出);仅复位运行标志
        self._started = False

    def status(self) -> dict:
        st: dict = {"name": self.name, "running": self._started}
        if self._status_fn is not None:
            st.update(self._status_fn())
        return st


class _LazyComponent(Component):
    """惰性组件:start 空操作(保持底层"首次使用才启动"的语义),status 读底层标志。

    用于 TaskMonitor(任务超时巡检线程,首个任务提交时惰性启动)与
    EventsGC(事件终态回收线程,首个终态事件发布时惰性启动)。
    """

    def __init__(
        self,
        name: str,
        probe: Callable[[], bool],
        depends: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self.depends = depends
        self._probe = probe

    def start(self) -> None:
        pass  # 保持惰性:不预启动,由业务路径首次触发

    def stop(self) -> None:
        pass

    def status(self) -> dict:
        return {"name": self.name, "running": bool(self._probe()), "lazy": True}


@dataclass(frozen=True)
class _Spec:
    """组件注册条目。factory / module_path / instance 三选一。

    - instance:直接传入组件实例(测试用桩)。
    - factory:零参工厂,start 时调用得到组件实例。
    - module_path:"module.path.ClassName",start 时才 import 并实例化
      (延迟加载,供 bt-scan 并行领地的模块;类与工厂函数均可)。
    """

    name: str
    depends: frozenset[str] = frozenset()
    factory: Callable[[], Component] | None = None
    module_path: str | None = None
    instance: Component | None = None


class Runtime:
    """运行时编排器:注册组件 → 拓扑排序启动 → 逆序停止 → 状态汇总。

    components 参数为启停配置(dict[str, bool]):空 = 全部启用;
    非空时未列出的组件默认启用,列出的按 value 决定(大小写不敏感匹配组件名)。
    """

    def __init__(self, components: dict[str, bool] | None = None) -> None:
        self._specs: dict[str, _Spec] = {}
        self._instances: dict[str, Component] = {}
        self._started_order: list[str] = []
        self._errors: dict[str, str] = {}
        self._enabled: dict[str, bool] = components or {}

    # ---------- 注册 ----------

    def register(
        self,
        name: str,
        *,
        depends: frozenset[str] | None = None,
        factory: Callable[[], Component] | None = None,
        module_path: str | None = None,
        instance: Component | None = None,
    ) -> None:
        """注册组件;factory/module_path/instance 必须且只能提供一个。

        depends 未显式给出时,若传入了 instance 则继承其 depends 属性
        (Component 协议自带 depends,避免双处定义不一致)。
        """
        if name in self._specs:
            raise ValueError(f"组件已注册: {name}")
        provided = [x is not None for x in (factory, module_path, instance)]
        if sum(provided) != 1:
            raise ValueError(
                f"组件 {name} 必须且只能提供 factory/module_path/instance 之一"
            )
        if depends is None:
            depends = (
                frozenset(getattr(instance, "depends", frozenset()))
                if instance is not None
                else frozenset()
            )
        self._specs[name] = _Spec(
            name=name,
            depends=depends,
            factory=factory,
            module_path=module_path,
            instance=instance,
        )

    # ---------- 配置与构建 ----------

    def _is_enabled(self, name: str) -> bool:
        """启停配置:空 = 全开;未列出的组件默认启用;列出的按 value(大小写不敏感)。"""
        if not self._enabled:
            return True
        for k, v in self._enabled.items():
            if k.lower() == name.lower():
                return v
        return True

    @staticmethod
    def _build(spec: _Spec) -> Component:
        if spec.instance is not None:
            return spec.instance
        if spec.factory is not None:
            return spec.factory()
        # 延迟 import:"module.path.ClassName" → import 模块 + getattr + 实例化
        mod_path, _, attr = spec.module_path.rpartition(".")
        mod = importlib.import_module(mod_path)
        target = getattr(mod, attr)
        return target() if isinstance(target, type) else target

    # ---------- 拓扑与启停 ----------

    def _topo_order(self) -> list[str]:
        """Kahn 拓扑排序(enabled 组件);依赖环成员剔除并记 error 日志。

        依赖缺失/被配置禁用 → 忽略该条依赖约束(组件照常启动,容错不阻塞)。
        """
        enabled = [n for n in self._specs if self._is_enabled(n)]
        indeg = {n: 0 for n in enabled}
        adj: dict[str, list[str]] = {n: [] for n in enabled}
        for name in enabled:
            for dep in self._specs[name].depends:
                if dep in adj:  # 依赖不在 enabled 集合(未注册/被禁用) → 忽略约束
                    adj[dep].append(name)
                    indeg[name] += 1
        q = deque(n for n in enabled if indeg[n] == 0)
        order: list[str] = []
        while q:
            n = q.popleft()
            order.append(n)
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    q.append(m)
        if len(order) < len(enabled):
            skipped = sorted(set(enabled) - set(order))
            logger.error("运行时组件存在依赖环，跳过: %s", skipped)
        return order

    def start(self) -> None:
        """按拓扑序启动全部启用组件;单组件失败仅 warning,不阻塞其他组件。"""
        self._started_order.clear()
        self._errors.clear()
        for name in self._topo_order():
            spec = self._specs[name]
            try:
                comp = self._build(spec)
                comp.start()
                self._instances[name] = comp
                self._started_order.append(name)
                logger.info("运行时组件已启动: %s", name)
            except Exception as e:
                self._instances.pop(name, None)
                self._errors[name] = str(e)
                logger.warning("运行时组件 %s 启动失败(跳过): %s", name, e)

    def stop(self) -> None:
        """逆拓扑序停止已启动组件;单组件异常仅 warning,不阻塞其他组件。"""
        for name in reversed(self._started_order):
            comp = self._instances.get(name)
            if comp is None:
                continue
            try:
                comp.stop()
                logger.info("运行时组件已停止: %s", name)
            except Exception as e:
                logger.warning("运行时组件 %s 停止异常: %s", name, e)
        self._started_order.clear()
        self._instances.clear()

    # ---------- 状态汇总 ----------

    def status(self) -> dict[str, dict]:
        """全部已注册组件的状态汇总:{组件名: {name, running, enabled, **组件自定义}}。

        组件自报 status() 为准(含 running 与自定义字段);未启动/失败/被禁用的
        组件由编排器补 running=False 与错误原因。
        """
        out: dict[str, dict] = {}
        for name in self._specs:
            enabled = self._is_enabled(name)
            comp = self._instances.get(name)
            if enabled and comp is not None:
                try:
                    st = comp.status()
                    st.setdefault("name", name)
                except Exception as e:
                    st = {"name": name, "running": False, "error": str(e)}
            else:
                st = {"name": name, "running": False}
                if name in self._errors:
                    st["error"] = self._errors[name]
            st["enabled"] = enabled
            out[name] = st
        return out

    def is_running(self, name: str) -> bool:
        """组件是否运行中(以组件自报 status 的 running 为准)。"""
        st = self.status().get(name)
        return bool(st and st.get("running"))


# ---------- 组件清单注册(供 main.py lifespan 调用) ----------

_runtime: Runtime | None = None


def setup_runtime(components: dict[str, bool] | None = None) -> Runtime:
    """构建运行时编排器并注册组件清单(幂等:重复调用返回同一实例)。

    components 缺省取 Settings.runtime_components(SR_RUNTIME_* 环境变量)。
    core.sources / core.scanning 属 bt-scan 并行领地,以 module_path 延迟注册,
    bt-scan 合并后自动生效;合并前 import 失败由组件级容错捕获(warning,不阻塞)。
    """
    global _runtime
    if _runtime is not None:
        return _runtime
    cfg = components if components is not None else settings.runtime_components
    _runtime = Runtime(components=cfg)

    def _warmup_factory() -> Component:
        # bt-fin 迁移完成:直接包装 app.core.warmup.start_warmup
        from ..core.warmup import start_warmup

        return _CallableComponent("Warmup", start_warmup)

    def _backend_factory() -> Component:
        # init_backend 为幂等初始化(无状态化要求低),作普通组件,依赖顺序最先
        from ..lib.alpha.backend import init_backend

        return _CallableComponent("Backend", init_backend)

    def _job_recovery_factory() -> Component:
        # 重启恢复（T-122）：遗留 pending/running 重新排队（不被无条件置 failed），
        # paused 保持。worker 模式（SR_TASKS_EMBEDDED=0）下恢复由独立 worker 进程
        # 启动时执行，Web 进程跳过——避免 reload 把 worker 正在跑的任务置回 pending。
        from ..core.tasks.runner import _embedded, recover_stale_jobs

        if not _embedded():
            return _CallableComponent("JobRecovery", lambda: 0)
        return _CallableComponent("JobRecovery", recover_stale_jobs)

    def _scanning_factory() -> Component:
        # 扫描调度统一由 core.scanning.Scanning 组件承载(原 services.market.scanner
        # 已并入,无回退路径;组件协议见 core/scanning.py)。
        from app.core.scanning import Scanning as _Scanning

        return _Scanning()

    def _task_monitor_factory() -> Component:
        # 任务超时巡检线程:保持惰性(首个任务提交时由 submit 触发启动)
        from .tasks import runner as _runner

        return _LazyComponent("TaskMonitor", lambda: _runner._timeout_monitor_started)

    def _events_gc_factory() -> Component:
        # 事件终态回收线程:保持惰性(首个终态事件发布时由 publish 触发启动)
        from . import events as _events

        return _LazyComponent("EventsGC", lambda: _events._gc_started)

    _runtime.register("SourceRecovery", module_path="app.core.sources.SourceRecovery")
    _runtime.register(
        "Scanning",
        depends=frozenset({"SourceRecovery"}),
        factory=_scanning_factory,
    )
    _runtime.register(
        "Warmup", depends=frozenset({"SourceRecovery"}), factory=_warmup_factory
    )
    _runtime.register("TaskMonitor", factory=_task_monitor_factory)
    _runtime.register("EventsGC", factory=_events_gc_factory)
    _runtime.register("Backend", factory=_backend_factory)
    _runtime.register("JobRecovery", factory=_job_recovery_factory)
    # 绑定 storage.klines 的拉取能力到 core.sources 门面(消除 storage→core 反向依赖,H1c)
    from ..storage.klines import bind_kline_source
    from ..storage.hs300 import bind_spot_getter
    from .sources import (
        get_kline as _sources_get_kline,
        get_provider as _sources_get_provider,
        get_spot as _sources_get_spot,
    )

    bind_kline_source(_sources_get_kline, _sources_get_provider)
    bind_spot_getter(_sources_get_spot)
    return _runtime
