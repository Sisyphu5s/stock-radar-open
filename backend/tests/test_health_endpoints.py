"""T-123 三类健康端点：/health/live、/health/ready（DB + 运行时组件可观测）。

函数级测试（不启动 lifespan/不触碰生产库）：monkeypatch SessionLocal 与 _runtime。
"""

from __future__ import annotations

import pytest


def test_health_live_ok():
    """Liveness：进程活着即 200 语义，不探任何依赖。"""
    from app.main import health_live

    assert health_live() == {"status": "ok"}


def test_health_ready_ok(monkeypatch):
    """Readiness：DB 可达 + 组件全 running → ok。"""
    from app import main

    class _FakeRT:
        def status(self):
            return {
                "SourceRecovery": {"name": "SourceRecovery", "running": True},
                "JobRecovery": {"name": "JobRecovery", "running": True},
            }

    monkeypatch.setattr(main, "_runtime", _FakeRT())
    # 真实 DB（测试库/生产库不可依赖）——用可执行 SELECT 1 的假会话替代
    class _FakeSession:
        def execute(self, _sql):
            return None

        def close(self):
            pass

    monkeypatch.setattr("app.storage.db.SessionLocal", lambda: _FakeSession())

    body = main.health_ready()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["degraded"] == []


def test_health_ready_db_unreachable_503(monkeypatch):
    """Readiness：DB 不可用 → 503（不接流量）。"""
    from app import main

    class _FakeRT:
        def status(self):
            return {}

    monkeypatch.setattr(main, "_runtime", _FakeRT())

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.storage.db.SessionLocal", _boom)

    from fastapi.responses import JSONResponse

    resp = main.health_ready()
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 503
    assert resp.body


def test_health_ready_degraded_components(monkeypatch):
    """组件级失败（容错设计）→ 200 + degraded 标记。"""
    from app import main

    class _FakeRT:
        def status(self):
            return {
                "Scanning": {"name": "Scanning", "running": False},
                "Preheat": {"name": "Preheat", "running": True},
            }

    monkeypatch.setattr(main, "_runtime", _FakeRT())

    class _FakeSession:
        def execute(self, _sql):
            return None

        def close(self):
            pass

    monkeypatch.setattr("app.storage.db.SessionLocal", lambda: _FakeSession())

    body = main.health_ready()
    assert body["status"] == "ok"
    assert body["degraded"] == ["Scanning"]