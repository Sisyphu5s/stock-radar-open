"""C5:uvicorn reload 配置显式化——新字段默认值与 env 覆盖。

背景:reload 开关/排除项收敛在 Settings(SR_UVICORN_RELOAD /
SR_UVICORN_RELOAD_EXCLUDE),默认值必须与旧硬编码行为一致(reload 开、排除 tests/)。
"""

from __future__ import annotations

from app.config import Settings


def test_uvicorn_reload_defaults(monkeypatch):
    """缺省环境变量:reload 开(保持旧行为)、排除 tests/。"""
    monkeypatch.delenv("SR_UVICORN_RELOAD", raising=False)
    monkeypatch.delenv("SR_UVICORN_RELOAD_EXCLUDE", raising=False)
    s = Settings()
    assert s.uvicorn_reload is True
    assert s.uvicorn_reload_exclude == "tests"


def test_uvicorn_reload_env_override(monkeypatch):
    """SR_UVICORN_RELOAD=false 关闭 reload;排除项逗号分隔可覆盖。"""
    monkeypatch.setenv("SR_UVICORN_RELOAD", "false")
    monkeypatch.setenv("SR_UVICORN_RELOAD_EXCLUDE", "tests,venv")
    s = Settings()
    assert s.uvicorn_reload is False
    assert s.uvicorn_reload_exclude == "tests,venv"
