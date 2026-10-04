"""T-93 / P1-58 修复测试：/system/exit 鉴权 + 优雅退出（SIGTERM 替代 os._exit）。

修复前：SR_API_TOKEN 为空时 exit 端点完全不设防（任何可达 8000 端口者可远程杀进程）；
退出用 os._exit(0) 跳过 atexit，丢失未落盘缓存。

鉴权测试复用 test_cors_auth.py 既有模式：setattr(settings.api_token) +
importlib.reload(main) 重建带/不带 auth 的 app；TestClient 不用 with（不触发
lifespan，不初始化 DB / 不启动扫描）。exit 为路由级依赖鉴权，即使全站中间件
未启用（token 为空）也会 401 —— 这正是本修复的核心。

退出测试：/exit 会真实发 SIGTERM（防真杀测试进程），所有会走到退出路径的
用例均 patch 模块级 os.kill / os._exit 屏蔽副作用；直接函数级调用用例复用
test_system_api.py 的同步线程手法保证断言确定性。
"""

from __future__ import annotations

import importlib
import os
import signal
import threading as _th
import types

import pytest
from fastapi.testclient import TestClient

import app.api.system as SYS
import app.config as CFG
import app.main as M

# 预导入（与 test_system_api.py 同因）：exit 测试会 patch 全局 threading.Thread/
# Timer，若 storage.cache/scanning 在 patch 后才首次 import，TTLCache 的磁盘后台
# 线程（while True）被同步执行 → 死锁。必须先于任何测试完成真实导入。
import app.core.scanning  # noqa: F401  (exit 测试 stop_scanner 的 monkeypatch 目标)
import app.storage.cache  # noqa: F401  (exit 测试 flush 的懒加载目标)

EXIT_URL = "/api/v1/system/exit"


# ---------------------------------------------------------------------------
# fixtures：无 token / 有 token 两个 app 形态（与 test_cors_auth 同模式）
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_token_client():
    """SR_API_TOKEN 为空（默认）→ 全站中间件不启用；exit 路由级依赖仍应 401。"""
    CFG.settings.api_token = ""
    mod = importlib.reload(M)
    client = TestClient(mod.app)
    yield client
    client.close()
    importlib.reload(M)  # 恢复导入态，避免污染其他用例


@pytest.fixture()
def exit_client(monkeypatch):
    """配置 token + 重建带 auth 的 app；patch os.kill 屏蔽 SIGTERM（防杀测试进程）。

    Timer 同步执行（_SyncTimer）：/exit 的 _shutdown 内 Timer(0.5, _graceful_exit)
    若真实异步触发，会在本 fixture teardown 恢复 os.kill 之后才执行 → 真实
    SIGTERM 杀掉 pytest 进程（全量跑时 81% 处 exit=143 的根因）。同步执行保证
    _graceful_exit 在 os.kill 仍被 patch 期间完成，无遗留定时器。
    """

    class _SyncTimer:
        def __init__(self, *a, **k):
            self._target, self._args, self._kwargs = a[0], a[1:], k

        def start(self):
            self._target(*self._args, **self._kwargs)

    CFG.settings.api_token = "secret"
    mod = importlib.reload(M)
    client = TestClient(mod.app)
    monkeypatch.setattr(SYS.os, "kill", lambda *a: None)
    monkeypatch.setattr(SYS.threading, "Timer", _SyncTimer)
    yield client
    client.close()
    CFG.settings.api_token = ""
    importlib.reload(M)


# ---------------------------------------------------------------------------
# 鉴权：无 token 被拒 / 有 token 可过（P1-58 核心）
# ---------------------------------------------------------------------------


def test_exit_401_when_token_unconfigured(no_token_client):
    """token 未配置：exit 必须拒绝（修复前此处全站不鉴权 → 任意可达者可远程杀进程）。"""
    resp = no_token_client.post(EXIT_URL)
    assert resp.status_code == 401, resp.text
    # 明确提示需配置 token，而非笼统 Unauthorized
    assert "SR_API_TOKEN" in resp.json()["detail"]


def test_exit_401_without_token(exit_client):
    """已配置 token：不带 X-API-Token → 401。"""
    resp = exit_client.post(EXIT_URL)
    assert resp.status_code == 401, resp.text


def test_exit_401_wrong_token(exit_client):
    """已配置 token：token 错误 → 401。"""
    resp = exit_client.post(EXIT_URL, headers={"X-API-Token": "wrong"})
    assert resp.status_code == 401, resp.text


def test_exit_200_with_correct_token(exit_client):
    """已配置 token：X-API-Token 正确 → 200，正常进入退出流程。"""
    resp = exit_client.post(EXIT_URL, headers={"X-API-Token": "secret"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (
        body["ok"] is True and body["status"] == "exiting" and body["reason"] == "user"
    )


def test_exit_idempotent_over_http(exit_client):
    """HTTP 层双调不炸：第二次短路返回 exiting（契约硬化 T-112 后 reason 恒输出，与首次一致）。"""
    h = {"X-API-Token": "secret"}
    first = exit_client.post(EXIT_URL, headers=h)
    second = exit_client.post(EXIT_URL, headers=h)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json() == {"ok": True, "status": "exiting", "reason": "user"}


# ---------------------------------------------------------------------------
# 优雅退出：SIGTERM 框架级关闭替代 os._exit(0)
# ---------------------------------------------------------------------------


def test_graceful_exit_sends_sigterm(monkeypatch):
    """_graceful_exit 发 SIGTERM 给自己（触发 uvicorn 优雅关闭 → atexit 落盘），
    绝不直接 os._exit。"""
    killed = []
    monkeypatch.setattr(SYS.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    exited = []
    monkeypatch.setattr(SYS.os, "_exit", exited.append)

    SYS._graceful_exit()

    assert killed == [(os.getpid(), signal.SIGTERM)], killed
    assert exited == []  # 未走 os._exit


def test_graceful_exit_signal_failure_no_os_exit(monkeypatch):
    """极端环境 SIGTERM 不可用：仅记日志，绝不 os._exit（B4 铁律 os_exit 禁止）。"""

    def _raise(*a):
        raise OSError("signal disabled")

    exited = []
    monkeypatch.setattr(SYS.os, "kill", _raise)
    monkeypatch.setattr(SYS.os, "_exit", exited.append)

    SYS._graceful_exit()

    assert exited == []  # 无 os._exit 兜底，进程退出交由外层服务管理


def test_exit_system_full_path_no_raise(monkeypatch):
    """exit_system 完整关闭链路（停调度 → dispose → flush → SIGTERM）不抛错，
    最终走 SIGTERM 而非 os._exit。"""
    calls = []
    monkeypatch.setattr(SYS, "_exiting", _th.Event())
    monkeypatch.setattr(
        "app.core.scanning.stop_scanner", lambda: calls.append("scanner")
    )
    # system.py 模块顶部 `from ..storage.db import engine` 是名字绑定，
    # 必须 patch SYS.engine（模块属性 patch 不影响已绑定的旧引用）
    monkeypatch.setattr(
        SYS, "engine", types.SimpleNamespace(dispose=lambda: calls.append("db"))
    )
    monkeypatch.setattr(SYS.os, "kill", lambda *a: calls.append("sigterm"))
    monkeypatch.setattr(SYS.os, "_exit", lambda code: calls.append("os_exit"))

    # Timer.start() 同步执行回调（确定性断言 SIGTERM 路径），Thread 同步执行
    class _SyncTimer:
        def __init__(self, *a, **k):
            self._target, self._args, self._kwargs = a[0], a[1:], k

        def start(self):
            self._target(*self._args, **self._kwargs)

    class _SyncThread:
        def __init__(
            self,
            group=None,
            target=None,
            name=None,
            args=(),
            kwargs=None,
            *,
            daemon=None,
        ):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(SYS.threading, "Timer", _SyncTimer)
    monkeypatch.setattr(SYS.threading, "Thread", _SyncThread)

    out = SYS.exit_system(reason="pytest")
    assert out == {"ok": True, "status": "exiting", "reason": "pytest"}
    # 关闭链路顺序：停调度 → dispose；最终经 SIGTERM 优雅退出，未走 os._exit
    assert calls[:2] == ["scanner", "db"], calls
    assert "sigterm" in calls, calls
    assert "os_exit" not in calls, calls
