"""信号事件仓储查询（api/signals、api/market 收敛入口）。

承接 api/signals.py 与 api/market.py 的 SignalEvent 查询，以及
kcache._meta_get 的公开等价（cache_meta 读，私有符号收敛）。
全部函数显式接收 db（api 由 Depends(get_db) 注入）。
"""

from __future__ import annotations

import json as _json
from datetime import datetime

from sqlalchemy import cast, func, or_
from sqlalchemy.types import String as _StrType

from ...lib.codes import in_chunks
from ...lib.codes import sector_of as _sector_of
from ..models import SignalEvent

# SQLite 变量上限 999：IN 子句按 400/批（与 scanner._load_existing_events、
# kcache.load_cached 同款留余量），自选股 >400 只时分批查询内存合并
IN_BATCH = 400

_EPOCH = datetime(1970, 1, 1)


def meta_get(db, key: str) -> str | None:
    """读 cache_meta 键值（meta:last_scan 等），等价 kcache._meta_get。

    绑定主库引擎的会话（api 经 get_db 注入）自动重定向到 K 线独立库会话；
    其他会话（测试临时库）原样复用。读不到返回 None。
    """
    from ...storage.db import engine as _main_engine
    from ...storage.klines_db import CacheMeta, ensure_schema, klines_session

    _own = False
    if db is not None and db.get_bind() is _main_engine:
        db = klines_session()
        _own = True
    try:
        ensure_schema(db.get_bind())
        row = db.query(CacheMeta).filter(CacheMeta.key == key).first()
        return str(row.value) if row is not None else None
    finally:
        if _own:
            db.close()


def meta_get_many(db, keys: list[str]) -> dict[str, str | None]:
    """批量读 cache_meta 多个键值（meta:last_scan 等），等价多次 meta_get。

    一次查询替代逐键往返（scan_status 每请求 9 键 → 1 次）；读不到返回 None。
    会话重定向语义与 meta_get 一致：绑定主库引擎的会话自动切 K 线独立库会话。
    """
    from ...storage.db import engine as _main_engine
    from ...storage.klines_db import CacheMeta, ensure_schema, klines_session

    _own = False
    if db is not None and db.get_bind() is _main_engine:
        db = klines_session()
        _own = True
    try:
        ensure_schema(db.get_bind())
        if not keys:
            return {}
        rows = db.query(CacheMeta).filter(CacheMeta.key.in_(keys)).all()
        vals = {r.key: str(r.value) for r in rows}
        return {k: vals.get(k) for k in keys}
    finally:
        if _own:
            db.close()


def sector_stock_codes(db, want: set[str]) -> set[str]:
    """事件表内按板块（代码前缀推导）筛出的股票代码集合。"""
    return {
        c
        for (c,) in db.query(SignalEvent.stock_code).distinct().all()
        if _sector_of(c) in want
    }


def _signals_json_cond(signal_type: str):
    """signals JSON 数组（SQLite 存为 JSON 文本）中精确匹配一个元素的文本条件。

    用 json.dumps 生成带引号片段（如 "volume_surge"），避免子串误匹配
    （volume_surge 不会命中 volume_surge2）；LIKE 通配符（%/_）已转义。
    """
    quoted = _json.dumps(signal_type)
    escaped = quoted.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return cast(SignalEvent.signals, _StrType).like(f"%{escaped}%", escape="\\")


def _sort_events(rows: list) -> None:
    """事件行按 triggered_at 降序、id 降序次级键（None 恒排最后）。

    T-135:同日同刻多事件(交易时段高频触发)仅按时刻排序无稳定序,offset 分页
    跨批会重复/跳过;id 为单调唯一次级键,与 SQL order_by 双键语义一致。
    """
    rows.sort(
        key=lambda e: (e.triggered_at is None, e.triggered_at or _EPOCH, e.id),
        reverse=True,
    )


def recent_events(
    db,
    *,
    period: str,
    status: str | None = None,
    code: str | None = None,
    codes: set[str] | None = None,
    start=None,
    end=None,
    limit: int = 200,
) -> list:
    """信号事件数组端点核心查询：triggered_at 降序取 limit 条。

    codes 为 None 不过滤；大集合（>IN_BATCH 只）分批 IN 查询 + 内存合并
    排序截断，语义与单次 in_ + order_by + limit 完全一致。
    """
    q = db.query(SignalEvent).filter(SignalEvent.period == period)
    if status:
        q = q.filter(SignalEvent.status == status)
    if code:
        q = q.filter(SignalEvent.stock_code == code)
    if codes is not None and len(codes) > IN_BATCH:
        # 大集合（自选股/板块代码超 SQLite 变量上限）：分批 IN + 内存合并
        q = q.filter(SignalEvent.triggered_at >= start) if start is not None else q
        q = q.filter(SignalEvent.triggered_at < end)
        rows: list = []
        for chunk in in_chunks(sorted(codes)):
            rows.extend(q.filter(SignalEvent.stock_code.in_(chunk)).all())
        _sort_events(rows)
        return rows[:limit]
    if codes is not None:
        q = q.filter(SignalEvent.stock_code.in_(codes))
    if start is not None:
        q = q.filter(SignalEvent.triggered_at >= start)
    q = q.filter(SignalEvent.triggered_at < end)
    return q.order_by(SignalEvent.triggered_at.desc(), SignalEvent.id.desc()).limit(limit).all()


def _empty_stats() -> dict:
    """空事件集的聚合统计（无事件：今日新增与股票数均为 0）。"""
    return {"today_new": 0, "stock_count": 0}


def _stats_from_rows(rows: list, today_start) -> dict:
    """由内存事件行（筛选后全量，未分页截断）聚合 stats{today_new, stock_count}。

    today_start 为 Asia/Shanghai 今日零点（naive）：triggered_at ≥ 该边界的
    事件计为「今日新增」；为 None 时 today_new 恒 0（不启用今日口径）。
    """
    stock_count = len({r.stock_code for r in rows})
    if today_start is None:
        today_new = 0
    else:
        today_new = sum(
            1
            for r in rows
            if r.triggered_at is not None and r.triggered_at >= today_start
        )
    return {"today_new": today_new, "stock_count": stock_count}


def _stats_from_query(q, today_start) -> dict:
    """由筛选后查询（SQL 路径，count/分页前）聚合 stats{today_new, stock_count}。

    复用同一个筛选查询做去重股票数计数与今日边界过滤，统计口径与 total 完全一致。
    """
    stock_count = (
        q.with_entities(func.count(func.distinct(SignalEvent.stock_code))).scalar() or 0
    )
    if today_start is None:
        today_new = 0
    else:
        today_new = q.filter(SignalEvent.triggered_at >= today_start).count()
    return {"today_new": today_new, "stock_count": stock_count}


def page_events(
    db,
    *,
    period: str,
    status: str | None = None,
    code: str | None = None,
    in_codes: set[str] | None = None,
    big_sets: list[set[str]] | None = None,
    start=None,
    end=None,
    limit: int = 50,
    offset: int = 0,
    sig_types: list[str] | None = None,
    signal_match: str = "any",
    today_start=None,
) -> tuple[list, int, dict]:
    """分页信号事件核心查询，返回 (events 行, total, stats)。

    语义与 api 现实现（base 单次 in_ + count + offset/limit）完全一致：
    - in_codes：小集合（≤IN_BATCH）直接拼入 base 的 IN 条件；
    - big_sets：大集合逐个分批 IN 后内存合并（多个集合取交集），
      sig_types 过滤在内存按与 SQL 路径一致语义复刻；
    - signal_match=all：股票级信号并集满足全部选中信号才保留该股票事件；
    - period/status/code/start/end 全部在 count/分页前过滤；
    - stats{today_new, stock_count}：按同一筛选口径对全量事件集聚合
      （不受 limit/offset 分页截断），SQL 与大集合内存两条路径各自聚合。
    """
    base: list = []
    base.append(SignalEvent.period == period)
    if status:
        base.append(SignalEvent.status == status)
    if code:
        base.append(SignalEvent.stock_code == code)
    if in_codes is not None:
        base.append(SignalEvent.stock_code.in_(in_codes))
    if start is not None:
        base.append(SignalEvent.triggered_at >= start)
    base.append(SignalEvent.triggered_at < end)

    if big_sets:
        # 多个大集合取交集（语义 = 同时满足各 in_ 条件），分批查询内存合并
        combined = big_sets[0]
        for s in big_sets[1:]:
            combined = combined & s
        if not combined:
            return [], 0, _empty_stats()
        rows: list = []
        for chunk in in_chunks(sorted(combined)):
            rows.extend(
                db.query(SignalEvent)
                .filter(*base, SignalEvent.stock_code.in_(chunk))
                .all()
            )
        if sig_types:
            want = set(sig_types)
            if signal_match == "all":
                stock_signals: dict[str, set[str]] = {}
                for r in rows:
                    stock_signals.setdefault(r.stock_code, set()).update(
                        r.signals or []
                    )
                keep = {c for c, s in stock_signals.items() if want.issubset(s)}
                rows = [
                    r
                    for r in rows
                    if r.stock_code in keep and want.intersection(r.signals or [])
                ]
            else:
                rows = [r for r in rows if want.intersection(r.signals or [])]
        total = len(rows)
        stats = _stats_from_rows(rows, today_start)
        _sort_events(rows)
        return rows[offset : offset + limit], total, stats

    if sig_types and signal_match == "all":
        # 股票级 all：先按股票聚合其事件信号并集，满足全部选中信号才保留该股票，
        # 随后只保留该股票与所选信号相关的事件；全部在 total/分页前完成
        agg = db.query(SignalEvent.stock_code, SignalEvent.signals).filter(*base).all()
        stock_signals = {}
        for c, sigs in agg:
            stock_signals.setdefault(c, set()).update(sigs or [])
        want = set(sig_types)
        keep = {c for c, s in stock_signals.items() if want.issubset(s)}
        if not keep:
            return [], 0, _empty_stats()
        if len(keep) > IN_BATCH:
            # keep 超 SQLite 变量上限（IN_BATCH=400 留余量）：分批 IN + 内存合并
            # 排序截断，语义与单次 in_ + order_by + offset/limit 完全一致
            rows: list = []
            for chunk in in_chunks(sorted(keep)):
                rows.extend(
                    db.query(SignalEvent)
                    .filter(
                        *base,
                        SignalEvent.stock_code.in_(chunk),
                        or_(*[_signals_json_cond(t) for t in sig_types]),
                    )
                    .all()
                )
            total = len(rows)
            stats = _stats_from_rows(rows, today_start)
            _sort_events(rows)
            return rows[offset : offset + limit], total, stats
        q = db.query(SignalEvent).filter(
            *base,
            SignalEvent.stock_code.in_(keep),
            or_(*[_signals_json_cond(t) for t in sig_types]),
        )
    else:
        q = db.query(SignalEvent).filter(*base)
        if sig_types:  # any 语义（默认）
            q = q.filter(or_(*[_signals_json_cond(t) for t in sig_types]))

    total = q.count()
    stats = _stats_from_query(q, today_start)
    events = (
        q.order_by(SignalEvent.triggered_at.desc(), SignalEvent.id.desc()).offset(offset).limit(limit).all()
    )
    return events, total, stats


def event_timeline(db, stock_code: str, limit: int) -> list:
    """单股事件时间线：triggered_at 降序取最近至多 limit 条。"""
    return (
        db.query(SignalEvent)
        .filter(SignalEvent.stock_code == stock_code)
        .order_by(SignalEvent.triggered_at.desc(), SignalEvent.id.desc())
        .limit(limit)
        .all()
    )


def events_after_id(db, codes: set[str], after_id: int) -> list:
    """自选股事件增量：codes 内 id > after_id 的事件，按 id 升序（分批 IN）。"""
    rows: list = []
    for chunk in in_chunks(sorted(codes)):
        rows.extend(
            db.query(SignalEvent)
            .filter(SignalEvent.stock_code.in_(chunk), SignalEvent.id > after_id)
            .all()
        )
    rows.sort(key=lambda e: e.id)
    return rows


def latest_event_id(db, codes: set[str]) -> int:
    """自选股事件最大 id（逐批取 max 后整体 max）；无事件返回 0。"""
    max_id = 0
    for chunk in in_chunks(sorted(codes)):
        row = (
            db.query(func.max(SignalEvent.id))
            .filter(SignalEvent.stock_code.in_(chunk))
            .first()
        )
        if row and row[0] is not None:
            max_id = max(max_id, int(row[0]))
    return max_id
