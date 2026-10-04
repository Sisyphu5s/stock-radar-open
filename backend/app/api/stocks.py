"""/api/v1/stocks/* : 个股工作台数据（多周期K线 / 基本面 / 新闻 / 财务历史）。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..lib.timex import PERIODS
from ..lib.codes import normalize_code as _norm
from ..storage.klines import cached_kline
from ..core.sources import get_news, get_provider, get_spot
from ..lib.session import now_cn

router = APIRouter(prefix="/stocks", tags=["stocks"])

VALID_PERIODS = PERIODS


# ---------------------------------------------------------------------------
# T-112 契约硬化: 响应模型（字段与各端点 return dict 逐一核对，缺字段=响应丢字段）
# ---------------------------------------------------------------------------


class SearchItem(BaseModel):
    """GET /search 单行：code（如 600519.SH）+ name。"""

    code: str
    name: str


class SearchResponse(BaseModel):
    data: list[SearchItem]


class KlineResponse(BaseModel):
    """GET /{code}/kline：多列等长列表，date 为 str（YYYY-MM-DD 或 YYYY-MM-DD HH:MM）。"""

    code: str
    period: str
    source: str
    dates: list[str]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[float]
    amount: list[float]
    pct_change: list[float]


class QuoteResponse(BaseModel):
    """GET /{code}/quote：单行快照 + 服务器上海时刻。"""

    code: str
    name: str
    price: float
    pct_change: float
    volume: float
    amount: float
    turnover_rate: float
    volume_ratio: float
    industry: str
    source: str
    timestamp: str


class FundamentalResponse(BaseModel):
    """GET /{code}/fundamental：PE / PB / 市值 / 换手 / 涨跌幅。"""

    code: str
    name: str
    price: float
    pe: float
    pb: float
    market_cap: float
    float_cap: float
    turnover_rate: float
    pct_change: float
    source: str


class NewsItem(BaseModel):
    title: str
    url: str
    time: str
    source: str


class NewsResponse(BaseModel):
    code: str
    data: list[NewsItem]
    cached: bool


class EarningsResponse(BaseModel):
    """GET /{code}/earnings：东财业绩报表最新一期，数值字段缺省为 null。"""

    code: str
    period: str
    eps: Optional[float] = None
    revenue_yoy: Optional[float] = None
    net_profit_yoy: Optional[float] = None
    roe: Optional[float] = None
    gross_margin: Optional[float] = None


class FinancialsResponse(BaseModel):
    """GET /{code}/financials：财务摘要（扁平化）。

    period/prev_period 为固定键；指标键为 akshare 数据驱动（wanted 集合，
    core/sources.get_financials 单一事实源），extra="allow" 透传全部动态键，
    防止 response_model 过滤掉未声明字段造成前端数据缺失。
    """

    model_config = ConfigDict(extra="allow")

    code: str
    period: Optional[str] = None
    prev_period: Optional[str] = None


class FinancialsHistoryResponse(BaseModel):
    """GET /{code}/financials/history：data 为库行序列化 dict（字段随表列，恒含 report_date/date）。"""

    code: str
    type: str
    data: list[dict[str, Any]]


class FinancialsLatestResponse(BaseModel):
    """GET /{code}/financials/latest：三表最新一期合并摘要，各表缺省为 null。"""

    code: str
    period: Optional[str] = None
    balance: Optional[dict[str, Any]] = None
    income: Optional[dict[str, Any]] = None
    cash: Optional[dict[str, Any]] = None


class CapitalHistoryResponse(BaseModel):
    """GET /{code}/capital/history：data 为资金行序列化 dict（字段随类型，恒含 date）。"""

    code: str
    type: str
    data: list[dict[str, Any]]


class CapitalLatestResponse(BaseModel):
    """GET /{code}/capital/latest：各类型最新一行合并摘要，各类型缺省为 null。"""

    code: str
    moneyflow: Optional[dict[str, Any]] = None
    lhb: Optional[dict[str, Any]] = None
    margin: Optional[dict[str, Any]] = None
    northbound: Optional[dict[str, Any]] = None

# 财务三表类型白名单（与 storage/repos/financials.REPORT_MODELS 同源）
FINANCIAL_TYPES = ("balance", "income", "cash")
# 资金类类型白名单（与 storage/repos/capital.CAPITAL_MODELS 同源）
CAPITAL_TYPES = ("moneyflow", "lhb", "margin", "northbound")
# 新鲜度策略常量（刷新阈值/增量窗口）单一事实源在 core/fundamentals.py，
# api 层不再持有副本


def _shanghai_now_iso() -> str:
    """服务器当前上海时刻（naive ISO，秒级）：now_cn 带 tz，去时区与微秒后输出。"""
    return now_cn().replace(microsecond=0, tzinfo=None).isoformat()


@router.get("/search", response_model=SearchResponse)
def search(q: str = "", limit: int = 20):
    limit = max(1, min(100, limit))  # clamp [1, 100]
    try:
        spot = get_spot()
    except Exception as e:
        raise HTTPException(503, f"行情源暂不可用: {str(e)[:120]}")
    if spot is None or spot.empty:
        return {"data": []}
    df = spot
    if q:
        bare = q.strip().split(".")[0]
        df = df[
            df["code"].astype(str).str.contains(bare, na=False)
            | df["name"].astype(str).str.contains(q.strip(), na=False)
        ]
    return {"data": df.head(limit)[["code", "name"]].to_dict(orient="records")}


@router.get("/{code}/kline", response_model=KlineResponse)
def kline(code: str, period: str = "daily", days: int = 250):
    """多周期 K 线（前复权）：1/5/15/30/60 分钟 + daily/weekly/monthly。"""
    days = max(1, min(2000, days))  # clamp [1, 2000]
    if period not in VALID_PERIODS:
        raise HTTPException(400, f"不支持周期 {period}，可选: {VALID_PERIODS}")
    # normalize：load_cached 返回 dict 的 key 为带后缀格式（600519.SH），裸代码
    # （600519）直接当 key 查缓存恒 miss → 每次都触发网络重拉（P2-8）
    code = _norm(code)
    k = cached_kline(code, period, max_rows=days)
    if k is None or k.empty:
        raise HTTPException(404, f"{code} {period} K 线不可用")
    k = k.reset_index(drop=True)
    return {
        "code": code,
        "period": period,
        "source": get_provider().name,
        "dates": k["date"].tolist(),
        "open": k["open"].tolist(),
        "high": k["high"].tolist(),
        "low": k["low"].tolist(),
        "close": k["close"].tolist(),
        "volume": k["volume"].tolist(),
        "amount": k["amount"].tolist(),
        "pct_change": k["pct_change"].tolist(),
    }


@router.get("/{code}/quote", response_model=QuoteResponse)
def quote(code: str):
    try:
        spot = get_spot()
    except Exception as e:
        raise HTTPException(503, f"行情源暂不可用: {str(e)[:120]}")
    if spot is None or spot.empty:
        raise HTTPException(404, "快照不可用")
    code = _norm(code)
    row = spot[spot["code"] == code]
    if row.empty:
        raise HTTPException(404, f"{code} 不在快照中")
    r = row.iloc[0]
    return {
        "code": code,
        "name": r["name"],
        "price": float(r["price"]),
        "pct_change": float(r["pct_change"]),
        "volume": float(r.get("volume", 0)),
        "amount": float(r.get("amount", 0)),
        "turnover_rate": float(r.get("turnover_rate", 0)),
        "volume_ratio": float(r.get("volume_ratio", 0) or 0),
        "industry": str(r.get("industry", "")),
        "source": get_provider().name,
        # 服务器上海时刻（naive ISO，秒级）：供前端标题栏展示服务器时间，
        # 替代浏览器本地钟（时区/时钟漂移不可信）；session.now_cn 已是 Asia/Shanghai
        "timestamp": _shanghai_now_iso(),
    }


@router.get("/{code}/fundamental", response_model=FundamentalResponse)
def fundamental(code: str):
    """基本面估值：PE / PB / 市值 / 换手 / 涨跌幅。"""
    try:
        spot = get_spot()
    except Exception as e:
        raise HTTPException(503, f"行情源暂不可用: {str(e)[:120]}")
    if spot is None or spot.empty:
        raise HTTPException(404, "快照不可用")
    code = _norm(code)
    row = spot[spot["code"] == code]
    if row.empty:
        raise HTTPException(404, f"{code} 不在快照中")
    r = row.iloc[0]
    return {
        "code": code,
        "name": r["name"],
        "price": float(r["price"]),
        "pe": float(r.get("pe", 0) or 0),
        "pb": float(r.get("pb", 0) or 0),
        "market_cap": float(r.get("market_cap", 0) or 0),  # 亿
        "float_cap": float(r.get("float_cap", 0) or 0),  # 亿
        "turnover_rate": float(r.get("turnover_rate", 0) or 0),
        "pct_change": float(r.get("pct_change", 0) or 0),
        "source": str(r.get("source", "")),
    }


@router.get("/{code}/news", response_model=NewsResponse)
def news(code: str, limit: int = 10):
    """个股新闻（5 分钟缓存，降低源请求频率）。"""
    from ..storage.cache import news_cache

    code = _norm(code)
    limit = max(1, min(limit, 50))
    key = f"{code}:{limit}"
    cached = news_cache.get(key)
    if cached is not None:
        return {"code": code, "data": cached, "cached": True}
    items = get_news(code, limit)
    news_cache.set(key, items)
    return {"code": code, "data": items, "cached": False}


@router.get("/{code}/earnings", response_model=EarningsResponse)
def earnings(code: str):
    """业绩报表最新一期（东财源独立调用，复用 quote 单例避免每次重建丢 spot 缓存）。"""
    from ..core.sources import get_earnings

    code = _norm(code)
    try:
        data = get_earnings(code)
    except Exception as e:
        raise HTTPException(503, f"业绩暂不可用: {str(e)[:100]}")
    if not data:
        raise HTTPException(503, "业绩数据暂不可用（东财源）")
    return {"code": code, **data}


@router.get("/{code}/financials", response_model=FinancialsResponse)
def financials(code: str):
    """财务摘要（新浪域独立调用，复用 quote 单例避免每次重建丢 spot 缓存）：最近两期关键指标。"""
    from ..core.sources import get_financials

    code = _norm(code)
    try:
        data = get_financials(code)
    except Exception as e:
        raise HTTPException(503, f"财务摘要暂不可用: {str(e)[:100]}")
    if not data:
        raise HTTPException(404, "财务摘要数据不可用")
    return {"code": code, **data}


# ---------------------------------------------------------------------------
# T-06 基本面历史库：财务三表历史 / 最近一期摘要 / 每日估值序列
# ---------------------------------------------------------------------------


def _fin_row_to_dict(r) -> dict:
    """ORM 财务/估值行 → dict（仅非 None 业务字段；report_date/date 恒在）。"""
    out = {}
    for c in r.__table__.columns:
        if c.name in ("id", "code", "updated_at"):
            continue
        v = getattr(r, c.name)
        if v is not None:
            out[c.name] = v
    return out


@router.get("/{code}/financials/history", response_model=FinancialsHistoryResponse)
def financials_history(code: str, type: str = "balance", limit: int = 40):
    """财务三表历史（按报告期倒序）或每日估值序列；库缺/过期时从 akshare 拉取落库。

    - type: balance|income|cash（按报告期）/ valuation（每日估值）；
    - limit: 返回条数上限（clamp [1, 200]，valuation 默认 250）。
    数据经 SQLite 缓存（financials_* / valuation_history），网络失败降级返回库内已有数据。
    新鲜度策略 + 拉取编排在 core/fundamentals（ensure_*），api 层仅校验/序列化/HTTP。
    """
    from ..core.fundamentals import ensure_financials_fresh, ensure_valuation_fresh
    from ..storage.repos.financials import list_financials, list_valuation

    code = _norm(code)
    limit = max(1, min(200, limit))
    if type not in FINANCIAL_TYPES + ("valuation",):
        raise HTTPException(
            400, f"不支持类型 {type}，可选: {FINANCIAL_TYPES + ('valuation',)}"
        )
    try:
        if type == "valuation":
            ensure_valuation_fresh(None, code)
            data = [_fin_row_to_dict(r) for r in list_valuation(None, code, limit)]
            return {"code": code, "type": type, "data": data}
        ensure_financials_fresh(None, code, type)
        data = [_fin_row_to_dict(r) for r in list_financials(None, code, type, limit)]
        return {"code": code, "type": type, "data": data}
    except Exception as e:
        raise HTTPException(503, f"财务历史暂不可用: {str(e)[:100]}")


@router.get("/{code}/financials/latest", response_model=FinancialsLatestResponse)
def financials_latest(code: str):
    """三表最近一期合并摘要（report_date 为最新披露期；各表独立缺省返回 null）。"""
    from ..core.fundamentals import ensure_financials_fresh
    from ..storage.repos.financials import latest_financials

    code = _norm(code)
    try:
        out: dict = {
            "code": code,
            "period": None,
            "balance": None,
            "income": None,
            "cash": None,
        }
        for t in FINANCIAL_TYPES:
            ensure_financials_fresh(None, code, t)
            row = latest_financials(None, code, t)
            if row is not None:
                d = _fin_row_to_dict(row)
                out[t] = d
                if out["period"] is None or d["report_date"] > out["period"]:
                    out["period"] = d["report_date"]
        return out
    except Exception as e:
        raise HTTPException(503, f"财务摘要历史暂不可用: {str(e)[:100]}")


# ---------------------------------------------------------------------------
# T-08 资金类历史：个股资金流 / 龙虎榜 / 两融 / 北向历史持股
# ---------------------------------------------------------------------------


@router.get("/{code}/capital/history", response_model=CapitalHistoryResponse)
def capital_history(code: str, type: str = "moneyflow", limit: int = 60):
    """资金类历史（日期倒序）：moneyflow 资金流 / lhb 龙虎榜 / margin 两融 / northbound 北向持股。

    - type: moneyflow|lhb|margin|northbound;limit: 返回条数上限(clamp [1, 200])。
    - 数据经 SQLite 缓存(capital_*),库缺/过期时从 akshare 增量拉取落库;
      网络失败降级返回库内已有数据(北向首次且接口不可用 → 503 说明)。
      新鲜度策略 + 拉取编排在 core/fundamentals（ensure_capital_fresh）。
    """
    from ..core.fundamentals import CapitalDataUnavailable, ensure_capital_fresh
    from ..storage.repos.capital import list_capital

    code = _norm(code)
    limit = max(1, min(200, limit))
    if type not in CAPITAL_TYPES:
        raise HTTPException(400, f"不支持类型 {type}，可选: {CAPITAL_TYPES}")
    try:
        ensure_capital_fresh(None, code, type)
        # 序列化复用 _fin_row_to_dict（逻辑同构：排除 id/code/updated_at，保留 date/reason）
        data = [_fin_row_to_dict(r) for r in list_capital(None, code, type, limit)]
        return {"code": code, "type": type, "data": data}
    except CapitalDataUnavailable as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(503, f"资金类数据暂不可用: {str(e)[:100]}")


@router.get("/{code}/capital/latest", response_model=CapitalLatestResponse)
def capital_latest(code: str):
    """资金类各类型最新一行合并摘要（各类型独立缺省返回 null）。"""
    from ..storage.repos.capital import latest_capital

    code = _norm(code)
    try:
        out: dict = {"code": code}
        for t in CAPITAL_TYPES:
            row = latest_capital(None, code, t)
            out[t] = _fin_row_to_dict(row) if row is not None else None
        return out
    except Exception as e:
        raise HTTPException(503, f"资金类摘要暂不可用: {str(e)[:100]}")
