"""Copilot 工具调用（apply_signal_center_filters / stock.navigate / 只读查询）流式解析测试。

覆盖（全程不发起真实网络请求，monkeypatch httpx.AsyncClient）：
- 合法参数（含 arguments 分片累积）→ 产出 action 事件，filters 等价
  「排除今天 + KDJ/MACD 金叉全部命中」（exclude_today=true、signal_types 两枚、signal_match=all）
- 非法/注入参数（未知信号、extra 字段、畸形 JSON、空参数）→ 不产出 action（仅保留文本流）
- 非白名单工具名 → 忽略不产出 action
- 无工具调用 → 纯文本事件；未传 tools → payload 不含 tools
- stock.navigate：合法 code → action 事件；非法 code（注入路径）→ 忽略
- 只读工具 jobs.status → 服务端执行查询，结果注入文本流末尾
- payload 携带 max_tokens；超长文本流被服务端截断
- LLM 未配置（SR_LLM_API_KEY 为空）→ 行为不变（仅未配置提示文本，无 action）
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.core.copilot import (
    FILTER_TOOL,
    MAX_RESPONSE_CHARS,
    MAX_TOKENS,
    stream_chat,
)

_CAPTURED: dict = {}
_FAKE_LINES: list[str] = []


class _FakeResp:
    def __init__(self, lines: list[str]):
        self.status_code = 200
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeStream:
    async def __aenter__(self):
        return _FakeResp(list(_FAKE_LINES))

    async def __aexit__(self, *_a):
        return None


class _FakeClient:
    """httpx.AsyncClient 替身：记录请求 payload，按模块级预设行流式返回。"""

    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    def stream(self, method, url, json=None, headers=None):
        _CAPTURED["payload"] = json or {}
        return _FakeStream()


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    _CAPTURED.clear()
    _FAKE_LINES.clear()


def _events(messages: list[dict], tools: list | None = None) -> list[dict]:
    out: list[dict] = []

    async def _run():
        async for ev in stream_chat(messages, tools=tools):
            out.append(ev)

    asyncio.run(_run())
    return out


def _delta(payload: dict) -> str:
    return (
        f"data: {json.dumps({'choices': [{'delta': payload}]}, ensure_ascii=False)}\n"
    )


def _tool_call_lines(
    name: str, args_fragments: list[str], call_id: str = "call_1"
) -> list[str]:
    """构造一次完整工具调用的 SSE 序列：首个 delta 带 id/name，arguments 按分片下发。"""
    lines = [
        _delta(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": ""},
                    }
                ]
            }
        ),
    ]
    for frag in args_fragments:
        lines.append(
            _delta({"tool_calls": [{"index": 0, "function": {"arguments": frag}}]})
        )
    lines.append("data: [DONE]\n")
    return lines


def _only_actions(evs: list[dict]) -> list[dict]:
    return [e for e in evs if e.get("kind") == "action"]


def _text_of(evs: list[dict]) -> str:
    return "".join(e.get("text", "") for e in evs if e.get("kind") == "text")


# ---------------------------------------------------------------------------
# 合法工具调用：产出 action 事件
# ---------------------------------------------------------------------------


def test_valid_action_emitted_with_fragmented_arguments():
    args = json.dumps(
        {
            "exclude_today": True,
            "signal_types": ["kdj_golden_cross", "macd_golden_cross"],
            "signal_match": "all",
        },
        ensure_ascii=False,
    )
    cut = len(args) // 2  # arguments 拆成两片（含中途截断），验证按 index 累积
    _FAKE_LINES[:] = [
        _delta({"content": "好的，正在筛选。"}),
    ] + _tool_call_lines("apply_signal_center_filters", [args[:cut], args[cut:]])

    evs = _events(
        [{"role": "user", "content": "排除今天，kdj和macd金叉共振"}],
        tools=[FILTER_TOOL],
    )

    actions = _only_actions(evs)
    assert len(actions) == 1
    act = actions[0]["action"]
    assert act["command"] == "signal_center.apply_filters"
    assert act["mode"] == "replace"
    f = act["filters"]
    assert f["exclude_today"] is True
    assert f["signal_types"] == ["kdj_golden_cross", "macd_golden_cross"]
    assert f["signal_match"] == "all"
    # 文本流照常保留，且 action 事件排在文本之后
    assert _text_of(evs) == "好的，正在筛选。"
    assert evs[-1]["kind"] == "action"
    # 请求体携带 tools（单一白名单工具）
    assert _CAPTURED["payload"].get("tools") == [FILTER_TOOL]


def test_valid_action_defaults_filled():
    """未提及字段由模型默认值补齐（replace 语义要求全字段）。"""
    _FAKE_LINES[:] = _tool_call_lines(
        "apply_signal_center_filters",
        ['{"signal_types": ["macd_golden_cross"], "signal_match": "all"}'],
    )
    evs = _events(
        [{"role": "user", "content": "筛选macd金叉共振"}], tools=[FILTER_TOOL]
    )
    act = _only_actions(evs)[0]["action"]
    assert act["filters"]["exclude_today"] is False
    assert act["filters"]["time_range"] == "3d"
    assert act["filters"]["period"] == "daily"
    assert act["filters"]["sectors"] == []
    assert act["filters"]["watchlist_only"] is False


# ---------------------------------------------------------------------------
# 非法/注入参数：不产出 action，仅保留文本流
# ---------------------------------------------------------------------------


def test_invalid_unknown_signal_no_action():
    _FAKE_LINES[:] = [
        _delta({"content": "我先尝试筛选。"}),
    ] + _tool_call_lines(
        "apply_signal_center_filters",
        ['{"signal_types": ["macd_golden_cross", "not_a_signal"]}'],
    )
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert _only_actions(evs) == []
    assert _text_of(evs) == "我先尝试筛选。"  # 文本流不受影响


def test_invalid_extra_field_no_action():
    _FAKE_LINES[:] = _tool_call_lines(
        "apply_signal_center_filters",
        [
            '{"signal_types": ["macd_golden_cross"], "evil_field": 1, "command": "rm -rf"}'
        ],
    )
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert _only_actions(evs) == []


def test_malformed_json_arguments_no_action():
    _FAKE_LINES[:] = _tool_call_lines(
        "apply_signal_center_filters", ["{not json", " at all}"]
    )
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert _only_actions(evs) == []


def test_empty_arguments_no_action():
    _FAKE_LINES[:] = [
        _delta(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "apply_signal_center_filters",
                            "arguments": "",
                        },
                    }
                ]
            }
        ),
        "data: [DONE]\n",
    ]
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert _only_actions(evs) == []


def test_empty_arguments_yields_unfinished_notice(caplog):
    """T-72 兜底：模型仅返回工具名、未提供 arguments 时，
    不静默丢弃——流末追加显式提示行，并用 logger.warning 记录（含模型名/工具名）。"""
    _FAKE_LINES[:] = [
        _delta(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "apply_signal_center_filters",
                            "arguments": "",
                        },
                    }
                ]
            }
        ),
        "data: [DONE]\n",
    ]
    with caplog.at_level("WARNING", logger="stockradar.copilot"):
        evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    # 仍不产出 action（非法调用无副作用），但文本流末尾有显式提示
    assert _only_actions(evs) == []
    text = _text_of(evs)
    assert "⚠ 工具调用未完成" in text
    # 日志记录含模型名与工具名
    assert any(
        "工具调用未完成" in r.getMessage()
        and settings.llm_model in r.getMessage()
        and "apply_signal_center_filters" in r.getMessage()
        for r in caplog.records
    )


def test_malformed_arguments_yields_unfinished_notice():
    """T-72：arguments 分片存在但非法（畸形 JSON/非对象）同样落入兜底提示。"""
    _FAKE_LINES[:] = [
        _delta({"content": "处理中…"}),
    ] + _tool_call_lines("apply_signal_center_filters", ["{not json", " at all}"])
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert _only_actions(evs) == []
    assert "⚠ 工具调用未完成" in _text_of(evs)


def test_non_whitelist_tool_name_ignored():
    _FAKE_LINES[:] = _tool_call_lines("evil_tool", ['{"x": 1}'])
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert _only_actions(evs) == []


# ---------------------------------------------------------------------------
# 无工具调用 / 未传 tools / LLM 未配置
# ---------------------------------------------------------------------------


def test_no_tool_call_pure_text_events():
    _FAKE_LINES[:] = [
        _delta({"content": "第一段"}),
        _delta({"content": "第二段"}),
        "data: [DONE]\n",
    ]
    evs = _events(
        [{"role": "user", "content": "KDJ金叉是什么意思"}], tools=[FILTER_TOOL]
    )
    assert evs == [
        {"kind": "text", "text": "第一段"},
        {"kind": "text", "text": "第二段"},
    ]


def test_no_tools_in_payload_when_omitted():
    _FAKE_LINES[:] = [_delta({"content": "hi"}), "data: [DONE]\n"]
    evs = _events([{"role": "user", "content": "hi"}])  # tools=None（研究/hermes 会话）
    assert "tools" not in _CAPTURED["payload"]
    assert evs == [{"kind": "text", "text": "hi"}]


def test_llm_not_configured_behavior_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    evs = _events([{"role": "user", "content": "筛选kdj金叉"}], tools=[FILTER_TOOL])
    assert len(evs) == 1
    assert evs[0]["kind"] == "text"
    assert "[LLM 未配置]" in evs[0]["text"]
    assert _only_actions(evs) == []


def test_http_error_yields_text_only(monkeypatch):
    class _ErrResp(_FakeResp):
        def __init__(self):
            self.status_code = 401
            self._body = b"unauthorized"

        async def aread(self):
            return self._body

    class _ErrStream(_FakeStream):
        async def __aenter__(self):
            return _ErrResp()

    class _ErrClient(_FakeClient):
        def stream(self, method, url, json=None, headers=None):
            _CAPTURED["payload"] = json or {}
            return _ErrStream()

    monkeypatch.setattr(httpx, "AsyncClient", _ErrClient)
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert len(evs) == 1
    assert evs[0]["kind"] == "text"
    assert "LLM 错误 401" in evs[0]["text"]


# ---------------------------------------------------------------------------
# stock.navigate：合法 code 产出 action，非法/注入 code 忽略
# ---------------------------------------------------------------------------


def test_stock_navigate_action_emitted():
    _FAKE_LINES[:] = _tool_call_lines("stock.navigate", ['{"code": "600519"}'])
    evs = _events(
        [{"role": "user", "content": "帮我看看贵州茅台"}], tools=[FILTER_TOOL]
    )
    actions = _only_actions(evs)
    assert len(actions) == 1
    assert actions[0]["action"] == {"command": "stock.navigate", "code": "600519"}


def test_stock_navigate_code_with_suffix():
    _FAKE_LINES[:] = _tool_call_lines("stock.navigate", ['{"code": "000001.SZ"}'])
    evs = _events([{"role": "user", "content": "打开平安银行"}], tools=[FILTER_TOOL])
    actions = _only_actions(evs)
    assert len(actions) == 1
    assert actions[0]["action"]["code"] == "000001.SZ"


def test_stock_navigate_invalid_code_ignored():
    for bad in ("../../etc/passwd", "600519;rm -rf", "x" * 20, ""):
        _FAKE_LINES[:] = _tool_call_lines("stock.navigate", [json.dumps({"code": bad})])
        evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
        assert _only_actions(evs) == [], f"非法 code {bad!r} 不应产出 action"


def test_stock_navigate_missing_code_ignored():
    _FAKE_LINES[:] = _tool_call_lines("stock.navigate", ["{}"])
    evs = _events([{"role": "user", "content": "x"}], tools=[FILTER_TOOL])
    assert _only_actions(evs) == []


# ---------------------------------------------------------------------------
# 只读查询工具：服务端执行并把结果注入文本流末尾
# ---------------------------------------------------------------------------


def test_readonly_jobs_status_injects_text():
    _FAKE_LINES[:] = _tool_call_lines("jobs.status", ['{"limit": 3}'])
    evs = _events([{"role": "user", "content": "最近任务进度"}], tools=[FILTER_TOOL])
    text = _text_of(evs)
    assert "[任务状态]" in text
    assert _only_actions(evs) == []


# ---------------------------------------------------------------------------
# max_tokens + 服务端字符截断
# ---------------------------------------------------------------------------


def test_payload_includes_max_tokens():
    _FAKE_LINES[:] = [_delta({"content": "hi"}), "data: [DONE]\n"]
    _events([{"role": "user", "content": "hi"}], tools=[FILTER_TOOL])
    assert _CAPTURED["payload"]["max_tokens"] == MAX_TOKENS


def test_response_char_truncation_guard():
    """模型异常超长输出：累积文本超 MAX_RESPONSE_CHARS 即截断停发，不无限烧 token。"""
    _FAKE_LINES[:] = [
        _delta({"content": "x" * 500})
        for _ in range(20)  # 10_000 字符 > MAX_RESPONSE_CHARS
    ] + ["data: [DONE]\n"]
    evs = _events([{"role": "user", "content": "写一篇长文"}], tools=[FILTER_TOOL])
    text = _text_of(evs)
    assert len(text) <= MAX_RESPONSE_CHARS
    assert "已截断" in text
