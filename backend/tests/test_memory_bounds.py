"""内存热点回归测试：有界缓存淘汰 / K线分批物化等价 / float32 评估精度 / 大条目不落盘。

覆盖后台盘点结论对应的四项改动：
1. signals.py `_sector_codes_cache`：OrderedDict 上限 + TTL 惰性过期 + 超限逐最旧。
2. kcache.py `load_cached`：分批物化（分批 IN 查询）与一次性查询结果完全一致。
3. evaluate.py：float32 路径与 float64 路径结果相对误差 < 1e-4，且不产生全量 float64 副本。
4. cache.py：超过 persist_max_bytes 的大条目不落盘（内存仍保留，读取路径兼容回退）。
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import signals as S
from app.storage.db import Base
from app.storage.models import Kline
from app.lib.alpha import evaluate as AE
from app.storage.cache import TTLCache, indicator_cache
from app.storage import klines as kcache


# ---------- 1. signals.py 有界缓存 ----------


def test_bounded_cache_evicts_oldest_lru(monkeypatch):
    """超限逐最旧（LRU）：访问过的条目不被后续写入淘汰；从未访问的最旧条目被逐。"""
    cache = S._sector_codes_cache
    cache.clear()
    try:
        assert S._SECTOR_CODES_MAX == 128, "上限配置回退检查"
        for i in range(S._SECTOR_CODES_MAX):
            S._cache_set(cache, f"k{i}", {i}, S._SECTOR_CODES_MAX)
        assert len(cache) == S._SECTOR_CODES_MAX
        # 访问 k5 → 刷新 recency
        assert S._cache_get(cache, "k5", S._SECTOR_CODES_TTL, S._SECTOR_CODES_MAX) == {
            5
        }
        for i in range(200, 200 + 10):
            S._cache_set(cache, f"k{i}", {i}, S._SECTOR_CODES_MAX)
        assert len(cache) == S._SECTOR_CODES_MAX
        assert "k5" in cache, "最近访问的条目应保留"
        assert "k0" not in cache, "最旧且未访问的条目应被淘汰"
        # 值语义不变：写入 (时间, 值) 结构，命中返回值本身
        assert S._cache_get(
            cache, "k200", S._SECTOR_CODES_TTL, S._SECTOR_CODES_MAX
        ) == {200}
    finally:
        cache.clear()


def test_bounded_cache_ttl_expiry(monkeypatch):
    """访问时 TTL 惰性淘汰：过期条目返回 None 并从缓存移除。"""
    cache = S._sector_codes_cache
    cache.clear()
    try:
        assert S._SECTOR_CODES_MAX == 128, "板块缓存上限配置回退检查"
        S._cache_set(cache, "key1", {"600519.SH"}, S._SECTOR_CODES_MAX)
        assert S._cache_get(
            cache, "key1", S._SECTOR_CODES_TTL, S._SECTOR_CODES_MAX
        ) == {"600519.SH"}
        assert "key1" in cache
        real_time = time.time
        monkeypatch.setattr("time.time", lambda: real_time() + S._SECTOR_CODES_TTL + 1)
        assert (
            S._cache_get(cache, "key1", S._SECTOR_CODES_TTL, S._SECTOR_CODES_MAX)
            is None
        )
        assert "key1" not in cache, "过期条目应被惰性淘汰"
    finally:
        cache.clear()


# ---------- 2. kcache.load_cached 分批物化等价 ----------


def _seed_klines(maker, n_codes: int = 10, bars: int = 12) -> list[str]:
    """向临时库写入 n_codes 只 daily K 线（裸代码，与 kcache 存储口径一致），返回代码列表。"""
    db = maker()
    try:
        dates = [
            d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=bars)
        ]
        codes = []
        for i in range(n_codes):
            code = f"6000{i:02d}"
            codes.append(code)
            base = 10.0 + i
            close = base + np.arange(bars) * 0.1
            for j, dt in enumerate(dates):
                db.add(
                    Kline(
                        code=code,
                        period="daily",
                        source="test",
                        date=dt,
                        open=float(close[j]),
                        high=float(close[j] + 0.2),
                        low=float(close[j] - 0.2),
                        close=float(close[j]),
                        volume=1000.0 + j,
                        amount=1e6 + j,
                    )
                )
        db.commit()
        return codes
    finally:
        db.close()


@pytest.fixture()
def kline_db(tmp_path, monkeypatch):
    """临时 SQLite K 线库：种子 10 只 daily 数据，替换 kcache.SessionLocal（不触碰生产库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'klines.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(kcache, "SessionLocal", Maker)
    _seed_klines(Maker)
    try:
        yield Maker
    finally:
        engine.dispose()


def _daily_codes(limit: int) -> list[str]:
    from sqlalchemy import text

    db = kcache.SessionLocal()
    try:
        rows = db.execute(
            text("SELECT DISTINCT code FROM klines WHERE period = 'daily' LIMIT :n"),
            {"n": limit},
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        db.close()


def test_load_cached_batched_equals_single_query(kline_db, monkeypatch):
    """分批物化（_LOAD_BATCH 缩小强制多批）与一次性查询返回完全一致。"""
    codes = _daily_codes(10)
    assert len(codes) == 10, "临时库应种子 10 只 daily 股票"
    single = kcache.load_cached(codes, "daily")  # 默认 _LOAD_BATCH=300 → 单批
    monkeypatch.setattr(kcache, "_LOAD_BATCH", 3)  # 10 只 → 4 批
    batched = kcache.load_cached(codes, "daily")
    assert set(batched) == set(single)
    assert len(batched) == len(codes)
    for c in single:
        pd.testing.assert_frame_equal(batched[c], single[c])


def test_load_cached_structure_and_empty(kline_db):
    """返回结构：date 升序、列顺序、pct_change 语义；空输入返回空 dict。"""
    assert kcache.load_cached([], "daily") == {}
    codes = _daily_codes(5)
    out = kcache.load_cached(codes, "daily")
    for c, df in out.items():
        assert list(df.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "pct_change",
        ]
        assert df["date"].is_monotonic_increasing
        # pct_change 与直接计算一致（float 列）
        expect = df["close"].pct_change().fillna(0) * 100
        pd.testing.assert_series_equal(df["pct_change"], expect, check_names=False)


# ---------- 3. evaluate.py float32 精度 ----------


def test_as_float_keeps_float32_no_copy():
    """float32 输入不产生全量 float64 副本；int 才提升。"""
    f32 = np.zeros((100, 10), dtype=np.float32)
    assert AE._as_float(f32) is f32
    assert AE._as_float(f32).dtype == np.float32
    assert AE._as_float(np.zeros((2, 2), dtype=np.float64)).dtype == np.float64
    assert AE._as_float(np.zeros((2, 2), dtype=np.int64)).dtype == np.float64


def test_forward_returns_keeps_float32():
    f32 = np.random.default_rng(1).normal(100, 5, (50, 40)).astype(np.float32)
    out32 = AE.forward_returns(f32, 5)
    out64 = AE.forward_returns(f32.astype(np.float64), 5)
    assert out32.dtype == np.float32, "float32 输入应保持 float32，避免全量提升"
    np.testing.assert_allclose(out32, out64, rtol=1e-4, atol=1e-6)


def test_evaluate_float32_vs_float64_precision():
    """float32 路径与 float64 路径相对误差 < 1e-4（内存热点：避免全量 float64 副本）。"""
    rng = np.random.default_rng(42)
    n_s, n_t = 300, 120
    factor = rng.normal(0, 1, (n_s, n_t)).astype(np.float32)
    fwd = rng.normal(0.001, 0.02, (n_s, n_t)).astype(np.float32)
    fwd[:, -5:] = np.nan  # forward_returns 固有结构：尾部 horizon 列全 NaN
    f64, r64 = factor.astype(np.float64), fwd.astype(np.float64)

    # ic_series
    ics32, ics64 = AE.ic_series(factor, fwd), AE.ic_series(f64, r64)
    np.testing.assert_allclose(ics32, ics64, rtol=1e-4, atol=1e-6)

    # factor_to_returns（top_annual, long_short_annual, turnover）
    t32, t64 = (
        AE.factor_to_returns(factor, fwd, top_pct=0.2),
        AE.factor_to_returns(f64, r64, top_pct=0.2),
    )
    for a, b in zip(t32, t64):
        if np.isfinite(a) and np.isfinite(b):
            assert abs(a - b) <= 1e-4 * max(abs(b), 1e-6), (a, b)

    # quantile_cum_returns
    q32 = AE.quantile_cum_returns(factor, fwd, n_quantiles=5)
    q64 = AE.quantile_cum_returns(f64, r64, n_quantiles=5)
    for k in q32:
        a, b = np.asarray(q32[k], dtype=float), np.asarray(q64[k], dtype=float)
        m = np.isfinite(a) & np.isfinite(b)
        np.testing.assert_allclose(a[m], b[m], rtol=1e-4, atol=1e-6)


# ---------- 4. cache.py 大条目不落盘 ----------


def test_persist_large_entry_skips_disk(tmp_path):
    """超过 persist_max_bytes 的大条目不写磁盘（内存仍保留）；小条目正常落盘。"""
    c = TTLCache(
        ttl=300,
        maxsize=16,
        persist_key="t",
        persist_dir=str(tmp_path),
        persist_max_bytes=256,
    )
    small, big = {"x": 1}, {"data": "a" * 1000}
    c.set("small", small)
    c.set("big", big)
    c.flush()  # 异步落盘：断言文件前同步写完队列
    files = list(tmp_path.glob("t_*.json"))
    assert len(files) == 1, f"只应有小条目落盘，实际 {len(files)} 个文件"
    # 内存缓存两者都在（TTL 内直接命中）
    assert c.get("small") == small
    assert c.get("big") == big
    # 重启后：小条目有磁盘兜底，大条目缺盘 → 回退（返回 None，调用方走重算路径）
    c2 = TTLCache(
        ttl=300,
        maxsize=16,
        persist_key="t",
        persist_dir=str(tmp_path),
        persist_max_bytes=256,
    )
    assert c2.get("small") == small
    assert c2.get("big") is None


def test_indicator_cache_disk_threshold_configured():
    """indicator_cache 已配置 256KB 落盘阈值（防止 days>1000 长序列 MB 级写放大）。"""
    assert indicator_cache._persist_max_bytes == 256 * 1024
    assert indicator_cache._ttl == 90, "T-02：指标缓存 TTL 60s → 90s"


def test_panel_cache_has_no_disk_persist():
    """panel_cache 不落盘（T-03）：persist_key="panel" 是死配置（numpy 面板 json.dumps
    必抛 TypeError，从未落盘成功）；冷缓存由 dataset.py npz 承担。"""
    from app.storage.cache import panel_cache

    assert panel_cache._persist is None, "panel 不应配置磁盘持久化"
    assert panel_cache._persist_dir is not None  # 默认目录保留（无副作用）


# ---------- 5. kcache.get_version 进程内短 TTL 缓存（P1-2） ----------


def test_get_version_short_ttl_cache(kline_db, monkeypatch):
    """get_version 5s 短 TTL 缓存：进程内再读不重新查询 SQLite；过期后读到新值。"""
    from sqlalchemy import text

    db = kcache.SessionLocal()
    try:
        kcache._ensure_meta(db)  # cache_meta 为运行时动态建表，先创建再插入
        db.execute(text("INSERT INTO cache_meta(key, value) VALUES ('ver:global', 7)"))
        db.commit()
    finally:
        db.close()
    assert kcache.get_version() == 7  # 冷启动：读库
    # 直接 SQL 改库（模拟 upsert_many 的 _bump_keys 路径，不经 bump_version 失效缓存）
    db = kcache.SessionLocal()
    try:
        db.execute(text("UPDATE cache_meta SET value = 8 WHERE key = 'ver:global'"))
        db.commit()
    finally:
        db.close()
    assert kcache.get_version() == 7, "5s TTL 内应返回缓存旧值"
    # TTL 过期 → 重新读库
    monkeypatch.setattr(kcache, "_VERSION_TTL", 0.01)
    time.sleep(0.02)
    assert kcache.get_version() == 8


def test_bump_version_invalidates_cache(kline_db):
    """bump_version 写入后主动失效缓存：bump 后立即读到新值（不等 5s TTL）。"""
    kcache.get_version()  # 冷启动缓存 0
    v = kcache.bump_version()
    assert v == 1, "bump 后应立即读到新版本（缓存已失效）"
    assert kcache.get_version() == 1
    v2 = kcache.bump_version(code="600001")
    assert v2 == 1
    assert kcache.get_version("600001") == 1
