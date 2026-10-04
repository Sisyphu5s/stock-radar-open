"""kcache 盘中日K刷新语义测试（partial 标记驱动）。

覆盖「盘中实时刷新」后端链路：
1. 盘中（< 15:05）写入含当日 bar 的 df → upsert_many 设置 partial 标记；is_stale True（需重拉）。
2. 收盘后（≥ 15:05）写同一 df → partial 标记清除；is_stale False。
3. _fetch_start：partial 标记 == 最后日期 → 从该日重拉完整 bar；无标记 → 最后日期+1 天增量。
4. 盘中缓存最后 bar 是昨天（days_since=1）→ is_stale True；非交易时段同数据 4 天内不 stale。
5. 周五收盘数据（最后日期=周五），周六/周日冻结时间 → 不 stale（off_session_grace_days）。

基建（仿 test_memory_bounds / test_signal_as_of，只读参考）：
- 临时 SQLite（tmp_path + monkeypatch kcache.SessionLocal），绝不触碰生产库。
- 冻结 kcache._cn_now 与 session.now_cn：kcache 的 is_stale 内部 `from .session import
  in_trading_session` 按真实时钟判定交易时段，联动冻结 session.now_cn 后，
  in_trading_session 自动跟随测试时钟（盘中/收盘后/周末均可精确模拟）。
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

# 测试基准日：2026-08-10 周一（2026-08-07 周五 / 08-08 周六 / 08-09 周日 / 08-11 周二）


class _FrozenClock:
    """固定 Asia/Shanghai 墙钟（naive）；_FROZEN 可在用例内改以模拟盘中/收盘后/周末。"""

    _FROZEN = datetime(2026, 8, 10, 10, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


@pytest.fixture()
def frozen_cn(monkeypatch):
    """冻结 kcache._cn_now（_is_partial_daily/_cn_day 取时）与 session.now_cn
    （is_stale 内 in_trading_session 的真实时钟来源，联动后交易时段判定跟随冻结时间）。"""
    monkeypatch.setattr(kcache, "_cn_now", lambda: _FrozenClock._FROZEN)
    monkeypatch.setattr(sess, "now_cn", lambda: _FrozenClock._FROZEN)
    return _FrozenClock


@pytest.fixture()
def kline_db(tmp_path, monkeypatch):
    """临时 SQLite K 线库：替换 kcache.SessionLocal（不触碰生产库）。"""
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
    """手工构造 K 线 df（date 升序，最后一行=last_date，date 为 'YYYY-MM-DD' 字符串）。"""
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
    """读 partial 标记（YYYY-MM-DD 或 None）。"""
    db = maker()
    try:
        return kcache._get_partial_date(code, "daily", db)
    finally:
        db.close()


def _cached_df(code: str = "600000.SH") -> pd.DataFrame:
    """从临时库读回缓存（load_cached 返回 key=normalize_code）。"""
    return kcache.load_cached([code], "daily")[code]


# ---------------------------------------------------------------------------
# 1) 盘中写入含当日 bar → 设置 partial 标记；is_stale True
# ---------------------------------------------------------------------------


def test_intraday_upsert_sets_partial_and_stale(kline_db, frozen_cn):
    """周一 10:00 盘中（< 15:05）写入含当日 bar 的 df：
    upsert_many 设置 partial 标记，is_stale 返回 True（需重拉完整 bar）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    df = _mk_df("2026-08-10")  # 最后一行 = 今天（盘中未收盘 bar）
    written = kcache.upsert_many([("600000.SH", "daily", df)], "test")
    assert written == len(df)
    # partial 标记 == 最后日期
    assert _partial_value(kline_db) == "2026-08-10"
    # 从缓存读回判定：partial 标记命中 → stale（盘中未收盘 bar 需重拉）
    cached = _cached_df()
    assert kcache.is_stale(cached, "daily", "600000.SH") is True


# ---------------------------------------------------------------------------
# 2) 收盘后写同一 df → 清除 partial 标记；is_stale False
# ---------------------------------------------------------------------------


def test_after_close_upsert_clears_partial_and_not_stale(kline_db, frozen_cn):
    """先盘中写（设标记），再 15:30 收盘后（≥ 15:05）写同一 df：
    标记被清除（当日 bar 视为已收盘完整），is_stale False。"""
    df = _mk_df("2026-08-10")
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", df)], "test")
    assert _partial_value(kline_db) == "2026-08-10"

    _FrozenClock._FROZEN = datetime(2026, 8, 10, 15, 30)  # 收盘后
    kcache.upsert_many([("600000.SH", "daily", df)], "test")
    assert _partial_value(kline_db) is None  # 标记已清除
    cached = _cached_df()
    assert kcache.is_stale(cached, "daily", "600000.SH") is False  # 当天数据，不 stale


# ---------------------------------------------------------------------------
# 3) _fetch_start：partial 命中 → 从该日重拉完整；无标记 → 增量（最后日期+1 天）
# ---------------------------------------------------------------------------


def test_fetch_start_partial_rerolls_complete_bar(kline_db, frozen_cn):
    """_fetch_start：partial 标记存在且 == 最后日期 → 返回最后日期（重拉完整 bar）。"""
    df = _mk_df("2026-08-10")
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)  # 盘中 → 设标记
    kcache.upsert_many([("600000.SH", "daily", df)], "test")
    assert _partial_value(kline_db) == "2026-08-10"
    db = kline_db()
    try:
        start, days = kcache._fetch_start(df, "daily", None, "600000.SH", db)
        assert start == "2026-08-10"  # 从该日重拉完整 bar（覆盖盘中未收盘 bar）
        assert days is None
    finally:
        db.close()


def test_fetch_start_no_partial_increments_next_day(kline_db, frozen_cn):
    """_fetch_start：无 partial 标记 → 返回最后日期+1 天（增量拉取）。"""
    df = _mk_df("2026-08-10")
    db = kline_db()
    try:
        start, days = kcache._fetch_start(df, "daily", None, "600000.SH", db)
        assert start == "2026-08-11"
        assert days is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4) 盘中滞后 1 天 → stale；非交易时段同数据 → 4 天内不 stale
# ---------------------------------------------------------------------------


def test_daily_lag_one_day_session_aware(kline_db, frozen_cn):
    """缓存最后 bar 是昨天（days_since=1）：
    盘中 → stale（滞后 >= session_lag_days 即刷新）；
    工作日收盘后（周二 21:00）→ stale（语义变更：收盘后缺今日 bar 即刷新——
    否则今日蜡烛要等次日盘中才出现，同花顺级体验要求收盘后当日可见；
    周末/节假日仍走 off_session_grace_days 宽限，见 test_friday_close_weekend_not_stale）。"""
    df = _mk_df("2026-08-10")  # 最后日期 = 昨天
    # 周二 10:00 盘中：滞后 1 天 → stale
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)
    assert kcache.is_stale(df, "daily", "600000.SH") is True
    # 周二 21:00 工作日收盘后：缺今日 bar → stale（收盘后首访即刷新）
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 21, 0)
    assert kcache.is_stale(df, "daily", "600000.SH") is True


# ---------------------------------------------------------------------------
# 5) 周五收盘数据：周六/周日冻结 → 不 stale（off_session_grace_days）
# ---------------------------------------------------------------------------


def test_friday_close_weekend_not_stale(kline_db, frozen_cn):
    """周五（2026-08-07）收盘数据，周六/周日冻结时间：
    days_since <= off_session_grace_days(4) 且非交易时段 → 不 stale（周一早盘前不重复拉）。"""
    df = _mk_df("2026-08-07")  # 最后日期 = 周五
    _FrozenClock._FROZEN = datetime(2026, 8, 8, 20, 0)  # 周六晚
    assert kcache.is_stale(df, "daily", "600000.SH") is False
    _FrozenClock._FROZEN = datetime(2026, 8, 9, 20, 0)  # 周日晚
    assert kcache.is_stale(df, "daily", "600000.SH") is False
    # 边界对照：超过 4 天宽限（周二晚，days_since=5）才 stale
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 21, 0)
    assert kcache.is_stale(df, "daily", "600000.SH") is True


# ===========================================================================
# 6) 周/月K staleness 联动日线缓存最后日期（compose_intraday 配套）
# ===========================================================================
# 背景：sina 周/月=日线重采样，当前按 max_lag_days(8/32) 判定，不感知盘中日K新 bar →
# 周K缓存停更永不刷新。修复：is_stale 新增 daily_last_date 参数联动日线最后日期；
# fetch_many 批量轻量 SQL 预载 map；单只路径内部读 daily 缓存；cached_kline 传
# compose_intraday=True（唯一 True），_fetch_one 不传（全市场扫描不合成）。


def test_weekly_monthly_linkage_via_daily_last_date(kline_db):
    """周/月联动：传入 daily_last_date > 周期缓存最后 bar → stale；<= → 不 stale。"""
    df = _mk_df("2026-08-07")  # 周/月缓存最后 bar = 周五 08-07
    # 日线已有更新 bar（08-10）→ 重采样未聚合 → stale
    assert kcache.is_stale(df, "weekly", daily_last_date="2026-08-10") is True
    assert kcache.is_stale(df, "monthly", daily_last_date="2026-08-10") is True
    # 日线最后日期 == 周期最后 bar → 已聚合 → 不 stale
    assert kcache.is_stale(df, "weekly", daily_last_date="2026-08-07") is False
    # 日线最后日期 < 周期最后 bar（正常，周K周五 bar 晚于日线？不常见但语义一致）→ 不 stale
    assert kcache.is_stale(df, "monthly", daily_last_date="2026-08-06") is False


def test_weekly_fallback_when_daily_cache_empty(kline_db, frozen_cn):
    """daily 缓存为空 → 联动不可用 → 兜底原 max_lag_days 判定，行为不变。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    # weekly 最后 bar 08-07（距今 3 天 <= 8）→ 不 stale
    assert kcache.is_stale(_mk_df("2026-08-07"), "weekly", "600000.SH") is False
    # 超过 8 天 → stale
    assert kcache.is_stale(_mk_df("2026-06-01"), "weekly", "600000.SH") is True
    # monthly：距今 3 天 <= 32 → 不 stale；超过 32 天 → stale
    assert kcache.is_stale(_mk_df("2026-08-07"), "monthly", "600000.SH") is False
    assert kcache.is_stale(_mk_df("2026-05-01"), "monthly", "600000.SH") is True


def test_daily_last_date_arg_skips_db_read(monkeypatch):
    """daily_last_date 传入时直接生效（不读库）：load_cached 若被调用即断言失败。"""

    def boom(*a, **k):
        raise AssertionError("daily_last_date 已传，不应内部读库")

    monkeypatch.setattr(kcache, "load_cached", boom)
    df = _mk_df("2026-08-07")
    assert (
        kcache.is_stale(df, "weekly", "600000.SH", daily_last_date="2026-08-10") is True
    )
    assert (
        kcache.is_stale(df, "weekly", "600000.SH", daily_last_date="2026-08-07")
        is False
    )


def test_is_stale_reads_daily_cache_when_arg_none(kline_db, frozen_cn):
    """单只路径（daily_last_date=None）：内部读该 code daily 缓存最后日期联动判定。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    # 种子 daily 最后日期 08-10；weekly 最后 bar 08-07
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-10"))], "test")
    weekly = _mk_df("2026-08-07")
    # daily(08-10) > weekly 最后 bar(08-07) → stale（日线有新 bar 未聚合）
    assert kcache.is_stale(weekly, "weekly", "600000.SH") is True
    # daily(08-10) == weekly 最后 bar(08-10) → 已聚合 → 不 stale
    assert kcache.is_stale(_mk_df("2026-08-10"), "weekly", "600000.SH") is False


def test_fetch_many_preloads_daily_last_map(kline_db, frozen_cn, monkeypatch):
    """fetch_many 批量预载 daily 最后日期 map：is_stale 收到正确的 daily_last_date。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    # 种子两只：daily 最后日期不同，weekly 均有缓存（is_stale 只对缓存的股票调用）
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-10", 8))], "test")
    kcache.upsert_many([("000001.SZ", "daily", _mk_df("2026-08-07", 8))], "test")
    kcache.upsert_many([("600000.SH", "weekly", _mk_df("2026-08-07", 8))], "test")
    kcache.upsert_many([("000001.SZ", "weekly", _mk_df("2026-08-07", 8))], "test")

    seen = []

    def spy_is_stale(
        df,
        period,
        code=None,
        db=None,
        daily_last_date=None,
        allow_after_close_refresh=True,
    ):
        seen.append((code, daily_last_date, allow_after_close_refresh))
        return False  # 全部视为新鲜 → 不触发网络拉取

    monkeypatch.setattr(kcache, "is_stale", spy_is_stale)
    kcache.fetch_many(["600000.SH", "000001.SZ"], "weekly")
    assert ("600000.SH", "2026-08-10", False) in seen  # 批量路径固定关闭收盘后刷新
    assert ("000001.SZ", "2026-08-07", False) in seen


class _FakeProvider:
    name = "test"


def test_cached_kline_passes_compose_intraday_true(kline_db, frozen_cn, monkeypatch):
    """单只路径 cached_kline：get_kline 收到 compose_intraday=True（唯一 True 处）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)  # 盘中
    # 种子 daily 缓存最后 bar 08-07 → 盘中滞后 3 天 → stale → 走拉取路径
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-07"))], "test")

    seen = {}

    def fake_get_kline(*args, **kwargs):
        seen["compose_intraday"] = kwargs.get("compose_intraday")
        return _mk_df("2026-08-10")

    monkeypatch.setattr(kcache, "get_kline", fake_get_kline)
    monkeypatch.setattr(kcache, "get_provider", lambda: _FakeProvider())

    res = kcache.cached_kline("600000.SH", "daily")
    assert seen.get("compose_intraday") is True
    assert str(res["date"].iloc[-1]) == "2026-08-10"


def test_fetch_one_defaults_compose_intraday_false(monkeypatch):
    """批量路径 _fetch_one：get_kline 不传 compose_intraday（默认 False，不合成）。"""
    seen = {}

    def fake_get_kline(*args, **kwargs):
        seen["compose_intraday"] = kwargs.get("compose_intraday")
        return _mk_df("2026-08-10")

    monkeypatch.setattr(kcache, "get_kline", fake_get_kline)
    res = kcache._fetch_one("600000.SH", "daily", _mk_df("2026-08-07"))
    assert res is not None and len(res)
    # 未传 → kwargs 无 compose_intraday（None，等同默认 False）；绝不为 True
    assert seen.get("compose_intraday") is not True


# ===========================================================================
# 7) 工作日收盘后刷新（off_session_grace_days 补洞）
# ===========================================================================
# 场景：收盘后（上海 >=15:05）首次访问，缓存最后 bar 停在更早交易日（且盘中未写过
# 合成 bar、无 partial 标记）→ 旧逻辑按 off_session_grace_days 不刷新，今日蜡烛缺失
# 一天。修复：工作日收盘后最后 bar < 今日 → stale（收盘后首访即拉当日真实 bar）。
# 周末/节假日不触发（数据冻结，仍走 grace 宽限）；周五收盘当天（最后 bar == 今日）不触发。


def test_weekday_after_close_stale_when_bar_older_than_today(frozen_cn):
    """工作日收盘后（周二 16:00），最后 bar=周一 → stale（收盘后首访即刷新今日）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 16, 0)  # 周二收盘后
    df = _mk_df("2026-08-10")  # 最后 bar = 周一
    assert kcache.is_stale(df, "daily", "600000.SH") is True


def test_weekday_after_close_fresh_when_bar_is_today(frozen_cn):
    """工作日收盘后（周五 16:00），最后 bar=周五（今日）→ 不 stale（不重复拉）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 14, 16, 0)  # 周五收盘后
    df = _mk_df("2026-08-14")  # 最后 bar = 周五 = 今日
    assert kcache.is_stale(df, "daily", "600000.SH") is False


def test_weekend_after_close_still_grace(frozen_cn):
    """周末收盘后（周六 16:00），最后 bar=周五 → 新分支不触发，仍按 grace 不 stale。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 15, 16, 0)  # 周六
    df = _mk_df("2026-08-14")  # 最后 bar = 周五
    assert kcache.is_stale(df, "daily", "600000.SH") is False


def test_weekday_before_close_unchanged(frozen_cn):
    """工作日盘中（周二 10:00），最后 bar=周一 → 原有盘中滞后判定 stale（行为不变）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 10, 0)  # 周二盘中
    df = _mk_df("2026-08-10")
    assert kcache.is_stale(df, "daily", "600000.SH") is True


def test_batch_path_disables_after_close_refresh(frozen_cn):
    """批量路径（allow_after_close_refresh=False）：工作日收盘后缺今日 → 仍按 grace 不 stale
    （防 5000 只全市场收盘后重拉冲击限流；fetch_many 调用处固定传 False）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 11, 21, 0)  # 周二晚
    df = _mk_df("2026-08-10")  # 最后 bar = 周一
    # 批量语义：不 stale
    assert (
        kcache.is_stale(df, "daily", "600000.SH", allow_after_close_refresh=False)
        is False
    )
    # 单只语义（默认 True）：stale
    assert kcache.is_stale(df, "daily", "600000.SH") is True


def test_future_label_treated_as_stale(frozen_cn):
    """未来标签守卫：周/月缓存最后 bar 是未来日期（旧版重采样残留，如月末 ME 标签）→
    一律 stale（脏缓存重拉修复），否则联动比较 daily_last < last_bar 会误判已聚合永不刷新；
    daily 同理（days_since<0 → stale）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)
    # 月K最后 bar = 8-31（未来）→ stale（即使 daily_last_date 参数更小）
    df = _mk_df("2026-08-07")
    df.loc[df.index[-1], "date"] = "2026-08-31"  # 模拟旧缓存未来标签
    assert kcache.is_stale(df, "monthly", daily_last_date="2026-08-12") is True
    assert kcache.is_stale(df, "weekly", daily_last_date="2026-08-12") is True
    # daily 未来标签（days_since < 0）→ stale
    df2 = _mk_df("2026-08-12")
    df2.loc[df2.index[-1], "date"] = "2026-08-31"
    assert kcache.is_stale(df2, "daily", "600000.SH") is True


def test_upsert_drops_future_dated_rows(kline_db, frozen_cn):
    """写入防御：upsert_many 的 _to_rows 剔除未来日期行（防脏标签再入库）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)
    df = _mk_df("2026-08-11")
    df.loc[df.index[-1], "date"] = "2026-08-31"  # 未来标签行
    assert kcache.upsert_many([("600000.SH", "monthly", df)], "test") == len(df) - 1
    rows = kcache.load_cached(["600000.SH"], "monthly")
    got = rows["600000.SH"]
    assert str(got["date"].iloc[-1]) == "2026-08-10"  # 未来行未入库，最后为 8-10


def test_weekly_monthly_upsert_full_rewrite(kline_db, frozen_cn):
    """周/月先删后插：重采样标签演进时同周期不留旧聚合 bar（防双 bar 时间轴重复）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)

    def one_bar(d: str) -> pd.DataFrame:
        """单根周期 bar（月K/周K重采样输出形态）。"""
        return pd.DataFrame(
            {
                "date": [d],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [100.0],
                "amount": [1000.0],
            }
        )

    # 第一次写入：8 月聚合到 08-07
    kcache.upsert_many([("600000.SH", "monthly", one_bar("2026-08-07"))], "test")
    # 第二次写入：8 月聚合推进到 08-12（新 bar）→ 旧 08-07 行应被清除
    kcache.upsert_many([("600000.SH", "monthly", one_bar("2026-08-12"))], "test")
    got = kcache.load_cached(["600000.SH"], "monthly")["600000.SH"]
    assert got["date"].tolist() == ["2026-08-12"]  # 仅新聚合行，旧行已删
    # weekly 同样生效
    kcache.upsert_many([("600000.SH", "weekly", one_bar("2026-08-10"))], "test")
    kcache.upsert_many([("600000.SH", "weekly", one_bar("2026-08-12"))], "test")
    w = kcache.load_cached(["600000.SH"], "weekly")["600000.SH"]
    assert w["date"].tolist() == ["2026-08-12"]


# ===========================================================================
# 8) 分钟 bar 未来日期过滤回归（T-36 / P0-20）
# ===========================================================================
# 背景：_to_rows 未来日期剔除曾用整串比较 `str(r["date"]) <= today`，分钟 bar 的
# date 为 "2026-08-12 10:30" 形态，整串比较恒大于 "2026-08-12" → 当日分钟 bar 全部
# 被误判为未来剔除（written=0）。修复：按日期前缀 str(r["date"])[:10] 比较。
# 本区块用冻结时钟固定"今日"并验证真实入库/读回路径（load_cached 按 period 过滤）。


def _minute_df(dates: list[str]) -> pd.DataFrame:
    """构造分钟 bar df（date 为 'YYYY-MM-DD HH:MM' 形态）。"""
    close = np.linspace(10.0, 10.0 + 0.2 * (len(dates) - 1), len(dates))
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.05,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": np.full(len(dates), 100.0),
            "amount": np.full(len(dates), 1000.0),
        }
    )


def test_minute_bar_today_not_dropped(kline_db, frozen_cn):
    """当日分钟 bar（date="2026-08-12 10:30" 形态）不被未来日期过滤误杀：
    修复前整串比较 str("2026-08-12 10:30") > "2026-08-12" → 3 根全部剔除（written=0）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)  # 收盘后，避免 partial 干扰
    df = _minute_df(["2026-08-12 09:30", "2026-08-12 10:30", "2026-08-12 11:00"])
    written = kcache.upsert_many([("600000.SH", "minute", df)], "test")
    assert written == 3
    got = kcache.load_cached(["600000.SH"], "minute")["600000.SH"]
    assert len(got) == 3
    assert got["date"].tolist() == [
        "2026-08-12 09:30",
        "2026-08-12 10:30",
        "2026-08-12 11:00",
    ]  # 当日分钟 bar 完整入库读回


def test_minute_bar_mixed_future_dropped(kline_db, frozen_cn):
    """同一 df 混入未来日期分钟 bar（"2026-08-13 10:30"）→ 仅未来行被剔除，
    当日 3 根保留（未来日期守卫对分钟 bar 仍生效）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)
    df = _minute_df(
        [
            "2026-08-12 09:30",
            "2026-08-12 10:30",
            "2026-08-12 11:00",
            "2026-08-13 10:30",  # 明日分钟 bar（未来）
        ]
    )
    written = kcache.upsert_many([("600000.SH", "minute", df)], "test")
    assert written == 3  # 未来行剔除
    got = kcache.load_cached(["600000.SH"], "minute")["600000.SH"]
    assert len(got) == 3
    assert not got["date"].astype(str).str.startswith("2026-08-13").any()
    assert got["date"].tolist() == [
        "2026-08-12 09:30",
        "2026-08-12 10:30",
        "2026-08-12 11:00",
    ]


# ===========================================================================
# 9) P2-9 盘中 partial 命中:增量参数只拉今日(免日线全量重拉)
# ===========================================================================
# 背景:盘中单股 60s 轮询,partial 标记==最后日期 → is_stale True → _fetch_start
# 返回 start_date=今日(days=None);quote 层(sina)据此短路为只拉当日 1 分钟线聚合。
# 本区块验证 kcache 层增量参数契约与 merge 语义(拉取结果优先,原地更新当日 bar)。


def test_cached_kline_partial_hit_increments_from_today(
    kline_db, frozen_cn, monkeypatch
):
    """盘中 partial 命中:cached_kline 拉取参数为 (start_date=今日, days=None,
    compose_intraday=True)——days=None 时 provider 短路只拉分钟线,免日线全量重拉。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)  # 盘中
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-10"))], "test")
    assert _partial_value(kline_db) == "2026-08-10"

    seen = []

    def fake_get_kline(
        code, period, days=None, start_date=None, compose_intraday=False
    ):
        seen.append((period, days, start_date, compose_intraday))
        return _mk_df("2026-08-10")  # 今日 bar(provider 层由分钟线合成返回)

    monkeypatch.setattr(kcache, "get_kline", fake_get_kline)
    monkeypatch.setattr(kcache, "get_provider", lambda: _FakeProvider())

    res = kcache.cached_kline("600000.SH", "daily")
    assert seen[0] == ("daily", None, "2026-08-10", True)  # days=None → 增量;start=今日
    assert str(res["date"].iloc[-1]) == "2026-08-10"
    # partial 标记保持(仍盘中)→ 下次轮询同样走增量,不破坏 15:05 判定语义
    assert _partial_value(kline_db) == "2026-08-10"


def test_cached_kline_partial_hit_fresh_bar_wins(kline_db, frozen_cn, monkeypatch):
    """盘中 partial 命中:合并结果中当日 bar 取拉取结果(新值)而非旧缓存
    (原地更新当日 bar;修复前 concat 旧缓存在前,返回 df 当日 bar 滞后一轮)。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 10, 10, 0)
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-10"))], "test")

    fresh = _mk_df("2026-08-10").copy()
    fresh.loc[fresh.index[-1], "close"] = 99.0  # 模拟新拉取的当日 bar
    monkeypatch.setattr(kcache, "get_kline", lambda *a, **k: fresh)
    monkeypatch.setattr(kcache, "get_provider", lambda: _FakeProvider())

    res = kcache.cached_kline("600000.SH", "daily")
    assert float(res["close"].iloc[-1]) == 99.0  # 新 bar 生效


def test_cached_kline_bare_code_hits_cache(kline_db, frozen_cn, monkeypatch):
    """T-104 P2-52：裸码调用 cached_kline 命中归一化键缓存（修复前静默 miss
    → 每次全量重拉）。缓存新鲜（周六，非交易时段）→ 不触发拉取直接返回缓存。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 8, 10, 0)  # 周六（非交易时段，数据冻结）
    kcache.upsert_many([("600000.SH", "daily", _mk_df("2026-08-07"))], "test")

    def boom(*args, **kwargs):
        raise AssertionError("缓存命中时不应触发拉取")

    monkeypatch.setattr(kcache, "get_kline", boom)
    monkeypatch.setattr(kcache, "get_provider", lambda: _FakeProvider())

    res = kcache.cached_kline("600000", "daily")  # 裸码（无交易所后缀）
    assert len(res) == 5
    assert str(res["date"].iloc[-1]) == "2026-08-07"
    assert len(res) == 5  # 历史行完整
