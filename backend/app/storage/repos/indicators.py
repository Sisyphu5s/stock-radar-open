"""IndicatorTune 仓储查询（api/indicators 调优历史收敛入口）。

自建会话（db=None）走 `..db`（storage 层）——测试 patch app.storage.db.SessionLocal 生效。
"""

from __future__ import annotations

from ..models import IndicatorTune


def indicator_tune_history(
    code: str, indicator: str | None = None, limit: int = 30, db=None
) -> list:
    """按股票（可选指标）的调优历史，created_at 降序取至多 limit 条。"""
    own = db is None
    if own:
        from ..db import SessionLocal

        db = SessionLocal()
    try:
        q = db.query(IndicatorTune).filter(IndicatorTune.stock_code == code)
        if indicator:
            q = q.filter(IndicatorTune.indicator == indicator)
        return q.order_by(IndicatorTune.created_at.desc()).limit(limit).all()
    finally:
        if own:
            db.close()
