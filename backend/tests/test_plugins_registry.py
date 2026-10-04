"""域插件注册表测试(T-110):SR_PLUGINS 配置驱动路由挂载。

覆盖(任务验收):
① 默认全开:13 组 API 前缀全部挂载(6 域 13 router,与 API-CONTRACT 冻结前缀一致)
② SR_PLUGINS=signals:仅挂 signals 域(system 域强制保留)
③ system 域强制保留:SR_PLUGINS=market 时 system 域仍挂载,其余域不挂
④ 未知插件名容错:配置含未注册名 → warning 跳过,不报错,已知域照常启用

挂载机制改变不应影响任何路由行为,行为级覆盖由既有测试(test_cors_auth /
test_system_api / test_todos / test_factors_api 等)承担,本文件只验证挂载面。

环境注入方式与 test_cors_auth 一致:setattr(settings.plugins) + importlib.reload(main)
重建 app(register_builtin 幂等,REGISTRY 进程内只建一次;enabled 状态每次 reload
按当前配置重算)。TestClient 不用 with(既有模式),不触发 lifespan。
"""

from __future__ import annotations

import importlib

import pytest

import app.config as CFG
import app.main as M
from app.core import plugins as PL

API = "/api/v1"

# 14 组冻结前缀(API-CONTRACT:路由路径不变;T-17 条件选股新增 /screener)
EXPECTED_PREFIXES = {
    f"{API}/alpha",
    f"{API}/copilot",
    f"{API}/copilot/hermes",
    f"{API}/datasets",
    f"{API}/experiments",
    f"{API}/factors",
    f"{API}/indicators",
    f"{API}/market",
    f"{API}/paper",
    f"{API}/screener",
    f"{API}/signals",
    f"{API}/stocks",
    f"{API}/system",
    f"{API}/todos",
}

ALL_DOMAINS = {"market", "research", "signals", "system", "copilot", "paper"}


@pytest.fixture()
def reset_plugins():
    """teardown:恢复 plugins 配置并 reload main(避免污染其他测试文件的默认全开)。"""
    original = CFG.settings.plugins
    yield
    CFG.settings.plugins = original
    importlib.reload(M)


def _mounted_prefixes(app) -> set[str]:
    """app 已挂载的 API 前缀集合(兼容 _IncludedRouter 结构)。

    本项目 Starlette 版本的 include_router 不展开子路由:app.routes 顶层是
    _IncludedRouter(无 .path),其 original_router.prefix 即该域前缀(不含外部
    API 前缀,补 {API} 后与 EXPECTED_PREFIXES 对齐)。health 为普通 APIRoute,
    无 original_router,自然跳过。另保留旧展开形态(按 APIRoute.path 归属)的
    兼容分支,防止 Starlette 升级后断言失效。
    """
    mounted: set[str] = set()
    for r in app.routes:
        # 形态一:新 Starlette _IncludedRouter(不展开,取 original_router.prefix)
        router = getattr(r, "original_router", None)
        pfx = getattr(router, "prefix", None)
        if pfx:
            mounted.add(f"{API}{pfx}")
            continue
        # 形态二:旧展开形态,按 APIRoute.path 归属前缀(最长前缀优先,
        # 使 /copilot/hermes/* 归入 /copilot/hermes 而非 /copilot)
        path = getattr(r, "path", "")
        # health 家族(/health、/health/live、/health/ready,T-123)为普通 APIRoute,跳过
        if not path.startswith(f"{API}/") or path.startswith(f"{API}/health"):
            continue
        for cand in sorted(EXPECTED_PREFIXES, key=len, reverse=True):
            if path.startswith(cand):
                mounted.add(cand)
                break
        else:
            pytest.fail(f"未知挂载路径: {path}")
    return mounted


def _reload(plugins: str):
    """设置 plugins 配置语义并 reload main,返回重建的 app。"""
    CFG.settings.plugins = plugins
    return importlib.reload(M).app


# ---------- ① 默认全开 ----------


def test_1_default_all_plugins_mounted(reset_plugins):
    """默认(空配置)全开:14 组前缀全部挂载,无多余路径。"""
    app = _reload("")
    mounted = _mounted_prefixes(app)
    assert mounted == EXPECTED_PREFIXES
    # 注册表层面:6 域插件共 15 个 router(API-CONTRACT 冻结 14 + T-04 行情 SSE
    # market_stream),全部启用;routers 经 resolve() 惰性解析
    total = sum(len(p.resolve()) for p in PL.REGISTRY)
    assert total == 15
    assert {p.name for p in PL.get_enabled_plugins()} == ALL_DOMAINS


# ---------- ② SR_PLUGINS=signals ----------


def test_2_signals_only(reset_plugins):
    """SR_PLUGINS=signals:仅 signals 域挂载,外加强制保留的 system 域。"""
    app = _reload("signals")
    mounted = _mounted_prefixes(app)
    assert mounted == {
        f"{API}/signals",
        f"{API}/stocks",
        f"{API}/system",
        f"{API}/todos",
    }
    assert not mounted & {
        f"{API}/market",
        f"{API}/alpha",
        f"{API}/copilot",
        f"{API}/paper",
        f"{API}/indicators",
    }


# ---------- ③ system 域强制保留 ----------


def test_3_system_always_enabled(reset_plugins):
    """SR_PLUGINS=market:market 域启用,system 域仍强制挂载,其余域不挂。"""
    app = _reload("market")
    mounted = _mounted_prefixes(app)
    assert {
        f"{API}/market",
        f"{API}/stocks",
        f"{API}/system",
        f"{API}/todos",
    } <= mounted
    assert not mounted & {f"{API}/signals", f"{API}/alpha", f"{API}/paper"}


# ---------- ④ 未知插件名容错 ----------


def test_4_unknown_plugin_warned(reset_plugins, caplog):
    """SR_PLUGINS 含未知插件名:warning 跳过,不报错,已知域照常启用。"""
    PL.load_plugins_from_config("signals,no_such_plugin")
    enabled = {p.name for p in PL.get_enabled_plugins()}
    assert enabled == {"signals", "system"}
    assert any("no_such_plugin" in r.message for r in caplog.records)


# ---------- ⑤ T-121 惰性边界 ----------


def test_5_disabled_domain_not_imported(reset_plugins, monkeypatch):
    """禁用域不触发 importlib（重依赖导入链零加载）——真插件边界验收。"""
    import importlib

    calls: list[str] = []

    def fake_import(name, *a, **kw):
        calls.append(name)
        return importlib.__import__(name, *a, **kw)

    PL.load_plugins_from_config("system")
    # 重置已解析的 routers 缓存,模拟干净进程
    for p in PL.REGISTRY:
        p.routers = []
    monkeypatch.setattr("builtins.__import__", fake_import)
    for p in PL.get_enabled_plugins():
        p.resolve()
    # 仅 system 域模块被导入;research(含 alpha/indicators,重依赖链)不触发
    assert not any(c.startswith("app.api.alpha") for c in calls)
    assert not any(c.startswith("app.api.indicators") for c in calls)
    assert not any(c.startswith("app.api.paper") for c in calls)
