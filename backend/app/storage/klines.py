"""K 线 SQLite 缓存：批量读 + 并发拉取去重 + 单事务写入（避免写锁竞争）。

（bt-fin 迁移：由原 kcache.py 模块全文迁入；模型/引擎引用 storage.klines_db，
quote 门面 get_kline/get_provider 经 bind_kline_source 依赖注入（未注入时抛错，
禁止反向 import 兜底，P1-53a），交易时段判定 now_cn/in_trading_session 经
lib.session 直连。）
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..lib.codes import bare_code as _bare
from ..lib.codes import in_chunks
from ..lib.codes import normalize_code
from .db import engine as _main_engine
from .klines_db import Kline, klines_session as SessionLocal
from ..lib.session import now_cn

# 数据拉取能力由编排层(core)注入,storage 不反向依赖 core(消除 core↔storage 循环链)。
# 未注入时抛错(P1-53a:禁止惰性反向 import 兜底——那会重建 storage→core 依赖链,
# 且静默掩盖「未 bind 即使用」的装配错误);生产启动由 core.runtime 调用
# bind_kline_source 注入,所有调用方必须在 bind 之后使用。
_kline_fetcher = None  # callable(code, period, days=None, start_date=None, compose_intraday=False) -> DataFrame
_provider_namer = None  # callable() -> str(当前生效源名)


def bind_kline_source(fetch_fn, provider_fn):
    global _kline_fetcher, _provider_namer
    _kline_fetcher = fetch_fn
    _provider_namer = provider_fn


def _resolve_source():
    if _kline_fetcher is None:
        raise RuntimeError(
            "K 线拉取能力未绑定：请先调用 bind_kline_source 注入 fetch_fn"
        )
    return _kline_fetcher


def get_kline(code, period="daily", days=None, start_date=None, compose_intraday=False):
    fetch = _resolve_source()
    return fetch(
        code,
        period,
        days=days,
        start_date=start_date,
        compose_intraday=compose_intraday,
    )


def get_provider():
    # 源名查询固定走绑定门面(core.sources.get_provider 内部动态读 active_key,
    # 源切换自然生效);未注入时抛错——与 get_kline 的 _resolve_source 同契约
    # (P1-53a:禁止惰性反向 import 兜底)。测试须在 setup 里 bind 或 patch 本函数。
    if _provider_namer is None:
        raise RuntimeError(
            "K 线源名查询能力未绑定：请先调用 bind_kline_source 注入 provider_fn"
        )
    return _provider_namer()


logger = logging.getLogger("stockradar.kcache")

_MINUTE_SCALE = {"1": 1, "5": 5, "15": 15, "30": 30, "60": 60}


def _route_meta_db(db: Session | None) -> Session | None:
    """cache_meta 已迁 K 线独立库（storage/klines_db.py，T-103）：业务方（api/signals、
    scanner）经主库会话读写 cache_meta 时，重定向为 kcache 新库会话——返回 None 表示
    "调用方自建 kcache 会话"；避免主库被 _ensure_meta 重建 cache_meta 表。

    测试临时库会话（绑定非主库引擎）不受影响：monkeypatch kcache.SessionLocal 后
    get_bind() 指向测试引擎，原样放行，测试隔离语义不变。
    """
    if db is None:
        return None
    try:
        if db.get_bind() is _main_engine:
            return None
    except Exception:
        pass
    return db


# 各周期缓存新鲜度（数据拉取频率），is_stale 唯一事实来源：
#   分钟线: 非交易时段数据冻结不拉；交易时段最后一条距现在 > 周期分钟×freq_mult 即 stale
#           （1分线2分钟/5分线10分钟/15分线30分钟/30分线60分钟/60分线120分钟）；< min_depth 根视为深历史不足
#   daily : partial 标记 == 最后日期 → stale（盘中未收盘 bar 强制重拉完整）
#           交易时段内滞后 >= session_lag_days 天即刷新；非交易时段 <= off_session_grace_days 天不拉
#           （周五收盘数据周一早盘前不重复拉；超期视为长假后首拉）
#   weekly: 超过 max_lag_days(8) 天才 stale；monthly: 超过 32 天
# 各周期实际拉取路径：
#   sina  : weekly/monthly 不单独拉取 —— SinaProvider.get_kline 直接拉日线后 _resample_period 重采样
#           （W-FRI / ME 聚合），周/月线历史深度 = 日线 datalen 上限（1600 根）
#   akshare: weekly/monthly 单独拉取 —— ak.stock_zh_a_hist(period="week"/"month")（腾讯备用源同理）
#   daily : 三源均原生支持；分钟线 sina 走 scale 接口、akshare 走 stock_zh_a_hist_min_em
# 注意：weekly/monthly 的增量拉取（_fetch_start）按日粒度推进，故新鲜度以"距最后根 bar 的天数"判定
STALENESS = {
    "minute": {"freq_mult": 2.0, "min_depth": 500},
    "daily": {"session_lag_days": 1, "off_session_grace_days": 4},
    "weekly": {"max_lag_days": 8},
    "monthly": {"max_lag_days": 32},
}

# 进程内同 code:period 并发拉取去重（工作台指标/K线/共振同时触发同一股票时只发一次网络请求）
_inflight: dict[
    str, Future
] = {}  # key=f"{code}:{period}"，Future 返回拉取结果 DataFrame
_inflight_lock = threading.Lock()
_INFLIGHT_TIMEOUT = 60.0  # 等待在途任务超时兜底，超时自行拉取


def _run_inflight(key: str, fn):
    """进程内同 key 并发去重：已有在途任务 → 等待其结果；否则注册 daemon 线程异步执行。

    等待超时（60s）或在途任务失败 → 走注册路径自行拉取（新 Future 覆盖旧 key，失败者已清理）。
    """
    while True:
        with _inflight_lock:
            fut = _inflight.get(key)
        if fut is not None:
            try:
                return fut.result(timeout=_INFLIGHT_TIMEOUT)
            except Exception:
                pass  # 超时/对方失败 → 尝试注册自拉
        with _inflight_lock:
            if key in _inflight:
                continue  # 等待期间被其他线程注册 → 重新等待其结果
            fut = Future()
            _inflight[key] = fut
            break

    def _run():
        try:
            fut.set_result(fn())
        except Exception as e:
            fut.set_exception(e)
        finally:
            with _inflight_lock:
                if (
                    _inflight.get(key) is fut
                ):  # 只清理自己的 Future（等待超时者可能已注册新任务）
                    del _inflight[key]

    threading.Thread(target=_run, daemon=True, name=f"kline-fetch:{key}").start()
    try:
        return fut.result(timeout=_INFLIGHT_TIMEOUT)
    except Exception:
        raise


def _cn_now() -> datetime:
    """当前时间（北京时间 naive，与 K 线日期字符串同口径）。

    委托 session.now_cn（ZoneInfo Asia/Shanghai），与 session.py 时区口径统一；
    原固定 UTC+8 手写偏移与 ZoneInfo 等价，收敛为一处实现（session 无本模块依赖，无循环导入）。
    """
    return now_cn().replace(tzinfo=None)


def _cn_day() -> str:
    return _cn_now().date().isoformat()


def _meta_get(db, key: str) -> str | None:
    """读 cache_meta 键值（版本号/partial 标记等）。db 可复用调用方会话；
    绑定主库引擎的会话（api 等外部传入）自动重定向到 kcache 新库会话。"""
    _own = False
    if db is not None and db.get_bind() is _main_engine:
        db = SessionLocal()
        _own = True
    try:
        _ensure_meta(db)
        row = db.execute(
            text("SELECT value FROM cache_meta WHERE key = :key"), {"key": key}
        ).first()
        return str(row[0]) if row is not None else None
    finally:
        if _own:
            db.close()


def _meta_set(db, key: str, value: int | None) -> None:
    _ensure_meta(db)
    if value is None:
        db.execute(text("DELETE FROM cache_meta WHERE key = :key"), {"key": key})
    else:
        db.execute(
            text(
                "INSERT INTO cache_meta(key, value) VALUES (:key, :value) "
                "ON CONFLICT(key) DO UPDATE SET value = :value"
            ),
            {"key": key, "value": int(value)},
        )


def set_meta(key: str, value: int, db: Session | None = None) -> None:
    """持久化全局元数据键值（如 meta:last_scan 最后扫描时刻），upsert 幂等。

    供扫描器等非 kcache 调用方写入 cache_meta。db 为 None 时自建 SessionLocal
    （独立写事务）；调用方已持有会话（如 scan_once 的 db）时传入复用——
    保证测试 patch 的 SessionLocal 生效，避免测试扫描写穿到生产库 cache_meta。
    模式同 _bump_keys/_meta_set：INSERT ... ON CONFLICT DO UPDATE。
    注：cache_meta 已迁 K 线独立库（T-103），绑定主库引擎的会话（scanner 业务会话）
    自动重定向为 kcache 新库会话，主库不再被重建 cache_meta 表。
    """
    _own = False
    if db is not None and _route_meta_db(db) is None:
        db = SessionLocal()
        _own = True
    if db is None:
        db = SessionLocal()
        try:
            _ensure_meta(db)
            db.execute(
                text(
                    "INSERT INTO cache_meta(key, value) VALUES (:k, :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"k": key, "v": int(value)},
            )
            db.commit()
        finally:
            db.close()
    else:
        _ensure_meta(db)
        db.execute(
            text(
                "INSERT INTO cache_meta(key, value) VALUES (:k, :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"k": key, "v": int(value)},
        )
        db.commit()
        if _own:
            db.close()


def _partial_marker(code: str, period: str) -> str:
    return f"partial:{_bare(code)}:{period}"


def _get_partial_date(code: str, period: str, db: Session | None = None) -> str | None:
    """读取 partial 标记（整数 YYYYMMDD → YYYY-MM-DD）；无标记返回 None。

    db 为 None 时自建 SessionLocal（单只路径，独立读事务）；批量路径（fetch_many
    等逐股票判定新鲜度）传入复用会话——否则全市场数千只股票各建一次 SQLite 连接。
    模式同 set_meta 的 db 参数；主库会话自动重定向到新库（cache_meta 已迁独立库）。
    """
    _own = False
    if db is not None and _route_meta_db(db) is None:
        db = SessionLocal()
        _own = True
    if db is None:
        db = SessionLocal()
        _own = True
    try:
        v = _meta_get(db, _partial_marker(code, period))
    finally:
        if _own:
            db.close()
    if v is None:
        return None
    d = str(v)
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return None


def _is_partial_daily(df: pd.DataFrame) -> bool:
    """盘中(UTC+8 15:05 前)写入的当日 bar 视为未收盘部分数据：
    最后一行 date==今天 且 当前时间 < 15:05。weekly/monthly 由日线重采样，不处理。"""
    if df is None or not len(df):
        return False
    last_date = str(df["date"].iloc[-1])
    if last_date != _cn_day():
        return False
    now = _cn_now()
    return (now.hour, now.minute) < (15, 5)


def _to_rows(code: str, period: str, source: str, df: pd.DataFrame) -> list[dict]:
    """df → 入库行。**未来日期标签一律剔除**（防旧版重采样 ME 标签等脏数据再入：
    缓存里出现 date > 上海今日 的行会造成时间轴倒挂/联动判定失效）。"""
    today = _cn_day()
    return [
        {
            "code": _bare(code),
            "period": period,
            "source": source,
            "date": str(r["date"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
            "amount": float(r.get("amount", 0) or 0),
        }
        # 按日期前缀比较：分钟 bar date 为 "2026-08-12 10:30" 形态，
        # 整串比较会大于 "2026-08-12" 被误判为未来而剔除当日分钟 bar。
        for _, r in df.iterrows()
        if str(r["date"])[:10] <= today
    ]


def _ensure_meta(db):
    """cache_meta 建表兜底：正式建表由 K 线独立库承担（klines_db.ensure_schema，
    init_db/migrate 时执行，表结构与 CacheMeta 模型一字不差）；此处 SQL 仅作
    运行时幂等兜底（测试临时库/首次访问路径），结构同模型定义。"""
    db.execute(
        text(
            "CREATE TABLE IF NOT EXISTS cache_meta "
            "(key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
        )
    )
    db.commit()


def _meta_key(code: str | None) -> str:
    return "ver:global" if code is None else f"ver:{_bare(code)}"


def _bump_keys(db, keys: list[str]) -> None:
    _ensure_meta(db)
    db.execute(
        text(
            "INSERT INTO cache_meta(key, value) VALUES (:key, 1) "
            "ON CONFLICT(key) DO UPDATE SET value = cache_meta.value + 1"
        ),
        [{"key": k} for k in keys],
    )


# 版本号进程内短 TTL 缓存（P1-2）：indicators/risk 每请求都调 get_version 拼缓存 key，
# 原实现每请求一次 SQLite 会话；5s 短缓存消除该开销，bump_version 主动失效保准确。
_ver_lock = threading.Lock()
_ver_cache: dict[str, tuple[float, int]] = {}
_VERSION_TTL = 5.0


def get_version(code: str | None = None) -> int:
    """读取数据版本（默认 0）；code=None 读取全局版本。

    进程内 5s 短 TTL 缓存；bump_version 写入后主动失效缓存（保证 bump 后立即读到新值）。
    """
    key = _meta_key(code)
    with _ver_lock:
        hit = _ver_cache.get(key)
        if hit is not None and time.time() - hit[0] < _VERSION_TTL:
            return hit[1]
    db = SessionLocal()
    try:
        _ensure_meta(db)
        row = db.execute(
            text("SELECT value FROM cache_meta WHERE key = :key"),
            {"key": key},
        ).first()
        val = int(row[0]) if row is not None else 0
    finally:
        db.close()
    with _ver_lock:
        _ver_cache[key] = (time.time(), val)
    return val


def bump_version(code: str | None = None, period: str | None = None) -> int:
    """数据版本 +1 并持久化；code=None 时 bump 全局版本（period 预留）。"""
    keys = ["ver:global"] if code is None else [_meta_key(code), "ver:global"]
    db = SessionLocal()
    try:
        _bump_keys(db, keys)
        db.commit()
    finally:
        db.close()
    with _ver_lock:  # 刚写入即失效缓存，避免 5s 窗口内读到旧版本
        for k in keys:
            _ver_cache.pop(k, None)
    return get_version(code)


def upsert_many(code_periods: list[tuple[str, str, pd.DataFrame]], source: str) -> int:
    """批量 upsert（单事务，分块执行避免 SQLite 变量数超限），避免写锁竞争。返回写入行数。"""
    rows: list[dict] = []
    for code, period, df in code_periods:
        if df is None or not len(df):
            continue
        rows.extend(_to_rows(code, period, source, df))
    if not rows:
        return 0
    db = SessionLocal()
    CHUNK = 400  # 400 行 × 10 列 = 4000 变量，远低于 SQLite 变量上限
    try:
        # 建表只执行一次：批量路径后的 _bump_keys/partial 标记均依赖 cache_meta 存在。
        # 放在事务开头，避免 _meta_set 每 code 一次 CREATE TABLE IF NOT EXISTS + commit
        # 把批量写入打碎成 N+1 个小事务。
        _ensure_meta(db)
        # 周/月周期：仅删除与新 df 覆盖周期重叠的旧行（防重采样标签演进残留双 bar、
        # 时间轴重复）。删除下界 = 新 df 最小日期所在周期起点：weekly→所在自然周周一
        # （W-FRI 组起点，组内无周六/日交易日，周一 cutoff 恰好覆盖整组）、monthly→
        # 当月 1 日；下界之后的旧行均落在新 df 覆盖周期内，会被新聚合 bar 替换。
        # 不能整表删——增量拉取（_fetch_start 返回缓存最后一根次日）只带回 1~2 根新
        # 周期 bar，全删会把更早历史周期 bar 一并截断（P0-21）。
        for code, period, df in code_periods:
            if period in ("weekly", "monthly") and df is not None and len(df):
                dmin = pd.Timestamp(str(df["date"].min()))
                cut = (
                    dmin - pd.Timedelta(days=dmin.weekday())
                    if period == "weekly"
                    else dmin.replace(day=1)
                )
                db.execute(
                    text(
                        "DELETE FROM klines WHERE code = :c AND period = :p "
                        "AND date >= :cut"
                    ),
                    {"c": _bare(code), "p": period, "cut": cut.strftime("%Y-%m-%d")},
                )
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i : i + CHUNK]
            stmt = sqlite_insert(Kline).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "period", "date"],
                set_={
                    "source": stmt.excluded.source,
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                },
            )
            db.execute(stmt)
        db.commit()
        codes = list(
            dict.fromkeys(
                _bare(c) for c, _, df in code_periods if df is not None and len(df)
            )
        )
        if codes:
            _bump_keys(db, [f"ver:{c}" for c in codes] + ["ver:global"])
        # 盘中 partial 日线标记：未收盘当日 bar 记录 partial:{code}:daily=日期，
        # 供 _fetch_start/is_stale 下次增量重拉该日完整 bar；完整数据写入则清除标记。
        # 直接内联 _meta_set 的 SQL（不再逐 code 调用其内部的 _ensure_meta + commit），
        # 全部标记统一在一次事务内提交——与批量 K 线写入保持同一事务边界。
        for code, period, df in code_periods:
            if df is None or not len(df) or period != "daily":
                continue
            key = _partial_marker(code, period)
            if _is_partial_daily(df):
                db.execute(
                    text(
                        "INSERT INTO cache_meta(key, value) VALUES (:key, :value) "
                        "ON CONFLICT(key) DO UPDATE SET value = :value"
                    ),
                    {"key": key, "value": int(_cn_day().replace("-", ""))},
                )
            else:
                db.execute(
                    text("DELETE FROM cache_meta WHERE key = :key"), {"key": key}
                )
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# 分批物化上限：load_cached 每批同时持有的查询结果行数（只 code）。一次全量 IN 查询
# （2000 只 × 上千行 → 数百万行同时驻留）瞬时峰值可达 1-2GB；
# 分批查询 + 逐批转 DataFrame 后释放行，避免全量物化。
_LOAD_BATCH = 300

# Core 列投影用 Table：select 列而非 ORM 实体 → 结果集直接是 Row（无 Kline 对象构造）
_KLINE_TABLE = Kline.__table__


def load_cached(codes: list[str], period: str) -> dict[str, pd.DataFrame]:
    """批量读取缓存，返回 code → DataFrame（含 pct_change）。

    分批物化：把一次全量 IN 查询拆成每批 ≤_LOAD_BATCH 只 code 的 IN 查询，
    SQLAlchemy Core 列投影直出结果集（跳过 ORM 对象构造，避免 ~320 万 Kline
    对象物化 + identity map 驻留），逐批转 DataFrame 后释放行再合并。
    返回结构与旧实现完全一致（key=normalize_code，date 升序，含 pct_change 列）；
    顺序与 codes 列表无关（调用方自行排序）。
    """
    if not codes:
        return {}
    # 去重：结果以 code 为 key，重复输入无意义且跨批重复查询会重复物化
    bare = list(dict.fromkeys(_bare(c) for c in codes))
    db = SessionLocal()
    try:
        result: dict[str, pd.DataFrame] = {}
        for i in range(0, len(bare), _LOAD_BATCH):
            chunk = bare[i : i + _LOAD_BATCH]
            rows = db.execute(
                select(
                    _KLINE_TABLE.c.code,
                    _KLINE_TABLE.c.date,
                    _KLINE_TABLE.c.open,
                    _KLINE_TABLE.c.high,
                    _KLINE_TABLE.c.low,
                    _KLINE_TABLE.c.close,
                    _KLINE_TABLE.c.volume,
                    _KLINE_TABLE.c.amount,
                )
                .where(
                    _KLINE_TABLE.c.code.in_(chunk),
                    _KLINE_TABLE.c.period == period,
                )
                .order_by(_KLINE_TABLE.c.date.asc())
            ).all()
            by_code: dict[str, list[tuple]] = {}
            for r in rows:
                by_code.setdefault(normalize_code(r[0]), []).append(
                    (r[1], r[2], r[3], r[4], r[5], r[6], r[7])
                )
            del rows
            for code, rs in by_code.items():
                df = pd.DataFrame(
                    rs,
                    columns=[
                        "date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "amount",
                    ],
                )
                if (
                    code in result
                ):  # codes 含重复 code 时跨批合并（正常调用每 code 只在一批）
                    df = (
                        pd.concat([result[code], df], ignore_index=True)
                        .sort_values("date")
                        .reset_index(drop=True)
                    )
                result[code] = df
    finally:
        db.close()
    for df in result.values():
        df["pct_change"] = df["close"].pct_change().fillna(0) * 100
    return result


def is_stale(
    df: pd.DataFrame,
    period: str,
    code: str | None = None,
    db: Session | None = None,
    daily_last_date: str | None = None,
    allow_after_close_refresh: bool = True,
) -> bool:
    """缓存新鲜度（交易时段感知，非交易时段避免重复拉取），规则统一读 STALENESS 表：

    - daily（STALENESS["daily"]）：
      * partial 标记 == 最后日期 → stale（盘中未收盘 bar 需重拉完整数据，收盘后首轮扫描修复）
      * 交易时段内：滞后 >= session_lag_days(1) 天 → stale（盘中滞后即刷新）
      * 非交易时段（晚/周末）：最后日期距今 <= off_session_grace_days(4) 天 → 不 stale
        （周五收盘数据周一早盘前不重复拉）；超过才 stale（长假后首次拉取）
      * allow_after_close_refresh=True（单只响应式路径 cached_kline）：工作日收盘后
        （上海 >=15:05）最后 bar 仍停在更早交易日 → stale，收盘后首访即可拉到当日真实
        bar（否则今日蜡烛要等次日盘中才出现）；批量扫描（fetch_many 传 False）保持
        grace 语义，收盘后不触发全市场重拉（限流/请求量保护）。
    - weekly/monthly：sina 周/月=日线重采样，感知盘中日K新 bar：
      * daily_last_date 非 None（批量路径预载 map 传入）→ 日线最后日期 > 本周期最后 bar → stale
        （日线有新 bar 尚未聚合进周/月）
      * daily_last_date 为 None（单只路径）→ 内部读该 code daily 缓存最后日期（单只 load_cached 量小）；
        daily 缓存空/读失败 → 兜底超过 STALENESS[period]["max_lag_days"]（8/32 天）才 stale
    - 分钟线（STALENESS["minute"]）：< min_depth(500) 根 stale；非交易时段（收盘后分钟数据冻结）
      不 stale；交易时段最后一条距现在超过 周期分钟×freq_mult(2.0) 即 stale。
    """
    # cache_meta 已迁 K 线独立库（T-103）：绑定主库引擎的会话（scanner 批量路径
    # 传入业务会话）重定向为 kcache 新库会话，避免主库被重建 cache_meta（partial
    # 标记读取走 _get_partial_date → _meta_get）。测试临时库会话不受影响。
    _own = False
    if db is not None and _route_meta_db(db) is None:
        db = SessionLocal()
        _own = True
    try:
        return _is_stale_impl(
            df, period, code, db, daily_last_date, allow_after_close_refresh
        )
    finally:
        if _own:
            db.close()


def _is_stale_impl(
    df: pd.DataFrame,
    period: str,
    code: str | None,
    db: Session | None,
    daily_last_date: str | None,
    allow_after_close_refresh: bool,
) -> bool:
    from ..lib.session import in_trading_session

    if df is None or not len(df):
        return True
    if period in ("daily", "weekly", "monthly"):
        try:
            last_dt = pd.Timestamp(df["date"].iloc[-1])
            if period == "daily":
                last_day = last_dt.date()
                today = pd.Timestamp(_cn_day()).date()
                days_since = (today - last_day).days
                # 未来标签守卫：数据源/旧缓存异常返回未来日期 → 视为脏缓存强制重拉
                if days_since < 0:
                    return True
                # partial 标记 == 最后日期 → 盘中写入的未收盘 bar，需重拉完整
                if code:
                    pdate = _get_partial_date(code, period, db)
                    if pdate and pdate == str(last_day):
                        return True
                if days_since >= STALENESS["daily"]["session_lag_days"]:
                    if in_trading_session():
                        return True
                    # 非交易时段：工作日收盘后（上海 >=15:05）最后 bar 仍停在更早交易日 → stale，
                    # 收盘后首访即可拉到当日真实 bar（否则要等次日盘中，今日蜡烛缺失一天）。
                    # 仅单只响应式路径开启（allow_after_close_refresh）；批量扫描保持 grace。
                    # 周末/节假日不触发（数据冻结，仍按 off_session_grace_days 宽限）。
                    # 函数内延迟 import（同 in_trading_session）：保证测试 monkeypatch 的
                    # session.now_cn 绑定生效（顶部 from-import 别名在模块加载时已绑定旧引用）。
                    if allow_after_close_refresh:
                        from ..lib.session import now_cn

                        n = now_cn()
                        if (
                            n.weekday() < 5
                            and (n.hour, n.minute) >= (15, 5)
                            and str(last_day) < str(n.date())
                        ):
                            return True
                    return days_since > STALENESS["daily"]["off_session_grace_days"]
                return False
            # weekly/monthly：联动日线缓存最后日期（sina 重采样，盘中日K新 bar → 周/月未聚合 → stale）
            last_bar = str(df["date"].iloc[-1])
            # 未来标签守卫：旧版重采样曾把未完成周期写成未来日期（如月末 ME 标签 8-31），
            # 联动比较 daily_last < last_bar 会被误判"已聚合"而永不刷新 → 未来日期一律视为脏缓存。
            if last_bar > _cn_day():
                return True
            if daily_last_date is not None:
                return daily_last_date > last_bar
            if code:
                try:
                    ddf = load_cached([code], "daily").get(normalize_code(code))
                    if ddf is not None and len(ddf):
                        return str(ddf["date"].iloc[-1]) > last_bar
                except Exception:
                    pass  # daily 缓存读失败 → 兜底 max_lag_days
            limit = STALENESS.get(period, STALENESS["weekly"])["max_lag_days"]
            days_since = (pd.Timestamp(_cn_day()) - last_dt).days
            return days_since > limit
        except Exception:
            return False
    # 分钟线：非交易时段分钟数据冻结（收盘后/周末不重复拉）。先判交易时段——
    # 深度不足检查若放前面，非交易时段（如长假）缓存 < min_depth 根会被误判 stale
    # 而触发重拉，与"数据冻结不拉"语义矛盾；交易时段内深度不足才需要回填。
    from ..lib.session import in_trading_session

    if not in_trading_session():
        return False
    if len(df) < STALENESS["minute"]["min_depth"]:
        return True
    try:
        scale = _MINUTE_SCALE.get(period, 60)
        last_ts = pd.Timestamp(df["date"].iloc[-1])
        age_min = (_cn_now() - last_ts).total_seconds() / 60
        return age_min > scale * STALENESS["minute"]["freq_mult"]
    except Exception:
        return False


def _fetch_start(
    df_old: pd.DataFrame | None,
    period: str,
    fallback_start: str | None = None,
    code: str | None = None,
    db: Session | None = None,
) -> tuple[str | None, int | None]:
    """拉取起点：
    - 已有缓存且深度足够 → 增量（缓存最后一根次日；盘中 partial 标记 == 最后日期时从该日重拉完整 bar）；
    - 已有缓存但深度不足 fallback_start → 从 fallback_start 全量补齐（深历史回填）；
    - 无缓存且给了 fallback_start → 从该日期全量拉；
    - 否则全量 days。返回 (start_date, days)。

    db 复用会话（同 is_stale），避免逐股票新建连接；worker 线程内不传（自建）。
    """
    if df_old is not None and len(df_old) and period in ("daily", "weekly", "monthly"):
        if fallback_start and str(df_old["date"].iloc[0]) > fallback_start:
            return fallback_start, None  # 覆盖深度不足 → 回填
        last_date = str(df_old["date"].iloc[-1])
        # 盘中 partial 标记：缓存最后一行是未收盘的当日 bar → 从该日重拉完整 bar
        if period == "daily" and code:
            pdate = _get_partial_date(code, period, db)
            if pdate and pdate == last_date:
                return last_date, None
        nxt = pd.Timestamp(last_date) + pd.Timedelta(days=1)
        return nxt.strftime("%Y-%m-%d"), None
    if fallback_start:
        return fallback_start, None
    days = 800 if period in ("daily", "weekly", "monthly") else 400
    return None, days


def cached_kline(
    code: str,
    period: str = "daily",
    max_rows: int = 400,
    refresh_if_stale: bool = True,
    start_date: str | None = None,
) -> pd.DataFrame:
    """单只读取：缓存未过期 → 返回；过期/缺失 → 增量拉取并写入（同 code:period 并发去重）；拉取失败回退旧缓存。

    start_date: 无缓存时的全量拉取起始日期（预热深历史用，如 5 年前）。
    """
    code = normalize_code(code)  # load_cached 键为 normalize_code 口径，裸码先归一化
    cached = load_cached([code], period)
    df_old = cached.get(code)
    depth_ok = (
        df_old is not None
        and len(df_old)
        and not (start_date and str(df_old["date"].iloc[0]) > start_date)
    )  # 深度不足 → 视为需刷新
    if df_old is not None and not is_stale(df_old, period, code) and depth_ok:
        return df_old.tail(max_rows).reset_index(drop=True)
    if not refresh_if_stale:
        return (
            df_old.tail(max_rows).reset_index(drop=True)
            if df_old is not None
            else pd.DataFrame()
        )
    try:
        fetch_from, days = _fetch_start(df_old, period, start_date, code)
        # 单只路径：compose_intraday=True（唯一传 True 处）——sina 日K盘中无当日 bar，
        # 由 quote.get_kline 内部用实时快照合成当日 bar；period 不限 daily（sina 周/月
        # 重采样内部会透传该开关，周/月盘中也能感知新 bar）。
        fetched = _run_inflight(
            f"{code}:{period}",
            lambda: get_kline(
                code,
                period,
                days=days,
                start_date=fetch_from,
                compose_intraday=True,
            ),
        )
        if fetched is not None and len(fetched):
            upsert_many([(code, period, fetched)], get_provider().name)
            if df_old is not None and len(df_old):
                # fetched 在前：盘中 partial 增量（start_date=今日）拉回的当日 bar 与
                # 缓存末行同 date，drop_duplicates 保留先出现者 → 新 bar 覆盖旧 partial
                # bar（原地更新当日 bar，P2-9）；历史行无冲突，顺序不影响结果。
                merged = (
                    pd.concat([fetched, df_old])
                    .drop_duplicates(subset=["date"])
                    .sort_values("date")
                    .reset_index(drop=True)
                )
                merged["pct_change"] = merged["close"].pct_change().fillna(0) * 100
                return merged.tail(max_rows).reset_index(drop=True)
            return fetched.tail(max_rows).reset_index(drop=True)
    except Exception as e:
        logger.warning("拉取K线失败 %s/%s: %s", code, period, str(e)[:100])
    # 刷新失败：回退旧缓存，避免下次重复拉取相同旧数据
    if df_old is not None and len(df_old):
        return df_old.tail(max_rows).reset_index(drop=True)
    return pd.DataFrame()


def fetch_many(
    codes: list[str],
    period: str = "daily",
    workers: int = 12,
    progress_cb=None,
    start_date: str | None = None,
) -> dict[str, pd.DataFrame]:
    """批量拉取：缓存一次读取 → 并发拉取缺失（增量，不写库）→ 单事务批量写入。

    返回 code → DataFrame。扫描器使用，避免 SQLite 写锁竞争。
    start_date: 无缓存股票的全量拉取起始日期（预热深历史用）。
    周/月周期：staleness 联动日线缓存最后日期（轻量 SQL 一次预载 map，见 missing 推导式）。
    """
    result = load_cached(codes, period)
    # 新鲜度判定复用单会话：missing 推导式对每只有缓存的股票调用 is_stale →
    # 内部读 partial 标记，若每次新建 SessionLocal，全市场数千只股票 = 数千次 SQLite 连接。
    # 拉取路径（_fetch_one → _fetch_start）在 worker 线程内自建会话（Session 非线程安全）。
    db = SessionLocal()
    try:
        # 周/月K staleness 联动日线缓存最后日期：对全部 codes 一次批量查询 daily
        # 最后日期 map（bare code → YYYY-MM-DD）。必须用轻量 SQL——load_cached 全量
        # 加载数千只×上千行 = 内存灾难；分块 IN 规避 SQLite 变量上限。复用本会话。
        daily_last: dict[str, str] = {}
        if period in ("weekly", "monthly") and codes:
            for chunk in in_chunks(list(dict.fromkeys(_bare(c) for c in codes))):
                ph = ", ".join(f":c{i}" for i in range(len(chunk)))
                rows = db.execute(
                    text(
                        "SELECT code, MAX(date) AS last_date FROM klines "
                        f"WHERE period = 'daily' AND code IN ({ph}) GROUP BY code"
                    ),
                    {f"c{i}": c for i, c in enumerate(chunk)},
                ).all()
                daily_last.update({str(r[0]): str(r[1]) for r in rows})
        missing = [
            c
            for c in codes
            if c not in result
            # 批量路径：收盘后不触发全市场重拉（allow_after_close_refresh=False，grace 语义），
            # 避免收盘后 5000 只全拉冲击 sina 限流；单只响应式路径(cached_kline)才有收盘后刷新。
            or is_stale(
                result[c],
                period,
                c,
                db,
                daily_last.get(_bare(c)),
                allow_after_close_refresh=False,
            )
            or (
                start_date
                and c in result
                and len(result[c])
                and str(result[c]["date"].iloc[0]) > start_date
            )
        ]  # 深度不足 → 回填
    finally:
        db.close()
    if missing:
        fetched: dict[str, pd.DataFrame] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_one, c, period, result.get(c), start_date): c
                for c in missing
            }
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    df = fut.result()
                    if df is not None and len(df):
                        fetched[code] = df
                except Exception as e:
                    logger.debug("K线获取失败 %s: %s", code, str(e)[:80])
                done += 1
                if progress_cb and done % 100 == 0:
                    progress_cb(done, len(missing))
        if fetched:
            try:
                upsert_many(
                    [(c, period, df) for c, df in fetched.items()], get_provider().name
                )
            except Exception as e:
                logger.warning("批量缓存写入失败: %s", str(e)[:100])
            for c, df in fetched.items():
                old = result.get(c)
                if old is not None and len(old):
                    merged = (
                        pd.concat([old, df])
                        .drop_duplicates(subset=["date"])
                        .sort_values("date")
                        .reset_index(drop=True)
                    )
                    merged["pct_change"] = merged["close"].pct_change().fillna(0) * 100
                    result[c] = merged
                else:
                    result[c] = df
    return {c: df for c, df in result.items() if len(df) >= 40}


def _is_sina_active() -> bool:
    """当前生效数据源是否为 sina（盘中 partial 跳过仅对 sina 生效）。

    sina 日线接口不支持服务端 start_date（全量下载 1600 根再本地切片）且盘中
    （<15:05）不返回当日 bar → 盘中 partial 重拉注定拉空；akshare/tencent 支持
    start_date 过滤、盘中可正常返回当日 bar，不得跳过。与 upsert_many 写库时
    取源的 get_provider().name 同口径。
    """
    return get_provider().name == "sina"


def _fetch_one(
    code: str,
    period: str,
    df_old: pd.DataFrame | None = None,
    fallback_start: str | None = None,
) -> pd.DataFrame | None:
    """单只拉取（增量优先，进程内同 code:period 并发去重）；失败静默重试 2 次，仍失败返回 None。"""
    fetch_from, days = _fetch_start(df_old, period, fallback_start, code)
    # P2-9 批量路径修正（T-73）：盘中 partial 命中时跳过重拉，沿用缓存旧值。
    # 背景：partial 标记 == 缓存最后日期 → _fetch_start 返回 start_date=最后日期的
    # 「重拉完整 bar」请求。单只路径（cached_kline）靠 compose_intraday=True 在
    # provider 层短路为只拉当日 1 分钟线聚合（P2-9）；批量路径不传 compose_intraday
    # （全市场扫描不合成，避免数千只逐一打实时快照）——sina 日线接口不支持服务端
    # start_date、盘中又无当日 bar，每次轮询都全量下载 1600 根日线再本地切片成空。
    # 命中条件收窄为：sina 源 + daily + 重拉最后 bar（days 未指定且 fetch_from ==
    # 缓存最后日期）+ 盘中 partial（_is_partial_daily，与 upsert/is_stale 同口径
    # 15:05 分界）→ 本轮直接返回缓存旧值；当日 bar 实时性由扫描器 spot 快照合成
    # （scanner._synthesize_daily_bar，内存态）承接，降频由 5min 扫描轮次天然承担；
    # 收盘后（≥15:05）判定不成立 → 正常重拉真实 bar 覆盖 partial。
    if (
        period == "daily"
        and days is None
        and fetch_from
        and df_old is not None
        and len(df_old)
        and fetch_from == str(df_old["date"].iloc[-1])
        and _is_partial_daily(df_old)
        and _is_sina_active()
    ):
        logger.debug("批量路径盘中 partial 命中 %s: 跳过日线重拉，沿用缓存", code)
        return df_old
    for attempt in range(3):
        try:
            # 批量路径不传 compose_intraday（默认 False）：全市场扫描不合成盘中 bar，
            # 避免数千只股票逐一打实时快照；仅单只 cached_kline 路径传 True。
            df = _run_inflight(
                f"{code}:{period}",
                lambda: get_kline(code, period, days=days, start_date=fetch_from),
            )
        except Exception:
            df = None
        if df is not None and len(df):
            return df
        if attempt < 2:
            time.sleep(1)
    return None
