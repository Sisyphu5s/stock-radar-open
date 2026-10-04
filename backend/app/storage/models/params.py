"""全局参数模型。"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .stock import utcnow


class AppParam(Base):
    """全局参数（如因子调优的全局默认参数），键值对存储。"""

    __tablename__ = "app_params"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
