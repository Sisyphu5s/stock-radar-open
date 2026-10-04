"""scan_once 分钟周期股票池范围（T-54）：universe watchlist/top_n/codes 三种范围。

覆盖：① watchlist 缺省（自选，保持旧行为）；② top_n 按成交额截断（显式 top_n /
缺省回落 market_scan_limit）；③ codes 显式代码集（未命中快照代码静默过滤、全未命中
→ source="no-codes"）；④ 未知 universe 防御性回退 watchlist；⑤ 分钟周期
full_universe=true（旧前端恒传）不影响 watchlist 缺省语义。
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
from app.storage.models import Stock


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


# 快照 4 只（amount 各不同，供 top_n 排序断言）；自选 600100.SH
_SPOT_ROWS = [
    ("600100.SH", "首股", 11.0, 10.0, 150.0, 1000.0, 1.0),
    ("000001.SZ", "深股", 11.0, 10.0, 150.0, 2000.0, 1.0),
    ("300750.SZ", "宁德", 11.0, 10.0, 150.0, 3000.0, 1.0),
    ("601318.SH", "平安", 11.0, 10.0, 150.0, 4000.0, 1.0),
]


def _install(monkeypatch, tmp_path, wl_codes: list[str]) -> tuple[sessionmaker, dict]:
    from app.core import scanning as SC

    engine = create_engine(
        f"sqlite:///{tmp_path / 'universe.db'}",
        connect_args={"check_same_thread": False},
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
                "code": [r[0] for r in _SPOT_ROWS],
                "name": [r[1] for r in _SPOT_ROWS],
                "price": [r[2] for r in _SPOT_ROWS],
                "pct_change": [r[3] for r in _SPOT_ROWS],
                "volume": [r[4] for r in _SPOT_ROWS],
                "amount": [r[5] for r in _SPOT_ROWS],
                "turnover_rate": [r[6] for r in _SPOT_ROWS],
            }
        ),
    )
    monkeypatch.setattr(SC, "fetch_many", fake_fetch)
    monkeypatch.setattr(SC, "get_provider", lambda: SimpleNamespace(name="mock"))
    return Maker, fetch_calls


def test_minute_scan_watchlist_default(tmp_path, monkeypatch):
    """universe 缺省 = watchlist：仅自选入池（与旧行为一致）。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, ["600100.SH"])

    r = SC.scan_once(period="5")
    assert r["source"] == "mock"
    assert r["scanned"] == 1
    assert fetch_calls["codes"] == ["600100.SH"]


def test_minute_scan_watchlist_empty_no_watchlist(tmp_path, monkeypatch):
    """watchlist 且自选为空 → source=no-watchlist（保持）。"""
    from app.core import scanning as SC

    _install(monkeypatch, tmp_path, [])

    r = SC.scan_once(period="5")
    assert r["scanned"] == 0
    assert r["events"] == 0
    assert r["source"] == "no-watchlist"


def test_minute_scan_top_n_truncates_by_amount(tmp_path, monkeypatch):
    """top_n=2：按成交额取前 2（601318/300750，amount 4000/3000）。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, [])

    r = SC.scan_once(period="5", universe="top_n", top_n=2)
    assert r["source"] == "mock"
    assert r["scanned"] == 2
    assert set(fetch_calls["codes"]) == {"601318.SH", "300750.SZ"}


def test_minute_scan_top_n_default_limit(tmp_path, monkeypatch):
    """top_n 缺省：回落 settings.market_scan_limit（500）> 快照 4 只 → 全量。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, [])

    r = SC.scan_once(period="5", universe="top_n")
    assert r["scanned"] == 4
    assert len(fetch_calls["codes"]) == 4


def test_minute_scan_codes_explicit(tmp_path, monkeypatch):
    """codes 显式集合：只扫给定代码。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, [])

    r = SC.scan_once(period="5", universe="codes", codes=["000001.SZ", "600100.SH"])
    assert r["scanned"] == 2
    assert set(fetch_calls["codes"]) == {"000001.SZ", "600100.SH"}


def test_minute_scan_codes_filters_unknown(tmp_path, monkeypatch):
    """codes 含未命中快照代码：静默过滤，只扫命中部分。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, [])

    r = SC.scan_once(period="5", universe="codes", codes=["000001.SZ", "999999.XX"])
    assert r["scanned"] == 1
    assert fetch_calls["codes"] == ["000001.SZ"]


def test_minute_scan_codes_all_miss_no_codes(tmp_path, monkeypatch):
    """codes 全部未命中快照 → source=no-codes，不触发 K 线拉取。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, [])

    r = SC.scan_once(period="5", universe="codes", codes=["999999.XX", "888888.BJ"])
    assert r["scanned"] == 0
    assert r["events"] == 0
    assert r["source"] == "no-codes"
    assert fetch_calls["workers"] is None, "空池不应触发 K 线拉取"


def test_minute_scan_unknown_universe_fallback_watchlist(tmp_path, monkeypatch):
    """未知 universe：防御性回退 watchlist。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, ["600100.SH"])

    r = SC.scan_once(period="5", universe="bogus")
    assert r["source"] == "mock"
    assert r["scanned"] == 1
    assert fetch_calls["codes"] == ["600100.SH"]


def test_minute_scan_full_universe_ignored(tmp_path, monkeypatch):
    """分钟周期 full_universe=true（旧前端恒传）不影响 watchlist 缺省语义。"""
    from app.core import scanning as SC

    _, fetch_calls = _install(monkeypatch, tmp_path, ["600100.SH"])

    r = SC.scan_once(period="5", full_universe=True)
    assert r["scanned"] == 1
    assert fetch_calls["codes"] == ["600100.SH"]
