"""模拟盘模型：实验项目 / 观察快照 + T-01 账户（资金/持仓/委托/成交流水）。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .stock import utcnow


class PaperProject(Base):
    """模拟盘实验项目：多实验配置持久化 + 最近一次 run 结果快照。

    kind 创建后不可改（experiment/watch）；result 仅由 run 端点写入。
    """

    __tablename__ = "paper_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="未命名项目")
    kind: Mapped[str] = mapped_column(
        String(16), default="experiment"
    )  # experiment/watch
    code: Mapped[str] = mapped_column(String(12))
    period: Mapped[str] = mapped_column(String(8), default="daily")
    signals: Mapped[list] = mapped_column(JSON, default=list)  # 信号代码列表
    days: Mapped[int] = mapped_column(Integer, default=250)
    result: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )  # 最近一次 run 快照
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class PaperProjectStock(Base):
    """模拟盘项目股票关联：project_id + code + 顺序号（多股批量）。

    旧项目（单股时代）无关联行 → 以 paper_projects.code 兜底（迁移兼容）。
    name 为创建/更新时的名称快照（Stock 表改名后不追更，仅展示用）。
    """

    __tablename__ = "paper_project_stocks"
    __table_args__ = (
        Index("ix_paper_project_stock", "project_id", "seq", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(64), default="")
    seq: Mapped[int] = mapped_column(Integer, default=0)


class PaperWatchSnapshot(Base):
    """模拟盘观察项目实时快照：watch 轮询按限频落库，供历史价格/信号曲线。

    prices 为 {code: 最新收盘价}（多股项目每股一键）；hit_signals 为命中
    信号代码列表（多股跨股合并去重）。ts 为轮询落库时点，bar_date 为最后 bar 标签。
    """

    __tablename__ = "paper_watch_snapshots"
    __table_args__ = (Index("ix_paper_watch_snapshot_project_ts", "project_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    bar_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    prices: Mapped[dict] = mapped_column(JSON, default=dict)  # {code: close}
    hit_signals: Mapped[list] = mapped_column(JSON, default=list)


# ---------------------------------------------------------------------------
# T-01 模拟盘账户：资金 / 持仓 / 委托 / 成交流水
# ---------------------------------------------------------------------------


class PaperAccount(Base):
    """模拟盘账户：初始资金与当前可用现金（撮合时点由委托的 bar_date 表达）。

    cash 由成交实时扣减/回补；initial_cash 用于绩效基准（净值首点）。
    """

    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="模拟账户")
    initial_cash: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    cash: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class PaperPosition(Base):
    """模拟盘持仓：account×code 唯一，quantity 为当前持有股数，avg_cost 含费用摊薄成本。"""

    __tablename__ = "paper_positions"
    __table_args__ = (
        Index("uq_paper_position_account_code", "account_id", "code", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(12))
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class PaperOrder(Base):
    """模拟盘委托：下单即撮合（按 bar 收盘价），终态 filled / pending / canceled / rejected。

    成交细节写入 paper_trades；挂起（限价未触发）与拒绝（资金/持仓不足）保留原因。
    """

    __tablename__ = "paper_orders"
    __table_args__ = (Index("ix_paper_order_account", "account_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(12))
    side: Mapped[str] = mapped_column(String(8))  # buy/sell
    order_type: Mapped[str] = mapped_column(String(8), default="market")  # market/limit
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # 限价单委托价
    quantity: Mapped[float] = mapped_column(Float)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    reject_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bar_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class PaperTrade(Base):
    """模拟盘成交流水：每笔成交一条（含成交价/数量/金额/费用/bar 时点），绩效重放依据。"""

    __tablename__ = "paper_trades"
    __table_args__ = (Index("ix_paper_trade_account_date", "account_id", "bar_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(12))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    bar_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
