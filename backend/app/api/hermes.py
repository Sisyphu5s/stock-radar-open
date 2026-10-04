"""/api/v1/copilot/hermes/* : Hermes 沙盒助手（UI 上下文 + LLM）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..core.copilot import stream_chat

router = APIRouter(prefix="/copilot/hermes", tags=["hermes"])


class HermesStatusResponse(BaseModel):
    """GET /copilot/hermes/status：沙盒助手状态（供浮窗显示）。"""

    available: bool
    model: str
    configured: bool

SYSTEM_HERMES = """你是 Hermes，嵌入 Stock Radar 量化工作台的 UI 助手。你运行在独立沙盒中，
能通过页面上下文感知前端各个元素（当前页面、卡片标题、按钮、表格列、关键状态）。
你的职责：
1. 帮助用户理解当前页面：解释界面元素、数据含义、下一步操作
2. 根据页面上下文回答"这个页面怎么用""这个按钮干什么""当前状态是什么"
3. 数据操作建议（筛选、切换周期、运行实验等），但不要编造上下文之外的数据
4. 遇到无法确定的信息，明确说明"上下文未包含该信息"
输出使用中文，简洁、结构化（短段落/列表）。不要虚构指标数值。"""


@router.post("/chat")
async def hermes_chat(payload: dict):
    """Hermes 对话：携带前端页面上下文（路由/元素/状态快照）。"""
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(400, "问题不能为空")
    question = question.strip()
    ctx = payload.get("context", {})
    if not isinstance(ctx, dict):
        raise HTTPException(400, "context 必须为 dict")
    lines = ["# 前端页面上下文（由主窗口收集）"]
    # 字段统一限长：route/pageTitle 为字符串字段，无界拼接会撑爆 LLM prompt
    # （恶意或异常前端可传任意长度值）；结构字段已有 json.dumps 切片截断。
    lines.append(
        "- 路由: {}  标题: {}".format(
            str(ctx.get("route", ""))[:200], str(ctx.get("pageTitle", ""))[:200]
        )
    )
    lines.append(
        f"- 关键状态: {json.dumps(ctx.get('state', {}), ensure_ascii=False)[:400]}"
    )
    if ctx.get("headings"):
        lines.append(
            f"- 页面卡片/区块: {json.dumps(ctx['headings'], ensure_ascii=False)[:300]}"
        )
    if ctx.get("buttons"):
        lines.append(
            f"- 页面按钮: {json.dumps(ctx['buttons'], ensure_ascii=False)[:300]}"
        )
    if ctx.get("tables"):
        lines.append(f"- 表格列: {json.dumps(ctx['tables'], ensure_ascii=False)[:300]}")
    if ctx.get("highlights"):
        lines.append(
            f"- 关键数据: {json.dumps(ctx['highlights'], ensure_ascii=False)[:300]}"
        )
    context_text = "\n".join(lines)[:8000]  # 整体限长：多字段叠加后不再膨胀

    messages = [
        {"role": "system", "content": SYSTEM_HERMES},
        {"role": "user", "content": f"{context_text}\n\n用户问题：{question}"},
    ]

    async def gen():
        async for chunk in stream_chat(messages):
            # stream_chat 现为 dict 契约：Hermes 不携带工具，只消费文本事件
            if chunk.get("kind") != "text":
                continue
            yield f"data: {json.dumps({'content': chunk.get('text', '')}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.get("/status", response_model=HermesStatusResponse)
def hermes_status():
    """沙盒助手状态（供浮窗显示）。"""
    return {
        "available": True,
        "model": settings.llm_model,
        "configured": bool(settings.llm_api_key),
    }
