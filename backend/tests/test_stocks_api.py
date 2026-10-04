"""个股 API 聚焦测试：search / kline / quote / fundamental / news / financials / earnings 冒烟。

全部走 mock 数据源（monkeypatch stocks 模块引用的 quote/kcache 函数），
不触碰生产库、不发起真实网络请求。模式与 test_todos 一致（直接调用路由函数）。
"""

from __future__ import annotations

import types

import pandas as pd
import pytest
from fastapi import HTTPException

from app.api import stocks as ST
from app.storage.cache import TTLCache


@pytest.fixture()
def spot() -> pd.DataFrame:
    """mock 全市场快照（内部列契约：code/name/price/pct_change/...）。"""
    return pd.DataFrame(
        [
            {
                "code": "600519.SH",
                "name": "贵州茅台",
                "price": 1700.0,
                "pct_change": 1.2,
                "volume": 30000.0,
                "amount": 5.1e8,
                "turnover_rate": 0.3,
                "volume_ratio": 1.25,
                "pe": 30.0,
                "pb": 9.0,
                "market_cap": 21350.0,
                "float_cap": 21350.0,
                "industry": "白酒",
                "source": "sina",
            },
            {
                "code": "000858.SZ",
                "name": "五粮液",
                "price": 150.0,
                "pct_change": -0.5,
                "volume": 20000.0,
                "amount": 3.0e8,
                "turnover_rate": 0.2,
                "pe": 20.0,
                "pb": 5.0,
                "market_cap": 5800.0,
                "float_cap": 5800.0,
                "industry": "白酒",
                "source": "sina",
            },
        ]
    )


def _fake_kline_df(n: int = 12) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=n).strftime("%Y-%m-%d")
    close = pd.Series(range(10, 10 + n)) * 1.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0,
            "amount": 1e6,
            "pct_change": close.pct_change().fillna(0) * 100,
        }
    )


def _mock_provider(monkeypatch):
    monkeypatch.setattr(ST, "get_provider", lambda: types.SimpleNamespace(name="mock"))


# ---------------------------------------------------------------------------
# /stocks/search
# ---------------------------------------------------------------------------


def test_search_empty_spot(monkeypatch):
    monkeypatch.setattr(ST, "get_spot", lambda: pd.DataFrame())
    assert ST.search(q="") == {"data": []}
    assert ST.search(q="600519") == {"data": []}


def test_search_no_query_returns_all(monkeypatch, spot):
    monkeypatch.setattr(ST, "get_spot", lambda: spot)
    out = ST.search(q="")["data"]
    assert len(out) == 2
    assert set(out[0].keys()) == {"code", "name"}


def test_search_filters_by_code_and_name(monkeypatch, spot):
    monkeypatch.setattr(ST, "get_spot", lambda: spot)
    assert [r["code"] for r in ST.search(q="600519")["data"]] == ["600519.SH"]
    assert [r["name"] for r in ST.search(q="五粮")["data"]] == ["五粮液"]
    assert ST.search(q="不存在的股票")["data"] == []
    # 带后缀的查询被剥离为裸代码匹配
    assert len(ST.search(q="600519.SH")["data"]) == 1


def test_search_limit_clamped(monkeypatch, spot):
    monkeypatch.setattr(ST, "get_spot", lambda: spot)
    assert len(ST.search(q="", limit=1)["data"]) == 1
    assert len(ST.search(q="", limit=9999)["data"]) == 2  # clamp 到 100，仍只返回 2 条


def test_search_source_unavailable_503(monkeypatch):
    monkeypatch.setattr(
        ST, "get_spot", lambda: (_ for _ in ()).throw(RuntimeError("挂"))
    )
    with pytest.raises(HTTPException) as e:
        ST.search(q="600519")
    assert e.value.status_code == 503


# ---------------------------------------------------------------------------
# /stocks/{code}/kline
# ---------------------------------------------------------------------------


def test_kline_structure(monkeypatch):
    _mock_provider(monkeypatch)
    monkeypatch.setattr(
        ST, "cached_kline", lambda code, period, max_rows=250: _fake_kline_df()
    )
    out = ST.kline("600519.SH", period="daily", days=10)
    assert out["code"] == "600519.SH"
    assert out["period"] == "daily"
    assert out["source"] == "mock"
    for key in (
        "dates",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pct_change",
    ):
        assert key in out, f"缺少 {key}"
    n = len(out["dates"])
    assert n == 12
    for key in ("open", "high", "low", "close", "volume", "amount", "pct_change"):
        assert len(out[key]) == n


def test_kline_invalid_period_400(monkeypatch):
    _mock_provider(monkeypatch)
    monkeypatch.setattr(
        ST, "cached_kline", lambda code, period, max_rows=250: _fake_kline_df()
    )
    with pytest.raises(HTTPException) as e:
        ST.kline("600519.SH", period="bogus")
    assert e.value.status_code == 400


def test_kline_empty_returns_404(monkeypatch):
    monkeypatch.setattr(
        ST, "cached_kline", lambda code, period, max_rows=250: pd.DataFrame()
    )
    with pytest.raises(HTTPException) as e:
        ST.kline("600519.SH")
    assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# /stocks/{code}/quote / fundamental
# ---------------------------------------------------------------------------


def test_quote_structure(monkeypatch, spot):
    _mock_provider(monkeypatch)
    monkeypatch.setattr(ST, "get_spot", lambda: spot)
    out = ST.quote("600519.SH")
    assert out["code"] == "600519.SH"
    assert out["name"] == "贵州茅台"
    assert out["price"] == 1700.0
    assert out["volume_ratio"] == 1.25  # P1-54: quote 透传量比
    assert out["source"] == "mock"
    # 裸代码自动规范化
    out2 = ST.quote("600519")
    assert out2["code"] == "600519.SH"


def test_quote_missing_code_404(monkeypatch, spot):
    monkeypatch.setattr(ST, "get_spot", lambda: spot)
    with pytest.raises(HTTPException) as e:
        ST.quote("999999.SZ")
    assert e.value.status_code == 404


def test_quote_empty_spot_404(monkeypatch):
    monkeypatch.setattr(ST, "get_spot", lambda: pd.DataFrame())
    with pytest.raises(HTTPException) as e:
        ST.quote("600519.SH")
    assert e.value.status_code == 404


def test_fundamental_structure(monkeypatch, spot):
    monkeypatch.setattr(ST, "get_spot", lambda: spot)
    out = ST.fundamental("600519.SH")
    assert out["code"] == "600519.SH"
    assert out["pe"] == 30.0 and out["pb"] == 9.0
    assert out["market_cap"] == 21350.0 and out["float_cap"] == 21350.0
    assert out["turnover_rate"] == 0.3


# ---------------------------------------------------------------------------
# /stocks/{code}/news（news_cache 命中/未命中）
# ---------------------------------------------------------------------------


def test_news_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ST,
        "get_news",
        lambda code, limit=10: [
            {
                "title": "茅台公告",
                "url": "http://x",
                "time": "2026-08-11 09:00",
                "source": "mock",
            }
        ],
    )
    fresh = TTLCache(ttl=300, maxsize=16)  # 无磁盘落盘，隔离生产 news_cache
    monkeypatch.setattr("app.storage.cache.news_cache", fresh)

    first = ST.news("600519.SH")
    assert first["code"] == "600519.SH"
    assert first["cached"] is False
    assert first["data"][0]["title"] == "茅台公告"

    second = ST.news("600519.SH")
    assert second["cached"] is True  # 5 分钟 TTL 内二次命中

    other = ST.news("000858.SZ")
    assert other["cached"] is False  # 不同 key 不共享缓存


# ---------------------------------------------------------------------------
# /stocks/{code}/financials / earnings（独立数据源调用，mock quote 单例 _instances）
# ---------------------------------------------------------------------------


def _patch_instances(monkeypatch, **providers):
    """替换 quote._instances 单例表：_instance(key) 直接返回 mock provider。"""
    monkeypatch.setattr("app.core.sources._instances", dict(providers))


def test_financials_structure(monkeypatch):
    class _FakeSina:
        def get_financials(self, code):
            return {
                "period": "2026-03-31",
                "prev_period": "2025-12-31",
                "归母净利润": "123亿",
                "净资产收益率": "15%",
            }

    _patch_instances(monkeypatch, sina=_FakeSina())
    out = ST.financials("600519")
    assert out["code"] == "600519.SH"  # 规范化
    assert out["period"] == "2026-03-31"
    assert out["归母净利润"] == "123亿"


def test_financials_empty_404(monkeypatch):
    class _FakeSina:
        def get_financials(self, code):
            return None

    _patch_instances(monkeypatch, sina=_FakeSina())
    with pytest.raises(HTTPException) as e:
        ST.financials("600519.SH")
    assert e.value.status_code == 404


def test_earnings_structure(monkeypatch):
    class _FakeAkshare:
        def get_earnings(self, code):
            return {"period": "2026-06-30", "eps": 1.5, "roe": 12.0}

    _patch_instances(monkeypatch, akshare=_FakeAkshare())
    out = ST.earnings("600519")
    assert out["code"] == "600519.SH"
    assert out["period"] == "2026-06-30"
    assert out["eps"] == 1.5
