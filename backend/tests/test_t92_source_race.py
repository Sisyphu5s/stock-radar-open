"""T-92 数据连接双通道并发竞速（P1-66）+ P1-62 成交量校验 + P1-63 新闻源策略 测试。

覆盖：
- spot/kline 并发竞速 first-success：慢失败源不阻塞成功路径、失败源冷却登记
  （含后台慢源 done-callback 登记）、全部失败语义、manual 锁定退化为单源串行；
- get_spot/get_kline 集成（打桩 _instances）；
- financials/earnings 候选降级（异常→冷却登记→下一候选）；
- _normalize_volume 单位校验（~100× 修正、一致/中间量级不动）；
- 新闻源东财优先 + 新浪空命中冷却。

全部走打桩，不触碰网络与生产库。
"""

from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

from app.core import sources as S


@pytest.fixture(autouse=True)
def _reset_sources_state():
    """每个测试前重置健康/新闻/活跃源状态，避免跨测试污染。"""
    S._active = None
    S._news_misses.clear()
    S._news_blocked_until.clear()
    for k in S._health:
        h = S._health[k]
        h["spot_fails"] = 0
        h["kline_fails"] = 0
        h["spot_blocked_until"] = 0.0
        h["kline_blocked_until"] = 0.0
        h["slow_until"] = 0.0
        h["ok"] = False
        h["last_error"] = ""
    yield
    S._active = None


def _spot_df(n=2):
    return pd.DataFrame(
        {
            "code": ["600519.SH", "000001.SZ"],
            "name": ["贵州茅台", "平安银行"],
            "price": [1700.0, 11.0],
            "pct_change": [1.2, 0.0],
            "volume": [30000.0, 50000.0],
            "amount": [5.1e8, 5.5e7],
            "turnover_rate": [0.3, 0.2],
            "pe": [30.0, 6.0],
            "pb": [9.0, 0.8],
            "market_cap": [21350.0, 2135.0],
            "float_cap": [21350.0, 2135.0],
            "industry": "",
            "source": "akshare",
        }
    )


# ---------------------------------------------------------------------------
# 1) spot 并发竞速 first-success
# ---------------------------------------------------------------------------


def test_spot_race_first_success_fast_source_wins(monkeypatch):
    df = _spot_df()
    calls: list[str] = []

    def fake_fetch(key):
        calls.append(key)
        if key == "akshare":
            time.sleep(0.3)
            raise RuntimeError("akshare 挂(测试模拟)")
        return df

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina"])
    errors: list[str] = []
    key, result = S.spot_first_success(fake_fetch, errors=errors)
    assert key == "sina"
    assert result is df
    assert set(calls) == {"akshare", "sina"}  # 双源并发发起
    assert S.active_key() == "sina"
    assert S._health["sina"]["ok"] is True
    # 慢失败源冷却登记由 done-callback 在后台完成，等待其落盘
    time.sleep(0.5)
    assert S._health["akshare"]["spot_fails"] == 1
    assert S._health["akshare"]["spot_blocked_until"] > time.time()
    assert any("akshare" in e for e in errors)


def test_spot_race_slow_source_does_not_block(monkeypatch):
    """first-success 不被慢失败源拖住（P1-66 核心目标）。"""
    df = _spot_df()
    gate = threading.Event()

    def fake_fetch(key):
        if key == "akshare":
            gate.wait(3)  # 慢失败源挂起
            raise RuntimeError("akshare 挂(测试模拟)")
        return df

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina"])
    t0 = time.time()
    key, result = S.spot_first_success(fake_fetch)
    elapsed = time.time() - t0
    assert key == "sina"
    assert result is df
    assert elapsed < 1.0, f"被慢源拖住: {elapsed:.2f}s"
    gate.set()
    time.sleep(0.5)
    assert S._health["akshare"]["spot_fails"] == 1  # 后台登记未丢失


def test_spot_race_all_fail_records_cooldown(monkeypatch):
    def boom(key):
        raise RuntimeError(f"{key} 网络不可达(测试模拟)")

    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina", "tencent"])
    errors: list[str] = []
    key, result = S.spot_first_success(boom, errors=errors)
    assert key is None and result is None
    for k in ("akshare", "sina", "tencent"):
        assert S._health[k]["spot_fails"] == 1, f"{k} 未记冷却"
        assert S._health[k]["spot_blocked_until"] > time.time()
    assert len(errors) == 3


def test_spot_race_manual_mode_single_candidate(monkeypatch):
    """manual 锁定单源 → 竞速退化为单源串行，不并发其他源。"""
    df = _spot_df()
    calls: list[str] = []

    def fake_fetch(key):
        calls.append(key)
        return df

    monkeypatch.setattr(S, "spot_candidates", lambda: ["sina"])  # manual 锁定 sina
    key, result = S.spot_first_success(fake_fetch)
    assert key == "sina"
    assert calls == ["sina"]


def test_spot_race_no_candidates_returns_none(monkeypatch):
    monkeypatch.setattr(S, "spot_candidates", lambda: [])
    assert S.spot_first_success(lambda k: None) == (None, None)


# ---------------------------------------------------------------------------
# 2) kline 并发竞速
# ---------------------------------------------------------------------------


def _kline_df(n=30):
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2026-01-05", periods=n).strftime("%Y-%m-%d"),
            "open": [10.0] * n,
            "high": [11.0] * n,
            "low": [9.0] * n,
            "close": [10.5] * n,
            "volume": [1000.0] * n,
            "amount": [1e6] * n,
        }
    )


def test_kline_race_first_success(monkeypatch):
    df = _kline_df()

    def fake_fetch(key):
        if key == "akshare":
            time.sleep(0.3)
            raise RuntimeError("akshare 挂(测试模拟)")
        return df

    monkeypatch.setattr(S, "kline_candidates", lambda: ["akshare", "sina"])
    key, result = S.kline_first_success(fake_fetch)
    assert key == "sina"
    assert result is df
    assert S.active_key() == "sina"
    time.sleep(0.5)
    assert S._health["akshare"]["kline_fails"] == 1


def test_kline_race_empty_data_no_cooldown(monkeypatch):
    """K 线空数据不记冷却（单只缺失≠源故障），异常才记。"""
    calls: list[str] = []

    def fake_fetch(key):
        calls.append(key)
        if key == "akshare":
            raise RuntimeError("akshare 挂(测试模拟)")
        return pd.DataFrame()  # sina 返回空 → 不记冷却

    monkeypatch.setattr(S, "kline_candidates", lambda: ["akshare", "sina"])
    key, result = S.kline_first_success(fake_fetch)
    assert key is None and result is None
    assert S._health["akshare"]["kline_fails"] == 1
    assert S._health["sina"]["kline_fails"] == 0  # 空数据不记
    assert S._health["sina"]["kline_blocked_until"] == 0.0


def test_kline_race_all_fail(monkeypatch):
    def boom(key):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(S, "kline_candidates", lambda: ["akshare", "sina"])
    key, result = S.kline_first_success(boom)
    assert key is None and result is None
    assert S._health["akshare"]["kline_fails"] == 1
    assert S._health["sina"]["kline_fails"] == 1
    # kline 冷却需连续 5 次才触发（120s）——第 1 次仅计数
    assert S._health["akshare"]["kline_blocked_until"] == 0.0


def test_kline_five_failures_trigger_block(monkeypatch):
    def boom(key):
        raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(S, "kline_candidates", lambda: ["akshare"])
    for _ in range(5):
        S.kline_first_success(boom)
    assert S._health["akshare"]["kline_blocked_until"] > time.time()


# ---------------------------------------------------------------------------
# 3) get_spot / get_kline 集成（打桩 _instances）
# ---------------------------------------------------------------------------


def test_get_spot_uses_race(monkeypatch):
    from app.core import sources as Q

    df = _spot_df()
    monkeypatch.setattr(S, "spot_candidates", lambda: ["akshare", "sina"])
    monkeypatch.setattr(S, "record_spot_success", lambda *a, **k: None)
    monkeypatch.setattr(S, "set_active", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.industry.apply_industry", lambda d: d.copy())
    monkeypatch.setattr("app.lib.session.session_spot_ttl", lambda base, now=None: 90)

    class _SlowAkshare:
        name = "akshare"

        def get_spot(self, refresh=False):
            time.sleep(0.2)
            raise RuntimeError("akshare 挂(测试模拟)")

    class _FastSina:
        name = "sina"

        def get_spot(self, refresh=False):
            return df

    monkeypatch.setattr(
        Q, "_instances", {"akshare": _SlowAkshare(), "sina": _FastSina()}
    )
    monkeypatch.setattr(Q, "_spot_industry_cache", None)
    monkeypatch.setattr(Q, "_spot_industry_ts", 0.0)
    out = Q.get_spot()
    assert out is not None and len(out) == 2
    assert out["code"].iloc[0] == "600519.SH"


def test_get_kline_uses_race(monkeypatch):
    from app.core import sources as Q

    df = _kline_df()
    monkeypatch.setattr(S, "kline_candidates", lambda: ["akshare", "sina"])

    class _BoomAkshare:
        name = "akshare"

        def get_kline(
            self,
            code,
            period="daily",
            start_date=None,
            end_date=None,
            days=None,
            compose_intraday=False,
        ):
            raise RuntimeError("akshare 挂(测试模拟)")

    class _OkSina:
        name = "sina"

        def get_kline(
            self,
            code,
            period="daily",
            start_date=None,
            end_date=None,
            days=None,
            compose_intraday=False,
        ):
            return df

    monkeypatch.setattr(Q, "_instances", {"akshare": _BoomAkshare(), "sina": _OkSina()})
    out = Q.get_kline("600519.SH", "daily", days=10)
    assert out is not None and len(out) == 30


def test_get_kline_all_fail_raises(monkeypatch):
    from app.core import sources as Q

    monkeypatch.setattr(S, "kline_candidates", lambda: ["akshare", "sina"])

    class _Boom:
        name = "boom"

        def get_kline(self, *a, **k):
            raise RuntimeError("网络不可达(测试模拟)")

    monkeypatch.setattr(Q, "_instances", {"akshare": _Boom(), "sina": _Boom()})
    with pytest.raises(RuntimeError, match="K线获取失败"):
        Q.get_kline("600519.SH")


# ---------------------------------------------------------------------------
# 4) financials / earnings 候选降级
# ---------------------------------------------------------------------------


def test_get_financials_degrade_to_next_candidate(monkeypatch):
    from app.core import sources as Q

    class _SinaBroken:
        def get_financials(self, code):
            raise RuntimeError("新浪财务摘要挂(测试模拟)")

    class _AkshareOk:
        def get_financials(self, code):
            return {"period": "2026-03-31", "归母净利润": "123亿"}

    monkeypatch.setattr(
        Q, "_instances", {"sina": _SinaBroken(), "akshare": _AkshareOk()}
    )
    out = Q.get_financials("600519.SH")
    assert out == {"period": "2026-03-31", "归母净利润": "123亿"}
    assert S._health["sina"]["kline_fails"] == 1  # 异常已按 kline 冷却语义登记


def test_get_financials_empty_returns_none_no_next_call(monkeypatch):
    """空数据（无财务摘要）直接返回 None，不跨候选（双候选同底层通道）。"""
    from app.core import sources as Q

    calls: list[str] = []

    class _SinaEmpty:
        def get_financials(self, code):
            calls.append("sina")
            return None

    monkeypatch.setattr(Q, "_instances", {"sina": _SinaEmpty()})
    assert Q.get_financials("600519.SH") is None
    assert calls == ["sina"]


def test_get_earnings_candidate(monkeypatch):
    from app.core import sources as Q

    class _Akshare:
        def get_earnings(self, code):
            return {"period": "2026-06-30", "eps": 1.5}

    monkeypatch.setattr(Q, "_instances", {"akshare": _Akshare()})
    out = Q.get_earnings("600519.SH")
    assert out == {"period": "2026-06-30", "eps": 1.5}


def test_get_financials_skips_blocked_source(monkeypatch):
    """源已冷却（spot+kline 双通道冷却）→ 候选直接跳过，降级到下一候选。"""
    from app.core import sources as Q

    calls: list[str] = []

    class _Fake:
        def get_financials(self, code):
            calls.append("called")
            return {"period": "2026-03-31"}

    S._health["sina"]["spot_blocked_until"] = time.time() + 600
    S._health["sina"]["kline_blocked_until"] = time.time() + 600
    monkeypatch.setattr(Q, "_instances", {"sina": _Fake(), "akshare": _Fake()})
    out = Q.get_financials("600519.SH")
    assert out == {"period": "2026-03-31"}  # sina 冷却 → 降级到 akshare
    assert calls == ["called"]  # sina 未被调用


# ---------------------------------------------------------------------------
# 5) _normalize_volume 单位校验（P1-62）
# ---------------------------------------------------------------------------


def test_normalize_volume_unit_fix():
    from app.core.sources import _normalize_volume

    # 一致量级 → 不动
    assert _normalize_volume(100.0, 100.0) == 100.0
    assert _normalize_volume(120.0, 100.0) == 120.0  # 1.2 倍
    # kline 偏大 ~100 倍（股→手）→ /100
    assert _normalize_volume(10_000.0, 100.0) == 100.0
    assert _normalize_volume(60_000.0, 1000.0) == 600.0  # 60 倍也在阈值内
    # kline 偏小 ~100 倍（反向）→ ×100
    assert _normalize_volume(1.0, 100.0) == 100.0
    # 中间量级（盘中合成 bar 与 spot 的口径差）→ 不动，不臆改
    assert _normalize_volume(500.0, 100.0) == 500.0  # 5 倍
    # 0 值 / spot 缺失 → 跳过
    assert _normalize_volume(0.0, 100.0) == 0.0
    assert _normalize_volume(100.0, None) == 100.0
    assert _normalize_volume(100.0, 0.0) == 100.0


def test_normalize_last_bar_volume_only_today(monkeypatch):
    """仅当日 bar 与 spot 比对修正；历史 bar / 无 spot 不动。"""
    from app.core import sources as Q
    from app.lib import session as sess

    monkeypatch.setattr(sess, "now_cn", lambda: pd.Timestamp("2026-08-12 10:00"))

    # 当日 bar + spot 可用 → 修正（kline 是 spot 的 100 倍 → /100）
    df = pd.DataFrame(
        {
            "date": ["2026-08-11", "2026-08-12"],
            "close": [10.0, 10.5],
            "volume": [1000.0, 3_000_000.0],
        }
    )
    spot_df = _spot_df()
    spot_df.loc[0, "volume"] = 30_000.0  # 600519.SH 当日 spot 30000 手
    inst = type("I", (), {"_spot_cache": spot_df})()
    monkeypatch.setattr(Q, "_instances", {"akshare": inst})
    out = Q._normalize_last_bar_volume(df.copy(), "600519.SH")
    assert out["volume"].iloc[-1] == 30_000.0  # 3000000 / 100
    assert out["volume"].iloc[0] == 1000.0  # 历史 bar 不动

    # 最后 bar 非当日 → 不校验
    df_old = pd.DataFrame({"date": ["2026-08-10", "2026-08-11"], "volume": [1.0, 2.0]})
    out2 = Q._normalize_last_bar_volume(df_old.copy(), "600519.SH")
    assert list(out2["volume"]) == [1.0, 2.0]

    # 无 spot 缓存 → 不动
    monkeypatch.setattr(Q, "_instances", {})
    out3 = Q._normalize_last_bar_volume(df.copy(), "600519.SH")
    assert out3["volume"].iloc[-1] == 3_000_000.0


# ---------------------------------------------------------------------------
# 6) 新闻源策略（P1-63）
# ---------------------------------------------------------------------------


def test_news_akshare_priority(monkeypatch):
    """东财固定优先：akshare 有新闻即返回，不走到新浪。"""
    from app.core import sources as Q

    calls: list[str] = []

    class _Akshare:
        def get_news(self, code, limit=10):
            calls.append("akshare")
            return [{"title": "东财新闻", "url": "", "time": "", "source": "东方财富"}]

    class _Sina:
        def get_news(self, code, limit=10):
            calls.append("sina")
            return [{"title": "新浪新闻", "url": "", "time": "", "source": "新浪财经"}]

    monkeypatch.setattr(Q, "_instances", {"akshare": _Akshare(), "sina": _Sina()})
    out = Q.get_news("600519.SH")
    assert out[0]["title"] == "东财新闻"
    assert calls == ["akshare"]


def test_news_sina_fallback_and_cooldown(monkeypatch):
    """akshare 无新闻 → 新浪兜底；新浪连续空命中 3 次 → 冷却跳过。"""
    from app.core import sources as Q

    class _Akshare:
        def get_news(self, code, limit=10):
            return []

    class _Sina:
        def get_news(self, code, limit=10):
            return []

    monkeypatch.setattr(Q, "_instances", {"akshare": _Akshare(), "sina": _Sina()})
    # 连续 3 次空命中 → 新浪进入冷却
    for _ in range(3):
        assert Q.get_news("600519.SH") == []
    assert S.is_news_blocked("sina") is True

    # 冷却中：新浪被跳过，且不累计调用
    calls: list[str] = []

    class _Sina2:
        def get_news(self, code, limit=10):
            calls.append("sina")
            return [{"title": "x", "url": "", "time": "", "source": "新浪"}]

    monkeypatch.setattr(Q, "_instances", {"akshare": _Akshare(), "sina": _Sina2()})
    assert Q.get_news("600519.SH") == []  # 新浪被冷却跳过 → 整体返回空
    assert calls == []


def test_news_hit_resets_cooldown():
    """命中清除冷却登记：record_news_hit 复位 miss 计数并解除冷却。"""
    S._news_misses["sina"] = 2
    S._news_blocked_until["sina"] = time.time() + 600
    S.record_news_hit("sina")
    assert S.is_news_blocked("sina") is False
    assert S._news_misses["sina"] == 0


def test_news_blocked_skips_sina(monkeypatch):
    """冷却中的新浪被跳过（不调用）；其他源无数据 → 整体返回空。"""
    from app.core import sources as Q

    calls: list[str] = []

    class _Sina:
        def get_news(self, code, limit=10):
            calls.append("sina")
            return [{"title": "x", "url": "", "time": "", "source": "新浪"}]

    S._news_blocked_until["sina"] = time.time() + 600  # 预置冷却
    monkeypatch.setattr(
        Q,
        "_instances",
        {
            "akshare": type("A", (), {"get_news": lambda s, c, limit=10: []})(),
            "sina": _Sina(),
        },
    )
    assert Q.get_news("600519.SH") == []  # 新浪被冷却跳过 → 整体返回空
    assert calls == []
