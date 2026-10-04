"""scan_once 分钟周期（1/5/15/30/60）：universe 仅自选股、低功耗、事件带 period。

覆盖：① 仅自选：自选 2 只 + 分钟 K 线（最后 bar=今天 11:30）→ 事件 period=="5"、
triggered_at 为分钟标签（今天 11:30，分钟无 15:00 归一）；② 自选为空 → 提前返回
scanned=0、source="no-watchlist"，不抛异常。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock


def _minute_kline(n: int = 80) -> pd.DataFrame:
    """80 根 5 分钟 bar（≥ _MIN_BARS["5"]=60），最后 bar=今天 11:30，末根涨 6%。"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    dates = pd.date_range(end=f"{today} 11:30:00", periods=n, freq="5min")
    close = np.linspace(10, 11, n)
    close[-1] = close[-2] * 1.06
    pct = np.zeros(n)
    pct[-1] = 6.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
            "pct_change": pct,
        }
    )


def _install(monkeypatch, tmp_path, wl_codes: list[str]) -> tuple[sessionmaker, dict]:
    from app.core import scanning as SC

    engine = create_engine(
        f"sqlite:///{tmp_path / 'minute.db'}", connect_args={"check_same_thread": False}
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
    for c in wl_codes:
        db.add(Stock(code=c, name=f"股{c[:6]}", is_watchlist=True))
    db.commit()
    db.close()

    fetch_calls = {"workers": None, "period": None, "codes": None}

    def fake_fetch(codes, period, **kw):
        fetch_calls["workers"] = kw.get("workers")
        fetch_calls["period"] = period
        fetch_calls["codes"] = list(codes)
        return {c: _minute_kline() for c in codes}

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(
        SC,
        "get_spot",
        lambda: pd.DataFrame(
            {
                "code": ["600100.SH", "000001.SZ"],
                "name": ["首股", "深股"],
                "price": [11.0, 11.0],
                "pct_change": [10.0, 10.0],
                "volume": [150.0, 150.0],
                "amount": [2000.0, 2000.0],
                "turnover_rate": [1.0, 1.0],
            }
        ),
    )
    monkeypatch.setattr(SC, "fetch_many", fake_fetch)
    monkeypatch.setattr(SC, "get_provider", lambda: SimpleNamespace(name="mock"))
    return Maker, fetch_calls


def test_minute_scan_watchlist_only(tmp_path, monkeypatch):
    """分钟扫描：仅自选 2 只入 universe，事件 period=="5"、triggered_at=分钟标签。"""
    from app.core import scanning as SC

    Maker, fetch_calls = _install(monkeypatch, tmp_path, ["600100.SH", "000001.SZ"])

    r = SC.scan_once(period="5")
    assert r["source"] == "mock"
    assert r["scanned"] == 2, "分钟扫描 universe 应仅含自选 2 只"
    assert r["events"] == 2
    # 低功耗：workers=4；直接拉 5 分钟周期
    assert fetch_calls["workers"] == 4
    assert fetch_calls["period"] == "5"
    assert set(fetch_calls["codes"]) == {"600100.SH", "000001.SZ"}

    db = Maker()
    evs = db.query(SignalEvent).order_by(SignalEvent.stock_code).all()
    assert len(evs) == 2
    assert all(e.period == "5" for e in evs)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    for e in evs:
        # 分钟事件 triggered_at = 精确时分标签（今天 11:30），不做 15:00 归一
        assert e.triggered_at == datetime.strptime(
            f"{today} 11:30:00", "%Y-%m-%d %H:%M:%S"
        )
        assert e.triggered_at.hour == 11 and e.triggered_at.minute == 30
    db.close()


def test_minute_scan_empty_watchlist(tmp_path, monkeypatch):
    """自选为空：分钟扫描提前返回 scanned=0，不抛异常。"""
    from app.core import scanning as SC

    Maker, fetch_calls = _install(monkeypatch, tmp_path, [])  # 无自选

    r = SC.scan_once(period="5")
    assert r["scanned"] == 0
    assert r["events"] == 0
    assert r["source"] == "no-watchlist"
    assert fetch_calls["workers"] is None, "自选为空不应触发 K 线拉取"
