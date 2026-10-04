"""指标参数调优记忆模型。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .stock import utcnow


class IndicatorTune(Base):
    """指标参数调优记忆：每只股票的调优结果（按 股票+指标+目标+周期 更新）。"""

    __tablename__ = "indicator_tunes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_code: Mapped[str] = mapped_column(String(12), index=True)
    indicator: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(16), default="ic")
    horizon: Mapped[int] = mapped_column(Integer, default=5)
    best_param: Mapped[str] = mapped_column(String(32), default="")
    best_ic: Mapped[float] = mapped_column(Float, default=0.0)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
