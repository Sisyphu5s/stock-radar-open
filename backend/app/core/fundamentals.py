"""财务/资金类历史协调层：新鲜度策略 + 拉取编排 + 写库（api 层编排下沉）。

归属原则（参照 core/sources.py 门面模式）：storage/repos/financials.py、capital.py
为纯读写仓储，core/sources.get_* 为纯拉取门面——「库缺/过期判断 → 增量窗口 →
拉取 → upsert」的协调逻辑归本模块，api 层只保留参数校验/序列化/HTTP 语义。

策略常量（刷新阈值/增量窗口）随之下沉，api 层不再持有副本。
注意：拉取函数（core.sources.get_*）在函数内局部 import——测试 monkeypatch
app.core.sources.get_* 可穿透（模块属性替换），与 api 层既有模式一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..lib.session import now_cn

# 财务历史刷新阈值（天）：库中最新报告期距今超过该值 → 触发 akshare 拉取。
# 口径：约一个季度（90d）+ 法定披露滞后余量（年报/季报披露窗口约 1~4 个月）。
REFRESH_FINANCIAL_DAYS = 130
# 龙虎榜/两融拉取窗口（自然日）：龙虎榜按日期区间全市场过滤、两融逐日请求，
# 窗口过大网络成本高；超过该窗口的库内历史直接返回（增量拉取）。
CAPITAL_FETCH_DAYS = 45
# 两融逐日请求（SSE/SZSE 每交易日一个全市场请求，~7s/个），独立更短窗口控制首次耗时
MARGIN_FETCH_DAYS = 30


class CapitalDataUnavailable(Exception):
    """资金类数据源确定性不可用（北向接口失效）——api 层据此转 503 说明。"""


def history_refresh_needed(last_date: str | None) -> bool:
    """库中最新报告期距今超过 REFRESH_FINANCIAL_DAYS（约一季度+披露滞后）→ 拉取新季度。"""
    if not last_date:
        return True
    try:
        d = datetime.strptime(last_date, "%Y-%m-%d")
    except ValueError:
        return True
    return (now_cn().date() - d.date()).days > REFRESH_FINANCIAL_DAYS


def ensure_financials_fresh(db, code: str, report_type: str) -> None:
    """财务三表新鲜度：库缺/过期 → 拉取并幂等 upsert 落库；拉取失败降级静默（库缓存兜底）。"""
    from ..core.sources import get_financial_history
    from ..storage.repos.financials import latest_financial_date, upsert_financials

    if history_refresh_needed(latest_financial_date(db, code, report_type)):
        rows = get_financial_history(code, report_type)
        if rows:
            upsert_financials(db, code, report_type, rows)


def ensure_valuation_fresh(db, code: str) -> None:
    """每日估值新鲜度：库中最新日期早于今天 → 拉取落库（幂等 upsert）。"""
    from ..core.sources import get_valuation_history
    from ..storage.repos.financials import latest_valuation_date, upsert_valuation

    last = latest_valuation_date(db, code)
    today = now_cn().date().isoformat()
    if last is None or last < today:
        rows = get_valuation_history(code)
        if rows:
            upsert_valuation(db, code, rows)


def ensure_capital_fresh(db, code: str, ctype: str) -> None:
    """资金类新鲜度：按类型策略拉取落库（幂等 upsert）。

    - northbound：库空才拉取；接口失效（返回 None）抛 CapitalDataUnavailable；
    - moneyflow：接口返回全量历史，库空才拉取；
    - lhb/margin：库空 → 拉初始窗口 (today-window, today]；库最新日期早于今天 →
      增量拉 (last+1, today]（窗口仅限制增量起点，防久未更新时回溯过深）。
    拉取失败（返回空）静默——库内已有数据直接返回（降级不阻塞）。
    """
    from ..core.sources import get_capital_data
    from ..storage.repos.capital import latest_capital_date, upsert_capital

    today = now_cn().date().isoformat()
    last = latest_capital_date(db, code, ctype)
    if ctype == "northbound":
        if last is None:
            rows = get_capital_data(code, "northbound")
            if rows is None:
                raise CapitalDataUnavailable(
                    "北向持股接口暂不可用（港交所 2024-08 起停止披露，东财通道亦失效）"
                )
            if rows:
                upsert_capital(db, code, "northbound", rows)
    elif ctype == "moneyflow":
        if last is None:
            rows = get_capital_data(code, "moneyflow")
            if rows:
                upsert_capital(db, code, "moneyflow", rows)
    else:
        window = MARGIN_FETCH_DAYS if ctype == "margin" else CAPITAL_FETCH_DAYS
        if last is None or last < today:
            start = (now_cn().date() - timedelta(days=window)).isoformat()
            if last:
                d = datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)
                start = max(start, d.date().isoformat())
            rows = get_capital_data(code, ctype, start_date=start, end_date=today)
            if rows:
                upsert_capital(db, code, ctype, rows)
