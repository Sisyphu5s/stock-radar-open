"""/api/v1/todos/* : 待办事项（任务中心「待办事项」视图的持久化后端）。"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from sqlalchemy.orm import Session

from ..storage.db import get_db
from ..storage.models import TodoItem, utcnow
from ..lib.timex import to_market_naive

router = APIRouter(prefix="/todos", tags=["todos"])


# ===== 响应模型（P2-29 契约硬化：逐路径 response_model） =====


class TodoItemResponse(BaseModel):
    """待办事项响应（_serialize 的严格形状；时间字段为 naive ISO，可空）。"""

    id: int
    title: str
    completed: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TodoListResponse(BaseModel):
    data: list[TodoItemResponse]


class TodoDeleteResponse(BaseModel):
    ok: bool

# title：先 strip 再校验长度（Pydantic v2 方式），空白标题与超长标题均 422
NonEmptyTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class TodoCreate(BaseModel):
    """新增 payload：title 必填，strip 后 1-200 字；多余字段 422。"""

    model_config = ConfigDict(extra="forbid")

    title: NonEmptyTitle


class TodoUpdate(BaseModel):
    """部分更新 payload：title / completed 至少提供一个；title 同样要求非空。"""

    model_config = ConfigDict(extra="forbid")

    title: NonEmptyTitle | None = None
    completed: bool | None = None

    @model_validator(mode="after")
    def _require_one_field(self) -> "TodoUpdate":
        if self.title is None and self.completed is None:
            raise ValueError("title 与 completed 至少提供一个")
        return self


def _serialize(item: TodoItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "completed": item.completed,
        "created_at": (
            to_market_naive(item.created_at).isoformat() if item.created_at else None
        ),
        "updated_at": (
            to_market_naive(item.updated_at).isoformat() if item.updated_at else None
        ),
    }


@router.get("", response_model=TodoListResponse)
def list_todos(completed: bool | None = None, db: Session = Depends(get_db)):
    """待办列表：未完成优先（completed ASC），同组内 updated_at 倒序。

    completed 可选筛选（true=已完成 / false=待完成，缺省返回全部）。
    """
    q = db.query(TodoItem)
    if completed is not None:
        q = q.filter(TodoItem.completed == completed)
    rows = q.order_by(TodoItem.completed.asc(), TodoItem.updated_at.desc()).all()
    return {"data": [_serialize(r) for r in rows]}


@router.post("", response_model=TodoItemResponse)
def create_todo(payload: TodoCreate, db: Session = Depends(get_db)):
    """新增待办：title 非空（strip 后 1-200 字），非法输入 422。"""
    item = TodoItem(title=payload.title)
    db.add(item)
    db.commit()
    db.refresh(item)
    return _serialize(item)


@router.patch("/{todo_id}", response_model=TodoItemResponse)
def update_todo(todo_id: int, payload: TodoUpdate, db: Session = Depends(get_db)):
    """部分更新：title / completed 至少一个；不存在返回 404。"""
    item = db.get(TodoItem, todo_id)
    if item is None:
        raise HTTPException(404, "待办事项不存在")
    if payload.title is not None:
        item.title = payload.title
    if payload.completed is not None:
        item.completed = payload.completed
    item.updated_at = utcnow()
    db.commit()
    db.refresh(item)
    return _serialize(item)


@router.delete("/{todo_id}", response_model=TodoDeleteResponse)
def delete_todo(todo_id: int, db: Session = Depends(get_db)):
    item = db.get(TodoItem, todo_id)
    if item is None:
        raise HTTPException(404, "待办事项不存在")
    db.delete(item)
    db.commit()
    return {"ok": True}
