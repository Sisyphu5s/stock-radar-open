"""待办事项 API 聚焦测试：CRUD、空标题/过长标题/无字段 422、404、排序与 completed 筛选。

模式与 test_page_filters 一致：临时 SQLite + 直接调用路由函数，不触碰生产库。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.todos import (
    TodoCreate,
    TodoUpdate,
    create_todo,
    delete_todo,
    list_todos,
    update_todo,
)
from app.storage.db import Base
from app.storage.models import TodoItem, utcnow


@pytest.fixture()
def session(tmp_path):
    """独立临时 SQLite（Base.metadata 含 TodoItem）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'todos.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Maker()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed(session, rows):
    """rows: list of (title, completed, updated_at 秒数偏移)；偏移越小越新。"""
    base = utcnow()
    for title, completed, delta in rows:
        t = TodoItem(
            title=title,
            completed=completed,
            created_at=base,
            updated_at=base - timedelta(seconds=delta),
        )
        session.add(t)
    session.commit()


def _titles(items):
    return [t["title"] for t in items]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_crud_roundtrip(session):
    created = create_todo(payload=TodoCreate(title="写周报"), db=session)
    assert created["title"] == "写周报"
    assert created["completed"] is False
    assert created["id"] > 0
    assert created["created_at"] and created["updated_at"]

    lst = list_todos(db=session)["data"]
    assert len(lst) == 1 and lst[0]["id"] == created["id"]

    updated = update_todo(
        todo_id=created["id"],
        payload=TodoUpdate(title="写季度报", completed=True),
        db=session,
    )
    assert updated["title"] == "写季度报" and updated["completed"] is True

    done = list_todos(completed=True, db=session)["data"]
    assert _titles(done) == ["写季度报"]

    assert delete_todo(todo_id=created["id"], db=session) == {"ok": True}
    assert list_todos(db=session)["data"] == []


def test_create_title_stripped(session):
    item = create_todo(payload=TodoCreate(title="  写代码  "), db=session)
    assert item["title"] == "写代码"


# ---------------------------------------------------------------------------
# Pydantic 严格校验（422）
# ---------------------------------------------------------------------------


def test_create_invalid_title_rejected(session):
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            create_todo(payload=TodoCreate(title=bad), db=session)
    with pytest.raises(ValidationError):
        create_todo(payload=TodoCreate(title="x" * 201), db=session)
    # 多余字段拒绝
    with pytest.raises(ValidationError):
        TodoCreate.model_validate({"title": "ok", "bogus": 1})
    assert list_todos(db=session)["data"] == []


def test_update_requires_at_least_one_field(session):
    item = create_todo(payload=TodoCreate(title="a"), db=session)
    # 无任何字段 → 422
    with pytest.raises(ValidationError):
        update_todo(todo_id=item["id"], payload=TodoUpdate(), db=session)
    # 空白 title → 422（即使 completed 同时提供也整体拒绝）
    with pytest.raises(ValidationError):
        update_todo(todo_id=item["id"], payload=TodoUpdate(title="   "), db=session)
    # 多余字段 → 422
    with pytest.raises(ValidationError):
        update_todo(
            todo_id=item["id"],
            payload=TodoUpdate.model_validate({"bogus": 1}),
            db=session,
        )
    # 只更新 completed 合法
    ok = update_todo(todo_id=item["id"], payload=TodoUpdate(completed=True), db=session)
    assert ok["completed"] is True and ok["title"] == "a"


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------


def test_update_delete_missing_404(session):
    with pytest.raises(HTTPException) as e1:
        update_todo(todo_id=999, payload=TodoUpdate(title="x"), db=session)
    assert e1.value.status_code == 404
    with pytest.raises(HTTPException) as e2:
        delete_todo(todo_id=999, db=session)
    assert e2.value.status_code == 404


# ---------------------------------------------------------------------------
# 排序（未完成优先 + updated_at 倒序）与 completed 筛选
# ---------------------------------------------------------------------------


def test_ordering_uncompleted_first_then_updated_at_desc(session):
    _seed(
        session,
        [
            ("旧未完成", False, 100),
            ("新未完成", False, 10),
            ("新完成", True, 20),
            ("旧完成", True, 200),
            ("中等完成", True, 50),
        ],
    )
    lst = list_todos(db=session)["data"]
    assert _titles(lst) == ["新未完成", "旧未完成", "新完成", "中等完成", "旧完成"]
    assert all(t["completed"] is False for t in lst[:2])
    assert all(t["completed"] is True for t in lst[2:])


def test_completed_filter(session):
    _seed(
        session,
        [
            ("旧未完成", False, 100),
            ("新未完成", False, 10),
            ("新完成", True, 20),
        ],
    )
    assert _titles(list_todos(completed=False, db=session)["data"]) == [
        "新未完成",
        "旧未完成",
    ]
    assert _titles(list_todos(completed=True, db=session)["data"]) == ["新完成"]
