"""行情 SSE 推送测试:Hub 快照首连 / 增量阈值过滤 / 信号增量 / 心跳 / 慢消费断开。

不启动真实网络与后台线程:monkeypatch core.sources.get_spot 为假快照序列、
Hub._universe_subset 返回全量(绕开真实 DB 连接)、信号 repo 为假数据;
Hub 独立构造(不经模块单例),monkeypatch 线程启停避免后台线程干扰。
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from app.api.market_stream import _SSE_GET_TIMEOUT, _stream
from app.core.market_stream import (
    MAX_QUEUED,
    PRICE_CHG_THRESHOLD,
    VOLUME_CHG_THRESHOLD,
    MarketStreamHub,
    _diff_changed,
)


def _row(
    code: str,
    price: float,
    pct_change: float = 0.0,
    volume: float = 1000.0,
    amount: float = 1_000_000.0,
    name: str = "测试",
) -> dict:
    return {
        "code": code,
        "name": name,
        "price": price,
        "pct_change": pct_change,
        "volume": volume,
        "amount": amount,
        "turnover_rate": 1.0,
        "industry": "测试行业",
        "source": "sina",
        "timestamp": "2026-08-14T10:00:00",
    }


def _spot(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class _FakeDB:
    def close(self) -> None:
        pass


class _SigRow:
    """模拟 SignalEvent ORM 行对象(repo 返回属性访问,非 dict)。"""

    def __init__(
        self, eid: int, code: str = "600519.SH", status: str = "active"
    ) -> None:
        from datetime import datetime

        self.id = eid
        self.stock_code = code
        self.signals = ["RSI超卖"]
        self.status = status
        self.triggered_at = datetime(2026, 8, 14, 10, 0, 0)
        self.as_of = datetime(2026, 8, 14, 15, 0, 0)
        self.scan_discovered_at = datetime(2026, 8, 14, 10, 0, 5)
        self.period = "daily"


@pytest.fixture()
def hub(monkeypatch):
    """独立 Hub 实例:线程启停 no-op(测试手动调 _tick_once)。"""
    h = MarketStreamHub()
    monkeypatch.setattr(h, "_ensure_thread", lambda: None)
    monkeypatch.setattr(h, "_stop_thread", lambda: None)
    return h


@pytest.fixture()
def fake_spot(monkeypatch):
    """假快照序列:snapshots[-1] 为当前 get_spot 返回值。"""
    from app.core import sources as _sources

    snapshots: list[pd.DataFrame] = []

    def _fake(refresh: bool = False) -> pd.DataFrame:
        return snapshots[-1]

    monkeypatch.setattr(_sources, "get_spot", _fake)
    return snapshots


@pytest.fixture()
def fake_universe(monkeypatch):
    """universe 过滤直通:spot 全量当子集(集合合并逻辑简单,不在本测试范围)。"""

    def _use_all(spot: pd.DataFrame) -> pd.DataFrame:
        return spot

    def _patch(h: MarketStreamHub) -> None:
        monkeypatch.setattr(h, "_universe_subset", _use_all)

    return _patch


@pytest.fixture()
def fake_signals(monkeypatch):
    """假信号 repo:after_id 返回事件行列表,latest 返回固定游标。"""
    import app.storage.db as _db
    from app.storage.repos import signals as _sig_repo
    from app.storage.repos import stocks as _stock_repo

    monkeypatch.setattr(_db, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(_stock_repo, "watchlist_codes", lambda db: {"600519.SH"})
    monkeypatch.setattr(
        _stock_repo, "stock_names", lambda db, codes: {"600519.SH": "贵州茅台"}
    )

    state = {"latest": 5, "rows": []}

    def _after_id(db, codes, after_id):
        return [r for r in state["rows"] if r.id > after_id]

    def _latest(db, codes):
        return state["latest"]

    monkeypatch.setattr(_sig_repo, "latest_event_id", _latest)
    monkeypatch.setattr(_sig_repo, "events_after_id", _after_id)
    return state


def _sig_event(eid: int, code: str = "600519.SH", status: str = "active") -> _SigRow:
    return _SigRow(eid, code, status)


# ---------------------------------------------------------------------------
# diff 阈值过滤(纯函数)
# ---------------------------------------------------------------------------


def test_diff_filters_below_threshold():
    """微变(涨跌 ≤0.1% 且量能 ≤5%)不推;超阈值/新增必推。"""
    old = _spot(
        [_row("600519.SH", 1000.0), _row("000001.SZ", 10.0), _row("300750.SZ", 200.0)]
    )
    new = _spot(
        [
            # 价格 +0.05%,量 +0% → 不推
            _row("600519.SH", 1000.5),
            # 价格不变,量 +10% → 推(量能变化)
            _row("000001.SZ", 10.0, volume=1100.0),
            # 新增 code → 必推
            _row("601318.SH", 50.0),
        ]
    )
    changed = _diff_changed(new, old)
    assert changed is not None
    got = set(changed["code"])
    assert "600519.SH" not in got, "0.05% 涨跌不得推送"
    assert "000001.SZ" in got, "量能变化 10% 必须推送"
    assert "601318.SH" in got, "新增 code 必须推送"


def test_diff_price_threshold_is_strict():
    """涨跌恰为 0.1% 不推(严格大于);超过才推。"""
    old = _spot([_row("600519.SH", 1000.0)])
    at = _diff_changed(_spot([_row("600519.SH", 1001.0)]), old)  # 恰 0.1%
    assert at is not None and at.empty
    over = _diff_changed(_spot([_row("600519.SH", 1001.5)]), old)  # 0.15%
    assert over is not None and list(over["code"]) == ["600519.SH"]


def test_diff_volume_threshold_strict():
    """量能恰 5% 不推;超过才推。"""
    old = _spot([_row("600519.SH", 1000.0, volume=1000.0)])
    at = _diff_changed(_spot([_row("600519.SH", 1000.0, volume=1050.0)]), old)
    assert at is not None and at.empty
    over = _diff_changed(_spot([_row("600519.SH", 1000.0, volume=1060.0)]), old)
    assert over is not None and list(over["code"]) == ["600519.SH"]


def test_diff_old_empty_returns_none():
    assert _diff_changed(_spot([_row("600519.SH", 1.0)]), None) is None
    assert _diff_changed(_spot([_row("600519.SH", 1.0)]), _spot([])) is None


# ---------------------------------------------------------------------------
# Hub 推送逻辑
# ---------------------------------------------------------------------------


def test_first_tick_broadcasts_snapshot_and_sets_ring(hub, fake_spot, fake_universe):
    """首轮:订阅者收到 snapshot 全量事件;ring 写入供新连速发。"""
    fake_universe(hub)
    fake_spot.append(_spot([_row("600519.SH", 1000.0), _row("000001.SZ", 10.0)]))
    sub_id, q = hub.subscribe()

    hub._tick_once()

    e = q.get(timeout=1)
    assert e["type"] == "snapshot"
    assert len(e["items"]) == 2
    assert e["items"][0]["code"] == "600519.SH"
    assert e["items"][0]["price"] == 1000.0
    ring = hub.snapshot()
    assert ring is not None and ring["type"] == "snapshot"
    assert len(ring["items"]) == 2
    hub.unsubscribe(sub_id)


def test_second_tick_broadcasts_only_changed(hub, fake_spot, fake_universe):
    """第二轮:只推变化行(打包 tick);无变化不推。"""
    fake_universe(hub)
    fake_spot.append(_spot([_row("600519.SH", 1000.0), _row("000001.SZ", 10.0)]))
    sub_id, q = hub.subscribe()
    hub._tick_once()
    q.get(timeout=1)  # 排空 snapshot

    # 第 2 轮:600519 涨 0.5%,000001 不动
    fake_spot.append(_spot([_row("600519.SH", 1005.0), _row("000001.SZ", 10.0)]))
    hub._tick_once()
    tick = q.get(timeout=1)
    assert tick["type"] == "tick"
    assert [i["code"] for i in tick["items"]] == ["600519.SH"]

    # 第 3 轮:完全无变化 → 不广播任何事件
    hub._tick_once()
    assert q.empty()


def test_spot_failure_skips_round(hub, monkeypatch, fake_universe):
    """快照获取失败:本轮跳过,不广播、不抛错。"""
    fake_universe(hub)
    from app.core import sources as _sources

    def _boom(refresh=False):
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(_sources, "get_spot", _boom)
    sub_id, q = hub.subscribe()
    hub._tick_once()  # 不应抛异常
    assert q.empty()
    hub.unsubscribe(sub_id)


# ---------------------------------------------------------------------------
# 信号增量
# ---------------------------------------------------------------------------


def test_signal_bootstrap_then_increment(hub, fake_signals):
    """信号:bootstrap 只建游标不推存量;新事件按 id 游标增量推送。"""
    sub_id, q = hub.subscribe()

    # bootstrap:latest=5,无事件可推
    hub._push_signals()
    assert q.empty()
    assert hub._last_signal_id == 5

    # 新事件 id=6/7 → 推 signal 事件,游标前进到 7
    fake_signals["rows"] = [_sig_event(6), _sig_event(7)]
    hub._push_signals()
    e = q.get(timeout=1)
    assert e["type"] == "signal"
    assert [ev["id"] for ev in e["events"]] == [6, 7]
    assert e["events"][0]["stock_name"] == "贵州茅台"
    assert hub._last_signal_id == 7

    # 无新事件 → 不推
    hub._push_signals()
    assert q.empty()
    hub.unsubscribe(sub_id)


def test_signal_no_watchlist_resets_cursor(hub, monkeypatch):
    """关注列表为空:游标重置为 None,不推信号。"""
    import app.storage.db as _db
    from app.storage.repos import stocks as _stock_repo

    monkeypatch.setattr(_db, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(_stock_repo, "watchlist_codes", lambda db: set())
    sub_id, q = hub.subscribe()
    hub._last_signal_id = 3
    hub._push_signals()
    assert hub._last_signal_id is None
    assert q.empty()
    hub.unsubscribe(sub_id)


def test_push_spot_does_not_force_refresh(hub, fake_universe, monkeypatch):
    """C10：推送不再按交易时段强制 refresh——快照刷新交给 TTL/冷却/竞速机制决定。

    每个推送周期不再对全部未冷却源并发全量抓取(refresh=True 会绕过 provider
    热缓存强制拉取);get_spot() 默认 refresh=False,TTL 内直接命中缓存零网络请求。
    """
    fake_universe(hub)
    from app.core import sources as _sources

    calls: list[bool] = []

    def _spy(refresh: bool = False) -> pd.DataFrame:
        calls.append(refresh)
        return _spot([_row("600519.SH", 1000.0)])

    monkeypatch.setattr(_sources, "get_spot", _spy)
    hub._tick_once()
    assert calls == [False], "不得以 refresh=True 强制全量抓取"


# ---------------------------------------------------------------------------
# SSE 生成器
# ---------------------------------------------------------------------------


def test_stream_heartbeat_when_idle(hub, monkeypatch):
    """队列空 → 15s 心跳保活(超时 monkeypatch 为小值)。"""
    monkeypatch.setattr("app.api.market_stream._SSE_GET_TIMEOUT", 0.05)

    async def drive():
        gen = _stream(hub)
        a = await anext(gen)
        b = await anext(gen)
        await gen.aclose()
        return a, b

    a, b = asyncio.run(drive())
    assert a == ": heartbeat\n\n"
    assert b == ": heartbeat\n\n"


def test_stream_snapshot_replay_on_connect(hub, fake_spot, fake_universe):
    """新连接:ring 已有快照 → 首条即 event: snapshot 速发。"""
    fake_universe(hub)
    fake_spot.append(_spot([_row("600519.SH", 1000.0)]))
    hub._tick_once()  # 产生 ring

    async def drive():
        gen = _stream(hub)
        first = await anext(gen)
        await gen.aclose()
        return first

    first = asyncio.run(drive())
    assert first.startswith("event: snapshot")
    assert '"600519.SH"' in first


def test_stream_disconnects_slow_consumer(hub, monkeypatch):
    """慢消费者:队列积压 > MAX_QUEUED → 生成器断开(客户端自动重连)。"""
    monkeypatch.setattr("app.api.market_stream._SSE_GET_TIMEOUT", 0.05)

    async def drive():
        gen = _stream(hub)
        await anext(gen)  # 推进生成器以触发 asubscribe
        q = next(iter(hub._subs.values()))
        # 积压 MAX_QUEUED + 2 条:get 第 1 条后 qsize 仍 > MAX_QUEUED → 断开(不 yield)
        for i in range(MAX_QUEUED + 2):
            q.put_nowait({"type": "tick", "ts": float(i), "items": []})
        await asyncio.sleep(0)  # 让 call_soon_threadsafe 回调执行入队
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        await gen.aclose()

    asyncio.run(drive())


def test_stream_unsubscribe_on_close(hub, monkeypatch):
    """生成器 close(客户端断开)→ finally 清理订阅。"""
    monkeypatch.setattr("app.api.market_stream._SSE_GET_TIMEOUT", 0.05)

    async def drive():
        gen = _stream(hub)
        await anext(gen)  # 推进生成器以触发 asubscribe(队列空 → 心跳)
        sid = next(iter(hub._subs))
        assert len(hub._subs) == 1
        await gen.aclose()
        assert sid not in hub._subs

    asyncio.run(drive())
