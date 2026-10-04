"""股票 + K 线模型。"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..klines_db import Kline  # noqa: F401  Kline 已迁独立 K 线库（klines_base），此处转发保兼容


def utcnow() -> datetime:
    """UTC 时间戳（naive，消除 datetime.utcnow 弃用警告）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Stock(Base):
    """股票基本信息 + 最近一次快照。"""

    __tablename__ = "stocks"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)  # 600519.SH
    name: Mapped[str] = mapped_column(String(64), index=True)
    industry: Mapped[str] = mapped_column(String(64), default="")
    is_watchlist: Mapped[bool] = mapped_column(Boolean, default=False)

    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    pct_change: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)  # 手
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # 元
    turnover_rate: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WatchlistGroup(Base):
    """自选股分组（T-12）：用户标签，name 唯一。"""

    __tablename__ = "watchlist_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class WatchlistGroupItem(Base):
    """分组内股票（T-12）：同组同股唯一（幂等加入）；随分组删除级联清理。"""

    __tablename__ = "watchlist_group_items"
    __table_args__ = (
        UniqueConstraint("group_id", "code", name="uq_watchlist_group_item"),
        Index("ix_watchlist_group_item_code", "code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("watchlist_groups.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(12))
