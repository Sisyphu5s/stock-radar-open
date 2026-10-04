"""T-77 运行时配置覆盖测试：读写 / 白名单拒绝 / 类型校验 / Settings 覆盖生效 / API 端点。

用临时 SQLite（不触碰生产库）；settings 为模块级单例，其 __getattribute__ hook 经
monkeypatched app.storage.db.SessionLocal 读临时库 AppParam（runtime_config 函数内
导入 SessionLocal，穿透生效），各测试独立 tmp_path 互不污染。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.storage.db as DB
from app.config import settings
from app.core.runtime_config import RUNTIME_SPECS, env_default, get_runtime, set_runtime
from app.storage.appparams import delete_app_params, get_app_param, set_app_params
from app.storage.db import Base
from app.storage.models import AppParam  # noqa: F401 (注册表)


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB：替换 app.storage.db.SessionLocal + 建全部表。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(DB, "SessionLocal", Maker)
    return Maker


# ---------------------------------------------------------------------------
# 注册表完整性
# ---------------------------------------------------------------------------


def test_specs_cover_six_keys():
    assert set(RUNTIME_SPECS) == {
        "scan_schedule",
        "market_scan_limit",
        "quote_cache_ttl",
        "job_concurrency",
        "job_io_concurrency",
        "job_queue_max",
        "llm_timeout",
        "llm_model",
    }


# ---------------------------------------------------------------------------
# get_runtime / set_runtime
# ---------------------------------------------------------------------------


def test_roundtrip_str_int_float_json(temp_db):
    set_runtime("llm_model", "gpt-4o-mini")
    assert get_runtime("llm_model", "fallback") == "gpt-4o-mini"
    assert get_app_param("rt:llm_model") == "gpt-4o-mini"

    set_runtime("market_scan_limit", 321)
    assert get_runtime("market_scan_limit", 1) == 321
    assert get_app_param("rt:market_scan_limit") == "321"

    set_runtime("llm_timeout", 45.5)
    assert get_runtime("llm_timeout", 1.0) == 45.5

    set_runtime("scan_schedule", {"daily": 120, "5": 0})
    assert get_runtime("scan_schedule", {}) == {"daily": 120, "5": 0}


def test_unknown_key_rejected(temp_db):
    with pytest.raises(ValueError):
        set_runtime("no_such_key", 1)
    # get_runtime 对未知 key 回落默认
    assert get_runtime("no_such_key", "d") == "d"


def test_type_and_range_validation(temp_db):
    with pytest.raises(ValueError):
        set_runtime("market_scan_limit", "abc")
    with pytest.raises(ValueError):
        set_runtime("market_scan_limit", True)  # bool 是 int 子类，拒绝
    with pytest.raises(ValueError):
        set_runtime("quote_cache_ttl", -5)
    with pytest.raises(ValueError):
        set_runtime("llm_timeout", "x")
    with pytest.raises(ValueError):
        set_runtime("llm_timeout", 0)
    with pytest.raises(ValueError):
        set_runtime("job_concurrency", 0)
    with pytest.raises(ValueError):
        set_runtime("job_concurrency", 99)  # 超上限 64
    with pytest.raises(ValueError):
        set_runtime("llm_model", "")
    with pytest.raises(ValueError):
        set_runtime("scan_schedule", [1, 2])  # 非 dict
    with pytest.raises(ValueError):
        set_runtime("scan_schedule", {"daily": -1})  # 负间隔


# ---------------------------------------------------------------------------
# Settings 读取覆盖生效
# ---------------------------------------------------------------------------


def test_settings_override_wins_and_restores(temp_db):
    base = env_default("market_scan_limit")
    assert settings.market_scan_limit == base
    set_app_params({"rt:market_scan_limit": "123"})
    assert settings.market_scan_limit == 123
    delete_app_params(["rt:market_scan_limit"])
    assert settings.market_scan_limit == base


def test_settings_override_float_and_str(temp_db):
    set_app_params({"rt:llm_timeout": "45.5", "rt:llm_model": "gpt-4o-mini"})
    assert settings.llm_timeout == 45.5
    assert settings.llm_model == "gpt-4o-mini"
    set_app_params({"rt:scan_schedule": '{"daily": 30}'})
    assert settings.scan_schedule == {"daily": 30}


def test_invalid_override_falls_back_to_env(temp_db):
    set_app_params({"rt:llm_timeout": "not-a-number"})
    assert settings.llm_timeout == env_default("llm_timeout")


# ---------------------------------------------------------------------------
# API 端点（TestClient 不触发 lifespan，无副作用）
# ---------------------------------------------------------------------------


def _client():
    from app.main import app

    return TestClient(app)


def test_get_api_lists_six_items(temp_db):
    r = _client().get("/api/v1/system/config/runtime")
    assert r.status_code == 200
    items = {i["key"]: i for i in r.json()["data"]}
    assert set(items) == set(RUNTIME_SPECS)
    item = items["market_scan_limit"]
    assert item["type"] == "int" and item["apply"] == "immediate"
    assert item["value"] == item["default"] == env_default("market_scan_limit")
    assert item["label"] and item["description"]
    sched = items["scan_schedule"]
    assert sched["type"] == "json" and sched["apply"] == "restart"
    assert isinstance(sched["value"], dict)
    assert sched["value"] == env_default("scan_schedule")


def test_put_api_roundtrip(temp_db):
    r = _client().put(
        "/api/v1/system/config/runtime",
        json={"key": "llm_model", "value": "gpt-4o-mini"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["value"] == "gpt-4o-mini"
    assert body["apply"] == "immediate"
    assert settings.llm_model == "gpt-4o-mini"  # hook 立即可见
    # GET 同步反映
    items = {
        i["key"]: i
        for i in _client().get("/api/v1/system/config/runtime").json()["data"]
    }
    assert items["llm_model"]["value"] == "gpt-4o-mini"


def test_put_api_rejects_unknown_key_and_bad_value(temp_db):
    client = _client()
    assert (
        client.put(
            "/api/v1/system/config/runtime", json={"key": "nope", "value": 1}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/system/config/runtime",
            json={"key": "market_scan_limit", "value": "abc"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/system/config/runtime",
            json={"key": "quote_cache_ttl", "value": -1},
        ).status_code
        == 400
    )
