"""scan_once 周期扩展：weekly/monthly 全市场重算，事件带 period 字段。

覆盖：① period='weekly' 写入事件 period 字段为 'weekly'，且与 daily 事件
去重隔离（同 bar 时点不同周期不互相覆盖/串扰）；② 周/月 universe = 快照全量
（不按成交额截断 Top N）；③ daily 默认行为不受影响（仍按成交额 Top
scan_max_stocks 截断，事件 period 默认 daily，不传 start_date）。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock


def _fake_kline_df(n: int = 400) -> pd.DataFrame:
    """400 个工作日日线（约 80 周 / 18 月，均满足周期阈值）。

    最后一天 close 跳升 6%：日线 pct_change=6.0 直接命中 price_up；
    周/月线由 _resample_period 按 close 重算 pct_change，无论周组边界
    如何划分（最后一周可能只含 1 天），最后 bar 涨幅均 ≥3% 命中。
    """
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    close = np.linspace(10, 11, n)
    close[-1] = close[-2] * 1.06  # 最后一天跳升 6%
    pct = np.zeros(n)
    pct[-1] = 6.0
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


def _fake_kline_ending(end: str, n: int = 400) -> pd.DataFrame:
    """K 线结束于指定交易日（P1-37 用：模拟进行中周期最后一根 bar 随交易日滑动）。"""
    dates = pd.date_range(end=end, periods=n, freq="B")
    close = np.linspace(10, 11, n)
    close[-1] = close[-2] * 1.06
    pct = np.zeros(n)
    pct[-1] = 6.0
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


def _spot_df(n: int) -> pd.DataFrame:
    """n 只股票快照，amount 递减（验证按成交额截断时取前 N 的基准）。

    适配快照合成：首只（600100.SH，唯一有 K 线的股票）快照价对齐 K 线最后
    close、量对齐 K 线最后 volume（100.0）→ 触发「无新交易」判定 → 跳过合成，
    保证 daily/weekly 既有断言语义不变（合成逻辑见 scanner._synthesize_daily_bar）。
    """
    first_price = float(_fake_kline_df()["close"].iloc[-1])
    return pd.DataFrame(
        {
            "code": [f"600{100 + i}.SH" for i in range(n)],
            "name": [f"股{i}" for i in range(n)],
            "price": [first_price] + [10.0] * (n - 1),
            "pct_change": [0.0] * n,
            "volume": [100.0] * n,
            "amount": [float(n - i) for i in range(n)],  # 降序
            "turnover_rate": [1.0] * n,
        }
    )


def _install(monkeypatch, tmp_path, n_stocks: int) -> tuple[sessionmaker, dict]:
    """建临时 SQLite 库 + patch 扫描器外部依赖；返回 (Maker, fetch_calls)。"""
    from app.core import scanning as SC

    engine = create_engine(
        f"sqlite:///{tmp_path / 'scan.db'}", connect_args={"check_same_thread": False}
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

    fetch_calls: dict = {}
    n = {"n": 0}

    def fake_fetch(codes, period, **kw):
        fetch_calls["period"] = period
        fetch_calls["codes"] = list(codes)
        fetch_calls["start_date"] = kw.get("start_date")
        n["n"] += 1
        # 仅首只返回 K 线（其余无 K 线 → 不参与判定），universe 截断验证看 codes 数量
        return {"600100.SH": _fake_kline_df()}

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(SC, "get_spot", lambda: _spot_df(n_stocks))
    monkeypatch.setattr(SC, "fetch_many", fake_fetch)
    monkeypatch.setattr(SC, "get_provider", lambda: SimpleNamespace(name="mock"))
    return Maker, fetch_calls


def test_weekly_event_period_field_and_dedup_isolated(tmp_path, monkeypatch):
    """period='weekly'：事件 period 字段为 weekly；与 daily 事件去重隔离。"""
    from app.core import scanning as SC

    Maker, fetch_calls = _install(monkeypatch, tmp_path, n_stocks=3)

    # 先 daily 扫描（默认行为）→ 1 条 daily 事件
    r1 = SC.scan_once()  # 默认 period="daily"
    assert r1["source"] == "mock"
    assert r1["events"] == 1
    db = Maker()
    ev = db.query(SignalEvent).one()
    assert ev.period == "daily"
    assert ev.triggered_at.hour == 15 and ev.triggered_at.minute == 0
    db.close()

    # 再 weekly 扫描：日线重采样得周线，事件 period='weekly'，
    # 且与 daily 事件 triggered_at 相同（同一最后交易日）也不互相覆盖
    r2 = SC.scan_once(period="weekly", full_universe=True)
    assert r2["events"] == 1
    db = Maker()
    evs = db.query(SignalEvent).order_by(SignalEvent.id).all()
    assert len(evs) == 2
    assert {e.period for e in evs} == {"daily", "weekly"}
    # P1-37：周线事件 triggered_at = 周期起点（该周周一 00:00）——进行中周期 bar 随
    # 交易日滑动，归一化到周一保证同一周期内去重稳定；daily 仍为 15:00 收盘标签
    for e in evs:
        if e.period == "weekly":
            assert e.triggered_at.hour == 0 and e.triggered_at.minute == 0
            assert e.triggered_at.weekday() == 0, "周事件应归一到周一"
        else:
            assert e.triggered_at.hour == 15 and e.triggered_at.minute == 0
    db.close()

    # 再次 weekly 扫描：去重键含 period，命中既有 weekly 事件 → 不新增
    r3 = SC.scan_once(period="weekly", full_universe=True)
    assert r3["events"] == 1
    db = Maker()
    assert db.query(SignalEvent).count() == 2
    db.close()

    # weekly 必须统一拉 daily（周线由重采样获得，绝不对 weekly 单独发请求），
    # 且带 5 年前起始日期保证重采样深度
    assert fetch_calls["period"] == "daily"
    assert fetch_calls["start_date"] is not None


def test_weekly_full_universe_not_truncated(tmp_path, monkeypatch):
    """period='weekly'：universe = 快照全量，不按成交额截断 Top N。"""
    from app.core import scanning as SC
    from app.config import settings

    n = settings.scan_max_stocks + 500  # 超过 daily 截断上限
    Maker, fetch_calls = _install(monkeypatch, tmp_path, n_stocks=n)

    r = SC.scan_once(period="weekly", full_universe=True)
    assert r["scanned"] == n
    assert len(fetch_calls["codes"]) == n, "周/月扫描不得按成交额截断 universe"


def test_daily_default_behavior_unchanged(tmp_path, monkeypatch):
    """daily 默认行为不受影响：仍按成交额 Top scan_max_stocks 截断、period 默认 daily。"""
    from app.core import scanning as SC
    from app.config import settings

    n = settings.scan_max_stocks + 500
    Maker, fetch_calls = _install(monkeypatch, tmp_path, n_stocks=n)

    r = SC.scan_once()  # 完全默认调用（period 缺省 daily，full_universe=True）
    assert r["scanned"] == settings.scan_max_stocks
    assert len(fetch_calls["codes"]) == settings.scan_max_stocks
    # daily 不传 start_date（避免触发深历史回填）
    assert fetch_calls.get("start_date") is None
    # 首只股票 amount 最高 → 必在 Top 内，事件 period 默认 daily
    assert r["events"] == 1
    db = Maker()
    assert db.query(SignalEvent).one().period == "daily"
    db.close()


# ---------------------------------------------------------------------------
# P1-37：周/月事件去重键含周期起点（周一 / 当月 1 日）——进行中周期 bar 随
# 交易日滑动也不新增事件，同一周期内观察事件原地更新（live 保持一条）
# ---------------------------------------------------------------------------


def test_event_key_time_period_anchor():
    """_event_key_time：weekly→周一 00:00、monthly→当月 1 日 00:00、其余原样（去微秒），幂等。"""
    from datetime import datetime

    from app.core.scanning import _event_key_time

    # weekly：周三/周五 15:00 → 同一周一 00:00（进行中周期滑动去重）
    assert _event_key_time(datetime(2025, 7, 16, 15, 0), "weekly") == datetime(
        2025, 7, 14, 0, 0
    )
    assert _event_key_time(datetime(2025, 7, 18, 15, 0), "weekly") == datetime(
        2025, 7, 14, 0, 0
    )
    # weekly 幂等：输入已是周期起点返回自身
    anchor = datetime(2025, 7, 14, 0, 0)
    assert _event_key_time(anchor, "weekly") == anchor
    # monthly：月中两日 → 当月 1 日 00:00；幂等
    assert _event_key_time(datetime(2025, 7, 10, 15, 0), "monthly") == datetime(
        2025, 7, 1, 0, 0
    )
    assert _event_key_time(datetime(2025, 7, 24, 15, 0), "monthly") == datetime(
        2025, 7, 1, 0, 0
    )
    assert _event_key_time(datetime(2025, 7, 1, 0, 0), "monthly") == datetime(
        2025, 7, 1, 0, 0
    )
    # daily / 分钟：原样（仅去微秒）
    assert _event_key_time(datetime(2025, 7, 16, 15, 0, 0, 123), "daily") == datetime(
        2025, 7, 16, 15, 0
    )
    assert _event_key_time(datetime(2025, 7, 16, 11, 30, 0, 123), "5") == datetime(
        2025, 7, 16, 11, 30
    )


def test_weekly_in_progress_cycle_sliding_bar_no_new_event(tmp_path, monkeypatch):
    """P1-37：进行中周内最后一根 bar 随交易日滑动（周三→周五，bar 时点推进），
    同一周期只保持一条 live 事件（不新增行），triggered_at 稳定为该周周一。"""
    from app.core import scanning as SC

    klines_state = {"end": "2025-07-16"}  # 周三
    Maker, fetch_calls = _install(monkeypatch, tmp_path, n_stocks=3)

    def sliding_fetch(codes, period, **kw):
        fetch_calls["period"] = period
        return {"600100.SH": _fake_kline_ending(klines_state["end"])}

    monkeypatch.setattr(SC, "fetch_many", sliding_fetch)

    r1 = SC.scan_once(period="weekly", full_universe=True)
    assert r1["events"] == 1
    db = Maker()
    evs = db.query(SignalEvent).all()
    assert len(evs) == 1
    first_id = evs[0].id
    assert evs[0].period == "weekly"
    assert evs[0].triggered_at == datetime(2025, 7, 14, 0, 0), "周事件归一到周一"
    db.close()

    # 同周期内下个交易日（周五）：bar 日期推进、bar_time 滑动，去重键仍归一到同一周一
    klines_state["end"] = "2025-07-18"
    r2 = SC.scan_once(period="weekly", full_universe=True)
    assert r2["events"] == 1
    db = Maker()
    evs = db.query(SignalEvent).all()
    assert len(evs) == 1, "同一进行中周期不得新增事件"
    assert evs[0].id == first_id, "应原地更新既有事件"
    assert evs[0].triggered_at == datetime(2025, 7, 14, 0, 0)
    db.close()


def test_monthly_in_progress_cycle_sliding_bar_no_new_event(tmp_path, monkeypatch):
    """P1-37：进行中月内 bar 滑动（7/10 → 7/24），同一月只保持一条 live 事件。"""
    from app.core import scanning as SC

    klines_state = {"end": "2025-07-10"}
    Maker, fetch_calls = _install(monkeypatch, tmp_path, n_stocks=3)

    def sliding_fetch(codes, period, **kw):
        fetch_calls["period"] = period
        return {"600100.SH": _fake_kline_ending(klines_state["end"])}

    monkeypatch.setattr(SC, "fetch_many", sliding_fetch)

    r1 = SC.scan_once(period="monthly", full_universe=True)
    assert r1["events"] == 1
    db = Maker()
    evs = db.query(SignalEvent).all()
    assert len(evs) == 1
    first_id = evs[0].id
    assert evs[0].period == "monthly"
    assert evs[0].triggered_at == datetime(2025, 7, 1, 0, 0), "月事件归一到当月 1 日"
    db.close()

    klines_state["end"] = "2025-07-24"
    r2 = SC.scan_once(period="monthly", full_universe=True)
    assert r2["events"] == 1
    db = Maker()
    evs = db.query(SignalEvent).all()
    assert len(evs) == 1, "同一进行中月份不得新增事件"
    assert evs[0].id == first_id
    assert evs[0].triggered_at == datetime(2025, 7, 1, 0, 0)
    db.close()


def test_weekly_observed_event_updated_in_place(tmp_path, monkeypatch):
    """P1-37：同一周期内事件原地更新——预置观察事件（周期起点键）被扫描刷新
    信号/证据，不新增行；状态保持「观察」。"""
    from app.core import scanning as SC

    Maker, fetch_calls = _install(monkeypatch, tmp_path, n_stocks=3)
    db = Maker()
    db.add(
        SignalEvent(
            stock_code="600100.SH",
            signals=["volume_surge"],
            status="观察",
            evidence={"marker": "original"},
            triggered_at=datetime(2025, 7, 14, 0, 0),  # 周期起点键（该周周一）
            period="weekly",
        )
    )
    db.commit()
    db.close()

    r = SC.scan_once(period="weekly", full_universe=True)
    assert r["events"] == 1

    db = Maker()
    evs = db.query(SignalEvent).all()
    assert len(evs) == 1, "同周期事件应原地更新而非新增"
    ev = evs[0]
    assert ev.status == "观察"
    assert "price_up" in ev.signals, "观察事件信号未刷新（+6% 日线应命中 price_up）"
    assert ev.evidence.get("marker") is None, "观察事件证据未刷新"
    assert ev.evidence.get("pct_change") == pytest.approx(6.0)  # 重采样浮点误差
    assert ev.triggered_at == datetime(2025, 7, 14, 0, 0)
    assert ev.period == "weekly"
    db.close()
