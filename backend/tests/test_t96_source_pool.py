"""T-96 源竞速线程池复用（P1-62）测试。

背景：_race_first_success 每次 get_spot/get_kline 新建 ThreadPoolExecutor
（≤3 线程），源故障期每请求遗留 3 个非 daemon 阻塞线程（~25s 超时）→
线程爆炸（≈3×并发请求）。修复为模块级单例 daemon 池（_get_race_pool），
线程数恒为 _RACE_MAX_WORKERS=len(SOURCES)。

覆盖：
- executor 单例（非每请求新建）；
- 池线程全部 daemon、数量恒为 _RACE_MAX_WORKERS（有界）；
- 故障场景（全源失败）循环竞速：线程数不随请求增长；
- 慢失败源后台挂起：不新增线程（P1-62 核心回归）；
- 单次竞速并发上限 ≤3（并发上限语义保持）；
- shutdown_race_pool 关闭路径：worker 线程退出，再次竞速自动重建；
- 竞速语义回归：首个成功返回 (key, result)、全败返回 (None, None)。

全部走打桩，不触碰网络与生产库。
"""

from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

from app.core import sources as S


@pytest.fixture(autouse=True)
def _reset_sources_state():
    """每个测试前重置健康/活跃源状态并关闭竞速池，避免跨测试污染。"""
    S._active = None
    for k in S._health:
        h = S._health[k]
        h["spot_fails"] = 0
        h["kline_fails"] = 0
        h["spot_blocked_until"] = 0.0
        h["kline_blocked_until"] = 0.0
        h["slow_until"] = 0.0
        h["ok"] = False
        h["last_error"] = ""
    S.shutdown_race_pool()
    time.sleep(0.1)  # 等旧 worker 收到哨兵退出，避免线程数断言受残留线程干扰
    yield
    S.shutdown_race_pool()
    time.sleep(0.1)


def _race_thread_count() -> int:
    """当前进程中竞速池工作线程数（按线程名前缀统计）。"""
    return sum(1 for t in threading.enumerate() if t.name.startswith("src-race"))


def _spot_df(n=2):
    return pd.DataFrame(
        {
            "code": ["600519.SH", "000001.SZ"],
            "name": ["贵州茅台", "平安银行"],
            "price": [1700.0, 11.0],
            "pct_change": [1.2, 0.0],
            "volume": [30000.0, 50000.0],
            "amount": [5.1e8, 5.5e7],
            "turnover_rate": [0.3, 0.2],
            "pe": [30.0, 6.0],
            "pb": [9.0, 0.8],
            "market_cap": [21350.0, 2135.0],
            "float_cap": [21350.0, 2135.0],
            "industry": "",
            "source": "akshare",
        }
    )


# ---------------------------------------------------------------------------
# 1) executor 单例 + 线程有界
# ---------------------------------------------------------------------------


def test_pool_is_singleton():
    """多次获取返回同一池实例（非每请求新建）。"""
    assert S._get_race_pool() is S._get_race_pool()


def test_pool_workers_daemon_and_bounded():
    """池线程全部 daemon，数量恒为 _RACE_MAX_WORKERS。"""
    pool = S._get_race_pool()
    assert len(pool._threads) == S._RACE_MAX_WORKERS == len(S.SOURCES)
    assert all(t.daemon for t in pool._threads)
    assert _race_thread_count() == S._RACE_MAX_WORKERS


def test_fault_scenario_thread_count_stable(monkeypatch):
    """故障期（全源失败）循环竞速：线程数恒为上界，不随请求增长。"""

    def boom(key):
        raise RuntimeError(f"{key} 网络不可达(测试模拟)")

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina", "tencent"])
    for _ in range(30):
        key, result = S.spot_first_success(boom)
        assert key is None and result is None
        assert _race_thread_count() == S._RACE_MAX_WORKERS
    assert _race_thread_count() == S._RACE_MAX_WORKERS


def test_slow_fail_background_does_not_grow_threads(monkeypatch):
    """慢失败源后台挂起：不新增线程（旧实现每请求 3 个新线程 → 爆炸）。"""
    df = _spot_df()
    gate = threading.Event()

    def fake_fetch(key):
        if key == "akshare":
            gate.wait(2)  # 慢失败源挂起，后台继续占用任务
            raise RuntimeError("akshare 挂(测试模拟)")
        return df

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina"])
    for _ in range(20):
        key, result = S.spot_first_success(fake_fetch)
        assert key == "sina"
        assert _race_thread_count() == S._RACE_MAX_WORKERS
    gate.set()
    time.sleep(0.3)
    assert _race_thread_count() == S._RACE_MAX_WORKERS


def test_concurrency_cap_within_race(monkeypatch):
    """单次竞速并发 ≤3（候选源数），并发上限语义保持。"""
    max_concurrent = 0
    cur = 0
    lock = threading.Lock()

    def fake_fetch(key):
        nonlocal max_concurrent, cur
        with lock:
            cur += 1
            max_concurrent = max(max_concurrent, cur)
        time.sleep(0.05)
        with lock:
            cur -= 1
        return _spot_df()

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina", "tencent"])
    key, result = S.spot_first_success(fake_fetch)
    assert key is not None and result is not None
    assert max_concurrent <= 3


# ---------------------------------------------------------------------------
# 2) 关闭路径
# ---------------------------------------------------------------------------


def test_shutdown_releases_threads_and_rebuild(monkeypatch):
    """shutdown_race_pool 后 worker 线程退出；再次竞速自动重建新池。"""
    monkeypatch.setattr(S, "spot_candidates", lambda: ["sina"])
    key, result = S.spot_first_success(lambda k: _spot_df())
    assert key == "sina"
    assert _race_thread_count() == S._RACE_MAX_WORKERS

    S.shutdown_race_pool()
    deadline = time.time() + 3
    while time.time() < deadline and _race_thread_count() > 0:
        time.sleep(0.05)
    assert _race_thread_count() == 0  # 关闭路径可达：线程已退出

    # 再次竞速 → 懒重建新池（线程数仍为上界，幂等可重入）
    key, result = S.spot_first_success(lambda k: _spot_df())
    assert key == "sina"
    assert _race_thread_count() == S._RACE_MAX_WORKERS


def test_shutdown_is_idempotent():
    """shutdown_race_pool 幂等可重入（未创建池时调用不报错）。"""
    S.shutdown_race_pool()
    S.shutdown_race_pool()


# ---------------------------------------------------------------------------
# 3) 竞速语义回归（首个成功 / 全败）
# ---------------------------------------------------------------------------


def test_race_semantics_fast_win(monkeypatch):
    """首个成功返回 (key, result)；失败源冷却登记不丢失。"""
    df = _spot_df()

    def fake_fetch(key):
        if key == "akshare":
            time.sleep(0.3)
            raise RuntimeError("akshare 挂(测试模拟)")
        return df

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina"])
    key, result = S.spot_first_success(fake_fetch)
    assert key == "sina"
    assert result is df
    time.sleep(0.5)
    assert S._health["akshare"]["spot_fails"] == 1  # 后台慢源登记未丢失


def test_race_semantics_all_fail(monkeypatch):
    """全败返回 (None, None)，各源冷却登记完成、errors 收集摘要。"""

    def boom(key):
        raise RuntimeError(f"{key} 网络不可达(测试模拟)")

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina", "tencent"])
    errors: list[str] = []
    key, result = S.spot_first_success(boom, errors=errors)
    assert key is None and result is None
    for k in ("akshare", "sina", "tencent"):
        assert S._health[k]["spot_fails"] == 1
        assert S._health[k]["spot_blocked_until"] > time.time()
    assert len(errors) == 3
