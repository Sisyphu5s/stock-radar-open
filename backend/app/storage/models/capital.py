"""资金类历史模型(T-08 资金流/龙虎榜/两融/北向历史持股)。

金额统一人民币元(融资/融券/龙虎榜/资金流净额均为元);北向持股占比
hold_ratio 为百分数值(3.12 = 3.12%);日期(date)为 naive YYYY-MM-DD 字符串
(交易日)。唯一约束:
- capital_moneyflow / capital_margin / capital_northbound:(code, date);
- capital_lhb:(code, date, reason)——同一股票同一交易日可能多次上榜
  (不同上榜原因,如"日涨幅偏离值达7%"与"连续三日涨幅偏离值累计达20%"),
  以 reason 区分,幂等 upsert 由 repos/capital.py 承担。
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .stock import utcnow


class CapitalMoneyFlow(Base):
    """个股每日资金流(东财 stock_individual_fund_flow):主力/超大单/大单/中单/小单 五档净流入额,单位元。"""

    __tablename__ = "capital_moneyflow"
    __table_args__ = (
        UniqueConstraint("code", "date", name="uq_cap_moneyflow_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))  # 600519.SH
    date: Mapped[str] = mapped_column(String(10))  # 交易日
    main_net: Mapped[float | None] = mapped_column(Float)  # 主力净流入(元)
    super_net: Mapped[float | None] = mapped_column(Float)  # 超大单净流入(元)
    large_net: Mapped[float | None] = mapped_column(Float)  # 大单净流入(元)
    medium_net: Mapped[float | None] = mapped_column(Float)  # 中单净流入(元)
    small_net: Mapped[float | None] = mapped_column(Float)  # 小单净流入(元)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CapitalLhb(Base):
    """龙虎榜(东财 stock_lhb_detail_em):上榜原因/买入额/卖出额/净买额,金额单位元。

    summary 为东财「解读」文本(机构动向简述,接口无结构化机构明细时兜底),
    无解读时回落上榜原因。
    """

    __tablename__ = "capital_lhb"
    __table_args__ = (
        UniqueConstraint("code", "date", "reason", name="uq_cap_lhb_code_date_reason"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))
    date: Mapped[str] = mapped_column(String(10))  # 上榜日
    reason: Mapped[str] = mapped_column(String(255))  # 上榜原因
    buy_amount: Mapped[float | None] = mapped_column(Float)  # 龙虎榜买入额(元)
    sell_amount: Mapped[float | None] = mapped_column(Float)  # 龙虎榜卖出额(元)
    net_amount: Mapped[float | None] = mapped_column(Float)  # 龙虎榜净买额(元)
    deal_amount: Mapped[float | None] = mapped_column(Float)  # 龙虎榜成交额(元)
    turnover_rate: Mapped[float | None] = mapped_column(Float)  # 换手率(%)
    summary: Mapped[str | None] = mapped_column(String(500))  # 解读/机构明细简述
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CapitalMargin(Base):
    """两融明细(SSE stock_margin_detail_sse / SZSE stock_margin_detail_szse,逐日)。

    融资余额/融资净买入单位元;融券余额仅深市接口提供(沪市接口仅有融券余量股数,
    沪市行 short_balance 为 None);融资净买入 = 当日融资余额环比变化(相邻交易日)。
    """

    __tablename__ = "capital_margin"
    __table_args__ = (UniqueConstraint("code", "date", name="uq_cap_margin_code_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))
    date: Mapped[str] = mapped_column(String(10))  # 交易日
    margin_balance: Mapped[float | None] = mapped_column(Float)  # 融资余额(元)
    short_balance: Mapped[float | None] = mapped_column(Float)  # 融券余额(元,仅深市)
    net_buy: Mapped[float | None] = mapped_column(Float)  # 融资净买入(元,余额环比)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CapitalNorthbound(Base):
    """北向历史持股(东财 stock_hsgt_individual_em):持股数量(股)与持股占A股比例(%)。

    港交所 2024-08 起停止披露北向实时/每日持股,该接口仅返回停止披露前的历史序列;
    接口失效时表留空,端点返回 503 说明(见 api/stocks.py)。
    """

    __tablename__ = "capital_northbound"
    __table_args__ = (
        UniqueConstraint("code", "date", name="uq_cap_northbound_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))
    date: Mapped[str] = mapped_column(String(10))  # 持股日期
    hold_shares: Mapped[float | None] = mapped_column(Float)  # 持股数量(股)
    hold_ratio: Mapped[float | None] = mapped_column(Float)  # 持股占A股比例(%)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
