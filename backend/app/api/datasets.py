"""/api/v1/datasets/* : 数据集管理。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import Optional
from pydantic import BaseModel

from ..storage.db import get_db
from ..storage.models import Dataset
from ..lib.timex import to_market_naive
from ..core.datasets import (
    UNIVERSES,
    build_dataset,
    default_range,
    delete_panel_files,
    list_universes,
)
from ..core.warmup import get_warmup_status

router = APIRouter(prefix="/datasets", tags=["datasets"])


# ===== 响应模型（T-113 契约硬化：P2-29 阶段三，逐路径契约） =====
# 字段与各端点 return dict 逐一核对，缺字段=response_model 裁剪响应=前端数据缺失。

class UniverseItem(BaseModel):
    """GET /datasets/universes 单选项（universe 契约 key + 中文名）。"""

    key: str
    name: str


class UniverseListResponse(BaseModel):
    data: list[UniverseItem]


class WarmupStatus(BaseModel):
    """启动预热进度：idle/running/done/failed + progress + message + dataset_id。"""

    status: str
    progress: float
    message: str
    dataset_id: Optional[int] = None


class WarmupStatusResponse(BaseModel):
    data: WarmupStatus


class DatasetRow(BaseModel):
    """数据集列表行（list_datasets 序列化；created_at 可空）。"""

    id: int
    name: str
    universe: str
    start_date: str
    end_date: str
    stock_count: int
    row_count: int
    status: str
    created_at: Optional[str] = None


class DatasetListResponse(BaseModel):
    data: list[DatasetRow]


class CreateDatasetResponse(BaseModel):
    """POST /datasets：{job_id, status=pending, name}（后台任务已受理）。"""

    job_id: int
    status: str
    name: str


class OkResponse(BaseModel):
    ok: bool


def _parse_iso_date(value: str, field: str) -> str:
    """ISO 日期解析校验：非法格式返回 400（避免脏字符串进任务参数）。"""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, f"{field} 格式非法: {value}（需 YYYY-MM-DD）")


@router.get("/universes", response_model=UniverseListResponse)
def universes():
    return {"data": list_universes()}


@router.get("/warmup/status", response_model=WarmupStatusResponse)
def warmup_status():
    """启动预热进度查询：idle/running/done/failed + progress + message + dataset_id。"""
    return {"data": get_warmup_status()}


@router.get("", response_model=DatasetListResponse)
def list_datasets(db: Session = Depends(get_db)):
    ds = db.query(Dataset).order_by(Dataset.id.desc()).all()
    return {
        "data": [
            {
                "id": d.id,
                "name": d.name,
                "universe": d.universe,
                "start_date": d.start_date,
                "end_date": d.end_date,
                "stock_count": d.stock_count,
                "row_count": d.row_count,
                "status": d.status,
                "created_at": (
                    to_market_naive(d.created_at).isoformat() if d.created_at else None
                ),
            }
            for d in ds
        ]
    }


@router.post("", response_model=CreateDatasetResponse)
def create_dataset(payload: dict, db: Session = Depends(get_db)):
    """构建数据集（后台任务，逐股票进度计入任务管理器）。返回 {job_id, status}。

    universe 契约（与 dataset.py 一致）：
      all 全市场 / hs300 沪深300 / top500 成交额Top500 / custom 自定义池；
    limit：>0 为上限，0 = universe 默认（all/hs300 全量、top500=500、custom 全部代码）。
    非法参数同步返回 HTTP 400（不静默忽略）。
    """
    from ..core.tasks.errors import QueueFullError
    from ..core.tasks.runner import submit

    try:
        limit = int(payload.get("limit", 300))
    except (TypeError, ValueError):
        raise HTTPException(400, "limit 必须是整数")
    if limit < 0:
        raise HTTPException(400, "limit 不能为负（0 表示 universe 默认规模）")
    universe = str(payload.get("universe", "custom"))
    if universe not in UNIVERSES:
        raise HTTPException(
            400, f"未知 universe: {universe}（可选: {', '.join(UNIVERSES)}）"
        )
    start, end = default_range()
    start_date = _parse_iso_date(str(payload.get("start_date", start)), "start_date")
    end_date = _parse_iso_date(str(payload.get("end_date", end)), "end_date")
    if start_date > end_date:
        raise HTTPException(
            400, f"日期范围非法: start_date({start_date}) 晚于 end_date({end_date})"
        )
    name = str(payload.get("name", "")).strip() or "未命名数据集"
    try:
        job_id = submit(
            "dataset_build",
            {
                "name": name,
                "universe": universe,
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
                "custom_codes": payload.get("custom_codes"),
            },
        )
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job_id, "status": "pending", "name": name}


@router.delete("/{ds_id}", response_model=OkResponse)
def delete_dataset(ds_id: int, db: Session = Depends(get_db)):
    ds = db.get(Dataset, ds_id)
    if ds is None:
        raise HTTPException(404, "数据集不存在")
    from ..storage.models import DatasetStock

    db.query(DatasetStock).filter(DatasetStock.dataset_id == ds_id).delete()
    db.delete(ds)
    db.commit()
    delete_panel_files(ds_id)
    return {"ok": True}
