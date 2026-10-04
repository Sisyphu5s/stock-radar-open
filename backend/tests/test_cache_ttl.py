"""TTLCache 磁盘后备 TTL / 并发语义 / LRU 淘汰 / 异步落盘聚焦测试（T-47 / P1-14 / P0-1 / P1-1）。

- 磁盘加载沿用磁盘 ts（不重置 TTL）；
- 读盘期间并发 set 的新值不被旧盘值覆盖；
- 内部 OrderedDict LRU 淘汰（O(1) 语义，非 ts 全表扫描）；
- set 异步落盘不阻塞请求线程，队列同 key 合并。
纯单实例 + tmp_path，不触碰全局缓存实例。
"""

from __future__ import annotations

import json
import queue
import time
from collections import OrderedDict

from app.storage.cache import TTLCache, _drain_queue


def test_get_preserves_disk_ts(tmp_path):
    """盘值写回内存沿用磁盘 ts（修复前用 time.time() 重置 TTL，本测试会失败）。"""
    c = TTLCache(ttl=60, persist_key="t", persist_dir=str(tmp_path))
    c.set("k", "A")
    c.flush()  # 异步落盘：手动断言前先同步写盘
    # 手动把磁盘 ts 改写为 30 秒前，再模拟内存 miss
    path = c._file_path("k")
    with open(path, "r", encoding="utf-8") as f:
        blob = json.load(f)
    blob["ts"] = time.time() - 30
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blob, f)
    del c._d["k"]
    assert c.get("k") == "A"
    assert abs(c._d["k"][0] - (time.time() - 30)) <= 2


def test_get_does_not_overwrite_concurrent_set(tmp_path, monkeypatch):
    """读盘期间另一线程 set 了新值 → 旧盘值不得覆盖内存新值（修复前返回 OLD 且覆盖）。"""
    c = TTLCache(ttl=60, persist_key="t", persist_dir=str(tmp_path))
    # 手动构造盘文件：值 OLD，ts 为当前（未过期）
    path = c._file_path("k")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "value": "OLD"}, f)
    real_load = c._load_disk

    def fake_load(key):
        out = real_load(key)  # 读到 OLD
        c.set(key, "NEW")  # 模拟读盘期间并发 set
        return out

    monkeypatch.setattr(c, "_load_disk", fake_load)
    assert c.get("k") == "NEW"
    assert c._d["k"][1] == "NEW"
    c.flush()  # T-112:收尾同步落盘,避免后台 worker 遗留跨测试在途任务


def test_basic_set_get_memory_hit(tmp_path):
    c = TTLCache(ttl=60, persist_key="t", persist_dir=str(tmp_path))
    c.set("a", 1)
    assert c.get("a") == 1  # 内存命中
    c.flush()  # T-112:收尾同步落盘,避免后台 worker 遗留跨测试在途任务


def test_miss_returns_none_without_persist():
    c = TTLCache(ttl=60)  # persist 为 None
    assert c.get("any") is None


# ---------- P0-1：OrderedDict LRU 淘汰（O(1) 语义） ----------


def test_lru_eviction_ordered_dict(tmp_path):
    """淘汰 O(1) 语义：内部 OrderedDict；超限淘汰最久未访问者（非最旧 ts）。"""
    c = TTLCache(ttl=300, maxsize=3)  # 纯内存实例，miss 返回 None 不读盘
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert c.get("a") == 1  # 访问 a → 刷新 recency，最久未用者变为 b
    c.set("d", 4)  # 超限 → popitem(last=False) 淘汰 b
    assert isinstance(c._d, OrderedDict), "内部结构应为 OrderedDict（淘汰 O(1)）"
    assert len(c._d) == 3
    assert c.get("a") == 1
    assert c.get("b") is None, "最久未访问者应被淘汰"
    assert c.get("c") == 3
    assert c.get("d") == 4


def test_set_existing_key_does_not_evict_other(tmp_path):
    """重写已有 key 不触发淘汰（旧实现满员时重复 set 误删最旧 ts 条目）。"""
    c = TTLCache(ttl=300, maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("a", 10)  # 更新已有 key，len 不变，不应淘汰 b
    assert c.get("a") == 10
    assert c.get("b") == 2


# ---------- P1-1：异步落盘（不阻塞请求线程 + 同 key 合并） ----------


def test_drain_queue_merges_same_key():
    """队列批量取出同 key 合并为最新值（减少写盘次数）。"""
    q = queue.Queue()
    for v in (1, 2, 3):
        q.put(("k", v))
    q.put(("j", 5))
    batch = _drain_queue(q, {})
    assert batch == {"k": 3, "j": 5}
    assert q.empty()


def test_set_does_not_block_on_disk(tmp_path, monkeypatch):
    """set 异步落盘：慢盘不阻塞请求线程（调用后立即返回）。"""
    c = TTLCache(ttl=300, persist_key="t", persist_dir=str(tmp_path))
    real = c._save_disk

    def slow(key, val):
        time.sleep(0.5)
        return real(key, val)

    monkeypatch.setattr(c, "_save_disk", slow)
    t0 = time.time()
    c.set("k", 1)
    elapsed = time.time() - t0
    assert elapsed < 0.2, f"set 应异步返回，实际 {elapsed:.3f}s"
    c.flush()  # flush 同步等待写盘完成
    assert c.get("k") == 1


def test_flush_persists_pending(tmp_path):
    """flush 把队列中未落盘条目同步写完；重启实例可从磁盘加载。"""
    c = TTLCache(ttl=300, persist_key="t", persist_dir=str(tmp_path))
    c.set("a", {"x": 1})
    c.flush()
    c2 = TTLCache(ttl=300, persist_key="t", persist_dir=str(tmp_path))
    assert c2.get("a") == {"x": 1}


def test_clear_discards_pending_disk(tmp_path):
    """clear 丢弃未落盘队列：flush 不会把已清数据写回。"""
    c = TTLCache(ttl=300, persist_key="t", persist_dir=str(tmp_path))
    c._disk_q.put(None)  # 停掉后台 worker（测试确定性：无并发写盘干扰）
    c._disk_q.put(("a", 1))  # 模拟已入队未落盘的写入
    c.clear()
    c.flush()
    assert c._disk_q.empty()
    assert list(tmp_path.glob("t_*.json")) == [], "clear 后不应再有落盘文件"
