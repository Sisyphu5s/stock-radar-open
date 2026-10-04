"""去模板化核心契约：evaluate_all_signals 全信号判定 + 扫描写入聚合事件（无模板字段）。

覆盖（任务卡 A 验收 7）：
① evaluate_all_signals 对已知构造 K 线（连续涨停）命中 limit_up/consecutive_limit_up 等信号，
   evidence 覆盖全部 SIGNAL_TYPES；
② scan_once 一次扫描写事件：无 template_id/template_name/resonance_score 字段，
   同 bar 重复扫描不新增事件且 signals/evidence 被刷新，scan_discovered_at 保留首次值；
③ bar 时点推进（新 K 线日期）→ 新增事件（去重键 (stock_code, triggered_at, period) 正确）。
临时 SQLite，不触碰生产库（monkeypatch scanner.SessionLocal 指向临时库）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent, Stock
from app.lib.timex import bar_market_time


def _cn_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _daily_bars(
    last_pct: float,
    second_last_pct: float = 0.0,
    n: int = 60,
    last_date: str | None = None,
) -> pd.DataFrame:
    """n 个工作日日线，最后一天固定为 last_date（缺省今天），最后两根涨跌 second_last_pct/last_pct。"""
    dates = list(
        pd.date_range(end=_cn_today(), periods=n, freq="B").strftime("%Y-%m-%d")
    )
    dates[-1] = last_date or _cn_today()  # 固定最后 bar 日期（避免周末/节假日漂移）
    close = np.full(n, 10.0)
    close[-2] = 10.0 * (1 + second_last_pct / 100)
    close[-1] = close[-2] * (1 + last_pct / 100)
    pct = np.zeros(n)
    pct[-2] = second_last_pct
    pct[-1] = last_pct
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


def _spot_df(price: float = 10.0, volume: float = 100.0) -> pd.DataFrame:
    """单只快照（600100.SH）。"""
    return pd.DataFrame(
        {
            "code": ["600100.SH"],
            "name": ["首股"],
            "price": [price],
            "pct_change": [0.0],
            "volume": [volume],
            "amount": [1000.0],
            "turnover_rate": [1.0],
        }
    )


def _install(monkeypatch, tmp_path, spot_df, kline_map) -> sessionmaker:
    """临时 SQLite + patch scanner 外部依赖；返回 Maker（SessionLocal 等价物）。"""
    from app.core import scanning as SC

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cleanup.db'}",
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
    db.add(Stock(code="600100.SH", name="首股", is_watchlist=False))
    db.commit()
    db.close()

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(SC, "get_spot", lambda: spot_df)
    monkeypatch.setattr(SC, "fetch_many", lambda codes, period, **kw: kline_map)
    monkeypatch.setattr(SC, "get_provider", lambda: SimpleNamespace(name="mock"))
    return Maker


def test_evaluate_all_signals_hits_consecutive_limit_up():
    """连续涨停 K 线 → limit_up/consecutive_limit_up/price_up 命中，evidence 全量覆盖。"""
    from app.lib.signals.engine import (
        SIGNAL_TYPES,
        evaluate_all_signals,
        indicator_memo,
    )

    df = _daily_bars(last_pct=10.0, second_last_pct=10.0)
    df["code"] = "600519.SH"
    memo: dict = {}
    with indicator_memo(memo):
        hits, evidence = evaluate_all_signals(df)

    assert "limit_up" in hits, "最后一日 +10% 应命中涨停"
    assert "consecutive_limit_up" in hits, "连续两日涨停应命中连续涨停"
    assert "price_up" in hits, "+10% 应命中当日上涨"
    assert len(evidence) == len(SIGNAL_TYPES), "evidence 应覆盖全部信号类型"
    assert evidence["limit_up"], "limit_up 证据应有描述"


def test_scan_writes_event_without_template_fields(tmp_path, monkeypatch):
    """扫描一次写事件：无模板三字段；同 bar 重扫不新增事件且 signals 刷新。"""
    from app.core import scanning as SC

    klines = {"600100.SH": _daily_bars(last_pct=5.5, second_last_pct=1.0)}
    Maker = _install(monkeypatch, tmp_path, _spot_df(), klines)

    r = SC.scan_once(period="daily")
    assert r["source"] == "mock"
    assert r["events"] == 1

    db = Maker()
    ev = db.query(SignalEvent).one()
    today = _cn_today()
    assert ev.triggered_at == bar_market_time(today, "daily")
    assert ev.period == "daily"
    assert ev.signals and "price_up" in ev.signals, "+5.5% 应命中 price_up"
    assert isinstance(ev.evidence, dict) and ev.evidence["price"] == pytest.approx(
        10.1 * 1.055
    ), "evidence 应含 price（最后一根 bar 收盘）"
    assert ev.status == "观察"
    assert ev.scan_discovered_at is not None
    # 模型无模板三字段：实例属性不存在 + DB 无对应列
    assert not hasattr(ev, "template_id")
    assert not hasattr(ev, "template_name")
    assert not hasattr(ev, "resonance_score")
    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("signal_events")}
    assert not {"template_id", "template_name", "resonance_score"} & cols
    discovered = ev.scan_discovered_at
    ev_id = ev.id
    first_signals = list(ev.signals)
    db.close()

    # 同 bar（最后 bar 日期不变）重复扫描：不新增事件，signals 刷新，scan_discovered_at 不变
    klines2 = {"600100.SH": _daily_bars(last_pct=-5.5, second_last_pct=0.0)}
    monkeypatch.setattr(SC, "fetch_many", lambda codes, period, **kw: klines2)
    r2 = SC.scan_once(period="daily")
    assert r2["events"] == 1

    db = Maker()
    evs = db.query(SignalEvent).all()
    assert len(evs) == 1, "同 bar 重复扫描不得新增事件"
    ev = evs[0]
    assert ev.id == ev_id, "应为原事件原地更新"
    assert ev.scan_discovered_at == discovered, "scan_discovered_at 首次值不可变"
    assert "big_bearish" in ev.signals, "信号集应被刷新为 -5.5% 大阴线"
    assert "price_up" not in ev.signals, "旧信号应随刷新移除"
    assert ev.signals != first_signals, "signals 应发生变化"
    assert ev.evidence["pct_change"] == pytest.approx(-5.5)
    db.close()


def test_scan_new_bar_creates_new_event(tmp_path, monkeypatch):
    """bar 时点推进（新 K 线日期）→ 新增事件而非覆盖。"""
    from app.core import scanning as SC

    yesterday = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    # 快照与缓存最后 bar 价量一致（无新交易）→ 不触发合成，bar 时点 = 缓存最后日期
    last_close = 10.0 * 1.055
    spot = _spot_df(price=last_close, volume=100.0)

    klines1 = {"600100.SH": _daily_bars(last_pct=5.5, last_date=yesterday)}
    Maker = _install(monkeypatch, tmp_path, spot, klines1)
    r1 = SC.scan_once(period="daily")
    assert r1["events"] == 1
    db = Maker()
    ev1 = db.query(SignalEvent).one()
    assert ev1.triggered_at == bar_market_time(yesterday, "daily")
    first_id = ev1.id
    db.close()

    klines2 = {"600100.SH": _daily_bars(last_pct=5.5, last_date=tomorrow)}
    monkeypatch.setattr(SC, "fetch_many", lambda codes, period, **kw: klines2)
    r2 = SC.scan_once(period="daily")
    assert r2["events"] == 1

    db = Maker()
    evs = db.query(SignalEvent).order_by(SignalEvent.id).all()
    assert len(evs) == 2, "bar 时点推进应新增事件"
    assert evs[0].id == first_id
    assert evs[1].triggered_at == bar_market_time(tomorrow, "daily")
    db.close()
