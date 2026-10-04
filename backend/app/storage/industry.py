"""行业分类映射：akshare 东财行业板块 → 全市场 bare code → 行业名。

数据源: ak.stock_board_industry_name_em()（板块列表）+ ak.stock_board_industry_cons_em()（板块成分）。
构建在后台 daemon 线程执行，绝不在调用路径上阻塞；磁盘缓存 TTL 24h。

（bt-fin 迁移：原 industry.py 全文迁入本模块；
缓存路径走 storage.paths.INDUSTRY_FILE，网络工具 _ak_call 经 .providers.base 直连。）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from ..lib.codes import bare_code as _bare
from .providers.base import _ak_call
from .paths import INDUSTRY_FILE

logger = logging.getLogger("stockradar.industry")

_STATE_DIR = INDUSTRY_FILE.parent
_CACHE_PATH = INDUSTRY_FILE
CACHE_TTL = 24 * 3600  # 行业归属变化极低频，24h 缓存足够

_BUILD_LOCK = threading.Lock()
_READ_LOCK = threading.Lock()
_BUILDING = False

# 测试钩子：可注入 {"ts": <epoch>, "map": {...}}；None 表示尚未初始化
_INDUSTRY_CACHE: dict | None = None


def _load_disk() -> dict | None:
    """读磁盘缓存；缺失/损坏/过期返回 None。"""
    try:
        if not _CACHE_PATH.exists():
            return None
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "map" not in data:
            return None
        if time.time() - float(data.get("ts", 0)) > CACHE_TTL:
            return None
        return data
    except Exception as e:
        logger.debug("行业映射磁盘缓存读取失败: %s", str(e)[:80])
        return None


def _save_disk(data: dict) -> None:
    """原子写盘：先写 tmp 再 replace。"""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as e:
        logger.warning("行业映射落盘失败: %s", str(e)[:80])


def _fetch_board_cons(board: str) -> dict[str, str]:
    """单个板块成分 → {bare code: 板块名}；失败抛错由上层捕获跳过。"""
    import akshare as ak

    raw = _ak_call(lambda: ak.stock_board_industry_cons_em(symbol=board), timeout=15)
    if raw is None or raw.empty:
        return {}
    codes = raw["代码"].astype(str).str.strip()
    return {c: board for c in codes.tolist() if c}


def _fetch_industries() -> dict[str, str]:
    """全市场 板块名列表 → 并发拉成分 → {bare code: 板块名}（仅后台线程调用）。"""
    import akshare as ak

    try:
        boards = _ak_call(lambda: ak.stock_board_industry_name_em(), timeout=15)
    except Exception as e:
        logger.warning("行业板块列表获取失败: %s", str(e)[:100])
        return {}
    if boards is None or boards.empty or "板块名称" not in boards.columns:
        logger.warning("行业板块列表为空或结构异常")
        return {}
    names = [str(x) for x in boards["板块名称"].tolist() if str(x)]
    mapping: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_board_cons, name): name for name in names}
        for fut in as_completed(futures):
            try:
                cons = fut.result()
            except Exception as e:
                logger.debug("板块成分获取失败 %s: %s", futures[fut], str(e)[:80])
                continue
            if cons:
                mapping.update(cons)
    return mapping


def _build_worker() -> None:
    global _BUILDING
    try:
        t0 = time.time()
        mapping = _fetch_industries()
        if mapping:
            data = {"ts": time.time(), "map": mapping}
            _save_disk(data)
            with _READ_LOCK:
                _INDUSTRY_CACHE = data
            logger.info(
                "行业映射构建完成: %d 只股票, %.1fs", len(mapping), time.time() - t0
            )
        else:
            logger.warning("行业映射构建结果为空，保留原缓存")
    except Exception as e:
        logger.warning("行业映射构建失败: %s", str(e)[:120])
    finally:
        with _BUILD_LOCK:
            _BUILDING = False


def _start_build() -> None:
    """启动后台构建（daemon 线程；线程级互斥，同一时间仅一个构建任务）。"""
    global _BUILDING
    with _BUILD_LOCK:
        if _BUILDING:
            return
        _BUILDING = True
    t = threading.Thread(target=_build_worker, name="industry-map-build", daemon=True)
    t.start()


def get_industry_map() -> dict[str, str]:
    """bare code（如 "600519"）→ 行业名（如 "白酒"）。

    磁盘缓存有效直接返回；缓存缺失/过期时启动后台构建并返回当前已有映射
    （可能为空 dict），绝不在调用路径上发起网络请求。过期状态每次调用都会
    重读磁盘并重试后台构建——不因单次构建失败把空值（ts=0）永久留在内存
    （P1-22 修复：修复前磁盘过期后一次构建失败 → 永不再读盘/重建）。
    """
    global _INDUSTRY_CACHE
    with _READ_LOCK:
        cache = _INDUSTRY_CACHE
        stale = cache is None or time.time() - float(cache.get("ts", 0)) > CACHE_TTL
        if stale:
            disk = _load_disk()
            if disk is not None:
                _INDUSTRY_CACHE = disk
            else:
                if cache is None:
                    # 首启无缓存：置空占位（ts=0 恒过期），后续调用自动重试
                    _INDUSTRY_CACHE = {"ts": 0.0, "map": {}}
                _start_build()
        return _INDUSTRY_CACHE["map"]


def apply_industry(df: pd.DataFrame) -> pd.DataFrame:
    """内部列快照 df（含 code，形如 "600519.SH"）→ 副本，industry 列填行业名。

    取 bare code 查映射，查不到保留原值；不修改入参 df，不写回 provider 缓存。
    """
    if df is None or df.empty or "code" not in df.columns:
        return df.copy() if df is not None else df
    mapping = get_industry_map()
    if not mapping:
        return df.copy()
    out = df.copy()
    if "industry" not in out.columns:
        out["industry"] = ""
    bare = out["code"].astype(str).map(_bare)
    out["industry"] = [
        mapping.get(b, orig) for b, orig in zip(bare.tolist(), out["industry"].tolist())
    ]
    return out
