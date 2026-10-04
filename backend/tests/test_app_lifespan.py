"""The real application lifespan must build a registered runtime before serving."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main as main_module
from app.config import settings
from app.core import runtime as runtime_module


_COMPONENTS = (
    "SourceRecovery",
    "Scanning",
    "Warmup",
    "TaskMonitor",
    "EventsGC",
    "Backend",
    "JobRecovery",
)


def test_lifespan_registers_runtime_and_serves_health(monkeypatch):
    # Disable background work while exercising the actual setup_runtime + lifespan path.
    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(settings, "runtime_components", {name: False for name in _COMPONENTS})
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    monkeypatch.setattr("app.storage.db.SessionLocal", sessionmaker(bind=engine))

    with TestClient(main_module.app) as client:
        runtime = main_module._runtime
        assert runtime is not None
        assert set(runtime.status()) == set(_COMPONENTS)
        assert all(not state["enabled"] for state in runtime.status().values())
        assert client.get("/api/v1/health/live").json() == {"status": "ok"}
        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 200
        assert ready.json()["db"] == "ok"

    assert main_module._runtime is None
    engine.dispose()
