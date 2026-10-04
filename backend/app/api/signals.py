"""/api/v1/signals/* : 信号事件、目录、扫描状态与时间线。"""

from __future__ import annotations

import collections
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..storage.db import get_db
from ..config import settings
from ..lib.timex import MARKET_SPANS, PERIODS, market_time_range
from ..lib.codes import sector_of as _sector_of
from ..lib.signals.engine import SIGNAL_TYPES
from ..lib.signals.catalog import (
    SIGNAL_CATALOG as _SIGNAL_CATALOG,
    SIGNAL_RANK as _SIGNAL_RANK,
)
from ..storage.repos import signals as _sig_repo
from ..storage.repos import stocks as _stock_repo

router = APIRouter(prefix="/signals", tags=["signals"])

logger = logging.getLogger("stockradar.signals")


class CatalogItem(BaseModel):
    """信号目录条目（GET /signals/catalog 数组元素，顺序 = SIGNAL_TYPES 注册顺序）。"""

    code: str
    name: str
    category: str
    desc: str
    params: dict[str, Any]
    rank: int


class CatalogResponse(BaseModel):
    data: list[CatalogItem]


class SignalEventItem(BaseModel):
    """信号事件条目（/events 与 /events/page 共用，见 _event_row；与前端 SignalEvent 对齐）。

    可空字段（旧行 NULL）以 Optional 兜底——response_model 严格过滤响应，
    缺字段=丢字段，字段过窄遇到 NULL 会 500，均不得发生。
    """

    id: int
    stock_code: str
    stock_name: str
    signals: Optional[list[str]] = None
    status: str
    evidence: Optional[dict[str, Any]] = None
    is_watchlist: bool
    sector: str
    triggered_at: Optional[str] = None
    as_of: Optional[str] = None
    scan_discovered_at: Optional[str] = None
    period: str


class SignalEventsResponse(BaseModel):
    """GET /signals/events：{data: SignalEventItem[]}。"""

    data: list[SignalEventItem]


class PageStats(BaseModel):
    """分页筛选口径聚合统计（stats{today_new, stock_count}）：全量聚合，不受分页截断。"""

    today_new: int
    stock_count: int


class SignalEventsPageResponse(BaseModel):
    """GET /signals/events/page：{items/total/limit/offset/has_more/period/stats}。"""

    items: list[SignalEventItem]
    total: int
    limit: int
    offset: int
    has_more: bool
    period: str
    stats: PageStats


class TimelineEventItem(BaseModel):
    """单股事件时间线条目（GET /events/{stock_code}/timeline；SignalEventItem 子集，无代码/名称）。"""

    id: int
    signals: Optional[list[str]] = None
    status: str
    evidence: Optional[dict[str, Any]] = None
    triggered_at: Optional[str] = None
    as_of: Optional[str] = None
    scan_discovered_at: Optional[str] = None


class TimelineResponse(BaseModel):
    data: list[TimelineEventItem]


class ScanStatusResponse(BaseModel):
    """GET /signals/scan/status：最后扫描时刻 / 交易时段 / 逐周期最近扫描 / 调度表（T-54）。"""

    last_scan_at: Optional[str] = None
    in_trading_session: bool
    periods: dict[str, Optional[str]]
    schedule: dict[str, int]


class ScanTriggerResponse(BaseModel):
    """POST /signals/scan：{job_id, status, label}（异步任务已受理）。"""

    job_id: str
    status: str
    label: str


@router.get("/catalog", response_model=CatalogResponse)
def signal_catalog():
    """全部信号目录：中文名、分类、说明、默认参数（顺序 = SIGNAL_TYPES 注册顺序）。"""
    return {
        "data": [
            {
                "code": code,
                "name": _SIGNAL_CATALOG.get(code, (code, "其他", "", {}))[0],
                "category": _SIGNAL_CATALOG.get(code, (code, "其他", "", {}))[1],
                "desc": _SIGNAL_CATALOG.get(code, (code, "其他", "", {}))[2],
                "params": _SIGNAL_CATALOG.get(code, (code, "其他", "", {}))[3],
                "rank": _SIGNAL_RANK.get(code, 999),
            }
            for code in SIGNAL_TYPES
        ]
    }


_PERIODS = PERIODS
# 板块→事件内股票代码集合（板块由代码前缀推导、静态不变，60s 缓存即可）
_sector_codes_cache: "collections.OrderedDict[str, tuple[float, set[str]]]" = (
    collections.OrderedDict()
)
_SECTOR_CODES_TTL = 60.0
_SECTOR_CODES_MAX = 128
# 私有 LRU（OrderedDict）无内置锁：多请求线程并发 del/move_to_end 会 KeyError 500，
# 用一把进程内互斥锁保护全部 get/set（条目少、操作微秒级，锁开销可忽略）。
_cache_lock = threading.Lock()


def _cache_get(cache: "collections.OrderedDict", key: str, ttl: float, maxsize: int):
    """有界 TTL 读：命中且未过期 → 刷新 recency 并返回值；过期则淘汰并返回 None。"""
    with _cache_lock:
        import time as _t

        now = _t.time()
        hit = cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            cache.move_to_end(
                key
            )  # 最近访问 → 保持 LRU 语义（超限时淘汰的是最久未用者）
            return hit[1]
        if hit is not None:
            del cache[key]  # TTL 过期 → 惰性淘汰
        return None


def _cache_set(cache: "collections.OrderedDict", key: str, value, maxsize: int) -> None:
    """写入并限制条目数：超限逐最旧（LRU 中最旧者）。"""
    with _cache_lock:
        import time as _t

        cache[key] = (_t.time(), value)
        cache.move_to_end(key)
        while len(cache) > maxsize:
            cache.popitem(last=False)  # 淘汰最久未用条目


def _sector_stock_codes(db: Session, want: set[str]) -> set[str]:
    """事件表内按板块（代码前缀推导）筛出的股票代码集合（含 60s 有界缓存）。"""
    key = ",".join(sorted(want))
    cached = _cache_get(_sector_codes_cache, key, _SECTOR_CODES_TTL, _SECTOR_CODES_MAX)
    if cached is not None:
        return cached
    codes = _sig_repo.sector_stock_codes(db, want)
    _cache_set(_sector_codes_cache, key, codes, _SECTOR_CODES_MAX)
    return codes


def _split_csv(v: str | None) -> list[str] | None:
    """逗号分隔参数 → 去空白列表；空输入返回 None（不过滤）。"""
    if not v:
        return None
    parts = [x.strip() for x in v.split(",") if x.strip()]
    return parts or None


def _event_row(e, name_map: dict, wl_codes: set) -> dict:
    """信号事件序列化（/events 与 /events/page 共用，输出结构一致）。"""
    return {
        "id": e.id,
        "stock_code": e.stock_code,
        "stock_name": name_map.get(e.stock_code, ""),
        "signals": e.signals,
        "status": e.status,
        "evidence": e.evidence,
        "is_watchlist": e.stock_code in wl_codes,
        "sector": _sector_of(e.stock_code),
        "triggered_at": e.triggered_at.isoformat() if e.triggered_at else None,
        "as_of": e.as_of.isoformat() if e.as_of else None,
        "scan_discovered_at": (
            e.scan_discovered_at.isoformat() if e.scan_discovered_at else None
        ),
        # 信号周期：与 /market/watchlist/notifications 同源输出（P2-67 契约对齐，旧行迁移默认 'daily'）
        "period": e.period,
    }


@router.get("/events", response_model=SignalEventsResponse)
def list_events(
    limit: int = 200,
    status: str | None = None,
    code: str | None = None,
    sector: str | None = None,
    watchlist_only: bool = False,
    period: str = "daily",
    time_range: str = "3d",
    exclude_today: bool = False,
    db: Session = Depends(get_db),
):
    """信号事件数组端点（triggered_at 降序，最多 limit 条）。

    sector 在 SQL limit 前过滤（板块 → 事件内代码集合，见 _sector_stock_codes），
    不做 limit 后内存筛，保证结果不被截断。
    time_range 为 Asia/Shanghai 自然日口径（Nd = 含今天在内最近 N 个自然日），
    与 scanner.calendar_window_start 的市场日历口径（跳过周末按工作日回溯）
    不同：周末查询时本端点把非交易日也算进窗口（窗口偏宽、不丢事件）。
    """
    if period not in _PERIODS:
        raise HTTPException(400, f"不支持周期 {period}，可选: {_PERIODS}")
    if time_range not in MARKET_SPANS:
        raise HTTPException(400, f"不支持时间范围 {time_range}，可选: {MARKET_SPANS}")
    limit = max(1, min(10000, limit))  # clamp [1, 10000]（支持无限滚动加载）
    start, end = market_time_range(
        time_range, exclude_today
    )  # 时间范围在分页/排序前过滤
    # sector 前置过滤：板块 → 事件内代码集合（_sector_stock_codes 已含缓存），
    # 空集直接返回空数据（与 /events/page 处理一致），不做 limit 后内存筛
    sector_codes: set[str] | None = None
    if sector:
        want = {s.strip() for s in sector.split(",") if s.strip()}
        sector_codes = _sector_stock_codes(db, want)
        if not sector_codes:
            return {"data": []}
    if watchlist_only:
        wl = _stock_repo.watchlist_codes(db)
        if not wl:
            return {"data": []}
        # sector 与自选股先交集（语义 = 同时满足两个 in_ 条件），再走统一查询
        codes = wl & sector_codes if sector_codes is not None else wl
        if not codes:
            return {"data": []}
    elif sector_codes is not None:
        codes = sector_codes
    else:
        codes = None
    events = _sig_repo.recent_events(
        db,
        period=period,
        status=status,
        code=code,
        codes=codes,
        start=start,
        end=end,
        limit=limit,
    )
    codes_out = {e.stock_code for e in events}
    name_map = _stock_repo.stock_names(db, codes_out) if codes_out else {}
    wl_codes = _stock_repo.watchlist_codes(db) if codes_out else set()
    return {"data": [_event_row(e, name_map, wl_codes) for e in events]}


@router.get("/events/page", response_model=SignalEventsPageResponse)
def list_events_page(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    code: str | None = None,
    sector: str | None = None,
    watchlist_only: bool = False,
    period: str = "daily",
    signal_types: str | None = None,
    time_range: str = "3d",
    exclude_today: bool = False,
    signal_match: str = "any",
    db: Session = Depends(get_db),
):
    """分页信号事件（向后兼容新增，旧 /events 数组端点保留）。

    全部周期（1/5/15/30/60/daily/weekly/monthly）统一按事件表 period 列过滤，
    筛选/排序语义一致（triggered_at 降序），total 为该筛选条件下的完整计数：
    - signal_types: 逗号分隔信号类型；signal_match=any（默认）命中任一即保留，
      在 SQLite 上对 signals JSON 用转义后的文本条件精确匹配；
      signal_match=all 时按股票级语义：同一股票事件信号并集必须满足
      全部选中信号才保留，且只返回该股票与所选信号相关的事件
    - time_range/exclude_today: Asia/Shanghai 自然日口径时间范围（end 开区间）。
      注意与 scanner.calendar_window_start 的市场日历口径（跳过周末按工作日回溯）
      不同：周末查询时本端点把非交易日也算进窗口（窗口偏宽、不丢事件），
      /events 与 /events/page 同用本口径，保持一致。
    返回 items/total/limit/offset/has_more/period/stats；
    stats{today_new, stock_count} 为筛选口径全量聚合（不受分页截断）：
    today_new=triggered_at 落今日（Asia/Shanghai 零点后）的事件数，
    stock_count=筛选事件集中的去重股票数。
    """
    if period not in _PERIODS:
        raise HTTPException(400, f"不支持周期 {period}，可选: {_PERIODS}")
    if time_range not in MARKET_SPANS:
        raise HTTPException(400, f"不支持时间范围 {time_range}，可选: {MARKET_SPANS}")
    if signal_match not in ("any", "all"):
        raise HTTPException(400, f"不支持 signal_match {signal_match}，可选: any|all")
    limit = max(1, min(1000, limit))
    offset = max(0, offset)

    sig_types = _split_csv(signal_types)

    def _empty() -> dict:
        return {
            "items": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "has_more": False,
            "period": period,
            "stats": {"today_new": 0, "stock_count": 0},
        }

    # 大 IN 集合（>IN_BATCH 只，SQLite 变量上限）从基础条件拆出，走分批内存路径
    big_sets: list[set[str]] = []
    small: set[str] | None = None
    if watchlist_only:
        wl = _stock_repo.watchlist_codes(db)
        if not wl:
            return _empty()
        if len(wl) > _sig_repo.IN_BATCH:
            big_sets.append(wl)
        else:
            small = wl
    start, end = market_time_range(
        time_range, exclude_today
    )  # 时间范围在 count/分页前过滤
    if sector:
        want = {s.strip() for s in sector.split(",") if s.strip()}
        sector_codes = _sector_stock_codes(db, want)
        if not sector_codes:
            return _empty()
        if len(sector_codes) > _sig_repo.IN_BATCH:
            big_sets.append(sector_codes)
        else:
            # 与 watchlist 小集合取交集（语义 = 同时满足两个 in_ 条件）
            small = sector_codes if small is None else small & sector_codes

    events, total, stats = _sig_repo.page_events(
        db,
        period=period,
        status=status,
        code=code,
        in_codes=small,
        big_sets=big_sets or None,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
        sig_types=sig_types,
        signal_match=signal_match,
        today_start=market_time_range("today")[0],
    )
    codes = {e.stock_code for e in events}
    name_map: dict[str, str] = {}
    if codes:
        # 名称回填分批 IN：分页上限 1000，代码集合可能超 SQLite 变量上限
        # （repos.stock_names 内部按 400/批处理）
        name_map = _stock_repo.stock_names(db, codes)
    wl_codes = _stock_repo.watchlist_codes(db) if codes else set()
    items = [_event_row(e, name_map, wl_codes) for e in events]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
        "period": period,
        "stats": stats,
    }


@router.get(
    "/events/{stock_code}/timeline", response_model=TimelineResponse
)
def stock_timeline(stock_code: str, days: int = 30, db: Session = Depends(get_db)):
    """单股事件时间线：按 triggered_at 降序返回最近至多 days 条事件。

    days 语义为「最大返回事件条数」（行数语义），**非自然日天数窗口**——前端
    getStockTimeline 不传 days（默认 30）并依赖该契约，K 线面板按事件覆盖 K 线
    范围取数，故不得改为时间窗口语义。0/负值/超上限防御性 clamp 到 1..2000。
    """
    # days 负值/0 无意义：clamp 到 1..2000（0 视为默认 30，防御性处理）
    days = max(1, min(2000, days)) if days > 0 else 30
    events = _sig_repo.event_timeline(db, stock_code, days)
    return {
        "data": [
            {
                "id": e.id,
                "signals": e.signals,
                "status": e.status,
                "evidence": e.evidence,
                "triggered_at": e.triggered_at.isoformat() if e.triggered_at else None,
                "as_of": e.as_of.isoformat() if e.as_of else None,
                "scan_discovered_at": (
                    e.scan_discovered_at.isoformat() if e.scan_discovered_at else None
                ),
            }
            for e in events
        ]
    }


@router.get("/scan/status", response_model=ScanStatusResponse)
def scan_status(db: Session = Depends(get_db)):
    """最后扫描时刻（epoch 秒 → 上海 naive ISO）与交易时段状态，供前端新鲜度条。

    last_scan_at 为 None 表示从未扫描过；in_trading_session 为当前是否交易时段。
    periods: 各周期（1/5/15/30/60/daily/weekly/monthly）最近扫描时刻
    （cache_meta meta:last_scan:{period}；daily 兼容旧键 meta:last_scan，
    即 daily 扫描沿用无后缀键写入，此处回退读取）。
    """
    from ..lib.session import in_trading_session
    from ..core.scanning import _SCHEDULE

    def _fmt(raw: str | None) -> str | None:
        if not raw:
            return None
        return (
            datetime.fromtimestamp(int(raw), tz=ZoneInfo("Asia/Shanghai"))
            .replace(tzinfo=None, microsecond=0)
            .isoformat()
        )

    # 一次批量读全部 9 键（meta_get_many 单查询替代逐键 meta_get，避免每请求
    # 多次 ensure_schema/会话重定向开销）
    keys = ["meta:last_scan"] + [f"meta:last_scan:{p}" for p in PERIODS]
    values = _sig_repo.meta_get_many(db, keys)
    raw = values.get("meta:last_scan")
    periods: dict[str, str | None] = {}
    for p in PERIODS:
        val = values.get(f"meta:last_scan:{p}")
        if val is None and p == "daily":
            val = raw  # daily 扫描写旧键 meta:last_scan（兼容旧）
        periods[p] = _fmt(val)
    return {
        "last_scan_at": _fmt(raw),
        "in_trading_session": in_trading_session(),
        "periods": periods,
        # T-54：扫描调度表（周期 → 间隔秒，单一事实源 Settings.scan_schedule /
        # scanning._SCHEDULE），前端新鲜度阈值按当前周期取对应间隔（缺失回退 5min）
        "schedule": dict(_SCHEDULE),
    }


@router.post("/scan", response_model=ScanTriggerResponse)
def trigger_scan(payload: dict | None = None):
    """手动触发一轮市场扫描（后台任务，返回 job_id，进度在任务管理器可见）。

    payload.period：指定扫描周期（1/5/15/30/60/daily/weekly/monthly），
    缺省不传（沿用默认 daily）；非法周期 400。
    payload.universe/top_n/codes（T-54，分钟周期生效）：股票池范围——
    watchlist 自选（缺省）/ top_n 成交额前 N（top_n 缺省=settings.market_scan_limit，
    即 500）/ codes 显式代码集；full_universe 仅影响 daily 分支（分钟缺省
    universe=watchlist，保持旧行为）。top_n ≤ settings.scan_top_n_max、codes
    长度 ≤ settings.scan_codes_max，超限 400（P2-45，防单任务 params 数 MB 落库）。
    """
    from ..core.scanning import SCAN_UNIVERSES
    from ..core.tasks.errors import QueueFullError
    from ..core.tasks.runner import submit

    payload = payload or {}
    period = payload.get("period")
    if period is not None and period not in PERIODS:
        raise HTTPException(400, f"不支持周期 {period}，可选: {PERIODS}")
    universe = payload.get("universe", "watchlist")
    if universe not in SCAN_UNIVERSES:
        raise HTTPException(
            400, f"不支持扫描范围 {universe}，可选: {', '.join(SCAN_UNIVERSES)}"
        )
    top_n = payload.get("top_n")
    if top_n is not None:
        if isinstance(top_n, bool) or not isinstance(top_n, (int, float)) or top_n <= 0:
            raise HTTPException(400, "top_n 必须为正整数")
        top_n = int(top_n)
        if top_n > settings.scan_top_n_max:
            raise HTTPException(
                400, f"top_n 超过上限 {settings.scan_top_n_max}（P2-45）"
            )
    codes = payload.get("codes")
    if codes is not None:
        if not isinstance(codes, list) or not all(
            isinstance(c, str) and c.strip() for c in codes
        ):
            raise HTTPException(400, "codes 必须为非空字符串数组")
        codes = [c.strip() for c in codes if c.strip()]
        if len(codes) > settings.scan_codes_max:
            raise HTTPException(
                400, f"codes 数量超过上限 {settings.scan_codes_max}（P2-45）"
            )
    if universe == "codes" and not codes:
        raise HTTPException(400, "扫描范围 codes 需要提供股票代码列表（codes）")
    params: dict = {"full_universe": bool(payload.get("full_universe", True))}
    if period is not None:
        params["period"] = period
    params["universe"] = universe
    if top_n is not None:
        params["top_n"] = top_n
    if codes is not None:
        params["codes"] = codes
    # 任务描述 label：分钟周期按 universe 区分（daily/周/月恒全市场语义）
    minute = period in ("1", "5", "15", "30", "60") if period else False
    label = {
        "watchlist": "自选股扫描" if minute else "全市场扫描",
        "top_n": "成交额 Top N 扫描",
        "codes": "指定代码扫描",
    }[universe]
    try:
        job_id = submit("market_scan", params)
    except QueueFullError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job_id, "status": "pending", "label": label}