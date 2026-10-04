"""沪深300 成分股：中证指数官方 → 新浪 → 快照成交额 Top300 三级并发降级。

结果 24h 内存缓存 + 落盘 backend/cache/hs300_codes.json，启动后可复用。

（bt-fin 迁移：由原 hs300.py 模块全文迁入；缓存路径走 storage.paths.HS300_FILE，
网络工具 _ak_call 经 .providers.base 直连，快照兜底 get_spot 经 bind_spot_getter
依赖注入，未注入时抛错（storage 不反向依赖 core，H1c 同款）。）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from ..lib.codes import normalize_code as _with_suffix
from .paths import HS300_FILE

logger = logging.getLogger("stockradar.hs300")

CACHE_FILE = str(HS300_FILE)
TTL = 24 * 3600  # 24h

_mem_codes: list[str] | None = None
_mem_ts = 0.0
_lock = threading.Lock()

# 快照 Top300 兜底需要「当前全市场快照」能力，由编排层(core)注入，storage 不反向
# 依赖 core（消除 storage→core 反向依赖）；未注入时抛错（P1-53a:禁止惰性反向
# import 兜底，与 klines 的 bind_kline_source 同契约）。
_spot_getter = None  # callable() -> DataFrame（全市场快照）


def bind_spot_getter(fn) -> None:
    global _spot_getter
    _spot_getter = fn


def _resolve_spot():
    if _spot_getter is None:
        raise RuntimeError("快照兜底能力未绑定：请先调用 bind_spot_getter 注入")
    return _spot_getter


def _load_disk() -> tuple[list[str] | None, float]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        return blob.get("codes"), blob.get("ts", 0)
    except Exception:
        return None, 0


def _save_disk(codes: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "codes": codes}, f)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logger.warning("HS300 成分缓存落盘失败: %s", str(e)[:100])


def _fetch_akshare() -> list[str]:
    """1. 中证指数官方成分（index_stock_cons_csindex，列「成分券代码」）。"""
    import akshare as ak
    from .providers.base import _ak_call

    raw = _ak_call(lambda: ak.index_stock_cons_csindex(symbol="000300"))
    if raw is None or raw.empty:
        raise RuntimeError("csindex 空数据")
    col = "成分券代码" if "成分券代码" in raw.columns else raw.columns[0]
    codes = [str(x).strip().zfill(6) for x in raw[col].tolist()]
    if not codes:
        raise RuntimeError("csindex 无成分代码")
    return [_with_suffix(c) for c in codes]


def _fetch_sina() -> list[str]:
    """2. 新浪源（index_stock_cons，列「品种代码」）。"""
    import akshare as ak
    from .providers.base import _ak_call

    raw = _ak_call(lambda: ak.index_stock_cons(symbol="000300"))
    if raw is None or raw.empty:
        raise RuntimeError("sina 成分空数据")
    col = "品种代码" if "品种代码" in raw.columns else raw.columns[0]
    codes = [str(x).strip().zfill(6) for x in raw[col].tolist()]
    if not codes:
        raise RuntimeError("sina 无成分代码")
    return [_with_suffix(c) for c in codes]


def _fetch_spot_top() -> list[str]:
    """3. 兜底：当前快照按成交额取前 300。

    P2-56：amount 列缺失（异常/降级快照）走显式退化——按快照原序取前 300，
    无金额语义并打 warning，不再 KeyError；code 列缺失属极端异常（快照必有
    该列），明确报错走外层降级链。
    """
    spot = _resolve_spot()()
    if spot is None or spot.empty:
        raise RuntimeError("快照为空")
    if "code" not in spot.columns:
        raise RuntimeError("快照缺 code 列")
    if "amount" not in spot.columns:
        logger.warning("快照缺 amount 列，快照Top300 退化为原序前 300 行（无金额语义）")
        return spot["code"].head(300).tolist()
    return spot.sort_values("amount", ascending=False)["code"].head(300).tolist()


def _fetch_concurrent() -> list[str]:
    """三级数据源并发降级（P2-34）：三个源同时发起，按优先级取首个成功。

    保留既有降级语义（中证指数 > 新浪 > 快照Top300，优先级不变）：并发仅消除
    每级 25s 硬超时串行叠加（最坏 ~75s → ~25s 墙钟），命中高优源即返回、其余
    后台自然结束（结果丢弃）。各源用独立 daemon 线程执行（与 _ak_call 同款
    线程语义，不阻塞进程退出）。
    """
    from concurrent.futures import Future

    global _mem_codes, _mem_ts
    last_err: Exception | None = None
    pending: list[tuple[str, Future]] = []
    for name, fn in (
        ("中证指数", _fetch_akshare),
        ("新浪", _fetch_sina),
        ("快照Top300", _fetch_spot_top),
    ):
        fut: Future = Future()

        def _run(fn=fn, fut=fut):
            try:
                fut.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - 与 _ak_call 线程语义一致
                fut.set_exception(exc)

        threading.Thread(target=_run, daemon=True, name=f"hs300-{name}").start()
        pending.append((name, fut))
    for name, fut in pending:
        try:
            codes = fut.result()
        except Exception as e:
            last_err = e
            logger.warning("HS300 成分股 %s 源失败: %s", name, str(e)[:120])
            continue
        if not codes:
            last_err = RuntimeError(f"{name} 返回空列表")
            logger.warning("HS300 成分股 %s 源失败: %s", name, str(last_err)[:120])
            continue
        with _lock:
            _mem_codes, _mem_ts = codes, time.time()
        _save_disk(codes)
        logger.info("HS300 成分股: %s 源（%d 只）", name, len(codes))
        return codes
    raise RuntimeError(f"HS300 成分股全部数据源失败: {str(last_err)[:120]}")


def get_hs300_codes(refresh: bool = False) -> list[str]:
    """沪深300 成分股（带后缀 .SH/.SZ）。三级并发降级 + 24h 内存/磁盘缓存。"""
    global _mem_codes, _mem_ts
    now = time.time()
    with _lock:
        if not refresh and _mem_codes and now - _mem_ts < TTL:
            return list(_mem_codes)
    if not refresh:
        disk_codes, disk_ts = _load_disk()
        if disk_codes and now - disk_ts < TTL:
            with _lock:
                _mem_codes, _mem_ts = disk_codes, now
            logger.info("HS300 成分股从磁盘缓存加载（%d 只）", len(disk_codes))
            return list(disk_codes)
    return _fetch_concurrent()
