"""T-98 扫描域三修：事件清理防误删 / 缺列显式降级 / 大代码集分批。

覆盖（对应任务卡 3 条问题）：
1. P2-55 事件表防膨胀只清理「观察」——已确认/已忽略复盘事件不被删，
   且 daily/周/月周期隔离（weekly 观察不受 daily 清理影响）；
2. P2-56 scan_once 快照缺 amount 列 → _top_n 显式退化不抛错；
3. P2-56 hs300 快照 Top300 兜底缺 amount 列 → 原序降级不 KeyError；
4. P2-80 recent_events 代码集 > 999 → 分批 IN 查询不炸 SQLite 变量上限。
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


# ---------------------------------------------------------------------------
# 公共构件
# ---------------------------------------------------------------------------


def _db(tmp_path) -> sessionmaker:
    """独立临时 SQLite 库，返回 sessionmaker。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 't98.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _fake_kline_df(n: int = 400) -> pd.DataFrame:
    """400 个工作日日线，最后一天 close 跳升 6% → 命中 price_up（复用 test_scan_period 构件）。"""
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
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
    """n 只股票快照（含 amount），首只 600100.SH 对齐 K 线 → 无新交易跳过合成。"""
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


def _install_scan_env(monkeypatch, tmp_path, spot_df) -> sessionmaker:
    """patch scanner 外部依赖（SessionLocal/get_spot/fetch_many/get_provider），返回 Maker。"""
    from app.core import scanning as SC

    Maker = _db(tmp_path)
    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(SC, "get_spot", lambda: spot_df)
    monkeypatch.setattr(
        SC,
        "fetch_many",
        lambda codes, period, **kw: {"600100.SH": _fake_kline_df()},
    )
    monkeypatch.setattr(SC, "get_provider", lambda: SimpleNamespace(name="mock"))
    return Maker


# ---------------------------------------------------------------------------
# P2-55：防膨胀清理只删「观察」，不删已确认/已忽略
# ---------------------------------------------------------------------------


def test_p2_55_cleanup_keeps_confirmed_and_ignored(tmp_path, monkeypatch):
    from app.core import scanning as SC

    Maker = _install_scan_env(monkeypatch, tmp_path, _spot_df(3))

    db = Maker()
    rows = []
    # 5002 条 daily 观察（超 _OBSERVE_EVENT_KEEP=5000，最老 3 条应被清理）
    for i in range(5002):
        rows.append(
            SignalEvent(
                stock_code=f"6001{i:04d}.SH",
                signals=["x"],
                status="观察",
                evidence={},
                triggered_at=datetime(2024, 1, 1),
                period="daily",
            )
        )
    # 已确认/已忽略复盘事件（应全部保留）
    for status in ("确认", "已忽略"):
        for i in range(3):
            rows.append(
                SignalEvent(
                    stock_code=f"60020{status[0]}{i}.SH",
                    signals=["x"],
                    status=status,
                    evidence={},
                    triggered_at=datetime(2024, 1, 1),
                    period="daily",
                )
            )
    # 2 条 weekly 观察：周期隔离，不受 daily 清理影响
    for i in range(2):
        rows.append(
            SignalEvent(
                stock_code=f"6003{i:04d}.SH",
                signals=["x"],
                status="观察",
                evidence={},
                triggered_at=datetime(2024, 1, 1),
                period="weekly",
            )
        )
    db.add_all(rows)
    db.commit()
    db.close()

    r = SC.scan_once()
    assert r["source"] == "mock"
    assert r["events"] == 1  # 首股命中（新增 1 条 daily 观察）

    db = Maker()
    # 观察事件有界：≤ 5000（5002 预置 + 1 新增 → 删 3 最老，留 5000）
    assert (
        db.query(SignalEvent)
        .filter(SignalEvent.status == "观察", SignalEvent.period == "daily")
        .count()
        == 5000
    )
    # 复盘数据零误删
    assert db.query(SignalEvent).filter(SignalEvent.status == "确认").count() == 3
    assert db.query(SignalEvent).filter(SignalEvent.status == "已忽略").count() == 3
    # 周期隔离：weekly 观察不受 daily 清理影响
    assert (
        db.query(SignalEvent)
        .filter(SignalEvent.status == "观察", SignalEvent.period == "weekly")
        .count()
        == 2
    )
    # 被删的是最老预置（60010000.SH 为 id 最小），扫描新增的 600100.SH 保留
    assert (
        db.query(SignalEvent).filter(SignalEvent.stock_code == "60010000.SH").count()
        == 0
    )
    assert (
        db.query(SignalEvent)
        .filter(SignalEvent.stock_code == "600100.SH", SignalEvent.status == "观察")
        .count()
        == 1
    )
    db.close()


# ---------------------------------------------------------------------------
# P2-56：scan_once 快照缺 amount 列 → _top_n 显式退化不抛错
# ---------------------------------------------------------------------------


def test_p2_56_top_n_missing_amount_degrades_gracefully(tmp_path, monkeypatch):
    from app.core import scanning as SC

    spot = _spot_df(5).drop(columns=["amount"])  # 缺 amount 列（异常/降级快照）
    Maker = _install_scan_env(monkeypatch, tmp_path, spot)

    r = SC.scan_once()  # 不抛 KeyError
    assert r["source"] == "mock"
    assert r["scanned"] == 5, "缺 amount 列应退化为快照原序前 N 行，universe 不受损"
    assert r["events"] == 1, "K 线判定不受影响（合成路径对缺列自跳）"


# ---------------------------------------------------------------------------
# P2-56：hs300 快照 Top300 兜底缺列显式降级
# ---------------------------------------------------------------------------


def test_p2_56_hs300_spot_top_missing_amount_degrades(monkeypatch):
    """缺 amount 列 → 按快照原序取前 300，不 KeyError。"""
    from app.storage import hs300

    spot = pd.DataFrame(
        {
            "code": [f"600{i:03d}.SH" for i in range(350)],
            "price": [10.0] * 350,
            "volume": [100.0] * 350,
        }
    )  # 无 amount 列
    monkeypatch.setattr(hs300, "_spot_getter", lambda: spot)

    codes = hs300._fetch_spot_top()
    assert codes == spot["code"].head(300).tolist()
    assert len(codes) == 300


def test_p2_56_hs300_spot_top_normal_sorts_by_amount(monkeypatch):
    """amount 列存在时语义不变：仍按成交额降序取前 300。"""
    from app.storage import hs300

    spot = pd.DataFrame(
        {"code": [f"600{i:03d}.SH" for i in range(350)], "amount": list(range(350))}
    )
    monkeypatch.setattr(hs300, "_spot_getter", lambda: spot)

    codes = hs300._fetch_spot_top()
    assert (
        codes == spot.sort_values("amount", ascending=False)["code"].head(300).tolist()
    )
    assert codes[0] == "600349.SH"  # amount 最大者居首


def test_p2_56_hs300_spot_top_missing_code_raises_explicit(monkeypatch):
    """code 列缺失属极端异常：明确 RuntimeError（而非 KeyError）走外层降级链。"""
    from app.storage import hs300

    monkeypatch.setattr(
        hs300, "_spot_getter", lambda: pd.DataFrame({"amount": [1.0, 2.0]})
    )
    with pytest.raises(RuntimeError, match="code"):
        hs300._fetch_spot_top()


# ---------------------------------------------------------------------------
# P2-80：recent_events 代码集 > 999 → 分批 IN 查询不炸 SQLite 变量上限
# ---------------------------------------------------------------------------


def test_p2_80_recent_events_chunked_in_query(tmp_path, monkeypatch):
    import app.core.scanning as SC

    Maker = _db(tmp_path)
    monkeypatch.setattr(SC, "SessionLocal", Maker)

    db = Maker()
    codes = [f"600{i:04d}.SH" for i in range(2000)]  # > 999，超 SQLite 变量上限
    db.add_all(
        [Stock(code=c, name=f"名{i}", is_watchlist=False) for i, c in enumerate(codes)]
    )
    db.add_all(
        [
            SignalEvent(
                stock_code=c, signals=["volume_surge"], status="观察", evidence={}
            )
            for c in codes
        ]
    )
    db.commit()
    db.close()

    rows = SC.recent_events(minutes=60 * 24)
    assert len(rows) == 2000, "2000 只事件应全部返回"
    assert all(r["stock_name"] for r in rows), "分批 IN 查询后名称映射完整"
    # 抽样验证映射正确性（名称 ↔ 代码对得上）
    by_code = {r["stock_code"]: r["stock_name"] for r in rows}
    assert by_code["6000000.SH"] == "名0"
    assert by_code["6001999.SH"] == "名1999"
