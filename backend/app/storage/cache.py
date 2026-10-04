"""通用 TTL 缓存：指标/面板/风险/调优 结果复用，减少历史数据反复读取。

（bt-fin 迁移：原 cache.py 全文迁入本模块。）
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import sys
import time
from collections import OrderedDict
from threading import Lock, Thread

from ..config import settings
from .paths import CACHE_DIR

_PERSIST_DIR = str(CACHE_DIR)


def _estimate_size(val) -> int:
    """近似内存字节估算（P2-41）：numpy 数组按 nbytes（float64 面板 8B/元素即真实占用），
    容器递归累加成员，其余对象走 sys.getsizeof 兜底。数量级上界即可防膨胀，不需精确。"""
    if isinstance(val, dict):
        return sum(_estimate_size(k) + _estimate_size(v) for k, v in val.items())
    if isinstance(val, (list, tuple, set, frozenset)):
        return sum(_estimate_size(x) for x in val)
    if isinstance(val, str):
        return len(val.encode("utf-8")) + 49
    try:
        import numpy as np

        if isinstance(val, (np.ndarray, np.generic)):
            return val.nbytes
    except Exception:
        pass
    try:
        return sys.getsizeof(val)
    except TypeError:
        return 8


class TTLCache:
    """线程安全 TTL 缓存（时间过期 + 最旧淘汰），可选磁盘后备（重启不丢）。"""

    def __init__(
        self,
        ttl: float,
        maxsize: int = 256,
        persist_key: str | None = None,
        persist_dir: str = _PERSIST_DIR,
        persist_max_bytes: int | None = None,
        max_bytes: int | None = None,
    ):
        # OrderedDict：get/set 均 move_to_end，淘汰走 popitem(last=False) → O(1)，
        # 替代旧 min(_d, key=ts) 全表扫描（P0-1）
        self._d: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._ttl = ttl
        self._max = maxsize
        # P2-41：条目字节估算（max_bytes 配置时启用，条目数 + 字节双上限；
        # 与 _d 同序维护，淘汰时同步剔除，读取路径直接返回 _d 值不受影响）
        self._max_bytes = max_bytes
        self._bytes: OrderedDict[str, int] = OrderedDict()
        self._lock = Lock()
        self._persist = persist_key
        self._persist_dir = persist_dir
        self._persist_max_bytes = persist_max_bytes  # 序列化 JSON 超此字节数 → 不落盘
        self._plock = Lock()
        self._hits = 0  # 内存命中计数（进程内累计；磁盘后备加载计为 miss）
        self._misses = 0
        # 异步落盘：set 只入队，后台线程批量写盘（同 key 合并），请求线程不阻塞（P1-1）
        self._disk_q: queue.Queue | None = None
        self._disk_worker: Thread | None = None
        if self._persist:
            os.makedirs(self._persist_dir, exist_ok=True)
            self._sweep_expired()
            self._disk_q = queue.Queue()
            self._disk_worker = Thread(
                target=self._disk_writer,
                daemon=True,
                name=f"cache-persist:{self._persist}",
            )
            self._disk_worker.start()
            atexit.register(self.flush)

    def _file_path(self, key: str) -> str:
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._persist_dir, f"{self._persist}_{h}.json")

    def _enforce_bounds(self) -> None:
        """锁内执行：条目数 + 字节（可选）双上限，超限淘汰最久未用条目（LRU，O(1)）。"""
        while len(self._d) > self._max or (
            self._max_bytes is not None and sum(self._bytes.values()) > self._max_bytes
        ):
            _rkey, _ = self._d.popitem(last=False)
            if self._max_bytes is not None:
                self._bytes.pop(_rkey, None)

    def get(self, key: str):
        with self._lock:
            item = self._d.get(key)
            if item is not None:
                ts, val = item
                if time.time() - ts <= self._ttl:
                    self._hits += 1
                    self._d.move_to_end(key)  # 最近访问 → 超限淘汰的是最久未用者
                    return val
                del self._d[key]
                self._bytes.pop(key, None)
            self._misses += 1
        if self._persist is None:
            return None
        got = self._load_disk(key)
        if got is not None:
            disk_ts, val = got
            with self._lock:
                # 二次检查：读盘期间另一线程 set/加载了同 key → 新值优先，
                # 旧盘值不覆盖并发新值、也不返回旧值
                item = self._d.get(key)
                if item is not None:
                    return item[1]
                if self._max_bytes is not None:
                    self._bytes[key] = _estimate_size(val)
                # 沿用磁盘 ts：TTL 判定已在 _load_disk 内做过（过期返回 None）
                self._d[key] = (disk_ts, val)  # 新条目自然置尾（LRU 语义）
                self._enforce_bounds()
            return val
        return None

    def set(self, key: str, val) -> None:
        with self._lock:
            self._d[key] = (time.time(), val)
            self._d.move_to_end(key)
            if self._max_bytes is not None:
                self._bytes[key] = _estimate_size(val)
            self._enforce_bounds()
        if self._persist is not None:
            self._enqueue_disk(key, val)  # 异步落盘，请求线程不阻塞

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes.clear()
        if self._persist is not None:
            self._discard_pending()  # 丢弃未落盘队列，避免 flush/worker 把已清数据写回
            self._drop_disk_files()

    def delete_prefix(self, prefix: str) -> None:
        """删除所有键以 prefix 开头的内存条目（如删除数据集时清理其面板热缓存）。

        磁盘后备文件按 key 哈希命名无法反查键，不主动删盘文件——过期后自然清除。
        """
        with self._lock:
            for k in [k for k in self._d if k.startswith(prefix)]:
                del self._d[k]
                self._bytes.pop(k, None)

    def size(self) -> int:
        with self._lock:
            return len(self._d)

    def stats(self) -> dict:
        """命中统计（线程安全）：{"size", "hits", "misses", "hit_rate"}。

        命中 = 内存 TTL 内直接返回；miss 含未命中与过期淘汰；磁盘后备加载计为 miss。
        计数为进程内累计（clear 不清零）。
        """
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._d),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }

    # ---- 磁盘后备（低频大对象专用；高频路径仅内存命中）----

    def _save_disk(self, key: str, val) -> None:
        try:
            blob = json.dumps({"ts": time.time(), "value": val})
            if (
                self._persist_max_bytes
                and len(blob.encode("utf-8")) > self._persist_max_bytes
            ):
                # 大条目（如 days>1000 的长序列 indicator，序列化 JSON 达 MB 级）不落盘：
                # 磁盘写放大且重启后照样要重算，收益低。内存缓存仍保留（TTL 内命中），
                # 读取路径缺盘时自然回退内存/重算，行为兼容。
                return
            path = self._file_path(key)
            tmp = path + ".tmp"
            with self._plock:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(blob)
                os.replace(tmp, path)
        except Exception:
            # 写盘失败（序列化/磁盘错误）：清理可能残留的 tmp（os.replace 未执行），
            # 避免孤儿 .tmp 堆积；内存缓存照常。
            # 清理与写盘同锁（T-112）：并发写盘线程可能在清理检查间隙已把 tmp
            # 写完整并 os.replace 走——不同锁时 os.remove 会误删新写的 .tmp 源文件
            # 或与并发写盘互相干扰；入锁后清理与写盘串行，行为确定。
            try:
                with self._plock:
                    if "tmp" in locals() and os.path.exists(tmp):
                        os.remove(tmp)
            except OSError:
                pass

    def _load_disk(self, key: str) -> tuple[float, object] | None:
        """读盘；返回 (磁盘 ts, value) 元组，损坏/过期文件删除后返回 None。"""
        path = self._file_path(key)
        with self._plock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    blob = json.load(f)
            except Exception:
                try:
                    os.remove(path)  # 损坏文件删除
                except OSError:
                    pass
                return None
            if time.time() - blob.get("ts", 0) > self._ttl:
                try:
                    os.remove(path)  # 过期文件删除
                except OSError:
                    pass
                return None
            return blob.get("ts"), blob.get("value")

    def _sweep_expired(self) -> None:
        """启动时清理本缓存实例的过期/损坏磁盘文件与崩溃遗留 .tmp 临时文件。"""
        prefix = self._persist + "_"
        with self._plock:
            try:
                names = os.listdir(self._persist_dir)
            except OSError:
                return
            for fn in names:
                if not fn.startswith(prefix):
                    continue
                path = os.path.join(self._persist_dir, fn)
                # .tmp 残留：写盘中途进程崩溃（os.replace 前）留下，无对应 .json 主文件，
                # 下次启动/写盘不会复用（_file_path 哈希相同会被覆盖），累积成孤儿文件
                if fn.endswith(".tmp"):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        blob = json.load(f)
                    if time.time() - blob.get("ts", 0) > self._ttl:
                        os.remove(path)
                except Exception:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def _drop_disk_files(self) -> None:
        """数据源切换等场景：内存与磁盘一并清空，避免跨源脏数据。"""
        prefix = self._persist + "_"
        with self._plock:
            try:
                names = os.listdir(self._persist_dir)
            except OSError:
                return
            for fn in names:
                if fn.startswith(prefix) and fn.endswith(".json"):
                    try:
                        os.remove(os.path.join(self._persist_dir, fn))
                    except OSError:
                        pass

    # ---- 异步落盘（P1-1）：set 入队 → 后台线程批量写盘，请求线程不阻塞 ----

    def _enqueue_disk(self, key: str, val) -> None:
        q = self._disk_q
        if q is None:
            return
        try:
            q.put((key, val))
        except Exception:
            pass  # 队列不可用 → 静默放弃落盘（内存照常）

    def _disk_writer(self) -> None:
        """后台落盘线程：阻塞取队首，随后 drain 队列并按 key 合并为最新值，逐个写盘。"""
        q = self._disk_q
        if q is None:
            return
        while True:
            try:
                item = q.get()
            except Exception:
                return
            if item is None:
                return
            batch = {item[0]: item[1]}
            _drain_queue(q, batch)
            for k, v in batch.items():
                self._save_disk(k, v)

    def flush(self) -> None:
        """同步落盘剩余队列条目（进程退出 atexit / 测试收尾时调用）。

        T-112：drain 前先与 _plock 同步一次——后台 worker 在途批次写盘持锁，
        等待其完成后再 drain，避免 flush 与 worker 的写盘交错（同 key 双写、
        boom 窗口内并发 os.replace）；flush 返回后磁盘状态确定。
        """
        q = self._disk_q
        if q is None:
            return
        with self._plock:
            pass
        batch = _drain_queue(q, {})
        for k, v in batch.items():
            self._save_disk(k, v)

    def _discard_pending(self) -> None:
        """丢弃队列中未落盘的写入（clear 后不得把已清数据写回）。"""
        q = self._disk_q
        if q is None:
            return
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass


def _drain_queue(q: queue.Queue, seed: dict) -> dict:
    """一次性取空队列并入 seed（后写覆盖先写）→ 同 key 合并为最新值，减少写盘次数。"""
    while True:
        try:
            k, v = q.get_nowait()
            seed[k] = v
        except queue.Empty:
            return seed


# 全局缓存实例（不同数据类型的生命周期）
panel_cache = TTLCache(
    ttl=3600,
    maxsize=16,
    max_bytes=settings.panel_cache_max_bytes or None,
)  # 面板数据（1 小时，内存热缓存；冷缓存由 dataset npz 承担，不落盘。
# P2-41：条目数 16 + 字节上界（默认 512MB，SR_PANEL_CACHE_MAX_BYTES 可调），
# 防 limit=0 全市场面板（≈200MB/条）多条目撑爆内存）
indicator_cache = TTLCache(
    ttl=90, maxsize=512, persist_key="ind", persist_max_bytes=256 * 1024
)  # 指标计算结果（90 秒，落盘；>256KB 大条目不落盘）
risk_cache = TTLCache(
    ttl=120, maxsize=256, persist_key="risk"
)  # 风险指标（2 分钟，落盘）
tune_cache = TTLCache(
    ttl=300, maxsize=128, persist_key="tune"
)  # 指标调优（5 分钟，落盘）
news_cache = TTLCache(
    ttl=300, maxsize=256, persist_key="news"
)  # 个股新闻（5 分钟，落盘，降低源请求频率）

_CACHE_INSTANCES: list[tuple[str, TTLCache]] = [
    ("panel", panel_cache),
    ("indicator", indicator_cache),
    ("risk", risk_cache),
    ("tune", tune_cache),
    ("news", news_cache),
]


def all_cache_stats() -> dict:
    """全部全局缓存实例命中统计汇总：{name: stats, ..., "total": 合计}。"""
    out: dict = {}
    hits = misses = size = 0
    for name, c in _CACHE_INSTANCES:
        s = c.stats()
        out[name] = s
        hits += s["hits"]
        misses += s["misses"]
        size += s["size"]
    out["total"] = {
        "size": size,
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / (hits + misses), 4) if hits + misses else 0.0,
    }
    return out


def clear_all() -> None:
    """数据源切换等场景下清空全部缓存，避免跨源脏数据。"""
    for _, c in _CACHE_INSTANCES:
        c.clear()
    from .klines import bump_version

    bump_version()
