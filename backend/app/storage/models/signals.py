"""信号事件模型。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .stock import utcnow


class SignalEvent(Base):
    """信号事件：一只股票 × 一个 bar 时点 = 一个聚合事件（携带命中信号数组）。

    同一 (stock_code, triggered_at, period) 只会存在一条事件：同 bar 重复扫描
    走 upsert 原地刷新 signals/evidence，不再按模板拆分多条。
    """

    __tablename__ = "signal_events"
    # 复合索引：仅新库生效（旧库由 database._migrate 收敛，见其去模板化迁移块）
    __table_args__ = (
        Index("ix_signal_event_stock_time", "stock_code", "triggered_at"),
        # (stock_code, triggered_at, period) 唯一：防同一 bar 时点重复事件
        # （调度扫描与手动扫描并发双写兜底；仅新库生效，旧库依赖 scan_once 互斥锁，
        # 见 scanner._scan_lock。triggered_at 经 bar_market_time 归一，无微秒漂移；
        # period 维度必须包含——daily/weekly bar 时点同为 15:00，缺 period 会跨周期误撞）
        Index(
            "uq_signal_event_stock_time",
            "stock_code",
            "triggered_at",
            "period",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # stock_code 不加单列索引：ix_signal_event_stock_time 最左前缀已覆盖。
    # status 单列索引：/events 与 /events/page 按 status 精确过滤（观察/确认/已忽略），
    # 复合索引前缀不覆盖 status，单列索引避免全表扫描。
    stock_code: Mapped[str] = mapped_column(String(12))
    signals: Mapped[list] = mapped_column(JSON)  # 命中信号名列表
    status: Mapped[str] = mapped_column(
        String(16), default="观察", index=True
    )  # 观察/确认/已忽略
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)  # 指标证据
    triggered_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=utcnow)
    # 数据实际截止时刻：盘中日线信号 = 当前上海时刻（去微秒）；否则 = bar_time（15:00）。
    # 承担去重/排序/筛选的仍是 triggered_at（bar 标签时点），as_of 仅作数据新鲜度展示。
    as_of: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 该事件首次被扫描发现的时间（上海 naive，去微秒，不可变）：新事件插入时写入，
    # 同 bar 重复扫描（upsert 命中）与「观察」原地刷新只推进 as_of，绝不改写本字段。
    # 时间三件套语义：triggered_at = bar 标签时点（去重/排序/筛选，不变）；
    # as_of = 数据截止时刻（重扫推进）；scan_discovered_at = 首次发现时刻（一次写入）。
    # 旧库迁移后旧行保持 NULL（迁移见 database._migrate）。
    scan_discovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 信号周期：daily/60/30/15/5/1（分钟级事件不入库；历史库迁移后旧行保持 daily）
    period: Mapped[str] = mapped_column(
        String(16), nullable=False, default="daily", server_default="daily"
    )
