"""T-62 批量修复回归测试(P2-5 输入校验 / P2-7 基础设施 / P2-8 行情细节)。

覆盖本次修复的新行为,与既有测试不重复:
- copilot 流式分片重复下发 tool name → 不拼接(原 += 会拼成重复串致校验失败)
- copilot/hermes question 非字符串 → 400(原 AttributeError → 500);hermes context 限长
- factors approved 字符串 "false" → False(原 bool("false")=True)
- metrics.max_drawdown_signed 净值归零除零防护(原返回 nan)
- kcache 分钟线:非交易时段浅深度不 stale(原深度检查先于时段判定 → 误拉)
- events: cancelled 终态 ring 可回收 + GC 并发只起一次
- cache: 崩溃遗留 .tmp 启动清理
- stocks kline: 裸代码 normalize(原缓存恒 miss)
- quote.get_spot: industry 映射结果 TTL 缓存(原每请求全市场 copy)
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.lib import metrics


# ---------------------------------------------------------------------------
# P2-5: copilot 流式分片重复 name
# ---------------------------------------------------------------------------


class _FakeLineStream:
    def __init__(self, lines):
        self._lines = lines

    def __aiter__(self):
        self._it = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def aiter_lines(self):
        return _FakeLineStream(self._lines)

    async def aread(self):
        return b""


class _FakeStreamCM:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    """模拟提供方在两个分片都下发完整 name(仅 arguments 增量)——修复前
    name 累加成重复串,工具校验失败不产出 action。"""

    def __init__(self, *a, **k):
        def sse(obj):
            return "data: " + json.dumps(obj, ensure_ascii=False)

        self._lines = [
            sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {
                                            "name": "apply_signal_center_filters",
                                            "arguments": '{"sectors":',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "name": "apply_signal_center_filters",
                                            "arguments": '["沪主板"]}',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            "data: [DONE]",
        ]

    def stream(self, method, url, **kw):
        return _FakeStreamCM(self._lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_copilot_stream_duplicate_name_no_concat():
    """重复 name 分片不拼接:仍产出合法 action(修复前 += 拼成重复串校验失败)。"""
    from app.core.copilot import FILTER_TOOL, stream_chat

    async def run():
        out = []
        with (
            patch("app.core.copilot.settings.llm_api_key", "test-key"),
            patch("app.core.copilot.httpx.AsyncClient", _FakeClient),
        ):
            async for chunk in stream_chat(
                [{"role": "user", "content": "筛选白酒"}], tools=[FILTER_TOOL]
            ):
                out.append(chunk)
        return out

    out = asyncio.run(run())
    actions = [c for c in out if c.get("kind") == "action"]
    assert len(actions) == 1
    assert actions[0]["action"]["filters"]["sectors"] == ["沪主板"]


# ---------------------------------------------------------------------------
# P2-5: question 非字符串 / context 非 dict → 400
# ---------------------------------------------------------------------------


def test_copilot_question_type_validation():
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/v1/copilot/market/chat", json={"question": 123})
    assert r.status_code == 400
    r = client.post(
        "/api/v1/copilot/market/chat", json={"question": "hi", "context": [1]}
    )
    assert r.status_code == 400


def test_hermes_question_and_context_validation():
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/v1/copilot/hermes/chat", json={"question": {"a": 1}})
    assert r.status_code == 400
    r = client.post(
        "/api/v1/copilot/hermes/chat", json={"question": "hi", "context": "x"}
    )
    assert r.status_code == 400


def test_hermes_context_truncated():
    """route/pageTitle 限长(原无界拼接) + context_text 整体限长。"""
    from app.api import hermes as H

    captured = {}

    async def fake_stream_chat(messages, tools=None):
        captured["user"] = messages[1]["content"]
        return
        yield

    async def run():
        with patch("app.api.hermes.stream_chat", side_effect=fake_stream_chat):
            resp = await H.hermes_chat(
                {
                    "question": "hi",
                    "context": {
                        "route": "x" * 5000,
                        "pageTitle": "y" * 5000,
                        "headings": ["h"] * 3000,
                    },
                }
            )
            async for _ in resp.body_iterator:
                pass

    asyncio.run(run())
    user_msg = captured["user"]
    assert "x" * 201 not in user_msg
    assert "y" * 201 not in user_msg
    assert len(user_msg) <= 8000 + len("hi") + 50


# ---------------------------------------------------------------------------
# P2-5: factors approved 字符串布尔解析
# ---------------------------------------------------------------------------


def test_factors_approved_string_bool():
    """approved="false"(字符串)必须解析为 False——原 bool("false")=True 误批准。"""
    from app.api import factors as F

    calls: dict = {}
    with (
        patch(
            "app.api.factors.advance_step",
            side_effect=lambda *a, **k: calls.setdefault("args", a) or {"ok": True},
        ),
        patch("app.core.factors.PUBLISH_STEPS", [("x", "X")]),
    ):
        F.advance(1, {"step": "x", "approved": "false"})
        assert calls["args"][2] is False
        calls.clear()
        F.advance(1, {"step": "x", "approved": "0"})
        assert calls["args"][2] is False
        calls.clear()
        F.advance(1, {"step": "x", "approved": "true"})
        assert calls["args"][2] is True
        calls.clear()
        F.advance(1, {"step": "x"})
        assert calls["args"][2] is True


# ---------------------------------------------------------------------------
# P2-7: metrics 除零防护
# ---------------------------------------------------------------------------


def test_max_drawdown_signed_zero_peak():
    """净值归零(peak 恒 0)时返回 0.0 而非 nan(原实现 0/0 除零)。"""
    assert metrics.max_drawdown_signed([-1.0, 0.5]) == 0.0
    assert metrics.max_drawdown_signed([-1.0]) == 0.0
    # 正常口径不受影响
    assert abs(metrics.max_drawdown_signed([0.1, -0.2, 0.1, 0.05]) - (-0.2)) < 1e-9


# ---------------------------------------------------------------------------
# P2-7: kcache 分钟线顺序(非交易时段浅深度不 stale)
# ---------------------------------------------------------------------------


def test_minute_stale_off_session_shallow_not_stale(monkeypatch):
    """非交易时段分钟线深度不足不判 stale(原深度检查先于时段判定 → 误触发重拉)。"""
    import app.lib.session as S
    from app.storage import klines as kcache

    df = pd.DataFrame({"date": ["2026-08-10 09:31"], "close": [1.0]})
    monkeypatch.setattr(S, "in_trading_session", lambda: False)
    monkeypatch.setattr(
        kcache, "_cn_now", lambda: pd.Timestamp("2026-08-08 10:00").to_pydatetime()
    )
    assert kcache.is_stale(df, "1") is False

    monkeypatch.setattr(S, "in_trading_session", lambda: True)
    assert kcache.is_stale(df, "1") is True


# ---------------------------------------------------------------------------
# P2-7: events cancelled ring 回收 + GC 并发只起一次
# ---------------------------------------------------------------------------


@pytest.fixture()
def events_clean():
    from app.core import events

    with events._lock:
        events._jobs.clear()
        events._subs.clear()
    events._gc_started = False
    yield
    with events._lock:
        events._jobs.clear()
        events._subs.clear()


def test_cancelled_ring_collected(events_clean):
    """cancelled 终态事件发布后 ring 可被 GC 回收(原取消任务 ring 永不清理)。"""
    from app.core import events

    events._TERMINAL_TTL = -1.0
    events.publish(7, "cancelled", status="cancelled")
    events._collect_terminal_rings()
    assert 7 not in events._jobs


def test_gc_single_thread_under_concurrency(events_clean):
    """并发 publish 终态只启动一个 GC 线程(原无锁双检可起双线程)。"""
    from app.core import events

    threads = [
        threading.Thread(target=lambda: events.publish(i, "done", status="done"))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert events._gc_started is True


def test_delete_job_cancel_publishes_event(events_clean, tmp_path, monkeypatch):
    """delete_job 取消成功发布 cancelled 终态事件(ring 有了终态尾才能被回收)。"""
    import app.core.tasks.runner as Q
    from app.storage.db import Base
    from app.storage.models import ExperimentJob
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(Q, "SessionLocal", Maker)
    db = Maker()
    job = ExperimentJob(job_type="gp", params={}, status="running")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    Q.delete_job(jid)
    from app.core import events

    ring = events._jobs.get(jid)
    assert ring and ring[-1]["type"] == "cancelled"


# ---------------------------------------------------------------------------
# P2-7: cache .tmp 残留清理
# ---------------------------------------------------------------------------


def test_cache_tmp_leftover_cleaned():
    from app.storage.cache import TTLCache

    with tempfile.TemporaryDirectory() as td:
        c = TTLCache(ttl=3600, persist_key="tmpx", persist_dir=td)
        c.set("k1", {"v": 1})
        c.flush()
        (Path(td) / "tmpx_orphan.tmp").write_text("garbage", encoding="utf-8")
        (Path(td) / "other.tmp").write_text("x", encoding="utf-8")

        # 写盘失败(模拟 os.replace 前崩溃)应清理残留 tmp
        real_replace = os.replace

        def boom(src, dst):
            if dst.endswith("tmpx_k2.json"):
                raise OSError("disk full")
            return real_replace(src, dst)

        os.replace = boom
        try:
            c.set("k2", {"v": 2})
        finally:
            os.replace = real_replace
        assert not (Path(td) / "tmpx_k2.json.tmp").exists()

        # 重建实例触发 _sweep_expired:清孤儿 tmp,不碰其它前缀
        TTLCache(ttl=3600, persist_key="tmpx", persist_dir=td)
        assert not (Path(td) / "tmpx_orphan.tmp").exists()
        assert (Path(td) / "other.tmp").exists()


# ---------------------------------------------------------------------------
# P2-8: stocks kline normalize
# ---------------------------------------------------------------------------


def test_stocks_kline_normalizes_code():
    from app.api.stocks import kline as stocks_kline

    called: dict = {}
    df = pd.DataFrame(
        {
            "date": ["2026-08-12"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [0.0],
            "amount": [0.0],
            "pct_change": [0.0],
        }
    )
    with patch(
        "app.api.stocks.cached_kline",
        side_effect=lambda code, period, max_rows: (
            called.setdefault("code", code),
            df,
        )[1],
    ):
        stocks_kline("600519", period="daily", days=100)
    assert called["code"] == "600519.SH"


# ---------------------------------------------------------------------------
# P2-8: quote.get_spot industry 映射缓存
# ---------------------------------------------------------------------------


def test_get_spot_industry_cached():
    from app.core import sources as Q
    from app.core import sources as srcmod

    df_spot = pd.DataFrame(
        {
            "code": ["600519.SH", "000001.SZ"],
            "name": ["贵州茅台", "平安银行"],
            "price": [1.0, 2.0],
            "industry": [""] * 2,
        }
    )
    apply_calls = {"n": 0}

    def fake_apply(df):
        apply_calls["n"] += 1
        out = df.copy()
        out["industry"] = ["白酒", "银行"]
        return out

    with (
        patch.object(Q, "_instance") as mi,
        patch.object(srcmod, "spot_candidates", return_value=["akshare"]),
        patch.object(srcmod, "active_key", return_value="akshare"),
        patch.object(srcmod, "record_spot_success"),
        patch.object(srcmod, "set_active"),
        patch.object(Q.settings, "quote_cache_ttl", 60),
        patch.object(Q, "_spot_industry_cache", None),
        patch.object(Q, "_spot_industry_ts", 0.0),
        patch("app.storage.industry.apply_industry", side_effect=fake_apply),
    ):
        mi.return_value.get_spot.return_value = df_spot
        r1 = Q.get_spot()
        r2 = Q.get_spot()  # TTL 内缓存命中,不再 apply
        r3 = Q.get_spot(refresh=True)  # refresh 强制重应用
    assert apply_calls["n"] == 2
    assert list(r1["industry"]) == ["白酒", "银行"]
    assert list(r3["industry"]) == ["白酒", "银行"]
