"""资金类数据抓取(T-08 资金流/龙虎榜/两融/北向历史持股,storage/providers)。

数据源 akshare 东财/交易所系列(与 financials.py 同纪律):
- 资金流:stock_individual_fund_flow(个股历史全量,主力/超大单/大单/中单/小单净额);
- 龙虎榜:stock_lhb_detail_em(按日期区间全市场,按代码过滤);
- 两融:stock_margin_detail_sse / stock_margin_detail_szse(按单交易日全市场,逐日拉取);
- 北向:stock_hsgt_individual_em(个股历史持股,港交所 2024-08 停披露后仅剩历史)。

纯网络 I/O,无业务状态;全部经 .base._ak_call 硬超时(25s)+ 异常分类;
**失败降级返回 []/None,不抛错不阻塞主流程**(库缓存兜底由 api 层承担)。
列映射数据驱动(单一事实源):akshare 中文列 → 内部数据库字段。
金额单位:元;北向持股占比为百分数值(3.12 = 3.12%)。
"""

from __future__ import annotations

import logging

from ...lib.codes import bare_code as _bare
from .base import _ak_call, _ak_error
from .financials import _date_str, _num

logger = logging.getLogger("stockradar.capital")

# 资金流:akshare 中文列 → 内部字段(五档净额,单位元)
MONEYFLOW_COLUMNS = {
    "主力净流入-净额": "main_net",
    "超大单净流入-净额": "super_net",
    "大单净流入-净额": "large_net",
    "中单净流入-净额": "medium_net",
    "小单净流入-净额": "small_net",
}

# 龙虎榜:akshare 中文列 → 内部字段
LHB_COLUMNS = {
    "龙虎榜买入额": "buy_amount",
    "龙虎榜卖出额": "sell_amount",
    "龙虎榜净买额": "net_amount",
    "龙虎榜成交额": "deal_amount",
    "换手率": "turnover_rate",
}

# 北向持股:akshare 中文列 → 内部字段
NORTHBOUND_COLUMNS = {
    "持股数量": "hold_shares",
    "持股数量占A股百分比": "hold_ratio",
}

# 两融逐日请求并发度(自然日窗口见 core/fundamentals.MARGIN_FETCH_DAYS 单一事实源):
# SSE/SZSE 每交易日一个全市场请求(~7s/个),逐日串行首拉最坏 ~30×7s≈210s 阻塞
# 请求线程;并发收敛为 ceil(天数/workers)×7s。并发任务由 _ak_call 25s 硬超时兜底。
MARGIN_FETCH_WORKERS = 6


def _market_of(code: str) -> str:
    """600519.SH / 600519 → 'sh'；000001 → 'sz'；4/8/920 开头 → 'bj'(东财资金流市场参数)。"""
    bare = _bare(code)
    if bare.startswith(("6", "9")):
        return "sh"
    if bare.startswith(("4", "8", "920")):
        return "bj"
    return "sz"


def fetch_moneyflow(code: str) -> list[dict]:
    """抓取个股资金流历史全量(东财 stock_individual_fund_flow);失败降级返回 []。

    返回 [{date, main_net, super_net, large_net, medium_net, small_net}…](日期升序)。
    """
    import akshare as ak

    try:
        raw = _ak_call(
            lambda: ak.stock_individual_fund_flow(
                stock=_bare(code), market=_market_of(code)
            )
        )
    except Exception as e:
        logger.warning(
            "资金流抓取失败 %s(stock_individual_fund_flow): %s",
            code,
            str(_ak_error(e, "fetch_moneyflow", code))[:120],
        )
        return []
    if raw is None or raw.empty or "日期" not in raw.columns:
        logger.debug("资金流空数据 %s(stock_individual_fund_flow)", code)
        return []
    out: list[dict] = []
    for _, r in raw.iterrows():
        row: dict = {"date": _date_str(r.get("日期"))}
        for cn_col, field in MONEYFLOW_COLUMNS.items():
            row[field] = _num(r.get(cn_col))
        out.append(row)
    return out


def fetch_lhb(code: str, start_date: str, end_date: str) -> list[dict]:
    """抓取龙虎榜(东财 stock_lhb_detail_em,日期区间全市场后按代码过滤)。

    start_date/end_date 为 YYYYMMDD。返回 [{date, reason, buy_amount, sell_amount,
    net_amount, deal_amount, turnover_rate, summary}…](接口排序:代码升序+日期倒序)。
    summary = 东财「解读」(机构动向简述),无解读回落上榜原因。
    """
    import akshare as ak

    bare = _bare(code)
    try:
        raw = _ak_call(
            lambda: ak.stock_lhb_detail_em(
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        )
    except Exception as e:
        logger.warning(
            "龙虎榜抓取失败 %s(%s~%s): %s",
            code,
            start_date,
            end_date,
            str(_ak_error(e, "fetch_lhb", code))[:120],
        )
        return []
    if raw is None or raw.empty or "代码" not in raw.columns:
        logger.debug("龙虎榜空数据 %s(%s~%s)", code, start_date, end_date)
        return []
    out: list[dict] = []
    for _, r in raw.iterrows():
        if str(r.get("代码", "")).strip() != bare:
            continue
        reason = str(r.get("上榜原因", "")).strip() or "龙虎榜"
        row: dict = {
            "date": _date_str(r.get("上榜日")),
            "reason": reason[:255],
            "summary": (str(r.get("解读", "")).strip() or reason)[:500],
        }
        for cn_col, field in LHB_COLUMNS.items():
            row[field] = _num(r.get(cn_col))
        out.append(row)
    return out


def _fetch_margin_day(api_name: str, code: str, date_str: str) -> list[dict]:
    """单交易日两融抓取(并行 worker);网络失败/空数据/无该股 → []。

    与逐日循环的「单日失败跳过,不拖垮整段」语义一致,供 fetch_margin 并发聚合。
    """
    import akshare as ak

    bare = _bare(code)
    try:
        raw = _ak_call(lambda: getattr(ak, api_name)(date=date_str.replace("-", "")))
    except Exception as e:
        logger.debug(
            "两融 %s %s 抓取失败(%s): %s",
            date_str,
            code,
            api_name,
            str(_ak_error(e, "fetch_margin", code))[:100],
        )
        return []
    if raw is None or raw.empty:
        return []
    market = _market_of(code)
    col_code = "标的证券代码" if market == "sh" else "证券代码"
    col_balance = "融资余额"
    col_short = "融券余额"  # 仅深市存在
    rows: list[dict] = []
    for _, r in raw.iterrows():
        if str(r.get(col_code, "")).strip() != bare:
            continue
        rows.append(
            {
                "date": date_str,
                "margin_balance": _num(r.get(col_balance)),
                "short_balance": _num(r.get(col_short))
                if col_short in raw.columns
                else None,
                "net_buy": None,
            }
        )
    return rows


def fetch_margin(code: str, start_date: str, end_date: str) -> list[dict]:
    """抓取两融明细(SSE/SZSE 逐日,按板块分发,过滤目标代码)。

    start_date/end_date 为 YYYY-MM-DD(闭区间,仅对周一~周五发起请求,非交易日
    接口返回空/异常 → 跳过,不视为失败)。返回 [{date, margin_balance,
    short_balance, net_buy}…]——net_buy = 融资余额环比(相邻交易日差值,元);
    short_balance 仅深市有,沪市为 None。网络失败直接返回 []。

    窗口内交易日经有界线程池(ThreadPoolExecutor, MARGIN_FETCH_WORKERS 并发)
    并行抓取后聚合排序——消除逐日串行首拉 ~210s 阻塞请求线程的 P2-33 债;
    每任务由 _ak_call 25s 硬超时兜底,失败单日跳过语义不变。
    """
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta

    market = _market_of(code)
    api_name = (
        "stock_margin_detail_sse" if market == "sh" else "stock_margin_detail_szse"
    )
    try:
        d0 = datetime.strptime(start_date, "%Y-%m-%d")
        d1 = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return []
    days: list[str] = []
    day = d0
    while day <= d1:
        if day.weekday() < 5:
            days.append(day.date().isoformat())
        day += timedelta(days=1)
    with ThreadPoolExecutor(max_workers=MARGIN_FETCH_WORKERS) as pool:
        per_day = pool.map(lambda ds: _fetch_margin_day(api_name, code, ds), days)
    out: list[dict] = [row for rows in per_day for row in rows]
    # 融资净买入 = 融资余额环比(排序后相邻差值;单日/首日无前值 → None)
    out.sort(key=lambda x: x["date"])
    for i in range(len(out) - 1, 0, -1):
        cur, prev = out[i]["margin_balance"], out[i - 1]["margin_balance"]
        if cur is not None and prev is not None:
            out[i]["net_buy"] = round(cur - prev, 2)
    return out


def fetch_northbound(code: str) -> list[dict] | None:
    """抓取北向历史持股(东财 stock_hsgt_individual_em);网络失败返回 None,空数据返回 []。

    港交所 2024-08 停止披露后该接口仅剩历史序列;若东财已下架(异常/空)同样
    返回 []——None 与 [] 区分由调用方用于「接口失效 → 503」判定:网络级失败
    (None)与数据级为空([])都视为不可用,表留空。
    返回 [{date, hold_shares, hold_ratio}…](日期升序)。
    """
    import akshare as ak

    try:
        raw = _ak_call(lambda: ak.stock_hsgt_individual_em(symbol=_bare(code)))
    except Exception as e:
        logger.warning(
            "北向持股抓取失败 %s(stock_hsgt_individual_em): %s",
            code,
            str(_ak_error(e, "fetch_northbound", code))[:120],
        )
        return None
    if raw is None or raw.empty or "持股日期" not in raw.columns:
        logger.debug("北向持股空数据 %s(stock_hsgt_individual_em)", code)
        return []
    out: list[dict] = []
    for _, r in raw.iterrows():
        row: dict = {"date": _date_str(r.get("持股日期"))}
        for cn_col, field in NORTHBOUND_COLUMNS.items():
            row[field] = _num(r.get(cn_col))
        out.append(row)
    return out
