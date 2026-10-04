"""行情 SSE 推送核心:快照 diff 生成 + 订阅广播 + 后台推送线程(core 层,bt-scan)。

复用 core/events.py 的成熟范式(ring + subscribe + 心跳 + 收尾)实现全市场级 Hub:
- 后台线程按 lib.session.session_spot_ttl 节奏(盘中 90s / 非交易时段放大)取
  「关注列表 + 沪深300」快照子集(数据源复用 core.sources.get_spot 三源降级链路,
  不新增源),与上一轮快照 diff:
  * 首轮(ring 为空)→ event: snapshot 全量子集,写入 ring 供新连接速发;
  * 之后有变化 → event: tick 变化行打包(阈值过滤:涨跌 >0.1% 或量能变化 >5%,
    防推送风暴);
- 信号事件增量:复用信号事件流(storage/repos/signals.events_after_id)按 id 游标
  增量查询自选股事件,打包 event: signal(字段契约与 /market/watchlist/notifications
  对齐);
- 慢消费者(订阅队列积压 > MAX_QUEUED)由 api/market_stream 生成器断开,客户端
  EventSource 自动重连后经 ring 速发最近快照,积压自然清空;
- 线程生命周期:订阅数 0→1 惰性启动,归 0 停止(与 events.py GC 同款惰性模式,
  不依赖 main.py lifespan / core/runtime 组件注册表改动)。

线程安全:全局 _lock 保护订阅表/ring/diff 基准/游标;每轮循环自持短临界区,
广播在锁外执行(非阻塞 put)。
事件格式:{"type": "snapshot"|"tick"|"signal", "ts": time.time(), **payload}。

量比说明:快照内部列契约(base.py)无 volume_ratio(量比),以 volume(手)为量能
代理——成交量相对变化 >5% 等价规格「量比变化>5% 才推」(量比 = 即时量/均量,
成交量骤变即量比骤变),不新增数据源字段。
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import pandas as pd

from ..config import settings
from ..lib.session import session_spot_ttl

logger = logging.getLogger("stockradar.market_stream")

# 慢消费者阈值:订阅队列积压超过该条数 → SSE 生成器断开连接(生产远快于消费)。
# 推送节奏 90s 一轮,积压 100 条 ≈ 2.5h,仅在极端场景触发。
MAX_QUEUED = 100

# 增量阈值(tick 过滤,防推送风暴):
# - 价格:与上一轮快照相比相对变化 > 0.1%(规格「涨跌>0.1%」);
# - 量能:成交量相对变化 > 5%(规格「量比变化>5%」,volume 为量比代理,见模块 docstring)。
PRICE_CHG_THRESHOLD = 0.001  # 0.1%
VOLUME_CHG_THRESHOLD = 0.05  # 5%

# 沪深300 成分获取失败后的重试冷却(秒):get_hs300_codes 失败时走网络三级降级,
# 每轮循环(90s)都重试会造成不必要的网络压力,冷却窗口内仅推送关注列表。
HS300_RETRY_COOLDOWN = 300.0

# 快照行 → 前端 quote 契约字段(与 GET /stocks/{code}/quote 响应同形状,便于前端直灌缓存)
_QUOTE_FIELDS = (
    "code",
    "name",
    "price",
    "pct_change",
    "volume",
    "amount",
    "turnover_rate",
    "industry",
    "source",
    "timestamp",
)


def _clean_value(v) -> object:
    """快照行值 → JSON 安全:NaN/None → None;其余原样(数字/字符串)。"""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v if isinstance(v, (str, int, float, bool)) else str(v)


def _row_item(r: pd.Series) -> dict:
    """单行快照 → 前端 quote 字段 dict(缺失列 → None)。"""
    return {k: _clean_value(r.get(k)) for k in _QUOTE_FIELDS}


def _subset_payload(subset: pd.DataFrame) -> dict:
    """快照子集 → snapshot 事件 payload(source/count/items)。"""
    items = [_row_item(r) for _, r in subset.iterrows()]
    source = ""
    try:
        from . import sources as _sources

        source = _sources.get_provider().name
    except Exception:  # noqa: BLE001 - source 仅展示用,失败置空不阻塞推送
        pass
    return {"source": source, "count": len(items), "items": items}


def _diff_changed(
    new_df: pd.DataFrame, old_df: pd.DataFrame | None
) -> pd.DataFrame | None:
    """两轮快照子集 diff:返回超过阈值的变化行(仅含 new 侧列)。

    - 价格相对变化 > 0.1%(old 价格 > 0 时比较;停牌价格不动自然不推);
    - 成交量相对变化 > 5%(old 量 > 0 时比较);old 量为 0 且新量 > 0(停牌/恢复)也算变化;
    - 上一轮缺失的 code(新进关注/成分)→ 必推(merge 后 old 列为 NaN);
    - old_df 为空 → 返回 None(调用方按首轮全量处理)。
    """
    if old_df is None or old_df.empty or new_df is None or new_df.empty:
        return None
    base = old_df[["code", "price", "volume"]]
    merged = new_df.merge(base, on="code", suffixes=("", "_old"), how="left")
    price_old = merged["price_old"]
    vol_old = merged["volume_old"]
    mask = pd.Series(False, index=merged.index)
    mask |= (price_old > 0) & (
        (merged["price"] - price_old).abs() / price_old > PRICE_CHG_THRESHOLD
    )
    mask |= (vol_old > 0) & (
        (merged["volume"] - vol_old).abs() / vol_old > VOLUME_CHG_THRESHOLD
    )
    mask |= (vol_old == 0) & (merged["volume"] > 0)
    mask |= price_old.isna()  # 新增 code
    return new_df[mask]


class MarketStreamHub:
    """单例行情推送 Hub:订阅表 + ring(最近 1 条快照)+ 后台推送线程。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 订阅队列:queue.Queue(同步)或 AsyncSubQueue(async SSE 生成器,跨线程桥)
        self._subs: dict[int, queue.Queue | AsyncSubQueue] = {}
        self._next_id = 0
        # ring:最近 1 条 snapshot 事件(新连速发;tick 不更新 ring,新连接始终拿到全量)
        self._ring: dict | None = None
        # 内部 diff 基准(上一轮快照子集,与 ring 同轮更新)
        self._last_df: pd.DataFrame | None = None
        # 信号事件 id 游标(关注列表维度;None = 尚未 bootstrap)
        self._last_signal_id: int | None = None
        # 沪深300 成分获取失败时间戳(重试冷却)
        self._hs300_fail_ts = 0.0
        self._ref_count = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------- 订阅 ----------

    def subscribe(self) -> tuple[int, queue.Queue]:
        """创建订阅队列;订阅数 0→1 时惰性启动后台推送线程。"""
        with self._lock:
            q: queue.Queue = queue.Queue()
            sub_id = self._next_id
            self._next_id += 1
            self._subs[sub_id] = q
            self._ref_count += 1
        self._ensure_thread()
        return sub_id, q

    def asubscribe(self) -> tuple[int, AsyncSubQueue]:
        """创建 async 订阅队列(SSE async 生成器用):跨线程桥,生成器 await 消费。

        惰性导入 events.AsyncSubQueue 避免模块加载顺序依赖;订阅数 0→1 同样
        惰性启动后台推送线程(与 subscribe 同生命周期语义)。
        """
        from ..core.events import AsyncSubQueue

        with self._lock:
            q = AsyncSubQueue()
            sub_id = self._next_id
            self._next_id += 1
            self._subs[sub_id] = q
            self._ref_count += 1
        self._ensure_thread()
        return sub_id, q

    def unsubscribe(self, sub_id: int) -> None:
        """移除订阅队列;订阅归零时停止后台推送线程。

        归零判断与递减在同一临界区内完成(锁外读 ref_count 存在读到
        中间值的窗口,虽无害但一并消除);停止信号只在锁内发送。
        """
        with self._lock:
            if self._subs.pop(sub_id, None) is not None:
                self._ref_count -= 1
                if self._ref_count <= 0:
                    self._stop_thread()

    def snapshot(self) -> dict | None:
        """返回最近 1 条 snapshot 事件(新连速发);从未推过返回 None。"""
        with self._lock:
            return self._ring

    # ---------- 线程生命周期(惰性,与 events.py GC 同款模式) ----------

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            # 仅当 _thread 为 None(初始,或旧线程已退出收尾)时 clear:此时不存在
            # 会读取停止信号的活线程,clear 不会误放走旧线程——这是与旧代码
            # 「锁内置 None + 新订阅 clear」竞态的根本区别(见 _thread_exit)。
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="market-stream-push"
            )
            self._thread.start()

    def _stop_thread(self) -> None:
        """发送停止信号:仅唤醒 wait 中的推送线程。

        不再直接置空 _thread——线程退出由自身在 _loop 收尾(_thread_exit)
        持锁复查引用计数后完成。否则旧线程未退出即被 _ensure_thread 新建
        顶替,旧线程醒来发现停止信号已被清除而继续循环,产生双推送线程。
        """
        self._stop.set()

    def _thread_exit(self) -> None:
        """推送线程退出收尾(仅在线程自身线程调用):清引用;期间有新订阅则让位重启。

        这是线程归零退出的唯一置空路径:旧线程先退出循环、持锁复查——
        若已有新订阅(ref_count > 0),说明断线重连已发生,旧线程清除停止
        信号并重启新线程(clear 此刻安全:旧线程已退出循环,不会误放行);
        否则 _thread 置 None,后续订阅经 _ensure_thread 正常冷启动。
        整个流程锁内完成,保证任意时刻存活推送线程恰 0 或 1 个。
        """
        with self._lock:
            self._thread = None
            if self._ref_count > 0:
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="market-stream-push"
                )
                self._thread.start()

    def _interval(self) -> float:
        """推送节奏:交易时段 session_spot_ttl(90s),非交易时段放大(数据不变)。"""
        return float(session_spot_ttl(settings.quote_cache_ttl))

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self._interval()):
                try:
                    self._tick_once()
                except Exception as e:
                    logger.warning("行情推送循环异常: %s", str(e)[:200])
        finally:
            self._thread_exit()

    # ---------- 推送逻辑 ----------

    def _tick_once(self) -> None:
        """一轮推送:快照 diff + 信号增量。公开供测试手动调用。"""
        self._push_spot()
        self._push_signals()

    def _push_spot(self) -> None:
        """取快照子集 → 首轮推 snapshot(入 ring)/后续 diff 推 tick。

        C10:不再按交易时段强制 refresh——快照刷新交给 provider 热缓存 TTL
        (session_spot_ttl:盘中 90s / 非交易放大)与源级冷却/竞速机制自然决定:
        TTL 内 get_spot 直接命中缓存(零网络请求),TTL 过期才触发竞速抓取,
        消除「每个推送周期对全部未冷却源并发全量抓取」的网络放大。
        """
        from ..core.sources import get_spot

        try:
            spot = get_spot()
        except Exception as e:
            logger.debug("推送快照获取失败,本轮跳过: %s", str(e)[:120])
            return
        if spot is None or spot.empty:
            return
        subset = self._universe_subset(spot)
        with self._lock:
            first = self._ring is None
            last_df = self._last_df
            self._last_df = subset
        if first:
            event = {"type": "snapshot", "ts": time.time(), **_subset_payload(subset)}
            with self._lock:
                self._ring = event
            self._broadcast(event)
            return
        changed = _diff_changed(subset, last_df)
        if changed is not None and not changed.empty:
            self._broadcast(
                {
                    "type": "tick",
                    "ts": time.time(),
                    "items": [_row_item(r) for _, r in changed.iterrows()],
                }
            )

    def _universe_subset(self, spot: pd.DataFrame) -> pd.DataFrame:
        """关注列表 + 沪深300 成分(并集,均带后缀);成分获取失败降级为仅关注列表。"""
        from ..storage.db import SessionLocal
        from ..storage.repos import stocks as _stock_repo

        codes: set[str] = set()
        db = SessionLocal()
        try:
            codes |= _stock_repo.watchlist_codes(db)
        finally:
            db.close()
        now = time.time()
        if now - self._hs300_fail_ts > HS300_RETRY_COOLDOWN:
            try:
                from ..storage.hs300 import get_hs300_codes

                codes |= set(get_hs300_codes())
            except Exception as e:
                self._hs300_fail_ts = now
                logger.debug("沪深300成分获取失败,仅推送关注列表: %s", str(e)[:120])
        if not codes:
            return spot.iloc[0:0]
        return spot[spot["code"].isin(codes)]

    def _push_signals(self) -> None:
        """自选股信号事件增量(复用信号事件流,id 游标增量)。"""
        from ..storage.db import SessionLocal
        from ..storage.repos import signals as _sig_repo
        from ..storage.repos import stocks as _stock_repo

        db = SessionLocal()
        try:
            wl = _stock_repo.watchlist_codes(db)
            if not wl:
                with self._lock:
                    self._last_signal_id = None
                return
            with self._lock:
                last_id = self._last_signal_id
            if last_id is None:  # bootstrap:只建立游标,不推存量
                with self._lock:
                    self._last_signal_id = _sig_repo.latest_event_id(db, wl)
                return
            rows = _sig_repo.events_after_id(db, wl, last_id)
            if not rows:
                return
            codes = {e.stock_code for e in rows}
            name_map = _stock_repo.stock_names(db, codes) if codes else {}
            events = [
                {
                    "id": e.id,
                    "stock_code": e.stock_code,
                    "stock_name": name_map.get(e.stock_code, ""),
                    "signals": e.signals,
                    "status": e.status,
                    "triggered_at": e.triggered_at.isoformat()
                    if e.triggered_at
                    else None,
                    "as_of": e.as_of.isoformat() if e.as_of else None,
                    "scan_discovered_at": (
                        e.scan_discovered_at.isoformat()
                        if e.scan_discovered_at
                        else None
                    ),
                    "period": e.period,
                }
                for e in rows
            ]
            with self._lock:
                self._last_signal_id = rows[-1].id
            self._broadcast({"type": "signal", "ts": time.time(), "events": events})
        except Exception as e:
            logger.debug("信号增量推送失败,本轮跳过: %s", str(e)[:120])
        finally:
            db.close()

    def _broadcast(self, event: dict) -> None:
        """非阻塞广播给全部订阅队列;队列满(极端)丢弃,靠 ring 重放兜底。"""
        with self._lock:
            subs = list(self._subs.values())
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                logger.debug("行情订阅队列已满,丢弃事件 %s", event.get("type"))


# 全站单例:api/market_stream 生成器与测试均经此引用
hub = MarketStreamHub()
