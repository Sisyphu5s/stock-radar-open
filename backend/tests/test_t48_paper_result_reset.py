"""T-48：update_project 改 code/period/signals 必须清空旧 result（P1-15 参数与结果失配）。

临时 SQLite + 直接调 update_project（与 test_paper_task.py 同模式）：
- 仅改 name/days（不改变结果口径）→ result 保留；
- 改 code/period/signals 任一 → result 置 None，避免前端展示基于旧参数的结果。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import PaperProject

OLD_RESULT = {
    "code": "600519.SH",
    "period": "daily",
    "days": 80,
    "computed_bars": 80,
    "results": [{"signal": "macd_golden_cross", "triggers": 3}],
}


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite（不触碰生产库），替换 queue.SessionLocal 指向。"""
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

    import app.core.tasks.runner as Q

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    monkeypatch.setattr(Q, "init_backend", lambda: None)

    yield Maker
    engine.dispose()


def _seed_project(maker, **kw):
    db = maker()
    p = PaperProject(
        name=kw.get("name", "实验项目"),
        kind=kw.get("kind", "experiment"),
        code=kw.get("code", "600519.SH"),
        period=kw.get("period", "daily"),
        signals=kw.get("signals", ["macd_golden_cross"]),
        days=kw.get("days", 80),
        result=kw.get("result"),
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


def _patch(maker, pid, body):
    """直接调 update_project：返回 (响应体, DB 中最新 result)。"""
    from app.api.paper import ProjectPatch, update_project

    res = update_project(pid, body, maker())
    db = maker()
    p = db.get(PaperProject, pid)
    db.close()
    return res, p.result


def test_patch_name_only_keeps_result(temp_db):
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result=dict(OLD_RESULT))
    res, db_result = _patch(temp_db, pid, ProjectPatch(name="改名项目"))
    assert res["result"] == OLD_RESULT
    assert res["has_result"] is True
    assert res["result_invalidated"] is False  # 仅改 name 不使结果失效
    assert db_result == OLD_RESULT  # DB 层同样保留


def test_patch_code_clears_result(temp_db):
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result=dict(OLD_RESULT))
    res, db_result = _patch(temp_db, pid, ProjectPatch(code="000001.SZ"))
    assert res["code"] == "000001.SZ"
    assert res["result"] is None
    assert res["has_result"] is False
    assert db_result is None


def test_patch_period_clears_result(temp_db):
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result=dict(OLD_RESULT))
    res, db_result = _patch(temp_db, pid, ProjectPatch(period="60"))
    assert res["period"] == "60"
    assert res["result"] is None
    assert res["has_result"] is False
    assert db_result is None


def test_patch_signals_clears_result(temp_db):
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result=dict(OLD_RESULT))
    res, db_result = _patch(temp_db, pid, ProjectPatch(signals=["volume_surge"]))
    assert res["signals"] == ["volume_surge"]
    assert res["result"] is None
    assert res["has_result"] is False
    assert db_result is None


def test_patch_days_clears_result(temp_db):
    """T-84 结果失效语义扩展：days 变更同样使旧 result 失效（旧快照基于旧 days/bar 数）。"""
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result=dict(OLD_RESULT))
    res, db_result = _patch(temp_db, pid, ProjectPatch(days=120))
    assert res["days"] == 120
    assert res["result"] is None
    assert res["has_result"] is False
    assert res["result_invalidated"] is True
    assert db_result is None


def test_patch_mixed_name_and_code_clears_result(temp_db):
    """组合更新：name 变更 + code 变更 → result 仍应清空（任一参数口径变化即失效）。"""
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result=dict(OLD_RESULT))
    res, db_result = _patch(temp_db, pid, ProjectPatch(name="改名", code="000001.SZ"))
    assert res["name"] == "改名"
    assert res["code"] == "000001.SZ"
    assert res["result"] is None
    assert db_result is None
