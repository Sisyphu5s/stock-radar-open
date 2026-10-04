"""实验任务队列模型。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .stock import utcnow


class ExperimentJob(Base):
    """实验任务队列：Alpha 符号回归 / 评估。"""

    __tablename__ = "experiment_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32))  # gp_run / evaluate
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending/running/done/failed
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100
    phase: Mapped[str] = mapped_column(
        String(64), default=""
    )  # 阶段文案(如"训练中 12/50")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
