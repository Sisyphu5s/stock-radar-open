"""CORS 预检 + API Token 鉴权顺序测试（T-41 / P0-25）。

背景：CORS 必须注册在 auth 中间件之后（Starlette add_middleware 后注册者在外层），
否则 OPTIONS 预检先过 api_token_auth，无 X-API-Token → 401，预检被拦。

注入方式：main.py 的 `if settings.api_token:` 在 import 时求值，模块级 env 注入
受 pytest 文件执行顺序影响不可靠，故用 setattr(settings) + importlib.reload(main)
重建带 auth 的 app。TestClient 不用 with（本仓库既有模式，见 test_events_sse.py），
不触发 lifespan，不初始化 DB / 不启动扫描。
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

import app.config as CFG
import app.main as M

API_PATH = "/api/v1/system/cache-stats"  # 纯内存只读接口，不碰 DB / 行情源


@pytest.fixture()
def token_client():
    """开 token 并 reload main → 带 auth + CORS 的 app；teardown 恢复无 token 状态。"""
    original = CFG.settings.api_token
    CFG.settings.api_token = "secret"
    mod = importlib.reload(M)
    client = TestClient(mod.app)
    yield client
    client.close()
    CFG.settings.api_token = original
    importlib.reload(M)  # 恢复无 auth 的 app，避免污染其他用例


def test_cors_preflight_ok_without_token(token_client):
    """开 token 时 OPTIONS 预检应 200 且带 CORS 头（修复核心：auth 不再拦截预检）。"""
    resp = token_client.options(
        API_PATH,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("access-control-allow-origin") in (
        "*",
        "http://localhost:5173",
    )


def test_get_without_token_unauthorized(token_client):
    resp = token_client.get(API_PATH)
    assert resp.status_code == 401


def test_get_with_token_ok(token_client):
    resp = token_client.get(API_PATH, headers={"X-API-Token": "secret"})
    assert resp.status_code == 200
    assert "data" in resp.json()
