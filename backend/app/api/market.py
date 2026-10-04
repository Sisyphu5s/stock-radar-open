"""/api/v1/market/* : 市场快照、自选池。"""

from __future__ import annotations

import csv
from io import StringIO
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..storage.db import get_db
from ..lib.codes import normalize_code as _norm_code
from ..lib.timex import to_market_naive
from ..core.sources import get_indices, get_provider, get_spot, reset_provider
from ..storage.repos import signals as _sig_repo
from ..storage.repos import stocks as _stock_repo

router = APIRouter(prefix="/market", tags=["market"])


# ===== 响应模型（T-112 契约硬化：P2-29 阶段二，逐路径契约） =====


class SnapshotResponse(BaseModel):
    """GET /market/snapshot：全市场实时快照。

    data 元素为快照行（列集随数据源/fields 裁剪动态变化，用 dict 承载，契约仅到行级）。
    """

    source: str
    count: int
    total: int
    data: list[dict[str, Any]]


class IndexRow(BaseModel):
    """/market/indices 单只指数（部分市场降级时 price/pct_change/updated_at 为 null）。"""

    code: str
    name: str
    market: str
    price: Optional[float] = None
    pct_change: Optional[float] = None
    currency: str
    updated_at: Optional[str] = None


class IndicesResponse(BaseModel):
    indices: list[IndexRow]


class SourceHealth(BaseModel):
    """/market/sources/* sources[] 单源健康状态。"""

    key: str
    name: str
    ok: bool
    active: bool
    latency_ms: Optional[float] = None
    spot_fails: int
    spot_blocked: bool
    blocked_until: Optional[float] = None
    degraded: bool
    kline_blocked: bool
    last_ok: Optional[str] = None
    last_fail: Optional[str] = None
    last_error: str = ""


class DataSourceStatus(BaseModel):
    """数据源总状态：模式/活跃源/偏好列表/最近切换/各源健康。"""

    mode: str
    override: str
    active: Optional[str] = None
    preference: list[str]
    last_switch_ts: Optional[str] = None
    last_switch_reason: str
    sources: list[SourceHealth]


class SourcesStatusResponse(BaseModel):
    data: DataSourceStatus


class WatchlistNotifyEvent(BaseModel):
    """/market/watchlist/notifications data[] 事件项。"""

    id: int
    stock_code: str
    stock_name: str
    signals: list[str]
    status: str
    triggered_at: Optional[str] = None
    as_of: Optional[str] = None
    scan_discovered_at: Optional[str] = None
    period: Optional[str] = None


class WatchlistNotificationsResponse(BaseModel):
    data: list[WatchlistNotifyEvent]
    latest_id: int
    bootstrap: bool


class WatchlistGroup(BaseModel):
    """/market/watchlist/groups 单分组（含成员 codes）。"""

    id: int
    name: str
    sort_order: int
    count: int
    codes: list[str]


class WatchlistGroupsResponse(BaseModel):
    data: list[WatchlistGroup]


class WatchlistGroupResponse(BaseModel):
    data: WatchlistGroup


class OkResponse(BaseModel):
    ok: bool


class WatchlistStockRow(BaseModel):
    """GET /market/watchlist 与分组明细共用的股票行。"""

    code: str
    name: str
    last_price: float
    pct_change: float
    industry: str
    updated_at: Optional[str] = None


class WatchlistDataResponse(BaseModel):
    data: list[WatchlistStockRow]


class AddGroupItemsResponse(BaseModel):
    ok: bool
    added: int
    codes: list[str]


class ImportResponse(BaseModel):
    ok: bool
    added: int
    total: int
    invalid: int


class WatchlistCodeResponse(BaseModel):
    ok: bool
    code: str


def _snapshot_columns(raw: str | None, available: list[str]) -> list[str]:
    """解析 /market/snapshot 的 fields 参数（P2-37）：逗号分隔列白名单裁剪。

    缺省时回落配置默认集（market_snapshot_default_fields，空 = 全列兼容旧契约）；
    未知列显式 400，避免静默丢字段造成调用方数据缺列难排查。
    """
    raw = raw or settings.market_snapshot_default_fields
    if not raw:
        return available
    want = [c.strip() for c in raw.split(",") if c.strip()]
    if not want:
        return available
    unknown = [c for c in want if c not in available]
    if unknown:
        raise HTTPException(400, f"未知快照字段: {', '.join(unknown)}")
    return want


@router.get("/snapshot", response_model=SnapshotResponse)
def snapshot(
    fields: str | None = None,
    limit: int | None = None,
    offset: int = 0,
):
    """全市场实时快照（缓存）。

    P2-37：字段裁剪 + 分页——fields 逗号分隔列白名单（缺省全列，兼容旧契约），
    limit/offset 分页（缺省全量；limit 钳制 [1, market_snapshot_max_rows]）。
    响应 count=本次行数、total=全量行数（不分页时两者相等，与旧契约一致）。
    """
    try:
        spot = get_spot()
    except Exception as e:
        raise HTTPException(503, f"行情源暂不可用: {str(e)[:120]}")
    columns = _snapshot_columns(fields, [c for c in spot.columns])
    rows = spot[columns]
    total = len(rows)
    if limit is not None:
        limit = max(1, min(limit, settings.market_snapshot_max_rows))
        offset = max(0, offset)
        rows = rows.iloc[offset : offset + limit]
    return {
        "source": get_provider().name,
        "count": len(rows),
        "total": total,
        "data": rows.to_dict(orient="records"),
    }


@router.get("/indices", response_model=IndicesResponse)
def indices():
    """全球核心指数快照（T-73）：A 股 4 / 美股 3 / 港股 1 共 8 只。

    返回 {indices: [{code,name,market,price,pct_change,currency,updated_at}]}。
    行情全部市场无数据 → 503（与 /snapshot 同语义）；部分市场失败保留旧缓存/空行，
    由前端静默降级（缺失指数 price 为 null）。"""
    try:
        df = get_indices()
    except Exception as e:
        raise HTTPException(503, f"指数行情暂不可用: {str(e)[:120]}")
    if df is None or df.empty:
        raise HTTPException(503, "指数行情暂不可用: 无数据")
    rows = df.astype(object).where(pd.notna(df), None).to_dict(orient="records")
    if not any(r.get("price") is not None for r in rows):
        raise HTTPException(503, "指数行情暂不可用: 全部市场无数据")
    return {"indices": rows}


@router.get("/sources/status", response_model=SourcesStatusResponse)
def sources_status():
    """数据源健康状态：模式/活跃源/各源延迟与冷却。"""
    from ..core import sources

    return {"data": sources.status()}


@router.post("/sources/switch", response_model=SourcesStatusResponse)
def sources_switch(payload: dict):
    """手动切换数据源：auto（自动切换）或 akshare/sina/tencent（锁定单源）。"""
    from ..core import sources

    value = str(payload.get("source", "")).strip().lower()
    if value not in ("auto", "akshare", "sina", "tencent"):
        raise HTTPException(400, f"未知数据源: {value}，可选 auto/akshare/sina/tencent")
    try:
        result = sources.set_mode(value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    reset_provider()
    return {"data": result}


@router.post("/sources/probe", response_model=SourcesStatusResponse)
def sources_probe():
    """立即快速探测全部数据源（单请求短超时，并发执行），更新健康表。"""
    from concurrent.futures import ThreadPoolExecutor

    from ..core import sources
    from ..core.sources import probe_source

    # P1-38a：3 源并发探测（串行最坏 3×8s=24s 阻塞请求线程；并发后最坏 ≈ 单源最长
    # 超时 8s）。probe_source 内部自带硬超时（akshare/tencent 8s、sina retries=0），
    # 各源独立超时语义不变；健康表写入保持原顺序与内容，返回结构不动（前端契约不变）。
    with ThreadPoolExecutor(max_workers=len(sources.SOURCES)) as pool:
        futures = {
            pool.submit(probe_source, s["key"], timeout=8): s["key"]
            for s in sources.SOURCES
        }
        for fut, key in futures.items():
            ok = fut.result()
            if ok:
                sources.record_spot_success(key)
            else:
                sources.record_spot_failure(key, "快速探测失败")
    return {"data": sources.status()}


@router.get("/watchlist/notifications", response_model=WatchlistNotificationsResponse)
def watchlist_notifications(
    after_id: int | None = None, limit: int = 50, db: Session = Depends(get_db)
):
    """自选股信号事件增量拉取（站内通知轮询用）。

    - after_id 为空：bootstrap，不返回任何旧消息，仅返回当前自选股事件最大 id 作为游标。
    - 提供 after_id：返回所有自选股 SignalEvent.id > after_id 的事件（按 id 升序），
      以及本次返回事件的最大 id（latest_id）作为下一轮游标；limit 仅做数量钳制，
      被截断的后续事件在下一轮继续拉取，不会丢失。
    """
    limit = max(1, min(200, limit))
    wl = _stock_repo.watchlist_codes(db)

    if after_id is None:
        max_id = _sig_repo.latest_event_id(db, wl)
        return {"data": [], "latest_id": max_id, "bootstrap": True}

    rows = _sig_repo.events_after_id(db, wl, after_id)
    events = rows[:limit]
    codes = {e.stock_code for e in events}
    name_map: dict[str, str] = {}
    if codes:
        name_map = _stock_repo.stock_names(db, codes)
    data = [
        {
            "id": e.id,
            "stock_code": e.stock_code,
            "stock_name": name_map.get(e.stock_code, ""),
            "signals": e.signals,
            "status": e.status,
            "triggered_at": e.triggered_at.isoformat() if e.triggered_at else None,
            "as_of": e.as_of.isoformat() if e.as_of else None,
            # 双时点契约（T-80）：扫描发现时刻 + 事件周期，浮窗走同一状态机渲染
            "scan_discovered_at": (
                e.scan_discovered_at.isoformat() if e.scan_discovered_at else None
            ),
            "period": e.period,
        }
        for e in events
    ]
    latest_id = data[-1]["id"] if data else after_id
    return {"data": data, "latest_id": latest_id, "bootstrap": False}


# ===== 自选股分组 / 批量导入导出（T-12）=====
# 路由注册顺序说明：/watchlist/import、/watchlist/export.csv 为单段路径，
# 必须先于下方 POST/DELETE /watchlist/{code} 注册，否则 "import" 会被路径参数当股票代码捕获。
# 分组路由为多段（/watchlist/groups/{id}），与单段 {code} 天然不冲突。

_A_CODE_SUFFIX = ("SH", "SZ", "BJ")


def _is_valid_stock_code(code: str) -> bool:
    """A 股代码校验：6 位数字 + 可选 .SH/.SZ/.BJ 后缀（含 920 北交所）。"""
    bare, _, suffix = code.partition(".")
    return (
        len(bare) == 6 and bare.isdigit() and (not suffix or suffix in _A_CODE_SUFFIX)
    )


def _group_dict(g, codes: set[str]) -> dict:
    """分组序列化：附带成员 codes（前端分组筛选/移入菜单共享一份数据）。"""
    return {
        "id": g.id,
        "name": g.name,
        "sort_order": g.sort_order,
        "count": len(codes),
        "codes": sorted(codes),
    }


@router.get("/watchlist/groups", response_model=WatchlistGroupsResponse)
def get_watchlist_groups(db: Session = Depends(get_db)):
    """全部分组及成员代码（sort_order 升序）。"""
    return {
        "data": [
            _group_dict(g, _stock_repo.group_item_codes(db, g.id))
            for g in _stock_repo.watchlist_groups(db)
        ]
    }


@router.post("/watchlist/groups", response_model=WatchlistGroupResponse)
def create_watchlist_group(payload: dict, db: Session = Depends(get_db)):
    """新建分组：name 必填且全库唯一（重名 400）。"""
    try:
        g = _stock_repo.create_watchlist_group(db, str(payload.get("name", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": _group_dict(g, set())}


@router.patch("/watchlist/groups/{group_id}", response_model=WatchlistGroupResponse)
def rename_watchlist_group(group_id: int, payload: dict, db: Session = Depends(get_db)):
    """重命名分组（规格「分组管理」的重命名入口）。"""
    try:
        g = _stock_repo.rename_watchlist_group(
            db, group_id, str(payload.get("name", ""))
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"data": _group_dict(g, _stock_repo.group_item_codes(db, g.id))}


@router.delete("/watchlist/groups/{group_id}", response_model=OkResponse)
def delete_watchlist_group(group_id: int, db: Session = Depends(get_db)):
    """删除分组（连带其全部 items）。"""
    try:
        _stock_repo.delete_watchlist_group(db, group_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.get("/watchlist/groups/{group_id}/items", response_model=WatchlistDataResponse)
def get_group_items(group_id: int, db: Session = Depends(get_db)):
    """分组内股票明细（结构同 GET /watchlist，含行情/板块字段）。"""
    if _stock_repo.get_watchlist_group(db, group_id) is None:
        raise HTTPException(404, f"分组不存在: {group_id}")
    stocks = _stock_repo.group_items_stocks(db, group_id)
    return {
        "data": [
            {
                "code": s.code,
                "name": s.name,
                "last_price": s.last_price,
                "pct_change": s.pct_change,
                "industry": s.industry,
                "updated_at": (
                    to_market_naive(s.updated_at).isoformat() if s.updated_at else None
                ),
            }
            for s in stocks
        ]
    }


@router.post("/watchlist/groups/{group_id}/items", response_model=AddGroupItemsResponse)
def add_group_items(group_id: int, payload: dict, db: Session = Depends(get_db)):
    """批量移入分组 {codes}：规范化后幂等加入（同组已有跳过）。"""
    raw = payload.get("codes") or []
    if not isinstance(raw, list):
        raise HTTPException(400, "codes 必须为数组")
    codes = [c for c in (_norm_code(str(x)) for x in raw) if c]
    try:
        added = _stock_repo.add_group_items(db, group_id, codes)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "added": added, "codes": sorted(set(codes))}


@router.delete("/watchlist/groups/{group_id}/items/{code}", response_model=OkResponse)
def remove_group_item(group_id: int, code: str, db: Session = Depends(get_db)):
    """从分组移出单只股票（不在组内幂等返回 ok）。"""
    _stock_repo.remove_group_item(db, group_id, _norm_code(code))
    return {"ok": True}


@router.post("/watchlist/import", response_model=ImportResponse)
def import_watchlist(payload: dict, db: Session = Depends(get_db)):
    """批量关注 {codes}：已关注幂等跳过；非 6 位数字的无效代码忽略并计数。"""
    raw = payload.get("codes") or []
    if not isinstance(raw, list):
        raise HTTPException(400, "codes 必须为数组")
    valid: set[str] = set()
    invalid = 0
    for x in raw:
        c = _norm_code(str(x)) if str(x).strip() else ""
        if c and _is_valid_stock_code(c):
            valid.add(c)
        else:
            invalid += 1
    fresh = valid - _stock_repo.watchlist_codes(db)
    for c in sorted(fresh):
        _stock_repo.add_watchlist(db, c)
    return {"ok": True, "added": len(fresh), "total": len(valid), "invalid": invalid}


@router.get("/watchlist/export.csv")
def export_watchlist_csv(db: Session = Depends(get_db)):
    """CSV 导出（text/csv 下载）：code,name,sector,分组。
    sector 取股票行业（stocks.industry）；分组为所属组名，多组以「 | 」连接；
    UTF-8 BOM 防 Excel 中文乱码。"""
    stocks = _stock_repo.watchlist_stocks(db)
    code_groups: dict[str, list[str]] = {}
    for g in _stock_repo.watchlist_groups(db):
        for c in _stock_repo.group_item_codes(db, g.id):
            code_groups.setdefault(c, []).append(g.name)
    buf = StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["code", "name", "sector", "分组"])
    for s in sorted(stocks, key=lambda x: x.code):
        writer.writerow(
            [s.code, s.name, s.industry, " | ".join(code_groups.get(s.code, []))]
        )
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="watchlist.csv"'},
    )


@router.post("/watchlist/{code}", response_model=WatchlistCodeResponse)
def add_watchlist(code: str, db: Session = Depends(get_db)):
    code = _norm_code(code)
    _stock_repo.add_watchlist(db, code)
    return {"ok": True, "code": code}


@router.delete("/watchlist/{code}", response_model=WatchlistCodeResponse)
def remove_watchlist(code: str, db: Session = Depends(get_db)):
    code = _norm_code(code)
    _stock_repo.remove_watchlist(db, code)
    return {"ok": True, "code": code}


@router.get("/watchlist", response_model=WatchlistDataResponse)
def get_watchlist(db: Session = Depends(get_db)):
    stocks = _stock_repo.watchlist_stocks(db)
    return {
        "data": [
            {
                "code": s.code,
                "name": s.name,
                "last_price": s.last_price,
                "pct_change": s.pct_change,
                "industry": s.industry,
                "updated_at": (
                    to_market_naive(s.updated_at).isoformat() if s.updated_at else None
                ),
            }
            for s in stocks
        ]
    }
