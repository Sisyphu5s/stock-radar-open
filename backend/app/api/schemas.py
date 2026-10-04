"""API 层响应模型基库(P2-29):统一错误/数据/分页包络,供各路由 response_model 复用。

- ErrorResponse:统一错误格式 {detail: str}(与 FastAPI HTTPException 同构)
- DataResponse[T]:数据/列表统一包络 {data: ...}
- Paginated[T]:分页统一包络 {items, total}(契约约定,偏移等扩展字段由路由模型承接)

contract_kind 为契约形状标注,schema_dump 读取写入 contract.json._schemas 供前端 R1 对账。
"""

from __future__ import annotations

from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ErrorResponse(BaseModel):
    """统一错误包络 {detail: str}。"""

    contract_kind: ClassVar[str] = "error"
    detail: str


class DataResponse(BaseModel, Generic[T]):
    """数据/列表统一包络 {data: ...}。"""

    contract_kind: ClassVar[str] = "envelope"
    data: T


class Paginated(BaseModel, Generic[T]):
    """分页统一包络 {items, total}。"""

    contract_kind: ClassVar[str] = "pagination"
    items: list[T]
    total: int
