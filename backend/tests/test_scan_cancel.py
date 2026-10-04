"""market_scan 协作取消：进度回调抛 JobCancelled 时必须向上传播。

回归：scan_once 曾以 except Exception 吞掉回调内异常，取消后的扫描继续
写事件/更新股票，取消迟迟不生效。本测试验证 JobCancelled 不再被吞。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import Stock


def _fake_kline_df(n: int = 60) -> pd.DataFrame:
    close = np.linspace(10, 11, n)
    return pd.DataFrame(
        {
            "date": [f"2026-01-{i % 28 + 1:02d}" for i in range(n)],
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "pct_change": np.zeros(n),
        }
    )


def test_scan_once_propagates_jobcancelled(tmp_path, monkeypatch):
    from app.core.tasks.runner import JobCancelled
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
    db.add(Stock(code="600519.SH", name="茅台", is_watchlist=False))
    db.commit()
    db.close()

    monkeypatch.setattr(SC, "SessionLocal", Maker)
    monkeypatch.setattr(
        SC,
        "get_spot",
        lambda: pd.DataFrame(
            {
                "code": ["600519.SH"],
                "name": ["茅台"],
                "price": [10.0],
                "pct_change": [0.0],
                "volume": [100.0],
                "amount": [1000.0],
                "turnover_rate": [1.0],
            }
        ),
    )
    monkeypatch.setattr(
        SC, "fetch_many", lambda codes, period, **kw: {"600519.SH": _fake_kline_df()}
    )

    calls = {"n": 0}

    def cb(pct, msg):
        calls["n"] += 1
        raise JobCancelled("job 1 已被用户取消")

    # 取消异常必须向上传播（不得被 except Exception 吞掉返回 error 字典）
    with pytest.raises(JobCancelled):
        SC.scan_once(full_universe=True, progress_cb=cb)
    assert calls["n"] >= 1
