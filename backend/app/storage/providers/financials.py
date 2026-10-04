"""财务三表历史 + 每日估值抓取（T-06 基本面历史库，storage/providers）。

数据源 akshare 东财系列（与 akshare.py 同纪律）：
- 三表：stock_balance_sheet_by_report_em / stock_profit_sheet_by_report_em /
  stock_cash_flow_sheet_by_report_em（按报告期全量历史，symbol=SH600519 格式）；
- 估值：stock_value_em（每日 PE(TTM)/PB/PS/总市值/流通市值，symbol=600519 裸码）。

纯网络 I/O，无业务状态；全部经 .base._ak_call 硬超时（25s）+ 异常分类；
**失败降级返回 []，不抛错不阻塞主流程**（库缓存兜底由 api 层承担）。

列映射数据驱动（单一事实源）：东财 EM 英文列 / 中文列 → 内部数据库字段。
金额单位：三表为元；估值市值转为亿元。比率字段（gross_margin/net_margin）为
百分数值（91.54 = 91.54%），与既有财务摘要展示口径一致。
"""

from __future__ import annotations

import logging

from ...lib.codes import bare_code as _bare
from .base import _ak_call, _ak_error

logger = logging.getLogger("stockradar.financials")

# 资产负债表：东财列 → 内部字段
BALANCE_COLUMNS = {
    "MONETARYFUNDS": "monetary_funds",  # 货币资金
    "ACCOUNTS_RECE": "accounts_receivable",  # 应收账款
    "INVENTORY": "inventories",  # 存货
    "FIXED_ASSET": "fixed_assets",  # 固定资产
    "INTANGIBLE_ASSET": "intangible_assets",  # 无形资产
    "GOODWILL": "goodwill",  # 商誉
    "TOTAL_CURRENT_ASSETS": "total_current_assets",  # 流动资产合计
    "TOTAL_ASSETS": "total_assets",  # 资产总计
    "TOTAL_CURRENT_LIAB": "total_current_liab",  # 流动负债合计
    "TOTAL_LIABILITIES": "total_liabilities",  # 负债合计
    "TOTAL_EQUITY": "total_equity",  # 所有者权益合计
    "TOTAL_PARENT_EQUITY": "parent_equity",  # 归母所有者权益
}

# 利润表：东财列 → 内部字段（gross_margin/net_margin 为派生计算列，抓取时填充）
INCOME_COLUMNS = {
    "OPERATE_INCOME": "revenue",  # 营业收入
    "OPERATE_COST": "operating_cost",  # 营业成本
    "OPERATE_PROFIT": "operating_profit",  # 营业利润
    "TOTAL_PROFIT": "total_profit",  # 利润总额
    "NETPROFIT": "net_profit",  # 净利润
    "PARENT_NETPROFIT": "parent_net_profit",  # 归母净利润
    "BASIC_EPS": "eps",  # 基本每股收益
}

# 现金流量表：东财列 → 内部字段
CASH_COLUMNS = {
    "NETCASH_OPERATE": "net_operate_cash",  # 经营活动现金流量净额
    "NETCASH_INVEST": "net_invest_cash",  # 投资活动现金流量净额
    "NETCASH_FINANCE": "net_finance_cash",  # 筹资活动现金流量净额
    "CCE_ADD": "cce_add",  # 现金及现金等价物净增加额
}

# 报表类型 → (akshare 接口名, 列映射)；抓取/入库共用同一注册表（数据驱动）
REPORT_API: dict[str, tuple[str, dict[str, str]]] = {
    "balance": ("stock_balance_sheet_by_report_em", BALANCE_COLUMNS),
    "income": ("stock_profit_sheet_by_report_em", INCOME_COLUMNS),
    "cash": ("stock_cash_flow_sheet_by_report_em", CASH_COLUMNS),
}

# 估值快照：东财中文列 → 内部字段（市值单位 元 → 亿，抓取时换算）
VALUATION_COLUMNS = {
    "数据日期": "date",
    "PE(TTM)": "pe",
    "市净率": "pb",
    "市销率": "ps",
    "总市值": "total_mv",
    "流通市值": "float_mv",
}


def _em_symbol(code: str) -> str:
    """600519 / 600519.SH → SH600519（东财 by_report_em 系列 symbol 大写前缀格式）。"""
    bare = _bare(code)
    if bare.startswith(("6", "9")):
        return f"SH{bare}"
    if bare.startswith(("4", "8")):
        return f"BJ{bare}"
    return f"SZ{bare}"


def _num(v):
    """数值清洗：None/空串/nan → None，其余 float（NaN 归 None）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "nan", "None", "<NA>", "NaT"):
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN 判定（float('nan') != float('nan')）


def _date_str(v) -> str:
    """日期归一：'2026-03-31 00:00:00' / datetime / 时间戳 → 'YYYY-MM-DD'。"""
    s = str(v).strip()
    if " " in s:
        s = s.split(" ")[0]
    if s.endswith(".0"):
        s = s[:-2]
    return s[:10]


def fetch_financials(code: str, report_type: str) -> list[dict]:
    """按报告期抓取三表历史（东财 by_report_em 全量）；失败降级返回 []。

    返回 [{report_date, <内部字段>…}]，report_date 为 naive YYYY-MM-DD（倒序语义
    由入库/查询统一处理，此处保持接口原始升序）。
    """
    import akshare as ak

    api_name, colmap = REPORT_API[report_type]
    try:
        raw = _ak_call(lambda: getattr(ak, api_name)(symbol=_em_symbol(code)))
    except Exception as e:
        logger.warning(
            "财报 %s 抓取失败 %s(%s): %s",
            report_type,
            code,
            api_name,
            str(_ak_error(e, f"fetch_financials_{report_type}", code))[:120],
        )
        return []
    if raw is None or raw.empty or "REPORT_DATE" not in raw.columns:
        logger.debug("财报 %s 空数据 %s(%s)", report_type, code, api_name)
        return []
    out: list[dict] = []
    for _, r in raw.iterrows():
        row: dict = {"report_date": _date_str(r.get("REPORT_DATE"))}
        for em_col, field in colmap.items():
            row[field] = _num(r.get(em_col))
        if report_type == "income":
            rev, cost = row.get("revenue"), row.get("operating_cost")
            if rev is not None and cost is not None and rev != 0:
                row["gross_margin"] = round((rev - cost) / rev * 100, 2)
            np_, rev2 = row.get("net_profit"), row.get("revenue")
            if np_ is not None and rev2 is not None and rev2 != 0:
                row["net_margin"] = round(np_ / rev2 * 100, 2)
        out.append(row)
    return out


def fetch_valuation_history(code: str) -> list[dict]:
    """抓取每日估值序列（东财 stock_value_em，symbol 裸码）；失败降级返回 []。

    返回 [{date, pe, pb, ps, total_mv, float_mv}…]，total_mv/float_mv 单位为亿元。
    """
    import akshare as ak

    try:
        raw = _ak_call(lambda: ak.stock_value_em(symbol=_bare(code)))
    except Exception as e:
        logger.warning(
            "估值历史抓取失败 %s(stock_value_em): %s",
            code,
            str(_ak_error(e, "fetch_valuation_history", code))[:120],
        )
        return []
    if raw is None or raw.empty or "数据日期" not in raw.columns:
        logger.debug("估值历史空数据 %s(stock_value_em)", code)
        return []
    out: list[dict] = []
    for _, r in raw.iterrows():
        row: dict = {"date": _date_str(r.get("数据日期"))}
        for cn_col, field in VALUATION_COLUMNS.items():
            if field == "date":
                continue
            v = _num(r.get(cn_col))
            if v is not None and field in ("total_mv", "float_mv"):
                v = round(v / 1e8, 4)  # 元 → 亿
            row[field] = v
        out.append(row)
    return out
