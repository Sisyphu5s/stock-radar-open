"""模拟盘 paper 模块测试：服务层纯计算（构造 K 线 DataFrame）+ 端点层（TestClient + mock kcache）。

不触碰生产库、不发起真实网络请求：
- 服务层直接测 app.core.paper 的 experiment/watch/detect_events/validate_signals；
- 端点层用 mini FastAPI app + monkeypatch app.api.paper.cached_kline/_stock_name。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.paper import (
    SUPPORTED_SIGNALS,
    detect_events,
    experiment,
    validate_signals,
    watch,
)
from app.lib.signals.engine import SIGNAL_TYPES


def _vshape_kline(n: int = 90) -> pd.DataFrame:
    """V 形收盘价（先跌后涨）→ 保证 macd/kdj 各出现金叉死叉。"""
    dates = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    half = n // 2
    close = np.concatenate(
        [np.linspace(100.0, 60.0, half), np.linspace(60.0, 120.0, n - half)]
    )
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
            "amount": 1e6,
        }
    )
    df["pct_change"] = df["close"].pct_change().fillna(0) * 100
    return df


def _surge_tail_kline(n: int = 30) -> pd.DataFrame:
    """收盘一路小涨、最后一根放量 3 倍 → 最后一根触发 volume_surge。"""
    dates = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    close = np.arange(10.0, 10.0 + n)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [100.0] * (n - 1) + [300.0],
            "amount": 1e6,
        }
    )
    df["pct_change"] = df["close"].pct_change().fillna(0) * 100
    return df


# ---------------------------------------------------------------------------
# 服务层：金叉/死叉逐 bar 计数（与指标函数独立重算的口径一致）
# ---------------------------------------------------------------------------


def test_macd_golden_cross_count_matches_manual():
    df = _vshape_kline(90)
    from app.lib.indicators.compute import macd

    m = macd(df["close"])
    dif, dea = m["dif"].to_numpy(), m["dea"].to_numpy()
    expected = sum(
        1 for i in range(1, len(dif)) if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
    )
    events = detect_events(df, ["macd_golden_cross"])["macd_golden_cross"]
    assert expected >= 1  # V 形保证至少一次金叉
    assert len(events) == expected
    assert [e["date"] for e in events] == sorted(e["date"] for e in events)  # 时间升序


def test_kdj_golden_cross_count_matches_manual():
    df = _vshape_kline(90)
    from app.lib.indicators.compute import kdj

    k = kdj(df["high"], df["low"], df["close"])
    ks, ds = k["k"].to_numpy(), k["d"].to_numpy()
    expected = sum(
        1 for i in range(1, len(ks)) if ks[i - 1] <= ds[i - 1] and ks[i] > ds[i]
    )
    events = detect_events(df, ["kdj_golden_cross"])["kdj_golden_cross"]
    assert expected >= 1
    assert len(events) == expected


def test_macd_dead_cross_and_kdj_dead_cross_match_manual():
    df = _vshape_kline(90)
    from app.lib.indicators.compute import kdj, macd

    m = macd(df["close"])
    dif, dea = m["dif"].to_numpy(), m["dea"].to_numpy()
    k = kdj(df["high"], df["low"], df["close"])
    ks, ds = k["k"].to_numpy(), k["d"].to_numpy()
    ev = detect_events(df, ["macd_dead_cross", "kdj_dead_cross"])
    exp_macd = sum(
        1 for i in range(1, len(dif)) if dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]
    )
    exp_kdj = sum(
        1 for i in range(1, len(ks)) if ks[i - 1] >= ds[i - 1] and ks[i] < ds[i]
    )
    assert len(ev["macd_dead_cross"]) == exp_macd
    assert len(ev["kdj_dead_cross"]) == exp_kdj
    assert exp_macd >= 1 and exp_kdj >= 1


# ---------------------------------------------------------------------------
# 服务层：r5/r10 收益计算
# ---------------------------------------------------------------------------


def test_r5_uses_future_close():
    df = _vshape_kline(90)
    close = df["close"].to_numpy()
    idx = {d: i for i, d in enumerate(df["date"].astype(str))}
    events = detect_events(df, ["macd_golden_cross"])["macd_golden_cross"]
    assert events  # V 形保证至少一次金叉
    for e in events:
        i = idx[e["date"]]
        assert e["close"] == round(float(close[i]), 4)
        # r5/r10 为小数（与 win_rate 同口径，前端 fmtPct ×100 展示）
        if i + 5 < len(close):
            assert e["r5"] == round(close[i + 5] / close[i] - 1, 4)
        else:
            assert e["r5"] is None
        if i + 10 < len(close):
            assert e["r10"] == round(close[i + 10] / close[i] - 1, 4)
        else:
            assert e["r10"] is None


def test_r5_none_at_series_tail():
    """末尾触发（无未来 bar）→ r5/r10 为 None。"""
    df = _surge_tail_kline(30)
    events = detect_events(df, ["volume_surge"])["volume_surge"]
    assert len(events) == 1  # 只有最后一根量比 3 且收涨
    assert events[0]["date"] == str(df["date"].iloc[-1])
    assert events[0]["r5"] is None
    assert events[0]["r10"] is None


# ---------------------------------------------------------------------------
# 服务层：信号校验
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_signal():
    with pytest.raises(ValueError):
        validate_signals(["not_a_signal"])


def test_validate_rejects_unsupported_signal():
    """SIGNAL_TYPES 白名单内但未实现逐 bar 规则的信号 → 400 语义（ValueError）。"""
    unsupported = [s for s in SIGNAL_TYPES if s not in SUPPORTED_SIGNALS]
    assert unsupported  # 引擎 66 个信号，paper 只支持 10 个
    with pytest.raises(ValueError) as e:
        validate_signals([unsupported[0]])
    assert "暂不支持" in str(e.value)


def test_validate_dedup_and_cap():
    assert validate_signals(["macd_golden_cross", "macd_golden_cross"]) == [
        "macd_golden_cross"
    ]
    with pytest.raises(ValueError):
        validate_signals(["macd_golden_cross"] * 11)
    with pytest.raises(ValueError):
        validate_signals([])


# ---------------------------------------------------------------------------
# 服务层：实验汇总 / watch 结构
# ---------------------------------------------------------------------------


def test_experiment_summary_fields():
    df = _vshape_kline(90)
    out = experiment(df, ["macd_golden_cross", "volume_surge"])
    assert out["computed_bars"] == 90
    assert len(out["results"]) == 2
    r = out["results"][0]
    assert r["signal"] == "macd_golden_cross"
    # 键名与前端 PaperSignalResult 契约对齐（count/last_triggered_at/events 已废弃）
    for key in (
        "triggers",
        "win_rate",
        "avg_r5",
        "avg_r10",
        "best_r5",
        "worst_r5",
        "last_trigger",
        "details",
    ):
        assert key in r, f"缺少 {key}"
    assert r["triggers"] == len(r["details"])
    assert r["last_trigger"] == r["details"][0]["date"]  # 最近一条在前
    if r["win_rate"] is not None:
        assert 0.0 <= r["win_rate"] <= 1.0
    if r["best_r5"] is not None:
        assert r["best_r5"] >= r["worst_r5"]
    # 收益为小数（与 win_rate 同口径），不应是百分比原值
    if r["avg_r5"] is not None:
        assert abs(r["avg_r5"]) < 1.0


def test_watch_structure_and_triggered():
    df = _surge_tail_kline(30)  # 最后一根触发 volume_surge
    out = watch(df, ["volume_surge", "macd_golden_cross"])
    assert set(out) == {"indicators", "triggered", "recent"}
    for key in (
        "rsi6",
        "rsi12",
        "rsi24",
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "macd_dif",
        "macd_dea",
        "macd_hist",
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "boll_upper",
        "boll_mid",
        "boll_lower",
        "volume_ratio",
    ):
        assert key in out["indicators"], f"缺少 indicators.{key}"
    # 最新 bar 命中 volume_surge（量比 3 且收涨）
    assert [t["signal"] for t in out["triggered"]] == ["volume_surge"]
    # label 单一事实源 = lib/signals/catalog（volume_surge 中文名「放量」）
    assert out["triggered"][0]["label"] == "放量"
    assert "量比" in out["triggered"][0]["detail"]
    # recent：最近 5 个触发点（新在前），含 date/signal
    assert len(out["recent"]) <= 5
    assert len(out["recent"]) >= 1
    assert out["recent"][0]["date"] == str(df["date"].iloc[-1])
    assert all(set(r) == {"date", "signal"} for r in out["recent"])


def test_watch_triggered_consistent_with_rule():
    """triggered 与逐 bar 规则一致：最新 bar 若命中规则则应出现在 triggered。"""
    df = _vshape_kline(90)
    from app.lib.indicators.compute import macd

    m = macd(df["close"])
    dif, dea = m["dif"].to_numpy(), m["dea"].to_numpy()
    last = len(df) - 1
    expected_hit = bool(dif[last - 1] <= dea[last - 1] and dif[last] > dea[last])
    out = watch(df, ["macd_golden_cross"])
    hit_now = any(t["signal"] == "macd_golden_cross" for t in out["triggered"])
    assert hit_now == expected_hit


# ---------------------------------------------------------------------------
# 端点层：TestClient + mock kcache
# ---------------------------------------------------------------------------


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.paper import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _patch_kline(monkeypatch, df=None):
    import app.api.paper as P

    monkeypatch.setattr(
        P,
        "cached_kline",
        lambda code, period, max_rows=250: df if df is not None else _vshape_kline(80),
    )
    monkeypatch.setattr(P, "_stock_name", lambda code: "测试股")


def test_experiment_endpoint(monkeypatch):
    _patch_kline(monkeypatch)
    resp = _client().post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "signals": ["macd_golden_cross", "volume_surge"],
            "days": 80,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "600519.SH"
    assert body["name"] == "测试股"
    assert body["period"] == "daily"
    assert body["days"] == 80
    assert body["computed_bars"] == 80
    assert len(body["results"]) == 2
    r = body["results"][0]
    assert r["signal"] == "macd_golden_cross"
    assert r["triggers"] >= 1
    assert len(r["details"]) <= 50
    assert all({"date", "close", "r5", "r10"} <= set(e) for e in r["details"])


def test_experiment_unknown_signal_400(monkeypatch):
    _patch_kline(monkeypatch)
    resp = _client().post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "signals": ["bogus_signal"],
        },
    )
    assert resp.status_code == 400
    assert "未知信号" in resp.json()["detail"]


def test_experiment_unsupported_signal_400(monkeypatch):
    _patch_kline(monkeypatch)
    unsupported = [s for s in SIGNAL_TYPES if s not in SUPPORTED_SIGNALS][0]
    resp = _client().post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "signals": [unsupported],
        },
    )
    assert resp.status_code == 400
    assert "暂不支持" in resp.json()["detail"]


def test_experiment_invalid_period_400(monkeypatch):
    _patch_kline(monkeypatch)
    resp = _client().post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "period": "weekly",
            "signals": ["macd_golden_cross"],
        },
    )
    assert resp.status_code == 400


def test_experiment_validation_422(monkeypatch):
    _patch_kline(monkeypatch)
    c = _client()
    # 超过 10 个信号
    resp = c.post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "signals": ["macd_golden_cross"] * 11,
        },
    )
    assert resp.status_code == 422
    # days 越界
    resp = c.post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "signals": ["macd_golden_cross"],
            "days": 0,
        },
    )
    assert resp.status_code == 422
    resp = c.post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "signals": ["macd_golden_cross"],
            "days": 801,
        },
    )
    assert resp.status_code == 422


def test_experiment_empty_kline_404(monkeypatch):
    _patch_kline(monkeypatch, df=pd.DataFrame())
    resp = _client().post(
        "/api/v1/paper/experiment",
        json={
            "code": "600519.SH",
            "signals": ["macd_golden_cross"],
        },
    )
    assert resp.status_code == 404


def test_watch_endpoint(monkeypatch):
    _patch_kline(monkeypatch, df=_surge_tail_kline(30))
    resp = _client().get(
        "/api/v1/paper/watch",
        params={"code": "600519.SH", "signals": "volume_surge,macd_golden_cross"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "600519.SH"
    assert body["name"] == "测试股"
    assert body["period"] == "daily"
    assert body["updated_at"] == str(_surge_tail_kline(30)["date"].iloc[-1])
    # API 契约结构（与前端 PaperWatchResult 对齐）：snapshot/hit_signals/recent_triggers
    assert set(body) == {
        "code",
        "name",
        "period",
        "updated_at",
        "snapshot",
        "hit_signals",
        "recent_triggers",
    }
    assert body["hit_signals"][0] == "volume_surge"
    assert body["snapshot"]["volume_ratio"] is not None
    assert len(body["recent_triggers"]) <= 5


def test_watch_unknown_signal_400(monkeypatch):
    _patch_kline(monkeypatch)
    resp = _client().get(
        "/api/v1/paper/watch", params={"code": "600519.SH", "signals": "bogus"}
    )
    assert resp.status_code == 400


def test_watch_missing_signals_400(monkeypatch):
    _patch_kline(monkeypatch)
    resp = _client().get(
        "/api/v1/paper/watch", params={"code": "600519.SH", "signals": ""}
    )
    assert resp.status_code == 400
