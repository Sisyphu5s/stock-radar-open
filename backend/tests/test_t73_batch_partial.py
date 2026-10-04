"""T-73 批量路径盘中 partial 命中:跳过日线全量重拉(fetch_many/_fetch_one)。

背景(T-67 残余):
- 单只路径 cached_kline 盘中 partial 命中 → _fetch_start 返回 (start_date=今日,
  days=None) + compose_intraday=True → provider 短路只拉当日 1 分钟线聚合(免日线
  全量重拉,见 test_compose_intraday)。
- 批量路径 _fetch_one(不传 compose_intraday)盘中 partial 命中时:同样拿到
  start_date=今日,但 sina 日线接口不支持服务端 start_date(全量下载 1600 根)、
  盘中又无当日 bar → 每次 5min 扫描轮询对每只 partial 股票发 3 次×1600 根日线
  请求且结果为空。T-73 修复:命中「sina 源 + daily + 重拉最后 bar + 盘中 partial」
  直接返回缓存旧值,不重拉;降频由扫描轮次天然承担,收盘后(≥15:05)恢复重拉。

契约:
1. 盘中(2026-08-11 10:00)+ 缓存最后 bar=今日(partial 标记) + sina 生效 →
   _fetch_one 不调 get_kline,直接返回缓存旧值。
2. 同缓存,收盘后(15:30)→ 正常重拉(真实 bar 覆盖 partial)。
3. 同缓存,盘中但非 sina 源(akshare)→ 正常重拉(akshare 支持 start_date)。
4. 盘中无 partial(缓存最后 bar=昨日)→ 正常增量拉取,参数不变。
5. fetch_many 全链路:盘中 partial → 结果保留缓存旧值,get_kline 未被调用。
6. 既有兼容:非 partial 场景 _fetch_one 拉取行为不变。

基建仿 test_kcache_intraday:临时 SQLite(monkeypatch kcache.SessionLocal,
不触碰生产库);冻结 kcache._cn_now 与 session.now_cn(盘中/收盘后精确模拟);
get_kline/get_provider 全部 mock,零网络。

测试基准日:2026-08-10 周一 / 2026-08-11 周二(与既有测试同基线)。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage import klines as kcache
from app.lib import session as sess


class _FrozenClock:
    """固定 Asia/Shanghai 墙钟(naive);_FROZEN 可在用例内改以模拟盘中/收盘后。"""

    _FROZEN = datetime(2026, 8, 11, 10, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


@pytest.fixture()
def frozen_cn(monkeypatch):
    """冻结 kcache._cn_now 与 session.now_cn(盘中判定/交易时段跟随冻结时间)。"""
    monkeypatch.setattr(kcache, "_cn_now", lambda: _FrozenClock._FROZEN)
    monkeypatch.setattr(sess, "now_cn", lambda: _FrozenClock._FROZEN)
    return _FrozenClock


@pytest.fixture()
def kline_db(tmp_path, monkeypatch):
    """临时 SQLite K 线库:替换 kcache.SessionLocal(不触碰生产库)。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'kcache.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(kcache, "SessionLocal", Maker)
    try:
        yield Maker
    finally:
        engine.dispose()


def _mk_df(last_date: str, n: int = 5) -> pd.DataFrame:
    """手工构造 K 线 df(date 升序,最后一行=last_date,date 为 'YYYY-MM-DD' 字符串)。"""
    dates = pd.bdate_range(end=last_date, periods=n).strftime("%Y-%m-%d").tolist()
    close = np.linspace(10.0, 11.0, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
        }
    )


def _partial_value(maker, code: str = "600000.SH") -> str | None:
    db = maker()
    try:
        return kcache._get_partial_date(code, "daily", db)
    finally:
        db.close()


class _FakeProvider:
    def __init__(self, name: str = "sina"):
        self.name = name


@pytest.fixture()
def sina_active(monkeypatch):
    """生效数据源固定为 sina(_fetch_one 的 _is_sina_active 判定来源)。

    patch 使用点 kcache.get_provider(bind 前唯一取值通道,与 kcache.get_kline 同惯例);
    未 bind 时 storage.klines.get_provider 抛 RuntimeError,禁止反向 import 兜底(P1-53a)。
    """
    monkeypatch.setattr(kcache, "get_provider", lambda: _FakeProvider("sina"))


def _install_net_spy(monkeypatch) -> dict:
    """mock kcache.get_kline(模块级导入绑定):记录调用次数,返回今日 df。"""
    calls = {"n": 0}

    def fake_get_kline(*args, **kwargs):
        calls["n"] += 1
        return _mk_df("2026-08-11")

    monkeypatch.setattr(kcache, "get_kline", fake_get_kline)
    return calls


# ---------------------------------------------------------------------------
# 1) 盘中 partial 命中 + sina → 跳过重拉,沿用缓存
# ---------------------------------------------------------------------------


def test_batch_partial_hit_intraday_skips_refetch(
    kline_db, frozen_cn, sina_active, monkeypatch
):
    """盘中(10:00)缓存最后 bar=今日(partial 标记),sina 生效:
    _fetch_one 不调 get_kline,直接返回缓存旧值(免 1600 根日线全量重拉)。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-11"))], "test")
    assert _partial_value(kline_db) == "2026-08-11"  # 盘中写入 → partial 标记

    calls = _install_net_spy(monkeypatch)
    cached = kcache.load_cached(["600000.SH"], "daily")["600000.SH"]

    res = kcache._fetch_one("600000.SH", "daily", cached)
    assert calls["n"] == 0, "盘中 partial 命中不得发日线重拉请求"
    assert res is not None and len(res)
    assert str(res["date"].iloc[-1]) == "2026-08-11"  # 沿用缓存今日 partial bar


# ---------------------------------------------------------------------------
# 2) 同缓存,收盘后 → 正常重拉(真实 bar 覆盖 partial)
# ---------------------------------------------------------------------------


def test_batch_partial_hit_after_close_refetches(
    kline_db, frozen_cn, sina_active, monkeypatch
):
    """同缓存,收盘后(15:30,≥15:05):partial 判定不成立 → 正常重拉,get_kline 被调用。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-11"))], "test")
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 15, 30)  # 收盘后

    calls = _install_net_spy(monkeypatch)
    cached = kcache.load_cached(["600000.SH"], "daily")["600000.SH"]

    res = kcache._fetch_one("600000.SH", "daily", cached)
    assert calls["n"] == 1, "收盘后必须正常重拉真实 bar"
    assert res is not None and len(res)


# ---------------------------------------------------------------------------
# 3) 盘中 partial + 非 sina 源 → 正常重拉(akshare 支持 start_date)
# ---------------------------------------------------------------------------


def test_batch_partial_hit_non_sina_refetches(kline_db, frozen_cn, monkeypatch):
    """盘中 partial 命中但生效源为 akshare → 不跳过,正常重拉
    (akshare 日线支持服务端 start_date,盘中可拉当日 bar)。"""
    monkeypatch.setattr(kcache, "get_provider", lambda: _FakeProvider("akshare"))
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-11"))], "test")
    assert _partial_value(kline_db) == "2026-08-11"

    calls = _install_net_spy(monkeypatch)
    cached = kcache.load_cached(["600000.SH"], "daily")["600000.SH"]

    res = kcache._fetch_one("600000.SH", "daily", cached)
    assert calls["n"] == 1, "非 sina 源不跳过重拉"
    assert res is not None and len(res)


# ---------------------------------------------------------------------------
# 4) 盘中无 partial(缓存最后 bar=昨日)→ 正常增量拉取,参数不变
# ---------------------------------------------------------------------------


def test_batch_no_partial_intraday_increments(
    kline_db, frozen_cn, sina_active, monkeypatch
):
    """盘中缓存最后 bar=昨日(无 partial 标记)→ 跳过条件不成立,
    正常增量拉取(start_date=今日,days=None),既有 _fetch_one 行为不变。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-10"))], "test")
    assert _partial_value(kline_db) is None  # 昨日 bar 不设 partial

    seen = []

    def spy_get_kline(*args, **kwargs):
        seen.append((kwargs.get("days"), kwargs.get("start_date")))
        return _mk_df("2026-08-11")

    monkeypatch.setattr(kcache, "get_kline", spy_get_kline)
    cached = kcache.load_cached(["600000.SH"], "daily")["600000.SH"]

    res = kcache._fetch_one("600000.SH", "daily", cached)
    assert len(seen) == 1
    assert seen[0] == (None, "2026-08-11")  # 增量参数契约不变
    assert str(res["date"].iloc[-1]) == "2026-08-11"


# ---------------------------------------------------------------------------
# 5) fetch_many 全链路:盘中 partial → 保留缓存,get_kline 零调用
# ---------------------------------------------------------------------------


def test_fetch_many_intraday_partial_keeps_cache(
    kline_db, frozen_cn, sina_active, monkeypatch
):
    """fetch_many 全链路:盘中 partial 命中(40 根缓存,满足返回下限)→
    missing 判定走 is_stale(partial 命中 → 需重拉),但 _fetch_one 跳过重拉,
    get_kline 零调用,结果仍含缓存今日 partial bar。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-11", 40))], "test")
    assert _partial_value(kline_db) == "2026-08-11"

    calls = _install_net_spy(monkeypatch)
    res = kcache.fetch_many(["600000.SH"], "daily")
    assert calls["n"] == 0, "批量扫描不得对盘中 partial 股票发日线重拉请求"
    assert "600000.SH" in res
    assert str(res["600000.SH"]["date"].iloc[-1]) == "2026-08-11"
    assert len(res["600000.SH"]) == 40  # 缓存完整保留


# ---------------------------------------------------------------------------
# 6) 兼容:收盘后 partial 命中 → 恢复重拉真实 bar 并清除标记
# ---------------------------------------------------------------------------


def test_fetch_many_after_close_partial_refetches(
    kline_db, frozen_cn, sina_active, monkeypatch
):
    """收盘后 partial 命中(标记仍存,15:30 ≥ 15:05):跳过判定不成立 →
    fetch_many 正常重拉真实 bar 写回,partial 标记清除(修复边界语义)。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-11", 40))], "test")
    assert _partial_value(kline_db) == "2026-08-11"
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 15, 30)  # 收盘后

    calls = _install_net_spy(monkeypatch)
    res = kcache.fetch_many(["600000.SH"], "daily")
    assert calls["n"] == 1, "收盘后 partial 应正常重拉真实 bar"
    assert "600000.SH" in res
    assert str(res["600000.SH"]["date"].iloc[-1]) == "2026-08-11"
    # 收盘后重拉写入 → partial 标记清除(当日 bar 视为完整)
    assert _partial_value(kline_db) is None


def test_fetch_one_period_gate_non_daily(kline_db, frozen_cn, sina_active, monkeypatch):
    """周期守卫:周/月线不走跳过逻辑(period != daily)→ 正常重拉。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    # 直接构造缓存最后 bar=今日 的周线(模拟盘中 partial 形态,周期守卫应拦截)
    calls = _install_net_spy(monkeypatch)
    weekly = _mk_df("2026-08-11")
    res = kcache._fetch_one("600000.SH", "weekly", weekly)
    assert calls["n"] == 1, "周/月线不得走盘中跳过逻辑"
    assert res is not None and len(res)
