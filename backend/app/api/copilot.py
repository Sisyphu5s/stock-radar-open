"""/api/v1/copilot/market/* 与 /api/v1/copilot/research/* : 双上下文 LLM；
另含 /api/v1/copilot/config（模型配置）与 /api/v1/copilot/commands/parse（筛选意图解析）。"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core.copilot import (
    FILTER_TOOL,
    JOBS_STATUS_TOOL,
    NAVIGATE_TOOL,
    SYSTEM_MARKET,
    SYSTEM_RESEARCH,
    build_market_context,
    build_research_context,
    parse_signal_command,
    stream_chat,
)

router = APIRouter(prefix="/copilot", tags=["copilot"])


# ===== 响应模型（P2-29 契约硬化：逐路径 response_model） =====


class CopilotConfigResponse(BaseModel):
    """GET /copilot/config：LLM 连接配置（只读展示）。"""

    base_url: str
    model: str
    configured: bool


class CommandFiltersResponse(BaseModel):
    """筛选命令参数（与 core/copilot/commands.CommandFilters 对齐，白名单结构）。"""

    period: str
    time_range: str
    exclude_today: bool
    sectors: list[str]
    signal_types: list[str]
    signal_match: str
    watchlist_only: bool


class CommandParseResponse(BaseModel):
    """POST /copilot/commands/parse：命令 / 普通问答（command=null）/ 解析失败（error）。

    三形态共用（command/mode/filters 与 error 互斥出现）；路由以
    response_model_exclude_unset 保持「未出现字段不输出」的既有字节契约。
    """

    command: Optional[str] = None
    mode: Optional[str] = None
    filters: Optional[CommandFiltersResponse] = None
    error: Optional[str] = None


# 多轮历史上限：上送会话内最近 N 轮（每轮最多 2 条 user/assistant 消息）
MAX_HISTORY_TURNS = 8
# 单条历史消息内容限长：user 消息含页面状态摘要（T-93 新增），超限截断防异常超大请求
MAX_HISTORY_MSG_CHARS = 2000


def _normalize_history(raw: object) -> list[dict]:
    """校验并裁剪多轮历史：仅接受 user/assistant 纯文本消息，保留最近 MAX_HISTORY_TURNS 轮。

    - 非 list / 缺失 → 空列表（向后兼容：旧请求不带上送历史）
    - 非法项（非 dict / 非法 role / 非字符串 content / 空白 content）→ 丢弃
    - 单条 content 超 MAX_HISTORY_MSG_CHARS 截断（防前端异常超大请求）
    """
    if not isinstance(raw, list):
        return []
    turns: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()[:MAX_HISTORY_MSG_CHARS]
        if not content:
            continue
        turns.append({"role": role, "content": content})
    return turns[-MAX_HISTORY_TURNS * 2 :]


def _stream(messages: list[dict], tools: list | None = None):
    async def gen():
        async for chunk in stream_chat(messages, tools=tools):
            if chunk.get("kind") == "action":
                yield f"data: {json.dumps({'action': chunk['action']}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps({'content': chunk.get('text', '')}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.post("/market/chat")
async def market_chat(payload: dict):
    """市场监控上下文对话。payload: {question, context: {...}, history?: [{role, content}, ...]}
    携带白名单工具：筛选命令 apply_signal_center_filters（action 事件）、个股跳转 stock.navigate
    （action 事件）；
    LLM 在流内直接理解意图并产出 action 事件（前端无需再预解析），普通问答不调用工具。
    history 为会话内最近 N 轮对话（可缺省），按序插入 system 之后、当前问题之前，
    供 LLM 追问答上下文（服务端再次裁剪上限，防前端异常超大请求）。"""
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(400, "问题不能为空")
    question = question.strip()
    context = payload.get("context") or {}
    if not isinstance(context, dict):
        raise HTTPException(400, "context 必须为 dict")
    # fundamental 非 dict 时忽略（服务层 build_market_context 假定 dict，
    # 否则 f.get() 抛 AttributeError → 500）
    if not isinstance(context.get("fundamental"), dict):
        context.pop("fundamental", None)
    history = _normalize_history(payload.get("history"))
    ctx = build_market_context(context)
    messages = [
        {"role": "system", "content": SYSTEM_MARKET},
        *history,
        {"role": "user", "content": f"{ctx}\n\n用户问题：{question}"},
    ]
    return _stream(messages, tools=[FILTER_TOOL, NAVIGATE_TOOL])


@router.post("/research/chat")
async def research_chat(payload: dict):
    """因子研究上下文对话。payload: {question, context: {...}, history?: [{role, content}, ...]}
    携带只读工具 jobs.status（任务状态，服务端查询后注入文本流末尾）。"""
    question = payload.get("question", "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    history = _normalize_history(payload.get("history"))
    ctx = build_research_context(payload.get("context", {}))
    messages = [
        {"role": "system", "content": SYSTEM_RESEARCH},
        *history,
        {"role": "user", "content": f"{ctx}\n\n用户问题：{question}"},
    ]
    return _stream(messages, tools=[JOBS_STATUS_TOOL])


@router.get("/config", response_model=CopilotConfigResponse)
def copilot_config():
    from ..config import settings

    return {
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "configured": bool(settings.llm_api_key),
    }


@router.post(
    "/commands/parse",
    response_model=CommandParseResponse,
    response_model_exclude_unset=True,
)
async def copilot_parse_command(payload: dict):
    """把筛选意图解析为安全的白名单命令 `signal_center.apply_filters`。

    payload: {question, context?: {...}}
    - 普通问答 → {"command": null}，不调用解析模型。
    - 稳定规则优先；规则不完整才调用 OpenAI 兼容模型做非流式 JSON 解析。
    - 返回的是纯数据命令（extra=forbid、枚举/长度/信号白名单校验），
      绝不包含任意 URL/JS/HTTP/系统命令，也不在服务端执行任何东西。
    """
    question = payload.get("question", "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    context = payload.get("context")
    if not isinstance(context, dict):
        context = {}
    return await parse_signal_command(question, context)
