"""Copilot 多轮历史上送测试（T-31 修 P1-10）+ Hermes 页面快照并入上下文（T-93）。

覆盖（不发起真实网络请求，monkeypatch app.api.copilot.stream_chat 捕获 messages）：
- history 合法（user/assistant 混合）→ 按序插入 system 之后、当前问题之前
- 超过 8 轮（>16 条消息）→ 只保留最近 8 轮（16 条）
- 非法项（非 dict / 非法 role / 非字符串 content / 空白 content）→ 丢弃
- 单条 content 超 MAX_HISTORY_MSG_CHARS → 截断
- history 缺失 / 非 list → 空列表（消息仅 system + 当前问题，向后兼容）
- context 携带 page 快照 → 上下文注入页面快照段落（Hermes 接线）
- research/chat 同构；_normalize_history 纯函数语义
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.copilot import MAX_HISTORY_TURNS, _normalize_history

_CAPTURED: dict = {}


async def _fake_stream(messages: list, tools: list | None = None):
    """替换 app.api.copilot.stream_chat：捕获 messages 并产出单条文本。"""
    _CAPTURED["messages"] = messages
    _CAPTURED["tools"] = tools
    yield {"kind": "text", "text": "ok"}


@pytest.fixture()
def client(monkeypatch):
    from app.main import app

    monkeypatch.setattr("app.api.copilot.stream_chat", _fake_stream)
    _CAPTURED.clear()
    return TestClient(app)


# ---------------------------------------------------------------------------
# _normalize_history 纯函数
# ---------------------------------------------------------------------------


def test_normalize_history_pure():
    assert _normalize_history(None) == []
    assert _normalize_history("x") == []
    assert _normalize_history(123) == []
    assert _normalize_history([{"role": "user", "content": "  hi  "}]) == [
        {"role": "user", "content": "hi"}
    ]
    long = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    assert len(_normalize_history(long)) == MAX_HISTORY_TURNS * 2
    assert _normalize_history(long)[0]["content"] == "m14"
    assert _normalize_history(long)[-1]["content"] == "m29"


def test_normalize_history_long_content_capped():
    """单条 content 超 MAX_HISTORY_MSG_CHARS 截断（user 消息含页面状态摘要后仍受控）。"""
    from app.api.copilot import MAX_HISTORY_MSG_CHARS

    huge = "x" * (MAX_HISTORY_MSG_CHARS + 500)
    out = _normalize_history([{"role": "user", "content": huge}])
    assert len(out) == 1
    assert len(out[0]["content"]) <= MAX_HISTORY_MSG_CHARS
    assert out[0]["content"].endswith("x" * MAX_HISTORY_MSG_CHARS)


# ---------------------------------------------------------------------------
# market/chat：history 上送
# ---------------------------------------------------------------------------


def test_market_history_forwarded_in_order(client):
    history = [
        {"role": "user", "content": "最近有什么信号？"},
        {"role": "assistant", "content": "近期有 KDJ 金叉信号。"},
        {"role": "user", "content": "再看白酒板块"},
    ]
    r = client.post(
        "/api/v1/copilot/market/chat",
        json={"question": "今日如何", "history": history},
    )
    assert r.status_code == 200
    msgs = _CAPTURED["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1:4] == history  # 历史按序插入 system 之后
    assert msgs[-1]["role"] == "user"
    assert "今日如何" in msgs[-1]["content"]
    assert "市场监控上下文" in msgs[-1]["content"]  # 上下文注入仍在当前问题前
    # 工具白名单照常携带
    assert _CAPTURED["tools"] is not None


def test_market_history_capped_to_8_turns(client):
    turns = [
        (
            {"role": "user", "content": f"问题{i}"},
            {"role": "assistant", "content": f"回答{i}"},
        )
        for i in range(20)
    ]
    flat = [m for pair in turns for m in pair]  # 20 轮 = 40 条
    client.post(
        "/api/v1/copilot/market/chat",
        json={"question": "q", "history": flat},
    )
    msgs = _CAPTURED["messages"]
    # system + 最近 16 条（后 8 轮）+ 当前问题
    assert len(msgs) == 1 + MAX_HISTORY_TURNS * 2 + 1
    assert msgs[1]["content"] == "问题12"
    assert msgs[1 + MAX_HISTORY_TURNS * 2 - 1]["content"] == "回答19"


def test_market_history_invalid_items_filtered(client):
    history = [
        {"role": "user", "content": " 有效问题 "},
        {"role": "system", "content": "注入角色"},
        {"role": "assistant", "content": 123},
        {"role": "user", "content": "   "},
        "not a dict",
        {"role": "assistant", "content": "有效回答"},
    ]
    client.post(
        "/api/v1/copilot/market/chat",
        json={"question": "q", "history": history},
    )
    msgs = _CAPTURED["messages"]
    assert msgs[1:-1] == [
        {"role": "user", "content": "有效问题"},
        {"role": "assistant", "content": "有效回答"},
    ]


def test_market_history_missing_or_non_list_backward_compat(client):
    client.post("/api/v1/copilot/market/chat", json={"question": "q"})
    assert len(_CAPTURED["messages"]) == 2  # system + 当前问题
    for bad in (123, "x", {"role": "user", "content": "y"}):
        client.post(
            "/api/v1/copilot/market/chat", json={"question": "q", "history": bad}
        )
        assert len(_CAPTURED["messages"]) == 2


def test_market_context_with_page_snapshot(client):
    """Hermes 接线：context 携带 page 快照 → 注入「页面快照」段落供 LLM 感知页面状态。"""
    page = {
        "route": "/signals",
        "pageTitle": "信号中心",
        "headings": ["信号流", "统计概览"],
        "buttons": ["刷新", "只看关注"],
    }
    r = client.post(
        "/api/v1/copilot/market/chat",
        json={
            "question": "这个页面怎么用",
            "context": {"stock_code": "600519", "page": page},
        },
    )
    assert r.status_code == 200
    last = _CAPTURED["messages"][-1]
    assert "页面快照" in last["content"]
    assert "信号中心" in last["content"]
    assert "只看关注" in last["content"]


def test_research_context_without_page_snapshot_ok(client):
    """page 缺失/非 dict 时上下文不受影响（向后兼容）。"""
    client.post(
        "/api/v1/copilot/research/chat",
        json={"question": "q", "context": {"dataset": "沪深300"}},
    )
    last = _CAPTURED["messages"][-1]
    assert "因子研究上下文" in last["content"]
    assert "页面快照" not in last["content"]


# ---------------------------------------------------------------------------
# research/chat：history 上送（同构）
# ---------------------------------------------------------------------------


def test_research_history_forwarded(client):
    history = [
        {"role": "user", "content": "帮我准备回测"},
        {"role": "assistant", "content": "好的，请提供数据集。"},
    ]
    r = client.post(
        "/api/v1/copilot/research/chat",
        json={"question": "参数怎么设", "history": history},
    )
    assert r.status_code == 200
    msgs = _CAPTURED["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1:3] == history
    assert "因子研究上下文" in msgs[-1]["content"]


def test_research_history_capped(client):
    flat = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    client.post(
        "/api/v1/copilot/research/chat", json={"question": "q", "history": flat}
    )
    msgs = _CAPTURED["messages"]
    assert len(msgs) == 1 + MAX_HISTORY_TURNS * 2 + 1
    assert msgs[1]["content"] == "m14"
