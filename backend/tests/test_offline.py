"""离线与缓存兜底测试：网络全挂时系统仍能服务数据（SQLite K线 / 磁盘快照 / 面板npz）。"""

from __future__ import annotations

import time

import pandas as pd
import pytest

from app.storage import klines as kcache
from app.core import sources as quote
from app.storage.providers import base as providers_base


def test_kline_offline_from_sqlite(tmp_path, monkeypatch):
    """K线数据源不可达 → 回退 SQLite 缓存（自建临时 K 线库，不依赖全局预热）。"""

    def boom(*a, **k):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(kcache, "get_kline", boom)

    # 自建临时 K 线库，插入 600519 旧日线（日期久远 → is_stale 恒 True 触发拉取路径）
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.storage import klines_db

    engine = create_engine(
        f"sqlite:///{tmp_path / 'kline.db'}", connect_args={"check_same_thread": False}
    )
    klines_db.ensure_schema(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(kcache, "SessionLocal", maker)
    days = 30
    df = pd.DataFrame(
        {
            "date": [f"2026-07-{d:02d}" for d in range(1, days + 1)],
            "open": [10.0] * days,
            "high": [11.0] * days,
            "low": [9.0] * days,
            "close": [10.5] * days,
            "volume": [100.0] * days,
            "amount": [1e6] * days,
        }
    )
    kcache.upsert_many([("600519.SH", "daily", df)], "test")

    k = kcache.cached_kline("600519.SH", "daily", max_rows=60, refresh_if_stale=True)
    assert k is not None and len(k) > 0
    assert "close" in k.columns and "date" in k.columns


def test_snapshot_disk_roundtrip(tmp_path, monkeypatch):
    """磁盘快照保存/加载往返。"""
    monkeypatch.setattr("app.storage.snapshots._SNAP_DIR", tmp_path)
    df = pd.DataFrame({"code": ["600519.SH"], "name": ["贵州茅台"], "price": [1500.0]})
    quote.save_snapshot_disk("sina", df)
    loaded = quote.load_snapshot_disk("sina")
    assert loaded is not None and len(loaded) == 1
    assert loaded.iloc[0]["code"] == "600519.SH"
    # 过期快照不返回
    import time

    (tmp_path / "sina.pkl").touch()
    import os

    old = time.time() - quote.SNAP_MAX_AGE_S - 10
    os.utime(tmp_path / "sina.pkl", (old, old))
    assert quote.load_snapshot_disk("sina") is None


def test_sina_provider_disk_fallback(tmp_path, monkeypatch):
    """新浪全量抓取失败且无内存缓存（模拟重启后离线）→ 磁盘快照兜底。"""
    monkeypatch.setattr("app.storage.snapshots._SNAP_DIR", tmp_path)
    quote.save_snapshot_disk(
        "sina",
        pd.DataFrame(
            {
                "code": ["600519.SH", "000001.SZ"],
                "name": ["贵州茅台", "平安银行"],
                "price": [1500.0, 11.0],
            }
        ),
    )
    p = quote.SinaProvider()
    p._spot_ts = 0.0  # 模拟重启后无内存缓存

    def boom(page, num=100, retries=0):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(p, "_fetch_page", boom)
    out = p.get_spot()
    assert out is not None and len(out) == 2
    assert set(out["code"]) == {"600519.SH", "000001.SZ"}


def test_akshare_provider_disk_fallback(tmp_path, monkeypatch):
    """akshare 双源(东财/腾讯)均失败且无内存缓存 → 磁盘快照兜底。"""
    monkeypatch.setattr("app.storage.snapshots._SNAP_DIR", tmp_path)
    quote.save_snapshot_disk(
        "akshare",
        pd.DataFrame(
            {
                "code": ["600519.SH"],
                "name": ["贵州茅台"],
                "price": [1500.0],
            }
        ),
    )
    p = quote.AkshareProvider()
    p._spot_ts = 0.0

    def boom(*a, **k):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(providers_base, "_ak_retry", boom)
    out = p.get_spot()
    assert out is not None and len(out) == 1


def test_panel_npz_offline(monkeypatch):
    """面板数据离线加载：K线源全挂时从 npz 磁盘冷缓存取面板。
    以 meta 记录的行数为准（模拟 klines 无变化的离线场景）。"""
    from app.core import datasets as D

    ds_id = 3
    npz_path, meta_path = D._panel_paths(ds_id)
    import json
    import os

    if not (os.path.exists(npz_path) and os.path.exists(meta_path)):
        pytest.skip("无 ds=3 面板磁盘缓存（需先构建沪深300数据集）")
    meta = json.load(open(meta_path, encoding="utf-8"))

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(D, "cached_kline", _Boom)
    monkeypatch.setattr(D, "_klines_row_count", lambda: meta["row_count"])
    info = D.load_panel(ds_id)
    assert info is not None
    assert info["stock_count"] > 0 and len(info["dates"]) > 100
    assert "close" in info["panel"]


def test_klines_row_count_from_independent_db(tmp_path, monkeypatch):
    """面板 row_count 校验走 K 线独立库：T-103 后主库已无 klines 表，
    修复前 _klines_row_count 查询主库恒抛异常恒返 0，导致冷缓存
    「klines 行数变化自动重建」契约静默失效（面板 npz 只靠 7 天 TTL 过期）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core import datasets as D
    from app.storage import klines_db
    from app.storage.klines_db import Kline

    engine = create_engine(
        f"sqlite:///{tmp_path / 'kline.db'}", connect_args={"check_same_thread": False}
    )
    klines_db.ensure_schema(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    # datasets 内为函数级 import（运行时读 klines_db.klines_session 当前值），
    # monkeypatch 后 _klines_row_count 自动指向临时库
    monkeypatch.setattr(klines_db, "klines_session", maker)

    # 空库 → 0
    assert D._klines_row_count() == 0

    # 插入 3 行 → 返回真实行数而非恒 0
    db = maker()
    try:
        db.add_all(
            [
                Kline(
                    code="600519.SH",
                    period="daily",
                    date=f"2026-08-{d:02d}",
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    volume=100.0,
                    amount=1e6,
                )
                for d in range(1, 4)
            ]
        )
        db.commit()
    finally:
        db.close()
    assert D._klines_row_count() == 3

    # 库不可用（模拟文件缺失/损坏）→ graceful 返 0 不抛异常
    def boom_maker():
        raise RuntimeError("无法打开 K 线库")

    monkeypatch.setattr(klines_db, "klines_session", boom_maker)
    assert D._klines_row_count() == 0


def test_panel_disk_missing_feature_triggers_rebuild(tmp_path, monkeypatch):
    """旧版 npz 缺新特征 → 视为磁盘 miss 触发重建（P0-29 修复前 load_panel
    在 `{f: result["panel"][f] for f in want}` 处 KeyError 500）。"""
    import json
    import time

    import numpy as np

    from app.core import datasets as D

    monkeypatch.setattr(D, "PANEL_DIR", tmp_path)
    monkeypatch.setattr(D, "_klines_row_count", lambda: 5)
    ds_id = 42
    npz_path, meta_path = D._panel_paths(ds_id)
    np.savez_compressed(
        npz_path,
        dates=np.array(["2026-01-01"] * 5, dtype=str),
        codes=np.array(["600519.SH", "000001.SZ"], dtype=str),
        close=np.zeros((2, 5)),
    )
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "row_count": 5}, f)

    # 只请求已存在特征 → 命中
    got = D._load_panel_disk(ds_id, want=["close"])
    assert got is not None and "close" in got["panel"]
    # 请求旧版缺失的新特征 → miss（触发重建路径，不返回缺特征面板）
    assert D._load_panel_disk(ds_id, want=["close", "volume"]) is None


def test_delete_panel_files_purges_hot_cache(monkeypatch, tmp_path):
    """删除数据集必须同时清理 panel:{ds_id}:* 内存热缓存（P0-29）。"""
    from app.core import datasets as D
    from app.storage.cache import TTLCache

    monkeypatch.setattr(D, "PANEL_DIR", tmp_path / "panels")
    fake = TTLCache(ttl=3600)
    fake.set("panel:5:close", {"x": 1})
    fake.set("panel:5:close,volume", {"x": 2})
    fake.set("panel:6:close", {"x": 3})  # 其它数据集不受影响
    monkeypatch.setattr("app.storage.cache.panel_cache", fake)

    D.delete_panel_files(5)
    assert fake.get("panel:5:close") is None
    assert fake.get("panel:5:close,volume") is None
    assert fake.get("panel:6:close") is not None


def test_delete_dataset_purges_hot_cache(tmp_path, monkeypatch):
    """API 删除数据集后：DB 行删除 + panel 热缓存立即失效（P0-29）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.api.datasets import delete_dataset
    from app.storage.db import Base
    from app.storage.models import Dataset
    from app.core import datasets as D
    from app.storage.cache import TTLCache

    monkeypatch.setattr(D, "PANEL_DIR", tmp_path / "panels")
    fake = TTLCache(ttl=3600)
    monkeypatch.setattr("app.storage.cache.panel_cache", fake)

    engine = create_engine(
        f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine)

    db = Maker()
    ds = Dataset(
        name="x",
        universe="custom",
        start_date="2026-01-01",
        end_date="2026-06-30",
    )
    db.add(ds)
    db.commit()
    ds_id = ds.id
    db.close()

    fake.set(f"panel:{ds_id}:close", {"close": 1})
    db = Maker()
    try:
        res = delete_dataset(ds_id, db=db)
        assert res["ok"] is True
    finally:
        db.close()
    assert fake.get(f"panel:{ds_id}:close") is None
    db = Maker()
    try:
        assert db.get(Dataset, ds_id) is None
    finally:
        db.close()


def test_industry_map_retries_after_failed_build(monkeypatch):
    """行业映射：磁盘过期后一次构建失败不得把空值（ts=0）永久留在内存——
    下次调用重新读盘并重试后台构建（P1-22 修复）。"""
    import time

    import app.storage.industry as IND

    calls = {"build": 0}

    def fake_load_disk():
        return None  # 无可用磁盘缓存（缺失/过期）

    def fake_start_build():
        calls["build"] += 1

    monkeypatch.setattr(IND, "_load_disk", fake_load_disk)
    monkeypatch.setattr(IND, "_start_build", fake_start_build)
    monkeypatch.setattr(IND, "_INDUSTRY_CACHE", None)

    # 首启无缓存 → 触发构建；构建失败后内存为 ts=0 空值
    assert IND.get_industry_map() == {}
    assert calls["build"] == 1
    # 再次调用：ts=0 恒过期 → 重读盘（仍无）→ 重试构建（修复前不再重试）
    assert IND.get_industry_map() == {}
    assert calls["build"] == 2

    # 构建成功落盘：读盘命中新映射，不再触发构建
    monkeypatch.setattr(
        IND, "_load_disk", lambda: {"ts": time.time(), "map": {"600519": "白酒"}}
    )
    assert IND.get_industry_map() == {"600519": "白酒"}
    assert calls["build"] == 2

    # 有旧映射但已过期且读盘失败 → 保留旧映射兜底并重试构建（不清空）
    monkeypatch.setattr(IND, "_load_disk", lambda: None)
    monkeypatch.setattr(
        IND,
        "_INDUSTRY_CACHE",
        {"ts": time.time() - IND.CACHE_TTL - 10, "map": {"000001": "银行"}},
    )
    assert IND.get_industry_map() == {"000001": "银行"}
    assert calls["build"] == 3


# ---------- 交易时段感知缓存 ----------


def test_session_spot_ttl():
    from app.lib.session import session_spot_ttl
    import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    weekday = datetime.datetime(2026, 8, 10, 10, 0, tzinfo=tz)  # 周一盘中
    weekend = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=tz)  # 周日
    assert session_spot_ttl(60, now=weekday) == 90  # 交易时段固定 90s（P0-2）
    assert session_spot_ttl(60, now=weekend) == 1800


def test_is_stale_session_aware(monkeypatch):
    """非交易时段：3 天前的日线不重复拉；交易时段内滞后即刷新。"""
    import datetime
    from app.storage import klines as kcache
    from app.lib import session as sess

    today = datetime.date(2026, 8, 10)  # 冻结时钟：周一
    monkeypatch.setattr(kcache, "_cn_day", lambda: today.isoformat())
    # is_stale 收盘后刷新路径读真实 session.now_cn（非 monkeypatch 的 _cn_day），
    # 不冻结会让断言依赖真实系统日期/时刻 → 冻结在 09:00（< 15:05 收盘线，跳过该分支）
    monkeypatch.setattr(sess, "now_cn", lambda: datetime.datetime(2026, 8, 10, 9, 0))
    monkeypatch.setattr(
        kcache, "_get_partial_date", lambda code, period, db=None: None
    )  # 隔离真实DB标记
    d = lambda n: (today - datetime.timedelta(days=n)).isoformat()
    df = pd.DataFrame({"date": [d(5), d(4), d(3)]})  # 升序，3 天前（上周五收盘）

    monkeypatch.setattr(sess, "in_trading_session", lambda now=None: True)
    assert kcache.is_stale(df, "daily", code="600519.SH") is True

    monkeypatch.setattr(sess, "in_trading_session", lambda now=None: False)
    assert kcache.is_stale(df, "daily", code="600519.SH") is False

    # 超过 4 天（长假后）：非交易时段也视为 stale
    df_old = pd.DataFrame({"date": [d(10), d(9)]})
    assert kcache.is_stale(df_old, "daily", code="600519.SH") is True


# ---------- 缓存统计 + 拉取频率表 + 并发去重 ----------


def test_ttlcache_stats():
    """TTLCache 命中统计：hits/misses/hit_rate 随 put/get 正确变化。"""
    from app.storage.cache import TTLCache

    c = TTLCache(ttl=60)
    assert c.stats() == {"size": 0, "hits": 0, "misses": 0, "hit_rate": 0.0}
    assert c.get("a") is None  # miss
    c.set("a", 1)
    assert c.get("a") == 1  # hit
    assert c.get("a") == 1  # hit
    assert c.get("b") is None  # miss
    s = c.stats()
    assert s["hits"] == 2 and s["misses"] == 2 and s["hit_rate"] == 0.5
    assert s["size"] == 1
    # 过期淘汰计入 miss
    c2 = TTLCache(ttl=0.01)
    c2.set("x", 1)
    time.sleep(0.05)
    assert c2.get("x") is None
    assert c2.stats()["misses"] == 1 and c2.stats()["hits"] == 0


def test_all_cache_stats_shape():
    """all_cache_stats 覆盖全部全局实例并含 total 合计。"""
    from app.storage.cache import all_cache_stats

    out = all_cache_stats()
    for name in ("panel", "indicator", "risk", "tune", "news"):
        assert set(out[name]) == {"size", "hits", "misses", "hit_rate"}
    t = out["total"]
    assert t["size"] == sum(out[n]["size"] for n in out if n != "total")
    assert t["hits"] == sum(out[n]["hits"] for n in out if n != "total")
    assert 0.0 <= t["hit_rate"] <= 1.0


def test_is_stale_reads_staleness_table(monkeypatch):
    """is_stale 行为由 STALENESS 表驱动：改表值 → 新鲜度判定随之改变。"""
    import datetime
    from app.storage import klines as kcache
    from app.lib import session as sess

    today = datetime.date(2026, 8, 10)  # 冻结时钟：周一
    monkeypatch.setattr(kcache, "_cn_day", lambda: today.isoformat())
    # 冻结 session.now_cn：收盘后刷新路径读真实时钟 → 断言依赖真实日期/时刻
    monkeypatch.setattr(sess, "now_cn", lambda: datetime.datetime(2026, 8, 10, 9, 0))
    monkeypatch.setattr(kcache, "_get_partial_date", lambda code, period, db=None: None)
    monkeypatch.setattr(sess, "in_trading_session", lambda now=None: False)

    d = lambda n: (today - datetime.timedelta(days=n)).isoformat()
    df = pd.DataFrame({"date": [d(5), d(4), d(3)]})  # 3 天前
    # 默认 off_session_grace_days=4 → 非交易时段 3 天前不 stale
    assert kcache.is_stale(df, "daily", code="600519.SH") is False
    # 收紧表值 → 3 天前即 stale
    monkeypatch.setitem(
        kcache.STALENESS, "daily", {"session_lag_days": 1, "off_session_grace_days": 2}
    )
    assert kcache.is_stale(df, "daily", code="600519.SH") is True

    # weekly：today 距 11 天前 → 默认 stale；放宽 max_lag_days 后不 stale
    dfw = pd.DataFrame({"date": [d(11)]})
    assert kcache.is_stale(dfw, "weekly") is True
    monkeypatch.setitem(kcache.STALENESS, "weekly", {"max_lag_days": 14})
    assert kcache.is_stale(dfw, "weekly") is False


def test_run_inflight_dedup():
    """并发同 key 拉取去重：同一时刻只执行一次 fn（barrier 保证并发到达）。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.storage import klines as kcache

    kcache._inflight.clear()
    N = 8
    barrier = threading.Barrier(N)
    calls = []

    def fn():
        calls.append(1)
        time.sleep(0.5)
        return "data"

    def caller():
        barrier.wait()
        return kcache._run_inflight("600519.SH:daily", fn)

    with ThreadPoolExecutor(max_workers=N) as pool:
        fs = [pool.submit(caller) for _ in range(N)]
        out = [f.result() for f in fs]
    assert len(calls) == 1
    assert out == ["data"] * N
    assert kcache._inflight == {}


def test_cached_kline_inflight_dedup(monkeypatch):
    """cached_kline 并发调用同一 code:period → 只发一次网络请求（barrier 保证并发到达）。"""
    import threading
    from app.storage import klines as kcache

    kcache._inflight.clear()
    N = 3
    barrier = threading.Barrier(N)
    lock = threading.Lock()
    calls = []

    def slow_get_kline(
        code, period, days=None, start_date=None, compose_intraday=False
    ):
        with lock:
            calls.append((code, period))
        time.sleep(0.5)
        return pd.DataFrame(
            {
                "date": ["2026-08-07"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [100.0],
                "amount": [1000.0],
            }
        )

    monkeypatch.setattr(kcache, "load_cached", lambda codes, period: {})
    monkeypatch.setattr(kcache, "get_kline", slow_get_kline)
    # patch 使用点 kcache.get_provider(未 bind 时 storage 层抛 RuntimeError,P1-53a)
    monkeypatch.setattr(
        kcache, "get_provider", lambda: type("P", (), {"name": "test"})()
    )
    upserts = []
    monkeypatch.setattr(kcache, "upsert_many", lambda cp, source: upserts.extend(cp))

    def caller():
        barrier.wait()
        return kcache.cached_kline("600519.SH", "daily")

    results: list = []
    ts = [threading.Thread(target=lambda: results.append(caller())) for _ in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1  # 只发了一次网络请求
    assert len(upserts) == N  # 各调用方分别写库（upsert 幂等，不重复拉取）
    assert all(u[0] == "600519.SH" and u[1] == "daily" for u in upserts)
    assert all(len(r) == 1 and r["close"].iloc[0] == 10.5 for r in results)
    assert kcache._inflight == {}
