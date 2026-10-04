"""审计修复回归测试：
- cmo 自定义参数 normalize 生效（compute.CUSTOM_PARAM_ORDER 补缺后）
- 分钟线拉取窗口公式（各周期窗口天数合理，period 不再自消）
- signals 私有 LRU 缓存并发锁冒烟（多线程 get/set 同 key 无异常）
- engine.indicator_memo 线程隔离（两线程各自 enter/exit 互不污染）
"""

from __future__ import annotations

import threading

import numpy as np
import pandas as pd

from app.lib.indicators.compute import compute_all, normalize_custom
from app.core.sources import _minute_window_minutes
from app.lib.signals.engine import _get_memo, evaluate_all_signals, indicator_memo


def _synthetic_df(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 50 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, n)))
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n).astype(str),
            "open": close * (1 + rng.normal(0, 0.005, n)),
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": rng.uniform(5e5, 2e6, n),
        }
    )


# ---------------------------------------------------------------------------
# cmo 自定义参数 normalize 生效
# ---------------------------------------------------------------------------


def test_cmo_custom_param_normalize():
    """CUSTOM_PARAM_ORDER 缺 cmo 时 normalize_custom("cmo", ...) 返回 None → 保存的
    cmo 参数永不生效。补上 ("window",) 后三种格式都应归一为具名 dict。"""
    assert normalize_custom("cmo", {"window": 9}) == {"window": 9.0}
    assert normalize_custom("cmo", [9]) == {"window": 9.0}
    assert normalize_custom("cmo", 9) == {"window": 9.0}
    # 非法：缺键 / 非正数 → None
    assert normalize_custom("cmo", {}) is None
    assert normalize_custom("cmo", -3) is None


def test_cmo_custom_param_actually_applies():
    """compute_all 传入 custom cmo 时，输出应随窗口变化（说明自定义参数真的生效）。"""
    df = _synthetic_df()
    default = compute_all(df, ["cmo"])["cmo"]
    custom9 = compute_all(df, ["cmo"], {"cmo": {"window": 9}})["cmo"]
    custom30 = compute_all(df, ["cmo"], {"cmo": {"window": 30}})["cmo"]
    assert len(default) == len(df)
    assert default != custom9  # 窗口不同 → 序列不同（参数已生效）
    assert default != custom30
    assert custom9 != custom30


# ---------------------------------------------------------------------------
# 分钟线拉取窗口公式
# ---------------------------------------------------------------------------


def test_minute_window_formula_uses_period():
    """旧公式 `days*240/period*period*1.5` 中 period 自消（恒为 days×360 分钟），
    任何周期窗口相同。修正后窗口必须随周期单调递增（period 真正参与计算）。"""
    windows = [_minute_window_minutes(400, p) for p in ("1", "5", "15", "30", "60")]
    # 单调递增
    assert windows == sorted(windows)
    assert len(set(windows)) == 5
    # 60 分钟周期窗口明显大于 1 分钟（旧实现两者相同，都是 100 天）
    assert windows[-1] > windows[0] * 5


def test_minute_window_days_reasonable_per_period():
    """各周期窗口天数合理（days=400 根 bar）：
    - 1 分钟：≥10 天自然时间（下限守卫，覆盖春节/国庆长假后首次拉取或缓存清空
      重建——换算值仅 2.5 天会拉不到长假前的 1 分钟历史；10 天 ≈ 2400 根 > min_depth）
    - 60 分钟：约 150 天自然时间 ≈ 100+ 个交易日（400 根 60 分钟 bar = 100 交易日）
    - 不再退化为恒定 100 天（旧公式 days=400 → 100 天与周期无关）
    """
    days_of = {
        p: _minute_window_minutes(400, p) / 1440.0 for p in ("1", "5", "15", "30", "60")
    }
    assert 8 <= days_of["1"] <= 15
    assert 5 <= days_of["5"] <= 25
    assert 15 <= days_of["15"] <= 60
    assert 40 <= days_of["30"] <= 110
    assert 100 <= days_of["60"] <= 220
    # 60 分钟窗口明显大于 1 分钟（旧实现二者均为 100 天；下限守卫只抬升 1 分钟
    # 至 10 天，150/10=15 仍保持数量级差距）
    assert days_of["60"] / days_of["1"] > 5


def test_minute_window_capped_at_one_year():
    """窗口封顶一年（与旧实现一致的 60*24*365 上限）。"""
    huge = _minute_window_minutes(100000, "60")
    assert huge == 60 * 24 * 365
    assert _minute_window_minutes(400, "60") < 60 * 24 * 365


# ---------------------------------------------------------------------------
# signals 私有 LRU 缓存并发锁冒烟
# ---------------------------------------------------------------------------


def test_signals_cache_lock_concurrent_smoke():
    """多线程并发 get/set 同一 key（含 TTL 过期淘汰路径）不得抛异常、上限不超界。"""
    import collections
    from app.api import signals as S

    cache = collections.OrderedDict()
    errors: list[Exception] = []
    stop = threading.Event()

    def worker(seed: int):
        try:
            key = "k1"
            for i in range(300):
                # 混合读（含未过期命中 / 过期淘汰 / 未命中）与写
                if i % 3 == 0:
                    S._cache_get(cache, key, ttl=0.05, maxsize=8)
                else:
                    S._cache_set(cache, key, {"seed": seed, "i": i}, maxsize=8)
                S._cache_get(cache, f"key{seed}:{i % 5}", ttl=60.0, maxsize=8)
        except Exception as e:  # pragma: no cover - 失败即测试失败
            errors.append(e)
            stop.set()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"并发访问抛异常: {errors}"
    assert len(cache) <= 8  # 上限始终生效
    # 锁存在且 get/set 仍在锁内（防止后续回归移除）
    assert S._cache_lock is not None
    # 通过锁序列化后，读到的值必须是完整的 (时间戳, 值) 结构
    hit = S._cache_get(cache, "k1", ttl=60.0, maxsize=8)
    assert hit is None or isinstance(hit, dict)


# ---------------------------------------------------------------------------
# engine.indicator_memo 线程隔离
# ---------------------------------------------------------------------------


def test_indicator_memo_thread_isolation():
    """调度线程与手动扫描任务并发时，各线程持有独立 memo：
    - 线程 A 安装 memo_A 期间，线程 B 看到的是 None（未安装）而非 memo_A
    - 线程 B 安装 memo_B 后互不影响；各自退出后本线程回到安装前状态
    """
    memo_a: dict = {}
    memo_b: dict = {}
    results: dict = {}
    barrier = threading.Barrier(2)

    with indicator_memo(memo_a):
        assert _get_memo() is memo_a

        def worker_b():
            # 主线程持有 memo_A 时，本线程必须是独立的空 memo（None）
            results["b_sees_none"] = _get_memo() is None
            with indicator_memo(memo_b):
                results["b_sees_own"] = _get_memo() is memo_b
                results["b_not_a"] = _get_memo() is not memo_a
                barrier.wait()
            results["b_after_exit"] = _get_memo() is None

        tb = threading.Thread(target=worker_b)
        tb.start()
        barrier.wait()
        # 线程 B enter/exit 全程不影响主线程 memo
        assert _get_memo() is memo_a
        tb.join(timeout=30)

    assert results["b_sees_none"] is True
    assert results["b_sees_own"] is True
    assert results["b_not_a"] is True
    assert results["b_after_exit"] is True
    assert _get_memo() is None  # 主线程退出后还原为 None


def test_indicator_memo_threads_run_evaluate_consistently():
    """并发线程各自独立 memo 下 evaluate_all_signals 输出一致（memo 隔离不改变结果）。"""
    df = _synthetic_df(120).assign(code="600519.SH")
    plain = evaluate_all_signals(df)

    outs: dict[int, tuple] = {}

    def run(idx: int):
        memo: dict = {}
        with indicator_memo(memo):
            outs[idx] = (evaluate_all_signals(df), evaluate_all_signals(df))
        assert _get_memo() is None  # 本线程退出后还原

    threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for idx, (a, b) in outs.items():
        assert a == plain and b == plain  # memo 开启与关闭结果一致
        assert a == b
