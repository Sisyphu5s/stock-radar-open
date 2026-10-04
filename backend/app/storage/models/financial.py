"""财务三表历史 + 每日估值模型（T-06 基本面历史库）。

金额统一人民币元（估值市值除外：total_mv/float_mv 为亿元）；
比率字段（gross_margin/net_margin）为百分数值（91.54 = 91.54%），
与既有财务摘要契约（akshare get_financials 的「销售毛利率」等）展示口径一致。
日期（report_date/date）为 naive YYYY-MM-DD 字符串（季度末 / 交易日）。
唯一约束 (code, report_date) / (code, date)：幂等 upsert 由 repos/financials.py 承担。
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .stock import utcnow


class FinancialsBalance(Base):
    """资产负债表（按报告期，东财 stock_balance_sheet_by_report_em）。"""

    __tablename__ = "financials_balance"
    __table_args__ = (
        UniqueConstraint("code", "report_date", name="uq_fin_balance_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))  # 600519.SH
    report_date: Mapped[str] = mapped_column(String(10))  # 报告期（季度末）
    monetary_funds: Mapped[float | None] = mapped_column(Float)  # 货币资金
    accounts_receivable: Mapped[float | None] = mapped_column(Float)  # 应收账款
    inventories: Mapped[float | None] = mapped_column(Float)  # 存货
    fixed_assets: Mapped[float | None] = mapped_column(Float)  # 固定资产
    intangible_assets: Mapped[float | None] = mapped_column(Float)  # 无形资产
    goodwill: Mapped[float | None] = mapped_column(Float)  # 商誉
    total_current_assets: Mapped[float | None] = mapped_column(Float)  # 流动资产合计
    total_assets: Mapped[float | None] = mapped_column(Float)  # 资产总计
    total_current_liab: Mapped[float | None] = mapped_column(Float)  # 流动负债合计
    total_liabilities: Mapped[float | None] = mapped_column(Float)  # 负债合计
    total_equity: Mapped[float | None] = mapped_column(Float)  # 所有者权益合计
    parent_equity: Mapped[float | None] = mapped_column(Float)  # 归母所有者权益
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinancialsIncome(Base):
    """利润表（按报告期，东财 stock_profit_sheet_by_report_em）。"""

    __tablename__ = "financials_income"
    __table_args__ = (
        UniqueConstraint("code", "report_date", name="uq_fin_income_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))
    report_date: Mapped[str] = mapped_column(String(10))
    revenue: Mapped[float | None] = mapped_column(Float)  # 营业收入
    operating_cost: Mapped[float | None] = mapped_column(Float)  # 营业成本
    operating_profit: Mapped[float | None] = mapped_column(Float)  # 营业利润
    total_profit: Mapped[float | None] = mapped_column(Float)  # 利润总额
    net_profit: Mapped[float | None] = mapped_column(Float)  # 净利润
    parent_net_profit: Mapped[float | None] = mapped_column(Float)  # 归母净利润
    eps: Mapped[float | None] = mapped_column(Float)  # 基本每股收益
    gross_margin: Mapped[float | None] = mapped_column(Float)  # 毛利率（%）
    net_margin: Mapped[float | None] = mapped_column(Float)  # 净利率（%）
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinancialsCash(Base):
    """现金流量表（按报告期，东财 stock_cash_flow_sheet_by_report_em）。"""

    __tablename__ = "financials_cash"
    __table_args__ = (
        UniqueConstraint("code", "report_date", name="uq_fin_cash_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))
    report_date: Mapped[str] = mapped_column(String(10))
    net_operate_cash: Mapped[float | None] = mapped_column(
        Float
    )  # 经营活动现金流量净额
    net_invest_cash: Mapped[float | None] = mapped_column(Float)  # 投资活动现金流量净额
    net_finance_cash: Mapped[float | None] = mapped_column(
        Float
    )  # 筹资活动现金流量净额
    cce_add: Mapped[float | None] = mapped_column(Float)  # 现金及现金等价物净增加额
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ValuationHistory(Base):
    """每日估值快照（东财 stock_value_em；市值单位亿元）。"""

    __tablename__ = "valuation_history"
    __table_args__ = (
        UniqueConstraint("code", "date", name="uq_val_history_code_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12))
    date: Mapped[str] = mapped_column(String(10))  # 交易日
    pe: Mapped[float | None] = mapped_column(Float)  # PE(TTM)
    pb: Mapped[float | None] = mapped_column(Float)  # 市净率
    ps: Mapped[float | None] = mapped_column(Float)  # 市销率
    total_mv: Mapped[float | None] = mapped_column(Float)  # 总市值（亿）
    float_mv: Mapped[float | None] = mapped_column(Float)  # 流通市值（亿）
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
