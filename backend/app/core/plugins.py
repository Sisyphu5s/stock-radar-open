"""域插件注册表(核心层,bt-plugins 领地):域插件协议 + 内置插件注册 + SR_PLUGINS 配置控制。

背景(T-110):main.py 原静态 include_router 13 个 api router;本模块将路由按域
分组为 Plugin,main.py 遍历 get_enabled_plugins() 挂载。新增域只需在
register_builtin() 增加 Plugin 条目,不再改动 main.py。API 层(app/api/)冻结,
本模块只读导入 router 对象,不修改任何 api 模块。

T-121 真插件边界(P1-64):register_builtin 不再一次性 import 全部 api 域模块——
Plugin 改为声明模块路径(modules 字符串),resolve() 惰性 import 仅启用域的模块。
禁用域(如 SR_PLUGINS 只列 system)不再加载其重依赖导入链(MLX/akshare 等),
启动故障隔离真正成立。

设计要点(机制 > 特判):
- 注册表数据驱动:REGISTRY 集中登记 (name, modules);可变性(启停)由
  Settings.plugins(SR_PLUGINS)表达:空 = 全开;非空 = 仅启用列出的域。
- system 域强制启用:stocks/todos/system 为基础服务,不参与 SR_PLUGINS 关闭
  范围;配置非空且未列出时自动补启并记 info。
- 未知插件名容错:配置里出现未注册的域名 → warning 跳过,不报错。
- 惰性 import:modules 声明 → resolve() 时 importlib 按需加载,仅启用域触发。
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field

from fastapi import APIRouter

logger = logging.getLogger("stockradar.core.plugins")


@dataclass
class Plugin:
    """域插件描述:name = 域标识;modules = 该域 api 模块路径声明(惰性 import);
    routers = resolve() 后的路由对象缓存(挂载顺序 = 声明顺序)。

    enabled 默认 True(全开),由 load_plugins_from_config(SR_PLUGINS)按配置改写。
    惰性边界(T-121):未启用域永远不触发 importlib,其重依赖导入链零加载。
    """

    name: str
    modules: list[str] = field(default_factory=list)
    routers: list[APIRouter] = field(default_factory=list)
    enabled: bool = True

    def resolve(self) -> list[APIRouter]:
        """惰性解析模块声明 → router 对象(仅启用域调用;结果缓存)。"""
        if not self.routers and self.modules:
            self.routers = [importlib.import_module(m).router for m in self.modules]
        return self.routers


# 模块级注册表:register_builtin() 一次性填充声明;enabled 状态由配置层维护
REGISTRY: list[Plugin] = []


def register(plugin: Plugin) -> None:
    """登记插件到注册表(重复注册同名 → ValueError,防静默覆盖)。"""
    if any(p.name == plugin.name for p in REGISTRY):
        raise ValueError(f"插件已注册: {plugin.name}")
    REGISTRY.append(plugin)


def get_enabled_plugins() -> list[Plugin]:
    """当前启用插件列表(注册顺序;main.py 遍历挂载)。"""
    return [p for p in REGISTRY if p.enabled]


def load_plugins_from_config(cfg: str) -> None:
    """按 SR_PLUGINS 配置刷新各插件 enabled 状态(幂等,可重复调用)。

    配置语义:
    - 空字符串/未设置 = 全开(全部插件启用)。
    - 非空 = 逗号分隔插件名(大小写不敏感),仅启用列出的域;
      system 域强制启用(基础服务,不在关闭范围),未列出时自动补启并记 info;
      未注册的未知插件名 → warning 跳过,不报错。
    """
    names = {s.strip().lower() for s in cfg.split(",") if s.strip()}
    if not names:
        for p in REGISTRY:
            p.enabled = True
        return
    registered = {p.name.lower() for p in REGISTRY}
    for p in REGISTRY:
        if p.name.lower() == "system":
            if "system" not in names:
                logger.info("system 域未列入 SR_PLUGINS,强制启用(基础服务不可关)")
            p.enabled = True
        else:
            p.enabled = p.name.lower() in names
    unknown = names - registered
    if unknown:
        logger.warning("SR_PLUGINS 含未知插件名,已跳过: %s", ", ".join(sorted(unknown)))


def register_builtin() -> None:
    """注册内置域插件(幂等:REGISTRY 非空即返回,reload main 安全不重复注册)。

    分组按业务域,modules 为 api 模块路径声明(惰性,仅在启用时 resolve import):
    - market:   行情/扫描
    - research: 因子研究域(指标 + Alpha 挖掘 + 数据集 + 实验/回测 + 因子库)
    - signals:  信号中心
    - system:   基础域(股票池 + 待办 + 系统服务;SR_PLUGINS 关闭范围外,强制保留)
    - copilot:  智能助手(对话 + 知识库检索)
    - paper:    模拟盘

    注册顺序决定默认挂载顺序(按原 13 router 静态序的引入次序):market →
    research → signals → system → copilot → paper。分组导致逐 router 展开顺序
    与原静态顺序不完全一致,但 13 组前缀互不重叠、无路径匹配依赖,行为等价。
    """
    if REGISTRY:
        return
    register(
        Plugin(
            "market",
            ["app.api.market", "app.api.screener", "app.api.market_stream"],
        )
    )
    register(
        Plugin(
            "research",
            [
                "app.api.indicators",
                "app.api.alpha",
                "app.api.datasets",
                "app.api.experiments",
                "app.api.factors",
            ],
        )
    )
    register(Plugin("signals", ["app.api.signals"]))
    register(Plugin("system", ["app.api.stocks", "app.api.todos", "app.api.system"]))
    register(Plugin("copilot", ["app.api.copilot", "app.api.hermes"]))
    register(Plugin("paper", ["app.api.paper"]))
