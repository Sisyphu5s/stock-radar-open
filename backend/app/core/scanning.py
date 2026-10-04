"""全市场扫描编排（bt-scan，core 层）：快照全量覆盖，K 线并发拉取 + SQLite
缓存，信号事件入库。

自原 scanner 模块逐字搬移（import 调整），APScheduler 调度器
外壳保留；模块级 _scheduler/_scan_lock 与 start_scanner/stop_scanner/
scan_once 公共 API 签名不变。新增 Scanning 供 bt-runtime 组件注册表编排启动。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from ..config import settings
from ..storage.db import SessionLocal
from ..lib.indicators.compute import volume_ratio
from ..lib.signals.engine import evaluate_all_signals
from ..storage.models import utcnow
from ..lib.timex import bar_market_time
from ..lib.codes import in_chunks, normalize_code
from ..storage.klines import _is_partial_daily, fetch_many, set_meta
from .sources import _resample_period, get_provider, get_spot

logger = logging.getLogger("stockradar.scanner")

_scheduler: BackgroundScheduler | None = None
# 上次实际扫描时间（调度降频用；非交易时段约 29min 一次，见 _OFF_HOUR_MIN_GAP）。
# 模块级定义必须早于 start_scanner，且 scheduled_scan 内需 global 声明——否则首次赋值抛 UnboundLocalError。
_last_scheduled: float | None = None
# scan_once 进程级互斥锁：APScheduler 调度扫描（后台线程）与手动 /signals/scan
# （queue market_scan 任务线程）可并发进入 scan_once；去重键 existing_map 基于各自
# session 启动时读到的既有事件，并发双写会与 DB 唯一索引（stock_code, triggered_at, period）
# 兜底互斥——超出一轮插入会以 IntegrityError 拒绝，避免重复行。
_scan_lock = threading.Lock()


def _signal_workers() -> int:
    """信号判定并行度：min(os.cpu_count(), 16)；≤1 时走纯串行回退。"""
    return max(1, min(os.cpu_count() or 1, 16))


# 周期 → 最少 bar 数阈值（不足视为数据不足，跳过判定）。周/月由日线重采样
# 天然满足（800 日 → 约 160 周 / 36 月）；daily 维持既有 40 根；分钟周期
# 统一 60 根（1/5/15/30/60，低功耗自选扫描，60 根约覆盖 1~3 个交易日）。
_MIN_BARS = {
    "daily": 40,
    "weekly": 20,
    "monthly": 12,
    "1": 60,
    "5": 60,
    "15": 60,
    "30": 60,
    "60": 60,
}

# 分钟周期：fetch workers=4 低功耗。
_MINUTE_PERIODS = ("1", "5", "15", "30", "60")

# 分钟扫描股票池范围（T-54）：watchlist 自选（缺省，保持旧行为）/ top_n 成交额前 N
# / codes 显式代码集（与快照求交）。API 层（signals.py）校验入参，scan_once 对未知值
# 防御性回退 watchlist。
SCAN_UNIVERSES = ("watchlist", "top_n", "codes")

# 扫描调度注册表：周期 → 间隔秒（0 = 不调度）。单一事实源 = Settings.scan_schedule
# （env SR_SCAN_SCHEDULE 传 JSON 可覆盖）；start_scanner 按表驱动逐周期注册 interval job，
# 非交易时段由 scheduled_scan 内全局降频（见 _OFF_HOUR_MIN_GAP）。测试可整体替换本表注入。
_SCHEDULE: dict[str, int] = settings.scan_schedule

# 非交易时段扫描最小间隔（秒）：29min = 30min - 60s 偏移。快照 TTL 非交易时段为
# base×30 = 1800s（quote.get_spot 内 session_spot_ttl）——若降频间隔恰好 1800s，
# 每次触发时缓存刚好过期（now - _spot_ts < ttl 恒假）→ 每轮都全量重拉 get_spot()；
# 减 60s 让降频后的扫描恒落在缓存 TTL 内命中（用缓存，不重拉全量）。
_OFF_HOUR_MIN_GAP = 30 * 60 - 60

# 事件表防膨胀：每周期保留的「观察」事件上限（P2-55）。只对「观察」生效——
# 「已确认/已忽略」是用户复盘决策数据，不参与自动清理（用户手动收敛，
# 静默删除即数据丢失）；观察事件数量有界（≤ 5000/周期）保证表整体可控。
_OBSERVE_EVENT_KEEP = 5000


def _event_key_time(ts: datetime, period: str) -> datetime:
    """信号归属时点（事件去重键与 triggered_at 共用）。

    - weekly：归一到该周一 00:00。进行中周 bar 的最后一根随交易日滑动（周一起
      至周五），bar_time 每交易日都变；不归一则同一进行中周期每天新增一条事件。
      归一后同一周期内任意交易日扫描命中同一条 live 事件（原地更新）。
    - monthly：归一到当月 1 日 00:00，理由同上。
    - 其余周期原样（仅去微秒）：daily 15:00 收盘标签、分钟精确时分标签不受影响。
    幂等：输入已是周期起点时返回自身（existing_map 回读已归一化的 triggered_at）。
    """
    if period == "weekly":
        return (ts - timedelta(days=ts.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if period == "monthly":
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(microsecond=0)


def _prepare_kline(df: pd.DataFrame) -> pd.DataFrame:
    """K 线按可解析日期升序排序并去重（同日期保留最后一行）。

    数据源偶发脏行（非法日期字符串/NaN）：先 coerce 过滤，保证最后一根 bar
    一定是真实可解析日期 —— 绝不能把扫描执行时间当 bar 时间。
    日期全部不可解析时返回空表（同列结构）。
    """
    if df is None or not len(df):
        return df
    parsed = pd.to_datetime(df["date"], errors="coerce")
    keep = parsed.notna()
    if not keep.any():
        return pd.DataFrame(columns=df.columns)
    out = df.loc[keep].copy()
    out["_dt"] = parsed[keep].to_numpy()
    out = out.sort_values("_dt").drop_duplicates("_dt", keep="last").drop(columns="_dt")
    return out.reset_index(drop=True)


def _synthesize_daily_bar(
    code: str, df: pd.DataFrame, spot_index: pd.DataFrame
) -> pd.DataFrame:
    """用实时快照合成今日日线 bar（数据源历史接口缺当日 bar 时的降级路径）。

    背景：部分数据源日K接口盘中不返回当日 bar（最后 bar 停在昨日），而全市场
    扫描不能逐只拉分钟线（成本高）；快照已含当日实时价/量，合成一根近似当日
    bar 即可让「今日」信号盘中实时性得到保证。

    spot_index 为 code → 快照行的索引（调用方一次性构建：spot_sub.drop_duplicates
    ("code", keep="first").set_index("code")）。旧实现逐股 spot[spot["code"]==code]
    对全市场快照布尔扫描（O(全市场行数)）；索引化后 code 查行 O(1)，全轮合成
    从 O(股票数×市场行数) 降为 O(市场行数 + 股票数)。重复 code 取首行与旧实现
    .iloc[0] 语义一致（快照 code 理论上唯一，drop_duplicates 仅防御）。

    近似口径（注释即契约）：
    - 复权口径近似：快照价格为未复权现价，缓存日线为 qfq 前复权——合成行与
      前序 bar 的价格连续性仅近似（只影响最后一根，量价关系指标仍可用）。
    - OHLC 振幅收敛：快照无 open/high/low，open 由昨收反推（price/(1+pct/100)），
      high/low 由 open 与 price 推导，振幅收敛于 [min(open, price), max(open, price)]。
    - 仅内存合成：不写 K 线缓存；数据源恢复后 fetch_many 增量拉真实当日 bar
      （kcache is_stale 盘中滞后即刷新）自动接管，合成行被真实数据替换。

    容错（任一条件 → 原样返回 df，绝不影响扫描主流程）：
    df 空 / spot_index 空 / spot_index 查不到该 code / price<=0 / 缓存最后 bar 已是今天 /
    无新交易（快照量与价均与缓存最后 bar 一致，视为休市/停牌/数据未更新）/
    必需列缺失（price/pct_change/volume/amount，如 mock 快照无 volume 列）/
    任何解析异常。
    """
    if df is None or not len(df) or spot_index is None or spot_index.empty:
        return df
    try:
        if code not in spot_index.index:
            return df
        row = spot_index.loc[code]
        if not all(
            col in row.index for col in ("price", "pct_change", "volume", "amount")
        ):
            return df  # 列缺失 → 跳过合成
        price = float(row["price"])
        if not math.isfinite(price) or price <= 0:
            return df
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        if str(df["date"].iloc[-1]) >= today:
            return df  # 已有当日 bar → 无需合成
        last_close = float(df["close"].iloc[-1])
        last_volume = float(df["volume"].iloc[-1]) if "volume" in df.columns else None
        snap_volume = float(row["volume"] or 0)
        if (
            last_volume is not None
            and snap_volume == last_volume
            and abs(price - last_close) < 1e-6
        ):
            return df  # 无新交易（休市/停牌/未更新）→ 防误报
        pct_raw = row.get("pct_change", 0)
        pct = float(pct_raw) if pd.notna(pct_raw) else 0.0
        open_ = last_close if pct == 0 else price / (1 + pct / 100)  # 昨收反推
        # 合成行日期格式对齐缓存最后一行：日线 date 列偶见带时分（数据源返回
        # "YYYY-MM-DD HH:MM:SS"），纯日期 "YYYY-MM-DD" 与之混排会被 _prepare_kline
        # 的 pd.to_datetime 混合格式推断解析为 NaT 而丢弃 → 对齐格式保证同列统一解析。
        synth_date = f"{today} 00:00:00" if " " in str(df["date"].iloc[-1]) else today
        synth = pd.DataFrame(
            [
                {
                    "date": synth_date,
                    "open": open_,
                    "high": max(open_, price),
                    "low": min(open_, price),
                    "close": price,
                    "volume": float(row["volume"] or 0),
                    "amount": float(row["amount"] or 0),
                    "pct_change": pct,
                }
            ]
        )
        return pd.concat([df, synth], ignore_index=True)
    except Exception:
        logger.debug("快照合成当日 bar 失败 %s，回退原 K 线", code)
        return df


def _check_one(
    code: str,
    klines: dict,
    turnover_map: dict,
    period: str = "daily",
) -> tuple | None:
    """单股票全信号判定（可并行）：_prepare_kline + evaluate_all_signals 一次判定。

    返回 (code, bar_time, as_of, hits, evidence_data) 或 None（无命中）；
    K 线缺失/过短/时间解析失败 → None。DB 写入由主线程在结果收集时完成。
    period 为 weekly/monthly 时入参 klines 已是重采样后的周/月线（由调用方
    _resample_period 完成），此处仅按目标周期做 bar 时点归一与长度阈值。
    """
    k = klines.get(code)
    if k is None or not len(k):
        return None
    k = _prepare_kline(k.reset_index(drop=True))
    if len(k) < _MIN_BARS.get(period, 40):
        return None
    # 注入 code 列（_limit_pct 依赖代码前缀判定阈值）与 turnover_rate 列（_turnover_spike）
    k = k.assign(code=code)
    if code in turnover_map:
        k = k.assign(turnover_rate=turnover_map[code])
    # 信号时点 = 具体股票最后一根参与判断的 bar 市场时间（分钟线保留分钟，日/周/月收盘）；
    # 显式传目标周期，解析失败跳过该股票（绝不写入当前扫描时间）
    try:
        bar_time = bar_market_time(k["date"].iloc[-1], period=period)
    except ValueError as e:
        logger.warning("跳过 %s: K 线时间解析失败 %s", code, e)
        return None
    # as_of = 数据实际截止时刻：最后 bar 为盘中部分数据（最后 bar 日期 == 今天且当前上海
    # 时刻 < 15:05，复用 kcache._is_partial_daily 判定）→ 当前上海时刻（去微秒，naive）；
    # 否则 as_of = bar_time（即 15:00 收盘标签）。仅影响展示，去重/排序仍走 triggered_at。
    partial = _is_partial_daily(k)
    now_cn = (
        datetime.now(ZoneInfo("Asia/Shanghai")).replace(microsecond=0, tzinfo=None)
        if partial
        else None
    )
    as_of = now_cn if partial else bar_time
    # 单次全信号判定：命中任一信号即产出事件（不再按模板拆分）
    hits, evidence = evaluate_all_signals(k)
    if not hits:
        return None
    vr = volume_ratio(k["volume"], 5).iloc[-1]
    evidence_data = {
        **evidence,
        "price": float(k["close"].iloc[-1]),
        "pct_change": float(k["pct_change"].iloc[-1]),
        "volume_ratio": round(float(vr), 2) if pd.notna(vr) else None,
    }
    return (code, bar_time, as_of, hits, evidence_data)


def _load_existing_events(db: Session, codes: list[str]) -> list["SignalEvent"]:
    """分批加载既有信号事件（SQLite 变量上限按 400/批），避免 IN 子句参数超限。

    全市场扫描 codes 可达数千只，`stock_code.in_(codes)` 单次查询在
    SQLite 下会触发 too many SQL variables；按 400 一批循环查询后合并结果，
    语义与单次查询完全一致（仅用于构建去重键映射）。
    """
    from ..storage.models import SignalEvent

    out: list[SignalEvent] = []
    for i in range(0, len(codes), 400):
        out.extend(
            db.query(SignalEvent)
            .filter(SignalEvent.stock_code.in_(codes[i : i + 400]))
            .all()
        )
    return out


def scan_once(
    period: str = "daily",
    full_universe: bool = True,
    progress_cb=None,
    *,
    universe: str = "watchlist",
    top_n: int | None = None,
    codes: list[str] | None = None,
) -> dict:
    """执行一轮扫描。period ∈ daily/weekly/monthly：周/月为全市场重算（快照全量，
    日线重采样得周/月线，事件带 period 字段与日线并存互不串扰）；period ∈ 1/5/15/30/60
    为分钟周期：universe 可配置（T-54）——watchlist 自选（缺省，保持旧行为）/ top_n
    成交额前 N / codes 显式代码集（与快照求交，未命中代码静默过滤），fetch workers=4
    低功耗。
    full_universe=True 覆盖全市场；False 仅成交额 Top N（仅 daily 生效，周/月恒全量；
    分钟分支不响应——universe 缺省 watchlist，与旧 full_universe=true 行为一致）。
    universe 仅分钟周期生效；top_n 缺省 settings.market_scan_limit（500）；
    codes 为空/全部未命中快照时提前返回 source="no-codes"（自选为空 source="no-watchlist"）。
    daily/weekly/monthly 在 fetch_many 后、周/月重采样前会尝试用实时快照合成
    「今日」日线 bar（数据源历史接口缺当日 bar 时的降级路径，仅内存不入缓存，
    分钟周期不合成）。

    progress_cb(pct: float, msg: str) 可选进度回调。
    返回 {'scanned': n, 'events': m, 'source': provider, 'elapsed': s}。
    并发互斥：调度扫描与手动扫描共用本函数，非阻塞锁保证同一时刻只有一轮
    在执行（另一路返回 source='busy'，下轮补扫）——见模块级 _scan_lock 注释。
    """
    from ..storage.models import SignalEvent, Stock

    if period not in ("daily", "weekly", "monthly", *_MINUTE_PERIODS):
        logger.warning("未知扫描周期 %r，跳过本轮", period)
        return {"scanned": 0, "events": 0, "source": "error", "elapsed": 0.0}

    if not _scan_lock.acquire(blocking=False):
        logger.info("已有扫描在进行，跳过本轮 scan_once（source=busy）")
        return {"scanned": 0, "events": 0, "source": "busy", "elapsed": 0.0}
    db = SessionLocal()
    try:
        t0 = time.time()
        if progress_cb:
            progress_cb(3.0, "获取行情快照")

        spot = get_spot()
        if spot is None or spot.empty:
            return {"scanned": 0, "events": 0, "source": "empty", "elapsed": 0.0}

        spot = spot.copy()
        spot["code"] = spot["code"].map(normalize_code)

        def _top_n(limit: int) -> pd.DataFrame:
            """成交额 Top N；amount 列缺失（异常/降级快照）→ 显式退化。

            退化语义：保留快照原序前 N 行——无成交额语义（等效随机顺序），
            仅 amount 列缺失时触发（正常快照恒有该列）；打 warning 明示，
            不再静默 head(limit)。
            """
            if "amount" in spot.columns:
                return spot.sort_values("amount", ascending=False).head(limit)
            logger.warning(
                "快照缺 amount 列，成交额 TopN 退化为快照原序前 %d 行（无金额语义）",
                limit,
            )
            return spot.head(limit)

        if period in ("weekly", "monthly"):
            # 周/月全市场重算：快照全部股票，不按成交额截断（自选池天然在快照内）
            universe = spot
        elif period in _MINUTE_PERIODS:
            # 分钟周期：universe 可配置（T-54）——watchlist 自选（缺省，低功耗）
            # / top_n 成交额前 N / codes 显式代码集（与快照求交，未命中代码静默
            # 过滤）；空池提前返回（source 区分：no-watchlist 自选为空、no-codes
            # 代码集为空或全部未命中快照；top_n 恒非空——快照非空时按 amount 取
            # 前 N 至少 N 行）。full_universe 仅影响 daily 分支，分钟分支不响应。
            if universe == "top_n":
                limit = (
                    int(top_n)
                    if (top_n and int(top_n) > 0)
                    else settings.market_scan_limit
                )
                universe = _top_n(limit)
            elif universe == "codes":
                wanted = [normalize_code(c) for c in (codes or [])]
                universe = spot[spot["code"].isin(wanted)]
                if universe.empty:
                    logger.info("分钟扫描 %s 代码集为空/未命中快照，跳过本轮", period)
                    return {
                        "scanned": 0,
                        "events": 0,
                        "source": "no-codes",
                        "elapsed": 0.0,
                    }
            else:
                # universe == "watchlist"（缺省）；未知值防御性回退 watchlist（API
                # 层已校验，直接调用方不受未知值破坏）
                if universe != "watchlist":
                    logger.warning("分钟扫描未知 universe %r，回退自选股", universe)
                wl = db.query(Stock.code).filter(Stock.is_watchlist.is_(True)).all()
                wl_codes = {r[0] for r in wl}
                if not wl_codes:
                    logger.info("分钟扫描 %s 自选为空，跳过本轮", period)
                    return {
                        "scanned": 0,
                        "events": 0,
                        "source": "no-watchlist",
                        "elapsed": 0.0,
                    }
                universe = spot[spot["code"].isin(wl_codes)]
        elif full_universe:
            # 信号扫描聚焦成交额前 N（雷达快照仍覆盖全市场）
            universe = _top_n(settings.scan_max_stocks)
        else:
            universe = _top_n(settings.market_scan_limit)
        # 自选池并入（分钟分支已按自选构建 universe，无需再次并入）
        if period not in _MINUTE_PERIODS:
            wl = db.query(Stock.code).filter(Stock.is_watchlist.is_(True)).all()
            wl_codes = {r[0] for r in wl}
            extra = spot[spot["code"].isin(wl_codes)]
            universe = pd.concat([universe, extra]).drop_duplicates("code")

        # 更新股票基础表：先按批（400/批，SQLite 变量上限）批量查回既有主键映射，
        # 循环内只做内存判断——修复前逐行 db.get(Stock) 是全市场数千只股票的 N+1 查询
        now = utcnow()
        existing_stocks: dict[str, Stock] = {}
        universe_codes = universe["code"].tolist()
        for i in range(0, len(universe_codes), 400):
            chunk = universe_codes[i : i + 400]
            for s in db.query(Stock).filter(Stock.code.in_(chunk)).all():
                existing_stocks[s.code] = s
        for _, r in universe.iterrows():
            stock = existing_stocks.get(r["code"])
            if stock is None:
                db.add(
                    Stock(
                        code=r["code"],
                        name=r["name"],
                        industry=r.get("industry", ""),
                        last_price=float(r["price"]),
                        pct_change=float(r["pct_change"]),
                        # 快照列缺失容错：volume/amount 缺列（如合成测试 mock）按 0 处理
                        volume=float(r.get("volume", 0.0) or 0.0),
                        amount=float(r.get("amount", 0.0) or 0.0),
                        turnover_rate=float(r.get("turnover_rate", 0.0)),
                    )
                )
            else:
                stock.name = r["name"]
                stock.last_price = float(r["price"])
                stock.pct_change = float(r["pct_change"])
                stock.volume = float(r.get("volume", 0.0) or 0.0)
                stock.amount = float(r.get("amount", 0.0) or 0.0)
                stock.turnover_rate = float(r.get("turnover_rate", 0.0))
                stock.updated_at = now
        db.commit()

        # 并发拉取 K 线（SQLite 缓存，增量）。周/月统一拉 daily（start_date 5 年前
        # 保证重采样深度），绝不对 weekly/monthly 单独发网络请求；分钟周期直接拉
        # 目标分钟线（workers=4 低功耗，universe 仅自选）。
        codes = universe["code"].tolist()
        total_codes = len(codes)
        fetch_start = (
            (datetime.now() - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
            if period in ("weekly", "monthly")
            else None
        )
        kline_period = "daily" if period in ("daily", "weekly", "monthly") else period
        kline_workers = 4 if period in _MINUTE_PERIODS else 12
        if progress_cb:
            klines = fetch_many(
                codes,
                kline_period,
                workers=kline_workers,
                start_date=fetch_start,
                progress_cb=lambda d, n: progress_cb(
                    8 + 50 * d / max(n, 1), f"K线缓存 {d}/{n}"
                ),
            )
        else:
            klines = fetch_many(
                codes,
                kline_period,
                workers=kline_workers,
                start_date=fetch_start,
            )
        if period in ("daily", "weekly", "monthly"):
            # A：快照合成当日 bar（降级路径）。数据源历史接口盘中无当日 bar 时，
            # 用实时快照合成今日日线挂到缓存尾部（仅内存，不入 K 线缓存），保证
            # 「今日」信号盘中实时性；周/月重采样在其后执行，自动受益（周/月 bar
            # 最后日期推进到今天）。分钟周期不合成。逐只调用，任一失败回退原 K 线。
            # code→行索引一次性构建（O(市场行数)），避免旧实现逐股
            # spot[spot["code"]==code] 全表布尔扫描（O(股票数×市场行数)）：
            # 每轮 ~2000 股 × 5500 行 ≈ 1100 万次比较降为 ~2000 次 O(1) 查行。
            # 重复 code 取首行（keep="first" 与旧实现 .iloc[0] 语义一致，防御性去重）。
            spot_sub = spot[spot["code"].isin(codes)]
            spot_index = spot_sub.drop_duplicates("code", keep="first").set_index(
                "code"
            )
            klines = {
                c: _synthesize_daily_bar(c, df, spot_index)
                for c, df in klines.items()
                if df is not None and len(df)
            }
        if period in ("weekly", "monthly"):
            # 逐股日线 → 周/月线重采样（仅内存，不入缓存、不发网络请求）
            klines = {
                c: _resample_period(df, period)
                for c, df in klines.items()
                if df is not None and len(df)
            }

        # 逐股全信号判定（仅对命中写入事件）。CPU 主导，按股票并行（每股票
        # = _prepare_kline + evaluate_all_signals）；DB 写入/事件去重保持在主线程。
        event_count = 0
        hits_rows: list[SignalEvent] = []
        # 去重键 = (股票, 归属时点, 周期)：同一股票在同一 bar 归属时点上重复扫描只更新
        # signals/evidence，不重复插入；bar 时点推进（新 K 线）才新增事件。
        # period 维度隔离周/月与日线事件（同 bar 时点不同周期互不串扰）。
        # 周/月的归属时点 = 周期起点（周一/当月 1 日，见 _event_key_time）——进行中周期
        # 最后一根 bar 随交易日滑动，不归一则同一进行中周期每天新增一条事件；
        # 归一后新事件 triggered_at 同样写入周期起点，与 models 唯一索引
        # uq_signal_event_stock_time(stock_code, triggered_at, period) 键空间一致
        # （并发双写兜底仍成立）。
        # A6：IN 子句分批查询（SQLite 变量上限 400/批），避免超大 universe 触发
        # too many SQL variables；合并结果语义与单次查询一致。
        existing_events = _load_existing_events(db, codes)
        existing_map: dict[tuple[str, datetime, str], SignalEvent] = {}
        for e in existing_events:
            if e.triggered_at is None:
                continue
            p = e.period or "daily"
            existing_map[(e.stock_code, _event_key_time(e.triggered_at, p), p)] = e
        # turnover_rate 提前映射（worker 线程不做 pandas 行过滤）
        turnover_map: dict[str, float] = {
            r["code"]: float(r.get("turnover_rate", 0.0) or 0.0)
            for _, r in universe.iterrows()
        }

        # scan_discovered_at = 事件首次被扫描发现的时间（不可变）：本轮扫描统一取
        # 一次上海时刻（去微秒，与 as_of 取时同款口径）。新插入事件写入该值；
        # 同 bar 重复命中（upsert）保留原值，仅「观察」事件推进 as_of/证据。
        discovered_now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
            microsecond=0, tzinfo=None
        )

        def _apply_hits(per) -> None:
            nonlocal event_count
            for code, bar_time, as_of, hits, evidence_data in per:
                # 周/月 bar_time 随交易日滑动 → 归一化到周期起点再参与去重（见 _event_key_time）；
                # triggered_at 写入同一归一值时点（与 existing_map 键、DB 唯一索引一致）
                key_time = _event_key_time(bar_time, period)
                ev = existing_map.get((code, key_time, period))
                if ev is not None:
                    # 同一归属时点重复命中：仅「观察」事件刷新信号/证据/时间；
                    # 「已确认」「已忽略」事件保留用户决策与原始证据，不做任何改写。
                    # scan_discovered_at 两者均不动（首次发现时刻不可变）
                    if ev.status == "观察":
                        ev.signals = hits
                        ev.evidence = evidence_data
                        ev.triggered_at = key_time
                        ev.as_of = as_of
                else:
                    hits_rows.append(
                        SignalEvent(
                            stock_code=code,
                            signals=hits,
                            status="观察",
                            evidence=evidence_data,
                            triggered_at=key_time,
                            as_of=as_of,
                            scan_discovered_at=discovered_now,
                            period=period,
                        )
                    )
                event_count += 1

        def _progress(done: int) -> None:
            if progress_cb:
                progress_cb(
                    60 + 35 * done / max(total_codes, 1),
                    f"信号判定 {done}/{total_codes}",
                )

        n_workers = _signal_workers()
        # 指标 memo：跨信号复用 rsi/macd/boll 等计算结果（作用域仅本轮单只股票）。
        # 串行路径用共享 memo dict，每处理 500 只清理一次（内存上界受控）；并行路径
        # worker 线程由 _run_check 按股安装独立 memo（engine.indicator_memo 基于
        # threading.local，不装则全部直算——单股票 66 个信号间同样复用指标序列，
        # memo 随调用结束释放，内存上界 = worker 数 × 单股票序列，无共享清理竞争）。
        from ..lib.signals import engine as _engine

        memo: dict = {}

        def _run_check(code: str) -> list[tuple]:
            """并行 worker 入口：按股安装指标 memo 后执行单股票全信号判定。"""
            m: dict = {}
            with _engine.indicator_memo(m):
                res = _check_one(code, klines, turnover_map, period)
                return [res] if res else []

        with _engine.indicator_memo(memo):
            if n_workers <= 1:
                # 纯串行回退：行为与并行路径完全一致（按股票顺序判定）
                for idx, code in enumerate(codes):
                    if progress_cb and idx % 50 == 0:
                        _progress(idx)
                    res = _check_one(code, klines, turnover_map, period)
                    _apply_hits([res] if res else [])
                    if idx and idx % 500 == 0:
                        memo.clear()
            else:
                with ThreadPoolExecutor(max_workers=n_workers) as pool:
                    futures = {pool.submit(_run_check, c): c for c in codes}
                    done = 0
                    try:
                        for fut in as_completed(futures):
                            # 异常（含 JobCancelled）在收集时经 future.result() 抛出，
                            # 由主线程统一重抛；DB 写入保持在主线程原样
                            _apply_hits(fut.result())
                            done += 1
                            if progress_cb and (done % 50 == 0 or done == total_codes):
                                _progress(done)
                    except BaseException:
                        for f in futures:
                            f.cancel()
                        raise
        # A1：既有事件更新（同 bar 时点重复命中）同样需要落库——旧实现仅当
        # 新增行非空时 commit，导致「只更新不新增」路径的 score/evidence 修改
        # 停留在 session 内随 close 丢弃。有更新或新增都提交。
        if hits_rows or event_count:
            if hits_rows:
                db.add_all(hits_rows)
            db.commit()

        # 事件表防膨胀（P2-55）：每周期「观察」事件保留最近 _OBSERVE_EVENT_KEEP 条
        # （daily/周/月互不影响；WHERE period=:p 只删本周期更旧的）。只清理
        # status='观察' 的未处理事件——「已确认/已忽略」是用户复盘决策数据，参与
        # 防膨胀删除即静默丢失复盘记录；观察事件数量有界（≤5000/周期）保证表整体
        # 可控，确认/忽略由用户手动收敛。
        # 保留阈值 = 第 _KEEP 大观察事件 id，删除其更旧的观察事件（修正原实现
        # offset(K).all() 恒删不掉的缺陷：desc 序 offset 末元素恒为全局最小 id，
        # 「id < 最小」恒空转，防膨胀从未生效）。
        keep_id = (
            db.query(SignalEvent.id)
            .filter(
                SignalEvent.period == period,
                SignalEvent.status == "观察",
            )
            .order_by(SignalEvent.id.desc())
            .offset(_OBSERVE_EVENT_KEEP - 1)
            .limit(1)
            .scalar()
        )
        if keep_id is not None:
            db.query(SignalEvent).filter(
                SignalEvent.period == period,
                SignalEvent.status == "观察",
                SignalEvent.id < keep_id,
            ).delete(synchronize_session=False)
            db.commit()

        if progress_cb:
            progress_cb(96.0, "写入事件")
        # 持久化最后扫描时刻（epoch 秒，cache_meta meta:last_scan[:period]），供前端
        # 新鲜度条。daily 保持 meta:last_scan 键不变（向后兼容）；周/月写独立键。
        # 复用本会话 db（测试 patch SC.SessionLocal 后同样走临时库，不污染生产库）。
        # 失败仅记日志，不阻塞扫描主流程（元数据缺失不致命）。
        try:
            meta_key = (
                "meta:last_scan" if period == "daily" else f"meta:last_scan:{period}"
            )
            set_meta(meta_key, int(time.time()), db=db)
        except Exception as e:
            logger.warning("写入最后扫描时刻失败: %s", str(e)[:100])
        elapsed = round(time.time() - t0, 1)
        logger.info(
            "扫描完成: %d 只, %d 事件, %.1fs", len(universe), event_count, elapsed
        )
        return {
            "scanned": len(universe),
            "events": event_count,
            "source": get_provider().name,
            "elapsed": elapsed,
        }
    except Exception as e:
        # 协作取消：进度回调内的 JobCancelled 必须向上传播（queue 层处理），
        # 否则整轮扫描在取消后仍继续写事件/更新股票，取消迟迟不生效。
        from .tasks.errors import JobCancelled

        if isinstance(e, JobCancelled):
            raise
        logger.exception("扫描异常")
        return {"scanned": 0, "events": 0, "source": "error", "elapsed": 0.0}
    finally:
        db.close()
        _scan_lock.release()


def calendar_window_start(days: int) -> datetime:
    """市场日历口径窗口起点：含今天往前数 days 个工作日的 00:00（Asia/Shanghai naive）。

    交易日判定仅跳过周末（与 session.in_trading_session 同级别）；项目无节假日表，
    节假日按工作日计（局限，报告见任务说明）。与 market_time_range（纯自然日边界）
    的差异：周末不占用窗口——保证周五收盘事件在周一查询仍可见；窗口锚定最近交易日
    边界，不随当前时刻滑动（同一天内任意时刻查询结果一致）。
    """
    today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    days = max(1, days)
    d = today
    seen = 0
    while seen < days:
        if d.weekday() < 5:
            seen += 1
        if seen < days:
            d -= timedelta(days=1)
    return d


def recent_events(minutes: int = 60 * 24) -> list[dict]:
    """最近信号事件（雷达/信号中心用），附带股票名称。

    市场日历口径：窗口 = 含今天往前数 N 个工作日的零点起（N = minutes 折算自然日数，
    向上取整、最少 3 天），不随当前时刻滑动——同一天内任意时刻查询结果一致。
    旧口径为「当前时刻 - minutes」的滑动窗口：昨日收盘事件今日 10:00 可见、16:00
    被排除，可见性随查询时刻漂移。新口径下事件可见性只取决于其发生日与最近交易日
    边界的关系（周末/非交易时段按最近交易日边界计算）。
    """
    from ..storage.models import SignalEvent, Stock

    db = SessionLocal()
    try:
        days = max(3, math.ceil(minutes / (24 * 60)))
        since = calendar_window_start(days)
        events = (
            db.query(SignalEvent)
            .filter(SignalEvent.triggered_at >= since)
            .order_by(SignalEvent.triggered_at.desc())
            .limit(2000)
            .all()
        )
        codes = {e.stock_code for e in events}
        name_map: dict[str, str] = {}
        if codes:
            # P2-80：事件代码集可达 2000（limit），SQLite 变量上限 999——按批
            # （400/批，项目铁律）IN 查询，避免 too many SQL variables；sorted
            # 保证批内/批间顺序确定（仅影响查询顺序，不影响结果集合）。
            for chunk in in_chunks(sorted(codes)):
                for s in db.query(Stock).filter(Stock.code.in_(chunk)).all():
                    name_map[s.code] = s.name
        return [
            {
                "id": e.id,
                "stock_code": e.stock_code,
                "stock_name": name_map.get(e.stock_code, ""),
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
    finally:
        db.close()


def start_scanner():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    def scheduled_scan(period: str):
        """调度入口：非交易时段全局降频（距上次 < _OFF_HOUR_MIN_GAP 跳过，即约 29min
        一次，避开快照 TTL 1800s 边界相撞——见 _OFF_HOUR_MIN_GAP 注释）。"""
        global _last_scheduled
        # 经 lib.session 函数级引用：函数内延迟 import 保证测试 monkeypatch
        # lib.session.in_trading_session 穿透（顶部 from-import 别名会绑旧引用）。
        from ..lib.session import in_trading_session

        if not in_trading_session():
            if _last_scheduled and time.time() - _last_scheduled < _OFF_HOUR_MIN_GAP:
                return
        _last_scheduled = time.time()
        scan_once(period)

    # 按调度注册表逐周期注册 interval job（0 = 不调度）。首轮稍后执行
    # （等快照缓存预热），之后按各自间隔触发；分钟周期仅扫自选股（低功耗分支）。
    # T-77：运行时覆盖（AppParam rt:scan_schedule 显式优先）在启动时读取——
    # interval job 注册后间隔固定，改值需重启后端生效（不硬做热更新）。
    from ..core.runtime_config import get_runtime

    schedule = get_runtime("scan_schedule", _SCHEDULE)
    registered = {p: i for p, i in schedule.items() if i > 0}
    for period, interval in registered.items():
        _scheduler.add_job(
            scheduled_scan,
            "interval",
            seconds=interval,
            id=f"market_scan:{period}",
            max_instances=1,
            coalesce=True,
            args=[period],
            next_run_time=datetime.now() + timedelta(seconds=8),
        )
    _scheduler.start()
    logger.info("扫描调度已启动: %s", registered)


def stop_scanner():
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning("停止调度器异常: %s", e)
        _scheduler = None


# ---------- 组件化（bt-runtime 注册） ----------


class Scanning:
    """扫描编排组件：start_scanner/stop_scanner 包装 + 运行状态。

    依赖数据源恢复组件（sources）：扫描前的快照/源状态由其先启动。
    """

    name = "scanning"
    depends: frozenset[str] = frozenset({"sources"})

    def start(self) -> None:
        start_scanner()

    def stop(self) -> None:
        stop_scanner()

    def status(self) -> dict:
        return {"name": self.name, "running": _scheduler is not None}
