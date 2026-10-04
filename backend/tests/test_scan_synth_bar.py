"""scan_once 快照合成当日 bar：数据源历史接口缺当日 bar 时，用实时快照合成今日日线。

覆盖：① 合成触发（快照有新交易 → 事件 triggered_at=今天 15:00、evidence.price=快照价）；
② 缓存已有当日 bar 不合成（事件仍来自缓存 bar，未被快照替换）；
③ 无新交易不合成（快照量与价与缓存最后一致 → 事件停在昨天，防休市误报）；
④ 快照列缺失容错（缺 volume/amount 列 → 扫描不抛异常，事件正常生成）；
⑤ 周线重采样合成（weekly 感知合成后的今日 bar → triggered_at=该周周一周期起点）。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock
from app.lib.timex import bar_market_time


def _cn_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _daily_until(
    last_date: str, last_close: float, last_pct: float = 0.0, n: int = 60
) -> pd.DataFrame:
    """n 个工作日日线，最后 bar 日期精确等于 last_date，最后一根涨跌 last_pct。

    测试需构造「最后一根 = 今日」的缓存（覆盖扫描的当日 bar 存在性判定
    `str(date.iloc[-1]) >= today`）；周末/休市时 freq="B" 会把 end 截到上一
    工作日，导致末根落不到 last_date——手动把末根置为 last_date（单元测试
    数据构造可含周末 bar，生产行为不受影响）。
    """
    dates = pd.date_range(end=last_date, periods=n, freq="B")
    if dates[-1].strftime("%Y-%m-%d") != last_date:
        dates = pd.DatetimeIndex(
            sorted(dates[:-1].tolist() + [pd.Timestamp(last_date)])
        )
    close = np.linspace(10, 10, n)
    close[-1] = last_close
    pct = np.zeros(n)
    pct[-1] = last_pct
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
            "pct_change": pct,
        }
    )


def _spot_df(
    price: float, pct: float = 10.0, volume: float = 150.0, amount: float = 2000.0
) -> pd.DataFrame:
    """单只快照（600100.SH），默认有新交易（价量均与缓存最后 bar 不同）。"""
    return pd.DataFrame(
        {
            "code": ["600100.SH"],
            "name": ["首股"],
            "price": [price],
            "pct_change": [pct],
            "volume": [volume],
            "amount": [amount],
            "turnover_rate": [1.0],
        }
    )


def _install(monkeypatch, tmp_path, spot_df, kline_map) -> sessionmaker:
    from app.core import scanning as SC

    engine = create_engine(
        f"sqlite:///{tmp_path / 'synth.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = Maker()
    db.add(Stock(code="600100.SH", name="首股", is_watchlist=False))
    db.commit()
    db.close()

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(SC, "get_spot", lambda: spot_df)
    monkeypatch.setattr(SC, "fetch_many", lambda codes, period, **kw: kline_map)
    monkeypatch.setattr(SC, "get_provider", lambda: SimpleNamespace(name="mock"))
    return Maker


def test_synth_bar_triggered_with_snapshot(tmp_path, monkeypatch):
    """合成触发：缓存最后 bar=昨天且无涨跌，快照有新交易 → 合成今日 bar 命中。"""
    from app.core import scanning as SC

    yesterday = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    klines = {"600100.SH": _daily_until(yesterday, last_close=10.0, last_pct=0.0)}
    spot = _spot_df(price=11.0, pct=10.0, volume=150.0)  # 新交易：价量均不同
    Maker = _install(monkeypatch, tmp_path, spot, klines)

    r = SC.scan_once(period="daily")
    assert r["source"] == "mock"
    assert r["events"] == 1, "合成今日 bar 应命中 price_up"

    db = Maker()
    ev = db.query(SignalEvent).one()
    assert ev.period == "daily"
    today = _cn_today()
    assert ev.triggered_at == bar_market_time(today, "daily")
    assert ev.triggered_at.hour == 15 and ev.triggered_at.minute == 0
    assert ev.evidence["price"] == 11.0, "evidence.price 应为快照现价"
    assert ev.evidence["pct_change"] == 10.0
    db.close()


def test_existing_today_bar_not_replaced(tmp_path, monkeypatch):
    """缓存已有当日 bar：不合成，事件证据仍来自缓存 bar（未被快照价替换）。"""
    from app.core import scanning as SC

    today = _cn_today()
    klines = {"600100.SH": _daily_until(today, last_close=11.0, last_pct=6.0)}
    spot = _spot_df(price=9.0, pct=10.0, volume=150.0)  # 快照价远低于缓存收盘
    Maker = _install(monkeypatch, tmp_path, spot, klines)

    r = SC.scan_once(period="daily")
    assert r["events"] == 1

    db = Maker()
    ev = db.query(SignalEvent).one()
    assert ev.triggered_at == bar_market_time(today, "daily")
    assert ev.evidence["price"] == 11.0, "当日 bar 已存在，不得被快照合成替换"
    db.close()


def test_no_new_trade_no_synth(tmp_path, monkeypatch):
    """无新交易（休市模拟）：快照量与价与缓存最后一致 → 不合成，事件停在昨天。"""
    from app.core import scanning as SC

    yesterday = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    klines = {"600100.SH": _daily_until(yesterday, last_close=10.6, last_pct=6.0)}
    spot = _spot_df(price=10.6, pct=6.0, volume=100.0)  # 与缓存最后 bar 完全一致
    Maker = _install(monkeypatch, tmp_path, spot, klines)

    r = SC.scan_once(period="daily")
    assert r["events"] == 1, "缓存昨日 bar 本身命中 price_up"
    db = Maker()
    ev = db.query(SignalEvent).one()
    assert ev.triggered_at == bar_market_time(yesterday, "daily")
    assert ev.evidence["price"] == 10.6
    db.close()


def test_missing_spot_columns_tolerant(tmp_path, monkeypatch):
    """快照缺 volume/amount 列：跳过合成，扫描不抛异常，事件由缓存 bar 正常生成。"""
    from app.core import scanning as SC

    yesterday = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    klines = {"600100.SH": _daily_until(yesterday, last_close=10.6, last_pct=6.0)}
    spot = pd.DataFrame(
        {
            "code": ["600100.SH"],
            "name": ["首股"],
            "price": [11.0],
            "pct_change": [10.0],
            "turnover_rate": [1.0],
            # 无 volume / amount 列
        }
    )
    Maker = _install(monkeypatch, tmp_path, spot, klines)

    r = SC.scan_once(period="daily")
    assert r["events"] == 1, "列缺失只应跳过合成，不应影响缓存 bar 的事件生成"
    db = Maker()
    ev = db.query(SignalEvent).one()
    assert ev.triggered_at == bar_market_time(yesterday, "daily")
    assert ev.evidence["price"] == 10.6
    db.close()


def test_weekly_resample_sees_synth_bar(tmp_path, monkeypatch):
    """周线重采样合成：合成今日 bar 先进周/月重采样 → 周线事件 triggered_at=该周周一
    （P1-37：周/月事件归属时点 = 周期起点，进行中周期 bar 随交易日滑动不新增事件）。"""
    from app.core import scanning as SC

    yesterday = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    # 200 个工作日 ≈ 40 周，满足 _MIN_BARS["weekly"]=20
    klines = {
        "600100.SH": _daily_until(yesterday, last_close=10.0, last_pct=0.0, n=200)
    }
    spot = _spot_df(price=11.0, pct=10.0, volume=150.0)
    Maker = _install(monkeypatch, tmp_path, spot, klines)

    r = SC.scan_once(period="weekly")
    assert r["events"] == 1, "合成今日 bar 重采样后周线应命中 price_up"
    db = Maker()
    ev = db.query(SignalEvent).one()
    assert ev.period == "weekly"
    # 周线事件归一到周期起点（该周周一 00:00），而非 bar 日的 15:00 收盘标签
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    monday = today - timedelta(days=today.weekday())
    assert ev.triggered_at == datetime.combine(monday, datetime.min.time())
    assert ev.triggered_at.hour == 0 and ev.triggered_at.weekday() == 0
    db.close()


def test_synth_index_lookup_equivalent():
    """C8 索引化取行与旧布尔扫描逐字段等价（重复 code 取首行、缺失 code 回退）。

    _synthesize_daily_bar 第三参已由整表 spot 改为 code→行索引（调用方一次性
    drop_duplicates("code", keep="first").set_index("code")）；本测试验证新旧
    两种取行语义在重复/单行/缺失 code 下逐字段一致。
    """
    from app.core import scanning as SC

    spot = pd.DataFrame(
        {
            "code": ["600100.SH", "600100.SH", "600200.SH"],
            "name": ["首股", "首股重复", "次股"],
            "price": [11.0, 12.0, 5.0],
            "pct_change": [10.0, 20.0, -3.0],
            "volume": [150.0, 999.0, 50.0],
            "amount": [2000.0, 8888.0, 500.0],
        }
    )
    spot_index = spot.drop_duplicates("code", keep="first").set_index("code")
    # 重复 code：新实现 keep="first" 取首行，与旧 .iloc[0] 语义一致
    # （code 列在索引化后成为 index，不参与字段对比；其余列逐字段等价）
    pd.testing.assert_series_equal(
        spot_index.loc["600100.SH"],
        spot[spot["code"] == "600100.SH"].iloc[0].drop("code"),
        check_names=False,
    )
    # 单行 code 亦等价
    pd.testing.assert_series_equal(
        spot_index.loc["600200.SH"],
        spot[spot["code"] == "600200.SH"].iloc[0].drop("code"),
        check_names=False,
    )
    # 缺失 code：旧实现 row.empty → 新实现 code not in index，均回退原 df
    assert spot[spot["code"] == "999999.SZ"].empty
    assert "999999.SZ" not in spot_index.index


def test_synth_bar_fields_unchanged():
    """索引化后直接调用合成：各字段口径与旧实现一致（open 昨收反推、high/low
    收敛、量额透传、前序 bar 原样、缺失 code/空索引回退原 df）。"""
    from app.core import scanning as SC

    yesterday = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    df = _daily_until(yesterday, last_close=10.0, last_pct=0.0)
    spot = _spot_df(price=11.0, pct=10.0, volume=150.0, amount=2000.0)
    spot_index = spot.drop_duplicates("code", keep="first").set_index("code")

    out = SC._synthesize_daily_bar("600100.SH", df, spot_index)
    assert len(out) == len(df) + 1
    synth = out.iloc[-1]
    today = _cn_today()
    assert synth["date"] == today
    assert abs(synth["open"] - 10.0) < 1e-9  # 11.0 / (1 + 10/100)
    assert abs(synth["high"] - 11.0) < 1e-9  # max(open, price)
    assert abs(synth["low"] - 10.0) < 1e-9  # min(open, price)
    assert abs(synth["close"] - 11.0) < 1e-9
    assert synth["volume"] == 150.0
    assert synth["amount"] == 2000.0
    assert abs(synth["pct_change"] - 10.0) < 1e-9
    # 前序 bar 原样保留（逐字段一致）
    pd.testing.assert_frame_equal(out.iloc[:-1].reset_index(drop=True), df)

    # 缺失 code / 空索引 → 原样返回原 df 引用
    assert SC._synthesize_daily_bar("999999.SZ", df, spot_index) is df
    empty_index = pd.DataFrame(columns=["code"]).set_index("code")
    assert SC._synthesize_daily_bar("600100.SH", df, empty_index) is df
