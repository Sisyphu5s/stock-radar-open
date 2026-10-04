"""数据源门面与健康管理（bt-scan，core 层）。

合流自原 services 层 sources.py 与 quote.py 门面（历史迁移，行为零变化）：
- sources.py：健康登记、自动切换（失败冷却 + 后台恢复探测）、
  手动覆盖（持久化）、spot/kline 双通道并发竞速（P1-66）、新闻源冷却（P1-63）；
- quote.py 门面部分：provider 实例缓存（_instances）、
  get_spot/get_kline/get_news/get_earnings/get_financials/get_sina_kline、
  磁盘快照回退（离线兜底）、_resample_period、probe_source。

存储适配器（网络 I/O 直连）在 storage/providers/；本模块负责多源竞速/冷却/
回退策略与组件化生命周期（SourceRecoveryComponent）。storage 禁 import core：
快照磁盘 I/O 已下沉 storage/snapshots.py（本模块 re-export 兼容旧 import 面），
成交量校验上提到 get_kline/get_sina_kline 门面（provider 内部不再校验）；
交易时段判定经 lib.session 直连。

组件化：原 quote.py 底部「import 即 load_state+start_recovery」的导入副作用
已移除——由 SourceRecovery.start() 显式启动（bt-runtime 注册）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import Future, as_completed
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

from ..config import settings
from ..lib.codes import bare_code as _bare
from ..storage.paths import SOURCE_STATE_FILE
from ..storage.snapshots import (  # noqa: F401 - 门面 re-export（快照 I/O 下沉 storage，H1a）
    SNAP_MAX_AGE_S,
    _SNAP_DIR,
    _snap_path,
    _spot_disk_fallback,
    load_snapshot_disk,
    save_snapshot_disk,
)
from ..storage.providers import (  # noqa: F401 - 门面层 re-export（兼容旧 import 面）
    PERIOD_SCALE,
    SESSION,
    AkshareProvider,
    QuoteProvider,
    SinaFinancialsMixin,
    SinaProvider,
    TencentProvider,
    _MINUTE_WALL_FACTOR,
    _MOCK_STOCKS,
    _ak_call,
    _ak_error,
    _ak_retry,
    _minute_window_minutes,
    _normalize_volume,
    _recent_quarters,
    _resample_period,
    _sina_symbol,
    _slice_kline,
)

logger = logging.getLogger("stockradar.sources")
_quote_logger = logging.getLogger("stockradar.quote")

SOURCES = [
    {"key": "akshare", "name": "akshare · 东财"},
    {"key": "sina", "name": "新浪财经"},
    {"key": "tencent", "name": "腾讯行情"},
]
PRIORITY = {s["key"]: i for i, s in enumerate(SOURCES)}
VALID = set(PRIORITY)

_STATE_DIR = SOURCE_STATE_FILE.parent
_STATE_FILE = SOURCE_STATE_FILE

_state: dict = {
    "mode": "auto",  # auto | manual
    "override": "",  # manual 模式锁定的源 key
    "last_switch_ts": None,
    "last_switch_reason": "",
}
_state_lock = threading.Lock()

# 运行时健康表：key -> 状态 dict
_health: dict[str, dict] = {
    k: {
        "ok": False,
        "spot_fails": 0,
        "kline_fails": 0,
        "last_ok": None,
        "last_fail": None,
        "spot_blocked_until": 0.0,
        "kline_blocked_until": 0.0,
        "slow_until": 0.0,
        "latency_ms": None,
        "last_error": "",
    }
    for k in PRIORITY
}
_health_lock = threading.Lock()

# 慢源阈值：单次快照超过该秒数视为病态（绕过内部降级，改由调度切到更快源）
SLOW_LATENCY_MS = 25_000
SLOW_COOLDOWN_S = 120

_active: str | None = None
_recovery_started = False
# 恢复探测线程停止信号（组件化：start/stop 可控，消除 import 即启动副作用）
_recovery_stop = threading.Event()


# ---------- 持久化 ----------


def load_state() -> None:
    global _state
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if (
            saved.get("mode") in ("auto", "manual")
            and saved.get("override", "") in VALID
        ):
            _state = saved
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("读取数据源状态失败: %s", str(e)[:100])


def _save_state() -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=1)
        tmp.replace(_STATE_FILE)
    except Exception as e:
        logger.warning("保存数据源状态失败: %s", str(e)[:100])


# ---------- 模式与优先级 ----------


def get_mode() -> str:
    with _state_lock:
        return _state["mode"]


def set_mode(value: str) -> dict:
    """设置模式：'auto' 或具体源 key（隐式切 manual）。持久化后由调用方 reset provider。"""
    global _active
    with _state_lock:
        if value == "auto":
            _state["mode"] = "auto"
            _state["override"] = ""
        elif value in VALID:
            _state["mode"] = "manual"
            _state["override"] = value
        else:
            raise ValueError(f"未知数据源: {value}")
        _state["last_switch_ts"] = datetime.now().isoformat(timespec="seconds")
        _state["last_switch_reason"] = "手动切换" if value != "auto" else "恢复自动切换"
        _save_state()
    with _health_lock:
        _active = None
    return status()


def preference_order() -> list[str]:
    """生效的源顺序：manual 锁定单个；auto 按优先级（env SR_DATA_PROVIDER 优先）。"""
    with _state_lock:
        if _state["mode"] == "manual" and _state["override"]:
            return [_state["override"]]
    order = list(PRIORITY)
    env = (settings.data_provider or "").strip().lower()
    if env in VALID:
        order.remove(env)
        order.insert(0, env)
    return order


# ---------- 健康登记 ----------


def _h(key: str) -> dict:
    return _health.setdefault(
        key,
        {
            "ok": False,
            "spot_fails": 0,
            "kline_fails": 0,
            "last_ok": None,
            "last_fail": None,
            "spot_blocked_until": 0.0,
            "kline_blocked_until": 0.0,
            "slow_until": 0.0,
            "latency_ms": None,
            "last_error": "",
        },
    )


def record_spot_success(key: str, latency_ms: float | None = None) -> None:
    with _health_lock:
        h = _h(key)
        h["ok"] = True
        h["spot_fails"] = 0
        h["last_ok"] = datetime.now().isoformat(timespec="seconds")
        h["latency_ms"] = (
            round(latency_ms, 1) if latency_ms is not None else h["latency_ms"]
        )
        h["last_error"] = ""
        h["spot_blocked_until"] = 0.0
        # 病态慢源：成功但耗时过长（如 akshare 内部 EM→TX 兜底 ~57s）→ 短期降级，
        # 让调度优先走更快源；恢复探测（8s 内 EM 单请求）通过后自动恢复。
        if latency_ms is not None and latency_ms > SLOW_LATENCY_MS:
            h["slow_until"] = time.time() + SLOW_COOLDOWN_S
            logger.warning(
                "数据源 %s 快照耗时 %.0fs（>%ds），降级 %ds",
                key,
                latency_ms / 1000,
                SLOW_LATENCY_MS / 1000,
                SLOW_COOLDOWN_S,
            )
        else:
            h["slow_until"] = 0.0


def record_spot_failure(key: str, error: str) -> None:
    with _health_lock:
        h = _h(key)
        h["ok"] = False
        h["spot_fails"] += 1
        h["last_fail"] = datetime.now().isoformat(timespec="seconds")
        h["last_error"] = error[:200]
        cooldown = min(60 * (2 ** max(0, h["spot_fails"] - 1)), 600)
        h["spot_blocked_until"] = time.time() + cooldown
        logger.warning(
            "数据源 %s 快照失败(第%d次)，冷却 %ds: %s",
            key,
            h["spot_fails"],
            cooldown,
            error[:120],
        )


def record_kline_success(key: str) -> None:
    with _health_lock:
        _h(key)["kline_fails"] = 0
        _h(key)["kline_blocked_until"] = 0.0


def record_kline_failure(key: str, error: str) -> None:
    with _health_lock:
        h = _h(key)
        h["kline_fails"] += 1
        if h["kline_fails"] >= 5:
            h["kline_blocked_until"] = time.time() + 120
            h["last_error"] = error[:200]
            logger.warning(
                "数据源 %s K线连续失败 %d 次，kline 冷却 120s: %s",
                key,
                h["kline_fails"],
                error[:100],
            )


def is_spot_blocked(key: str) -> bool:
    with _health_lock:
        now = time.time()
        h = _h(key)
        return now < h["spot_blocked_until"] or now < h["slow_until"]


def is_kline_blocked(key: str) -> bool:
    with _health_lock:
        return time.time() < _h(key)["kline_blocked_until"]


def spot_candidates() -> list[str]:
    return [k for k in preference_order() if not is_spot_blocked(k)]


def kline_candidates() -> list[str]:
    # 快照已冷却（网络级故障）的源不参与 K 线候选：避免每只股票都走其内部重试超时
    return [
        k
        for k in preference_order()
        if not is_kline_blocked(k) and not is_spot_blocked(k)
    ]


def network_down() -> bool:
    """全部快照源均处于冷却/降级 → 网络层不可达（离线状态）。"""
    return not spot_candidates()


# ---------- 并发竞速（P1-66：双通道 first-success） ----------

# 新闻通道健康：新浪滚动新闻按股票名过滤命中率低，连续空命中进入短冷却。
NEWS_MISS_STREAK = 3  # 连续 0 命中次数阈值
NEWS_COOLDOWN_S = 120  # 触发后的冷却时长

_news_misses: dict[str, int] = {}
_news_blocked_until: dict[str, float] = {}


def record_news_miss(key: str) -> None:
    """新闻源一次空命中登记；连续 NEWS_MISS_STREAK 次进入短冷却。"""
    with _health_lock:
        n = _news_misses.get(key, 0) + 1
        _news_misses[key] = n
        if n >= NEWS_MISS_STREAK:
            _news_blocked_until[key] = time.time() + NEWS_COOLDOWN_S
            _news_misses[key] = 0
            logger.info(
                "新闻源 %s 连续 %d 次空命中，冷却 %ds",
                key,
                NEWS_MISS_STREAK,
                NEWS_COOLDOWN_S,
            )


def record_news_hit(key: str) -> None:
    with _health_lock:
        _news_misses[key] = 0
        _news_blocked_until.pop(key, None)


def is_news_blocked(key: str) -> bool:
    with _health_lock:
        return time.time() < _news_blocked_until.get(key, 0.0)


def _is_empty_result(r) -> bool:
    """判定结果为「空」：None 或 DataFrame/list 等 len==0。"""
    if r is None:
        return True
    empty_attr = getattr(r, "empty", None)
    if empty_attr is not None:
        try:
            return bool(empty_attr)
        except Exception:
            pass
    try:
        return len(r) == 0
    except TypeError:
        return False


# ---------- 竞速线程池（P1-62：复用，线程有界） ----------

# 并发上限与候选源数量一致（每次竞速最多 3 并发）。旧实现每次竞速新建
# ThreadPoolExecutor（3.9+ 工作线程非 daemon，bpo-39812），源故障期每请求
# 遗留 3 个阻塞线程（~25s 超时）→ 线程爆炸（线程数≈3×并发请求）、拖慢退出。
# 改为模块级单例 daemon 线程池：线程数恒为 _RACE_MAX_WORKERS。
_RACE_MAX_WORKERS = len(SOURCES)


class _RacePool:
    """固定容量 daemon 工作线程池（资源有界，版本无关）。

    ThreadPoolExecutor（3.9+，bpo-39812）工作线程非 daemon 且无 daemon 注入
    入口，故以 queue.Queue + 显式 daemon 线程实现同语义的固定线程池：
    - 并发上限 = 工作线程数（有界；单次竞速最多提交 _RACE_MAX_WORKERS 个任务，
      池空闲时单次竞速仍可全量并发，语义与既有 max_workers=len(candidates) 一致）；
    - Future 复用 concurrent.futures.Future（公开 API），兼容 as_completed /
      add_done_callback / result 用法——竞速语义与返回值约定不变；
    - shutdown(cancel_futures=True)：取消排队任务并唤醒工作线程退出
      （SourceRecovery.stop / lifespan shutdown 时调用，关闭路径可达）。
    """

    def __init__(self, max_workers: int, name: str) -> None:
        self._max_workers = max_workers
        self._threads: list[threading.Thread] = []
        self._tasks: queue.Queue[tuple[Future, Callable[[], object]]] = queue.Queue()
        self._shutdown = False
        for i in range(max_workers):
            t = threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        if self._shutdown:
            raise RuntimeError("cannot schedule new futures after shutdown")
        fut: Future = Future()
        self._tasks.put((fut, lambda: fn(*args, **kwargs)))
        return fut

    def _run(self) -> None:
        while True:
            item = self._tasks.get()
            if item is None:  # shutdown 哨兵 → 工作线程退出
                return
            fut, task = item
            if fut.set_running_or_notify_cancel():
                try:
                    result = task()
                except BaseException as exc:  # 与 ThreadPoolExecutor 语义一致
                    fut.set_exception(exc)
                else:
                    fut.set_result(result)

    def shutdown(self, *, cancel_futures: bool = False, wait: bool = False) -> None:
        """关闭池：取消排队任务，唤醒工作线程退出（幂等，可重入）。"""
        self._shutdown = True
        if cancel_futures:
            # 取消排队未执行的任务（仅进程关闭时调用，done-callback 无人消费）
            while True:
                try:
                    item = self._tasks.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    fut, _ = item
                    fut.cancel()
        for _ in range(self._max_workers):
            self._tasks.put(None)
        if wait:
            for t in self._threads:
                t.join()


_race_pool: _RacePool | None = None
_race_pool_lock = threading.Lock()


def _get_race_pool() -> _RacePool:
    """竞速线程池单例（懒创建）：首次竞速时实例化，此后全部竞速复用。

    线程数恒为 _RACE_MAX_WORKERS，不随请求/竞速次数增长。
    """
    global _race_pool
    if _race_pool is None:
        with _race_pool_lock:
            if _race_pool is None:
                _race_pool = _RacePool(_RACE_MAX_WORKERS, "src-race")
    return _race_pool


def shutdown_race_pool() -> None:
    """关闭竞速线程池（SourceRecovery.stop / lifespan shutdown 时调用）。

    取消排队任务并唤醒工作线程退出；下次竞速会重建新池（幂等、可重入）。
    """
    global _race_pool
    with _race_pool_lock:
        pool = _race_pool
        _race_pool = None
    if pool is not None:
        pool.shutdown(cancel_futures=True, wait=False)


def _race_first_success(
    candidates: list[str],
    fetch,
    record_success,
    record_failure,
    *,
    empty_is_failure: bool,
    active_reason: str,
    errors: list[str] | None,
) -> tuple[str | None, object]:
    """并发竞速 first-success：候选源同时发起 fetch(key)，首个成功返回 (key, result)。

    - 成功：record_success(key, latency_ms) + set_active(key, active_reason)
      （由主线程在首个成功 future 上执行，返回前完成登记）；
    - 失败：经 done-callback 即时登记（record_failure）——即使已有成功者返回、
      慢失败源仍在后台，其冷却登记也不会丢失（既有冷却语义：spot 指数退避 /
      kline 5 次冷却）；
    - 空数据：empty_is_failure=True 时按失败记冷却（spot 空快照），否则仅收集错误摘要；
    - 全部失败：返回 (None, None)，errors 收集各源摘要；
     - manual 锁定：candidates 单元素 → 退化为单源串行，语义与既有实现一致。
     并发仅跨源（每个候选一个任务）；同源内部由 provider 自身锁串行化。
     找到成功者后不等待未完成任务（复用池，慢源在后台自然结束，失败登记走
     done-callback）——first-success 不被慢源拖住。
     线程复用（P1-62）：模块级单例 daemon 池（_get_race_pool，线程数恒为
     _RACE_MAX_WORKERS=len(SOURCES)），不随请求新建线程；每次竞速提交
     ≤len(candidates) 个任务，池空闲时单次竞速仍全量并发（并发上限语义不变）。
    """
    if errors is None:
        errors = []
    if not candidates:
        return None, None
    pool = _get_race_pool()

    def _register_failure(key: str, msg: str) -> None:
        record_failure(key, msg[:200])
        errors.append(f"{key}: {msg[:100]}")

    def _make_cb(key: str):
        """后台登记回调：仅挂给「首个成功返回时仍未完成」的慢源任务。"""

        def _cb(f):
            try:
                result = f.result()
            except Exception as e:
                _register_failure(key, str(e))
                return
            if _is_empty_result(result):
                if empty_is_failure:
                    record_failure(key, "返回空数据")
                errors.append(f"{key}: 返回空数据")

        return _cb

    fut_meta = {}
    for k in candidates:
        fut = pool.submit(fetch, k)
        fut_meta[fut] = (k, time.time())
    # 失败/空数据登记由主线程在 as_completed 循环内同步完成（返回前必然落盘，
    # 无 worker 回调竞态）；仅命中首个成功、提前返回时，为未完成慢源挂后台回调，
    # 其冷却登记不丢失（与旧 done-callback 语义一致）。复用池不可 shutdown：
    # 未完成任务留在池内后台自然结束。
    for fut in as_completed(fut_meta):
        key, t0 = fut_meta[fut]
        try:
            result = fut.result()
        except Exception as e:
            _register_failure(key, str(e))
            continue
        if _is_empty_result(result):
            if empty_is_failure:
                record_failure(key, "返回空数据")
            errors.append(f"{key}: 返回空数据")
            continue
        # 命中首个成功：返回前给未完成任务挂后台失败登记回调（慢源不拖慢返回）
        for f2, (k2, _t2) in fut_meta.items():
            if not f2.done():
                f2.add_done_callback(_make_cb(k2))
        record_success(key, (time.time() - t0) * 1000)
        set_active(key, active_reason)
        return key, result
    return None, None


def spot_first_success(fetch, errors: list[str] | None = None):
    """spot 通道并发竞速：对未冷却候选源并发发起 fetch(key)，首个成功即返回并
    set_active（记录源）；失败源即时记冷却（指数退避）；全部失败返回 (None, None)。
    manual 锁定模式退化为单源串行，语义不变。"""
    return _race_first_success(
        spot_candidates(),
        fetch,
        record_spot_success,
        record_spot_failure,
        empty_is_failure=True,  # spot 空快照按失败冷却（与既有串行 raise 语义一致）
        active_reason="自动切换（spot 竞速成功）",
        errors=errors,
    )


def kline_first_success(fetch, errors: list[str] | None = None):
    """kline 通道并发竞速：对未冷却候选源并发发起 fetch(key)，首个成功即返回并
    set_active（记录源）；异常即时记 kline 冷却（连续 5 次才触发 120s，避免单只
    股票缺失拖垮整源）；空数据不记冷却（个股无数据≠源故障）。全部失败返回
    (None, None)。manual 锁定模式退化为单源串行，语义不变。"""
    return _race_first_success(
        kline_candidates(),
        fetch,
        lambda k, lat: record_kline_success(k),
        record_kline_failure,
        empty_is_failure=False,  # K 线空数据不记冷却（与既有串行语义一致）
        active_reason="自动切换（K线竞速成功）",
        errors=errors,
    )


# ---------- 活跃源 ----------


def active_key() -> str | None:
    with _health_lock:
        return _active


def set_active(key: str, reason: str) -> None:
    global _active
    with _health_lock:
        changed = _active != key
        _active = key
    if changed:
        with _state_lock:
            _state["last_switch_ts"] = datetime.now().isoformat(timespec="seconds")
            _state["last_switch_reason"] = reason[:120]
            _save_state()
        logger.info("数据源切换: %s（%s）", key, reason[:80])


# ---------- 状态 ----------


def status() -> dict:
    now = time.time()
    # preference_order 自身会取 _state_lock（不可重入），须在取锁前算好
    pref = preference_order()
    with _health_lock, _state_lock:
        sources = []
        for s in SOURCES:
            k = s["key"]
            h = _h(k)
            blocked = now < h["spot_blocked_until"]
            sources.append(
                {
                    "key": k,
                    "name": s["name"],
                    "ok": bool(h["ok"]),
                    "active": k == _active,
                    "latency_ms": h["latency_ms"],
                    "spot_fails": h["spot_fails"],
                    "spot_blocked": blocked,
                    "blocked_until": h["spot_blocked_until"] if blocked else None,
                    "degraded": now < h["slow_until"],
                    "kline_blocked": now < h["kline_blocked_until"],
                    "last_ok": h["last_ok"],
                    "last_fail": h["last_fail"],
                    "last_error": h["last_error"],
                }
            )
        return {
            "mode": _state["mode"],
            "override": _state["override"],
            "active": _active,
            "preference": pref,
            "last_switch_ts": _state["last_switch_ts"],
            "last_switch_reason": _state["last_switch_reason"],
            "sources": sources,
        }


# ---------- 后台恢复探测 ----------


def start_recovery() -> None:
    """后台 daemon 线程：auto 模式下当前源不是首选源时，每 60s 快速探测首选源。

    可控启停（组件化）：_recovery_stop 信号置位后线程在下一个等待周期退出，
    不再「import 即启动」。重复调用幂等（仅启动一次）。
    """
    global _recovery_started
    if _recovery_started:
        return
    _recovery_started = True
    _recovery_stop.clear()

    def loop():
        while not _recovery_stop.wait(60):
            try:
                if get_mode() != "auto":
                    continue
                pref = preference_order()
                if not pref or active_key() == pref[0]:
                    continue
                key = pref[0]
                if probe_source(key, timeout=8):
                    record_spot_success(key)
                    set_active(key, "恢复探测成功，自动切回首选源")
                else:
                    record_spot_failure(key, "恢复探测失败")
            except Exception as e:
                logger.debug("恢复探测异常: %s", str(e)[:100])

    threading.Thread(target=loop, daemon=True, name="source-recovery").start()


def stop_recovery() -> None:
    """停止后台恢复探测线程（组件 stop 调用；daemon 线程最多一个周期内退出）。"""
    _recovery_stop.set()


# ============================================================================
# quote 门面：provider 缓存 / 快照回退 / 门面
# ============================================================================

# ---------- 数据源调度（自动切换 / 手动覆盖） ----------

_instances: dict[str, QuoteProvider] = {}
_provider_lock = threading.Lock()

# apply_industry 结果缓存：apply_industry 对全市场快照做整表 copy + 逐行行业映射
# （数千行 Python 循环），get_spot 每请求（搜索/自选/雷达轮询）都调一次是明显浪费。
# 与 provider 快照 TTL 同口径缓存，refresh=True（强制刷新快照）时重新应用。
_spot_industry_cache: pd.DataFrame | None = None
_spot_industry_ts = 0.0

_PROVIDER_CLASSES = {
    "akshare": AkshareProvider,
    "sina": SinaProvider,
    "tencent": TencentProvider,
}


def _instance(key: str) -> QuoteProvider:
    with _provider_lock:
        p = _instances.get(key)
        if p is None:
            cls = _PROVIDER_CLASSES[key]
            p = cls()
            _instances[key] = p
        return p


def get_provider() -> QuoteProvider:
    """返回当前生效数据源实例（懒创建、不探测网络）。仅供 .name / 兼容调用。"""
    key = active_key() or (preference_order() or ["akshare"])[0]
    return _instance(key)


def reset_provider():
    """清空实例缓存并清除全部缓存（手动切换数据源后调用）。"""
    global _spot_industry_cache, _spot_industry_ts
    with _provider_lock:
        _instances.clear()
        _spot_industry_cache = None  # 源切换后旧快照的 industry 映射一并失效
        _spot_industry_ts = 0.0
    set_active("", "reset")
    from ..storage.cache import clear_all

    clear_all()


def get_spot(refresh: bool = False) -> pd.DataFrame:
    """取快照：spot 通道并发竞速（P1-66），首个成功源即生效并 set_active。

    候选源（排除冷却者）同时发起，first-success 立即返回——慢源（如 akshare
    内部 EM→TX 兜底 ~57s）不再阻塞成功路径，由慢源判定（>25s）自动短期降级。
    全部失败时按优先级尝试各源磁盘离线快照兜底，仍无则抛错。
    """
    global _spot_industry_cache, _spot_industry_ts  # 函数内赋值必须声明 global

    errors: list[str] = []
    key, df = spot_first_success(
        lambda k: _instance(k).get_spot(refresh=refresh), errors=errors
    )
    if df is None:
        # 全部候选冷却或失败：磁盘离线快照兜底
        for k in preference_order():
            disk = load_snapshot_disk(k)
            if disk is not None:
                _quote_logger.warning(
                    "全部源不可用，使用 %s 磁盘离线快照（%d 只）", k, len(disk)
                )
                return disk
            errors.append(f"{k}: 不可用且无离线快照")
        raise RuntimeError(
            "全部数据源不可用（" + " | ".join(errors) + "）—— 请在数据连接页查看状态"
        )
    # 行业分类映射（惰性导入，避免与 industry 模块循环依赖；apply 为幂等拷贝）。
    # TTL 内复用已映射结果，避免每请求全市场 copy + 逐行映射（见 _spot_industry_cache）。
    # TTL 与 provider 快照同口径（session_spot_ttl：盘中 90s / 非交易时段放大），
    # 避免固定基础 TTL 与快照 TTL 不同步导致重复映射或映射滞后。
    from ..lib.session import session_spot_ttl

    ttl = session_spot_ttl(settings.quote_cache_ttl)
    now = time.time()
    with _provider_lock:
        if (
            not refresh
            and _spot_industry_cache is not None
            and now - _spot_industry_ts < ttl
        ):
            return _spot_industry_cache
        from ..storage.industry import apply_industry

        applied = apply_industry(df)
        _spot_industry_cache = applied
        _spot_industry_ts = now
        return applied


def get_indices(refresh: bool = False) -> pd.DataFrame:
    """全球核心指数快照门面（T-73）。

    转发 akshare provider：内部东财 index_global_spot_em 主源（一次全量 8 只）→
    新浪分市场降级（A 股/港股/美股 hq 实时），per-market TTL 缓存（lib.session
    index_spot_ttl，交易时段 90s / 非交易 ×30）+ 市场级失败冷却 120s。

    指数通道独立于 spot 竞速（单源实现，sina/tencent 未实现指数接口）：
    失败不登记 spot 冷却（指数接口失败不代表全市场 spot 不可用），仅记 warning。
    返回固定 8 行内部列（code/name/market/price/pct_change/currency/updated_at），
    市场级失败保留旧缓存、无缓存该市场行空值——全部市场无数据时由 API 层判 503。
    """
    try:
        return _instance("akshare").get_indices(refresh=refresh)
    except Exception as e:
        _quote_logger.warning("指数快照失败: %s", str(e)[:120])
        raise


def get_kline(
    code: str,
    period: str = "daily",
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    compose_intraday: bool = False,
) -> pd.DataFrame:
    """取 K 线：kline 通道并发竞速（P1-66），首个成功源即生效并 set_active。

    候选源（排除 spot/kline 冷却者）同时发起，first-success 立即返回——慢源
    不再阻塞成功路径。异常按 kline 冷却语义登记（连续 5 次才冷却 120s，避免
    单只股票缺失拖垮整源）；空数据不记冷却。

    compose_intraday=True 时,新浪日K盘中缺当日 bar 会用当日 1 分钟线合成
    (仅 sina 源实现,其他源透传忽略);收盘后由真实数据覆盖。
    """
    errors: list[str] = []
    key, df = kline_first_success(
        lambda k: _instance(k).get_kline(
            code,
            period,
            start_date=start_date,
            end_date=end_date,
            days=days,
            compose_intraday=compose_intraday,
        ),
        errors=errors,
    )
    if df is not None:
        # P1-62：成交量单位校验上提到门面（provider 内部不再校验，H1b）
        return _normalize_last_bar_volume(df, code)
    raise RuntimeError("K线获取失败: " + " | ".join(errors))


# 财报候选（纳入 sources 管理，P1-66 扩展）。financials 候选=[sina, akshare]：
# sina=新浪财务摘要通道（SinaFinancialsMixin 转 akshare 新浪接口），akshare=akshare
# 直接封装的新浪接口（stock_financial_abstract），双入口容错（同一底层通道）；
# earnings 候选=[东财, akshare]：东财业绩报表（stock_yjbb_em）独占，候选列表为
# 将来备用通道保留（当前仅 akshare provider 实现）。
FINANCIALS_CANDIDATES = ["sina", "akshare"]
EARNINGS_CANDIDATES = ["akshare"]

# 新闻候选（P1-63）：akshare(东财个股新闻) 固定优先 → sina(滚动过滤兜底) →
# tencent（无 get_news 实现，自动跳过）。独立于行情冷却通道。
NEWS_CANDIDATES = ["akshare", "sina", "tencent"]


def get_news(code: str, limit: int = 10) -> list[dict]:
    """取新闻：候选 [akshare(东财个股), sina(滚动过滤), tencent(未实现→跳过)]。

    P1-63 修正：东财固定优先（个股级新闻，命中率高）；新浪滚动新闻按股票名
    过滤命中率低，仅作兜底且连续空命中进入短冷却（record_news_miss），
    不再因行情源冷却（kline_candidates）被连带启用/跳过——新闻是独立通道。
    """
    for key in NEWS_CANDIDATES:
        if is_news_blocked(key):
            continue
        getter = getattr(_instance(key), "get_news", None)
        if getter is None:
            continue
        try:
            items = getter(code, limit)
        except Exception as e:
            _quote_logger.debug("新闻源 %s 失败 %s: %s", key, code, str(e)[:80])
            continue
        if items:
            record_news_hit(key)
            return items
        # 空命中：新浪滚动过滤 0 命中属常态 → 计数进冷却（防反复白打）；
        # 东财为空 = 该股确无新闻（精确接口），不算 miss。
        if key == "sina":
            record_news_miss(key)
    return []


def get_earnings(code: str) -> dict | None:
    """公开门面：业绩报表最新一期（东财 stock_yjbb_em，候选 [东财, akshare]）。

    纳入 sources 管理：候选按序尝试，异常按 kline 冷却语义登记（连续 5 次才
    冷却，个股缺失不拖垮整源）；空数据（该股无业绩记录）不记冷却。API 层勿
    直接触碰 _instance。
    """
    for key in EARNINGS_CANDIDATES:
        if is_kline_blocked(key) and is_spot_blocked(key):
            continue
        try:
            data = _instance(key).get_earnings(code)
        except Exception as e:
            record_kline_failure(key, f"earnings: {str(e)[:120]}")
            _quote_logger.warning("业绩报表 %s 失败 %s: %s", key, code, str(e)[:80])
            continue
        if data:
            return data
    return None


def get_financials(code: str) -> dict | None:
    """公开门面：财务摘要（候选 [sina, akshare]，最近两期关键指标）。

    纳入 sources 管理：候选按序尝试，异常按 kline 冷却语义登记并降级到下一
    候选；空数据（该股无财务摘要）直接返回（不试下一候选——两候选同底层新浪
    通道，空即无数据）。API 层勿直接触碰 _instance。
    """
    for key in FINANCIALS_CANDIDATES:
        if is_kline_blocked(key) and is_spot_blocked(key):
            continue
        try:
            data = _instance(key).get_financials(code)
        except Exception as e:
            record_kline_failure(key, f"financials: {str(e)[:120]}")
            _quote_logger.warning("财务摘要 %s 失败 %s: %s", key, code, str(e)[:80])
            continue
        if data:
            return data
        return None  # 空数据 = 无财务摘要（双候选同底层通道，无降级意义）
    return None


def get_financial_history(code: str, report_type: str) -> list[dict]:
    """公开门面：财务三表历史（东财 by_report_em，按报告期全量）。

    T-06 新增：单源（akshare 东财）抓取；失败降级返回 []（库缓存兜底由 API 层
    承担），仅按 kline 冷却语义登记（连续 5 次才冷却，个股缺失不拖垮整源）。
    API 层勿直接触碰 provider。
    """
    from ..storage.providers.financials import fetch_financials

    try:
        return fetch_financials(code, report_type) or []
    except Exception as e:
        record_kline_failure("akshare", f"financial_history: {str(e)[:120]}")
        _quote_logger.warning("财务历史 %s 失败 %s: %s", report_type, code, str(e)[:80])
        return []


def get_valuation_history(code: str) -> list[dict]:
    """公开门面：每日估值序列（东财 stock_value_em）。

    T-06 新增：失败降级返回 []（库缓存兜底由 API 层承担），语义同 get_financial_history。
    """
    from ..storage.providers.financials import fetch_valuation_history

    try:
        return fetch_valuation_history(code) or []
    except Exception as e:
        record_kline_failure("akshare", f"valuation_history: {str(e)[:120]}")
        _quote_logger.warning("估值历史 %s 失败: %s", code, str(e)[:80])
        return []


def get_capital_data(
    code: str, ctype: str, start_date: str = "", end_date: str = ""
) -> list[dict]:
    """公开门面：资金类数据抓取（T-08 资金流/龙虎榜/两融/北向历史持股）。

    单源（akshare 东财/交易所）抓取；失败降级返回 []（库缓存兜底由 API 层
    承担），仅按 kline 冷却语义登记（连续 5 次才冷却，个股缺失不拖垮整源）。
    北向接口网络失败返回 None（调用方据此判定「接口失效 → 503」）。
    API 层勿直接触碰 provider。
    """
    from ..storage.providers.capital import (
        fetch_lhb,
        fetch_margin,
        fetch_moneyflow,
        fetch_northbound,
    )

    fetch_map = {
        "moneyflow": lambda: fetch_moneyflow(code),
        "lhb": lambda: fetch_lhb(code, start_date, end_date),
        "margin": lambda: fetch_margin(code, start_date, end_date),
        "northbound": lambda: fetch_northbound(code),
    }
    fn = fetch_map.get(ctype)
    if fn is None:
        return []
    try:
        # northbound 网络失败返回 None(调用方据此判定「接口失效 → 503」)，
        # 其余类型失败统一降级为 []（库缓存兜底）。
        return fn()
    except Exception as e:
        record_kline_failure("akshare", f"capital_{ctype}: {str(e)[:120]}")
        _quote_logger.warning("资金 %s %s 失败: %s", ctype, code, str(e)[:80])
        return [] if ctype != "northbound" else None


def get_sina_kline(
    code: str,
    period: str = "daily",
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """公开门面：固定新浪源取 K 线（指数基准等需固定源的场景，无源切换副作用）。

    转发 _instance("sina").get_kline；成交量校验统一在本门面收尾（H1b）。
    API 层勿直接触碰 _instance。
    """
    df = _instance("sina").get_kline(
        code, period, start_date=start_date, end_date=end_date, days=days
    )
    return _normalize_last_bar_volume(df, code)


def probe_source(key: str, timeout: float = 8.0) -> bool:
    """快速探测单个源（单请求、短超时），供恢复线程/状态页使用。"""
    try:
        if key == "akshare":
            import akshare as ak

            # 轻量探测：单只股票近 30 天日线（与生产 K 线同一东财 hist 通道，
            # 代表性一致且秒级返回）。原实现拉全市场快照（常态 25s+），8s 超时
            # 下必失败 → 降级源永久不恢复
            today = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
            raw = _ak_call(
                lambda: ak.stock_zh_a_hist(
                    symbol="600519",
                    period="daily",
                    start_date=start,
                    end_date=today,
                    adjust="",
                    timeout=timeout,
                ),
                timeout=timeout,
            )
            return raw is not None and len(raw) > 0
        if key == "sina":
            p = _instance(key)
            rows = p._fetch_page(1, num=5, retries=0)  # type: ignore[attr-defined]
            return bool(rows)
        if key == "tencent":
            import requests as _rq

            r = _rq.get("https://qt.gtimg.cn/q=sh600519", timeout=timeout)
            return r.status_code == 200 and '="' in r.text
    except Exception as e:
        _quote_logger.debug("探测 %s 失败: %s", key, str(e)[:100])
    return False


# ---------- 成交量单位运行时校验（P1-62） ----------


def _spot_volume_of(code: str) -> float | None:
    """取该股最近内存 spot 快照的成交量（手）：遍历 provider 单例的内存缓存，
    无缓存/缺失/非正值返回 None（调用方跳过校验）。"""
    bare = _bare(code)
    for inst in list(_instances.values()):
        cache = getattr(inst, "_spot_cache", None)
        df = cache.get("all") if isinstance(cache, dict) else cache
        if df is None or df.empty or "volume" not in df.columns:
            continue
        try:
            row = df[df["code"].astype(str).str.startswith(bare + ".")]
            if row.empty:
                continue
            v = float(row.iloc[0]["volume"])
            if v > 0:
                return v
        except Exception:
            continue
    return None


def _normalize_last_bar_volume(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """日线最后一根若为当日 bar → 与 spot 快照比对修正 volume 单位（手）。

    仅比对「当日」bar：历史 bar 成交量是历史总量，与今日 spot 累计不在同一
    时间基准，不可比（避免把历史 bar 误改）。校验失败/不可比时原样返回。
    """
    if df is None or not len(df) or "volume" not in df.columns:
        return df
    try:
        from ..lib.session import now_cn

        today = now_cn().date().isoformat()
        if str(df["date"].iloc[-1]) != today:
            return df
        spot_vol = _spot_volume_of(code)
        if spot_vol is None:
            return df
        last = df.index[-1]
        df.loc[last, "volume"] = _normalize_volume(
            float(df["volume"].iloc[last]), spot_vol
        )
    except Exception as e:
        _quote_logger.debug("成交量单位校验跳过 %s: %s", code, str(e)[:80])
    return df


# ---------- 组件化（bt-runtime 注册） ----------


class SourceRecovery:
    """数据源恢复组件：加载持久化状态 + 启动/停止后台恢复探测线程。

    消除旧 quote.py 底部「import 即 load_state()+start_recovery()」的导入
    副作用——现由 bt-runtime 组件注册表显式 start() 启动。
    """

    name = "sources"
    depends: frozenset[str] = frozenset()

    def start(self) -> None:
        load_state()
        start_recovery()

    def stop(self) -> None:
        stop_recovery()
        # 竞速线程池关闭（取消排队任务并唤醒 daemon 工作线程退出，幂等可重入；
        # 下次竞速自动重建新池）。进程退出场景下 daemon 线程本不会阻塞退出，
        # 此处显式关闭以回收资源。
        shutdown_race_pool()

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": _recovery_started and not _recovery_stop.is_set(),
            **status(),
        }
