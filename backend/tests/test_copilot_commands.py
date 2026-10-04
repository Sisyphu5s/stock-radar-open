"""Copilot 安全命令解析测试：`signal_center.apply_filters` 白名单命令。

覆盖：示例句规则解析（排除今天 + kdj/macd 金叉共振）、默认值、基本别名、
普通问答 command=null（且不调用解析模型）、LLM 兜底、注入/额外字段/未知信号拒绝、
LLM 输出严格校验。全程不发起真实 LLM 请求（monkeypatch 解析函数）。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.core.copilot import parse_signal_command
from app.core.copilot.commands import (
    _CMD,
    CommandFilters,
    FilterCommand,
    _parse_llm_json,
    _rules_parse,
    _normalize,
)


def _run(q, ctx=None):
    return asyncio.run(parse_signal_command(q, ctx or {}))


def _noop_llm(**kwargs):
    """默认替换：一旦被调用即测试失败（普通问答/规则完整时不应触碰解析模型）。"""
    raise AssertionError("不应调用解析模型")


# ---------------------------------------------------------------------------
# 稳定规则：示例句
# ---------------------------------------------------------------------------


def test_example_sentence_parses_via_rules(monkeypatch):
    """必须可靠识别示例：排除今天 + kdj 与 macd 金叉共振。"""
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    r = _run("帮我筛选出排除今天，出现kdj和macd金叉共振的股票")
    assert r["command"] == _CMD
    assert r["mode"] == "replace"
    f = r["filters"]
    assert f["exclude_today"] is True
    assert f["signal_types"] == ["kdj_golden_cross", "macd_golden_cross"]
    assert f["signal_match"] == "all"
    assert f["time_range"] == "3d"
    assert f["period"] == "daily"
    assert f["sectors"] == []
    assert f["watchlist_only"] is False


def test_example_uppercase_and_concurrence_variants(monkeypatch):
    """中文大小写不敏感（MACD/KDJ）；「共振/同时出现」→ all。"""
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    r = _run("筛选MACD金叉和KDJ金叉共振的股票")
    assert r["command"] == _CMD
    assert r["filters"]["signal_types"] == ["macd_golden_cross", "kdj_golden_cross"]
    assert r["filters"]["signal_match"] == "all"

    r2 = _run("筛选出现macd金叉和kdj金叉同时出现的股票")
    assert r2["filters"]["signal_types"] == ["macd_golden_cross", "kdj_golden_cross"]
    assert r2["filters"]["signal_match"] == "all"


# ---------------------------------------------------------------------------
# 稳定规则：默认值与基本别名
# ---------------------------------------------------------------------------


def test_simple_filter_defaults(monkeypatch):
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    r = _run("帮我筛选出现macd金叉的股票")
    f = r["filters"]
    assert f["period"] == "daily"
    assert f["time_range"] == "3d"
    assert f["exclude_today"] is False
    assert f["signal_types"] == ["macd_golden_cross"]
    assert f["signal_match"] == "any"
    assert f["watchlist_only"] is False


def test_time_range_aliases(monkeypatch):
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    assert _run("帮我筛选出过去7天出现kdj金叉的股票")["filters"]["time_range"] == "7d"
    assert _run("帮我筛选出近一周出现kdj金叉的股票")["filters"]["time_range"] == "7d"
    assert _run("帮我筛选出全部时间出现kdj金叉的股票")["filters"]["time_range"] == "all"
    assert _run("帮我筛选出近30天出现kdj金叉的股票")["filters"]["time_range"] == "30d"
    assert _run("筛选今天出现kdj金叉的股票")["filters"]["time_range"] == "today"
    # 排除今天与「今天」同现时，不误判 time_range=today
    assert _run("筛选出排除今天出现kdj金叉的股票")["filters"]["time_range"] == "3d"


def test_time_range_week_month_span_fix(monkeypatch):
    """T-104 P2-54：周/月单位不再恒映射 3d/30d，2周/2月/3月各不相同且语义正确。"""
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    assert _run("帮我筛选出近1周出现kdj金叉的股票")["filters"]["time_range"] == "7d"
    assert _run("帮我筛选出近2周出现kdj金叉的股票")["filters"]["time_range"] == "14d"
    assert _run("帮我筛选出近4周出现kdj金叉的股票")["filters"]["time_range"] == "30d"
    assert _run("帮我筛选出近1月出现kdj金叉的股票")["filters"]["time_range"] == "30d"
    assert _run("帮我筛选出近2月出现kdj金叉的股票")["filters"]["time_range"] == "60d"
    assert _run("帮我筛选出近3月出现kdj金叉的股票")["filters"]["time_range"] == "90d"


def test_exclude_today_keeps_explicit_span(monkeypatch):
    """exclude_today 不得短路显式时间跨度（P1-24 修复）：排除今天 + 最近7天 → 7d。"""
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    r = _run("帮我筛选出排除今天最近7天出现kdj金叉的股票")
    assert r["command"] == _CMD
    f = r["filters"]
    assert f["exclude_today"] is True
    assert f["time_range"] == "7d"

    r2 = _run("帮我筛选出排除今天近一周出现kdj金叉的股票")
    assert r2["filters"]["time_range"] == "7d"

    r3 = _run("帮我筛选出排除今天全部时间出现kdj金叉的股票")
    assert r3["filters"]["time_range"] == "all"


def test_watchlist_only_alias(monkeypatch):
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    r = _run("帮我筛选仅关注的自选股")
    assert r["command"] == _CMD
    assert r["filters"]["watchlist_only"] is True

    r2 = _run("帮我筛选出自选股中出现macd金叉的股票")
    assert r2["filters"]["watchlist_only"] is True
    assert r2["filters"]["signal_types"] == ["macd_golden_cross"]


def test_period_and_sector_aliases(monkeypatch):
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    assert _run("帮我筛选周线出现macd金叉的股票")["filters"]["period"] == "weekly"
    r = _run("帮我筛选出科创板出现kdj金叉的股票")
    assert r["filters"]["sectors"] == ["科创板"]
    assert r["filters"]["signal_types"] == ["kdj_golden_cross"]


# ---------------------------------------------------------------------------
# 普通问答：command=null，且绝不调用解析模型
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "KDJ金叉是什么意思",
        "帮我分析一下macd金叉",
        "什么是均线多头排列",
        "今天股市怎么样",
        "600519的走势如何",
    ],
)
def test_plain_qa_returns_null_without_llm(q, monkeypatch):
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)

    r = _run(q)
    assert r == {"command": None}


# ---------------------------------------------------------------------------
# 规则完整时直接返回，不依赖 LLM
# ---------------------------------------------------------------------------


def test_complete_rules_do_not_call_llm(monkeypatch):
    from app.core.copilot import commands as C

    called = []

    async def _spy(question, context, hint=None):
        called.append(question)
        return None, "不应走到 LLM"

    monkeypatch.setattr(C, "_llm_parse_filters", _spy)
    r = _run("帮我筛选出排除今天，出现kdj和macd金叉共振的股票")
    assert r["command"] == _CMD
    assert called == []


# ---------------------------------------------------------------------------
# LLM 兜底：规则不完整但确为筛选意图
# ---------------------------------------------------------------------------


def test_llm_fallback_when_rules_incomplete(monkeypatch):
    """示例：规则识别出排除今天，但信号名未知 → 交给解析模型。"""
    from app.core.copilot import commands as C

    seen_hint = {}

    async def _fake_llm(question, context, hint=None):
        seen_hint.update(hint or {})
        return FilterCommand(
            command=_CMD,
            mode="replace",
            filters=CommandFilters(
                exclude_today=True,
                signal_types=["kdj_golden_cross", "macd_golden_cross"],
                signal_match="all",
            ),
        ), None

    monkeypatch.setattr(C, "_llm_parse_filters", _fake_llm)
    r = _run("帮我筛选出排除今天出现神秘金叉共振的股票")
    assert r["command"] == _CMD
    assert r["filters"]["exclude_today"] is True
    assert r["filters"]["signal_types"] == ["kdj_golden_cross", "macd_golden_cross"]
    assert r["filters"]["signal_match"] == "all"
    # 规则初筛结果作为 hint 传给模型
    assert seen_hint.get("exclude_today") is True


def test_partial_signal_resolution_still_falls_back_to_llm(monkeypatch):
    """已解析出任一信号后，其余未解析信号表述仍触发 LLM 兜底（P1-24 修复：
    修复前 resolved 非空即短路返回，未解析的“死叉”被丢弃）。"""
    from app.core.copilot import commands as C

    seen_hint = {}

    async def _fake_llm(question, context, hint=None):
        seen_hint.update(hint or {})
        return FilterCommand(
            command=_CMD,
            mode="replace",
            filters=CommandFilters(
                signal_types=["kdj_golden_cross", "kdj_dead_cross"],
                signal_match="any",
            ),
        ), None

    monkeypatch.setattr(C, "_llm_parse_filters", _fake_llm)
    r = _run("帮我筛选出出现kdj金叉或神秘死叉的股票")
    assert r["command"] == _CMD
    # 规则已解析部分作为 hint 传给模型
    assert seen_hint.get("signal_types") == ["kdj_golden_cross"]
    assert set(r["filters"]["signal_types"]) == {"kdj_golden_cross", "kdj_dead_cross"}


def test_fully_resolved_multiple_signals_no_llm(monkeypatch):
    """多信号全部解析 → 不再触发 LLM 兜底（部分解析修复不得引入全解析误判）。"""
    from app.core.copilot import commands as C

    called = []

    async def _spy(question, context, hint=None):
        called.append(question)
        return None, "不应走到 LLM"

    monkeypatch.setattr(C, "_llm_parse_filters", _spy)
    r = _run("帮我筛选出出现kdj金叉和macd金叉共振的股票")
    assert r["command"] == _CMD
    assert called == []


def test_llm_fallback_failure_returns_null_with_error(monkeypatch):
    from app.core.copilot import commands as C

    async def _fail(question, context, hint=None):
        return None, "解析模型连接失败（timeout）"

    monkeypatch.setattr(C, "_llm_parse_filters", _fail)
    r = _run("帮我筛选出出现神秘信号共振的股票")
    assert r["command"] is None
    assert "失败" in r["error"]


def test_llm_fallback_empty_result_rejected(monkeypatch):
    """LLM 返回无实质筛选条件的空壳命令 → command null（不做无意义筛选）。"""
    from app.core.copilot import commands as C

    async def _empty(question, context, hint=None):
        return FilterCommand(
            command=_CMD, mode="replace", filters=CommandFilters()
        ), None

    monkeypatch.setattr(C, "_llm_parse_filters", _empty)
    r = _run("帮我筛选出出现神秘信号的股票")
    assert r["command"] is None
    assert "error" in r


# ---------------------------------------------------------------------------
# LLM 输出严格校验：注入 / 额外字段 / 未知信号 / 非法枚举
# ---------------------------------------------------------------------------


def test_llm_json_rejects_non_whitelist_command():
    assert _parse_llm_json('{"command": "shell.exec", "filters": {}}') is None
    assert _parse_llm_json('{"command": "fetch(url)", "filters": {}}') is None
    assert _parse_llm_json('{"command": "eval(js)", "filters": {}}') is None


def test_llm_json_rejects_extra_fields():
    assert (
        _parse_llm_json(
            '{"filters": {"signal_types": ["macd_golden_cross"], "evil_field": 1}}'
        )
        is None
    )
    assert (
        _parse_llm_json(
            '{"command": "signal_center.apply_filters", "filters": {"signal_types": ["macd_golden_cross"]}, "pwn": true}'
        )
        is None
    )
    # 注入命令字段混在 filters 里同样被 extra=forbid 拒绝
    assert (
        _parse_llm_json(
            '{"filters": {"signal_types": ["macd_golden_cross"], "command": "rm -rf"}}'
        )
        is None
    )


def test_llm_json_rejects_unknown_signal_and_sector():
    assert (
        _parse_llm_json(
            '{"filters": {"signal_types": ["macd_golden_cross", "not_a_signal"]}}'
        )
        is None
    )
    assert (
        _parse_llm_json(
            '{"filters": {"signal_types": ["javascript://x"], "sectors": ["黑客板块"]}}'
        )
        is None
    )


def test_llm_json_rejects_invalid_enum_and_values():
    assert _parse_llm_json('{"filters": {"signal_match": "sometimes"}}') is None
    assert _parse_llm_json('{"filters": {"period": "millennium"}}') is None
    assert _parse_llm_json('{"filters": {"time_range": "forever"}}') is None
    # 已删除字段（min_score 随共振得分移除）经 extra=forbid 拒绝，防 LLM 注入
    assert _parse_llm_json('{"filters": {"min_score": 50}}') is None


def test_llm_json_accepts_wrapped_or_partial_output():
    ok = _parse_llm_json(
        "好的，筛选结果如下：\n"
        '{"command": "signal_center.apply_filters", "mode": "replace", '
        '"filters": {"signal_types": ["macd_golden_cross"], "signal_match": "all"}}'
    )
    assert ok is not None and ok.filters.signal_types == ["macd_golden_cross"]
    assert ok.filters.signal_match == "all"

    ok2 = _parse_llm_json(
        '```json\n{"filters": {"signal_types": ["kdj_golden_cross"]}}\n```'
    )
    assert ok2 is not None and ok2.filters.signal_types == ["kdj_golden_cross"]

    ok3 = _parse_llm_json("not json at all")
    assert ok3 is None


def test_filter_command_model_rejects_unknown_signal_and_extra_field():
    """Pydantic 模型本身即安全边界：信号必须来自 SIGNAL_TYPES。"""
    with pytest.raises(ValidationError):
        CommandFilters(signal_types=["macd_golden_cross", "bogus_signal"])
    with pytest.raises(ValidationError):
        FilterCommand.model_validate(
            {
                "command": "signal_center.apply_filters",
                "mode": "replace",
                "filters": {
                    "signal_types": ["macd_golden_cross"],
                    "url": "http://evil",
                },
            }
        )
    with pytest.raises(ValidationError):
        FilterCommand.model_validate(
            {
                "command": "signal_center.apply_filters",
                "mode": "replace",
                "filters": {"signal_types": ["macd_golden_cross"] * 25},
            }
        )  # 长度上限 20


def test_rules_reject_signal_not_in_catalog():
    """规则层不会产出未知信号代码。"""
    cmd = _rules_parse(_normalize("帮我筛选出出现完全不存在信号的股票"))
    assert cmd is None or cmd.filters.signal_types == []


# ---------------------------------------------------------------------------
# 端到端：POST /copilot/commands/parse（独立 mini app，不触碰生产库）
# ---------------------------------------------------------------------------


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.copilot import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_endpoint_parse_returns_command(monkeypatch):
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)
    c = _client()
    resp = c.post(
        "/api/v1/copilot/commands/parse",
        json={"question": "帮我筛选出排除今天，出现kdj和macd金叉共振的股票"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["command"] == _CMD
    assert body["filters"]["exclude_today"] is True
    assert body["filters"]["signal_types"] == ["kdj_golden_cross", "macd_golden_cross"]


def test_endpoint_parse_plain_qa_returns_null(monkeypatch):
    from app.core.copilot import commands as C

    monkeypatch.setattr(C, "_llm_parse_filters", _noop_llm)
    c = _client()
    resp = c.post(
        "/api/v1/copilot/commands/parse", json={"question": "KDJ金叉是什么意思"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"command": None}


def test_endpoint_parse_empty_question_400():
    c = _client()
    resp = c.post("/api/v1/copilot/commands/parse", json={"question": "  "})
    assert resp.status_code == 400
