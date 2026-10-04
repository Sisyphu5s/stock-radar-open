"""T-54 行情源守卫测试。

- _slice_kline 单传 start_date / end_date：修复前落入 else 分支返回全量
  （降级源增量拉取退化为全量）；且 days 优先级不变（start_date+days 同时传时
  仍按 days 取末尾 N 根）
- probe_source akshare：修复前用 8s 超时拉全市场快照（常态 25s+ 必失败，
  降级源永久不恢复）→ 改为轻量单股日线探测（与生产 K 线同一东财 hist 通道）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _df(n=30, start="2025-01-01"):
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=n).astype(str),
            "close": np.arange(n, dtype=float),
            "volume": np.ones(n),
        }
    )


# ---------------------------------------------------------------------------
# 1) _slice_kline 单参数分支
# ---------------------------------------------------------------------------


def test_slice_kline_single_start_date():
    from app.core.sources import _slice_kline

    df = _df(30)
    out = _slice_kline(df, start_date="2025-01-15", end_date=None, days=None)
    assert len(out) == 16  # 1/15 起（含当天）
    assert out["date"].iloc[0] == "2025-01-15"
    assert out["date"].iloc[-1] == "2025-01-30"
    # 修复前该分支返回全量 30 行


def test_slice_kline_single_end_date():
    from app.core.sources import _slice_kline

    df = _df(30)
    out = _slice_kline(df, start_date=None, end_date="2025-01-15", days=None)
    assert len(out) == 15
    assert out["date"].iloc[-1] == "2025-01-15"


def test_slice_kline_days_still_wins_when_both_given():
    """start_date + days 同时传：days（末尾 N 根）优先级保持（kcache 增量拉取契约）。"""
    from app.core.sources import _slice_kline

    df = _df(30)
    out = _slice_kline(df, start_date="2025-01-10", end_date=None, days=5)
    assert len(out) == 5
    assert out["date"].iloc[-1] == "2025-01-30"


def test_slice_kline_both_dates_range():
    from app.core.sources import _slice_kline

    df = _df(30)
    out = _slice_kline(df, start_date="2025-01-10", end_date="2025-01-20", days=None)
    assert len(out) == 11


# ---------------------------------------------------------------------------
# 2) probe_source akshare 轻量探测
# ---------------------------------------------------------------------------


def test_probe_akshare_uses_lightweight_hist(monkeypatch):
    """探测走单股 hist 轻量接口，不拉全市场快照。"""
    import akshare as ak

    from app.core.sources import probe_source

    calls = {"hist": [], "spot": []}
    real_spot = ak.stock_zh_a_spot_em

    def fake_hist(**kw):
        calls["hist"].append(kw)
        return pd.DataFrame({"日期": ["2025-01-02", "2025-01-03"]})

    def fake_spot(*a, **k):
        calls["spot"].append(True)
        raise AssertionError("探测不得调用全市场快照接口")

    monkeypatch.setattr(ak, "stock_zh_a_hist", fake_hist)
    monkeypatch.setattr(ak, "stock_zh_a_spot_em", fake_spot)

    assert probe_source("akshare", timeout=2.0) is True
    assert calls["spot"] == []
    assert calls["hist"] and calls["hist"][0]["symbol"] == "600519"
    assert calls["hist"][0]["period"] == "daily"
    assert calls["hist"][0]["timeout"] == 2.0
    monkeypatch.setattr(ak, "stock_zh_a_spot_em", real_spot)


def test_probe_akshare_failure_returns_false(monkeypatch):
    import akshare as ak

    from app.core.sources import probe_source

    def boom(**kw):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(ak, "stock_zh_a_hist", boom)
    assert probe_source("akshare", timeout=2.0) is False


def test_probe_akshare_empty_returns_false(monkeypatch):
    import akshare as ak

    from app.core.sources import probe_source

    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **kw: pd.DataFrame())
    assert probe_source("akshare", timeout=2.0) is False
