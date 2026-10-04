"""NNModel 仓储查询（api/alpha nn-models 列表、api/factors nn 因子 model_id 校验收敛入口）。

自建会话（db=None）走 `..db`（storage 层）——测试 patch app.storage.db.SessionLocal 生效。
"""

from __future__ import annotations

from ..models import NNModel


def list_nn_models(db=None) -> list:
    """神经网络模型列表（id 降序）。db 为 None 时自建会话并关闭。"""
    own = db is None
    if own:
        from ..db import SessionLocal

        db = SessionLocal()
    try:
        return db.query(NNModel).order_by(NNModel.id.desc()).all()
    finally:
        if own:
            db.close()


def get_nn_model(model_id: int, db=None) -> NNModel | None:
    """按 id 取 NNModel，不存在返回 None。db 为 None 时自建会话并关闭。"""
    own = db is None
    if own:
        from ..db import SessionLocal

        db = SessionLocal()
    try:
        return db.get(NNModel, model_id)
    finally:
        if own:
            db.close()
