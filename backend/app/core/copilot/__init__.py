"""LLM Copilot：市场/研究双上下文构建 + OpenAI 兼容流式对话。"""

from __future__ import annotations

import json
import logging
import re

import httpx

from ...config import settings
from .commands import CommandFilters, FilterCommand, parse_signal_command  # noqa: F401

logger = logging.getLogger("stockradar.copilot")

# 模型单次回复最大 token 数（payload max_tokens）：长回复可读性由前端折叠处理，服务端控总量
MAX_TOKENS = 1024
# 服务端字符级保护上限：流式累积文本超限即停发（防模型异常超长输出烧 token / 撑爆前端）
MAX_RESPONSE_CHARS = 4096
# 前端页面快照（Hermes）整体限长：与 api/hermes.py 的 8000 保持一致
SNAPSHOT_MAX_CHARS = 8000
# 个股跳转 code 严格格式：6 位数字，可选交易所后缀（防任意路径/脚本注入导航）
_STOCK_CODE_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$")
# 只读查询型工具白名单：服务端执行查询、结果注入文本流末尾（模型不得编造数据）
_READONLY_TOOLS = ("jobs.status",)

SYSTEM_MARKET = """你是 A 股市场监控助手。你只能基于提供的市场上下文回答，用于盘中快速决策支持。
输出使用中文，简洁、结构化。不要编造上下文之外的数据。

【筛选工具使用规则】当用户要求筛选/筛出/选出/找出股票、设置信号过滤条件
（如 KDJ/MACD 金叉、排除今天/今日、信号共振、板块过滤、只看自选、指定时间范围/周期/最小得分等）时，
必须调用 apply_signal_center_filters 工具完成筛选，并在回复中给出简洁中文说明（说明所应用的筛选条件）。
普通问答（解释概念、分析行情、询问指标含义等）不要调用工具，直接回答。

【个股跳转】当用户要求查看/打开某只具体股票（给出 6 位股票代码）时，调用 stock.navigate 跳转到个股工作台；
code 取用户提到的 6 位代码，不要加任何额外内容。"""

# 单一白名单工具：只允许 LLM 产出 signal_center.apply_filters 筛选命令（参数经 FilterCommand 严格校验）
FILTER_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "apply_signal_center_filters",
        "description": (
            "筛选信号中心股票列表：按板块、信号类型（如 kdj_golden_cross 金叉、"
            "macd_golden_cross 金叉）、触发时间范围、排除当日、只看自选、最小得分等条件过滤。"
            "当用户要求筛选/筛出/找出符合条件的股票或设置信号过滤条件时调用。"
        ),
        "parameters": CommandFilters.model_json_schema(),
    },
}

# 个股跳转工具：LLM 只产出 6 位股票代码，服务端校验格式后由前端按固定路径导航 /stocks/{code}
NAVIGATE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "stock.navigate",
        "description": (
            "跳转到指定个股工作台。当用户要求查看/分析某只具体股票（明确提到 6 位股票代码）时调用；"
            "code 为股票代码（如 600519 或 600519.SH）。只用于跳转，不修改任何数据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "股票代码，6 位数字，可带 .SH/.SZ/.BJ 后缀",
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}

# 只读查询工具：服务端执行真实查询并把结果注入文本流末尾（防模型编造任务数据）
JOBS_STATUS_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "jobs.status",
        "description": (
            "查看最近后台任务状态（因子挖掘/评估/回测/扫描等）。当用户询问任务进度、有哪些任务、"
            "最近任务状态时调用，返回真实任务数据而非猜测。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "最多返回任务数（1-10，缺省 5）",
                }
            },
            "additionalProperties": False,
        },
    },
}

SYSTEM_RESEARCH = """你是量化因子研究助手。你只能基于提供的因子研究上下文回答，帮助研究者分析因子质量与改进方向。
输出使用中文，专业、简洁。可以给出表达式改进建议，但不要编造实验结果。

【任务状态工具规则】当用户询问任务进度/有哪些任务/最近任务状态时，
必须调用 jobs.status 工具获取真实任务数据，并在回复中基于工具结果说明。"""


def _append_page_snapshot(lines: list[str], ctx: dict) -> None:
    """Hermes 页面快照段：ctx['page'] 为前端 DOM 快照（route/pageTitle/headings/buttons）。
    非 dict 忽略；序列化整体限长 SNAPSHOT_MAX_CHARS（与 hermes 端点同限）。"""
    page = ctx.get("page")
    if not isinstance(page, dict):
        return
    try:
        payload = json.dumps(page, ensure_ascii=False)[:SNAPSHOT_MAX_CHARS]
    except (TypeError, ValueError):
        return
    lines.append(f"- 页面快照: {payload}")


def build_market_context(ctx: dict) -> str:
    indicators = ctx.get("indicators")
    if not isinstance(indicators, dict):
        indicators = {}
    kline = ctx.get("kline") or []
    if not isinstance(kline, list):
        kline = []
    lines = [
        "# 市场监控上下文",
        f"- 当前股票: {ctx.get('stock_code')} {ctx.get('stock_name', '')}",
        f"- 行情快照: 现价 {ctx.get('price')} 涨跌幅 {ctx.get('pct_change')}% "
        f"数据源 {ctx.get('data_source', ctx.get('market_status', '未知'))}",
        f"- 市场状态: {ctx.get('market_status', '未知')}",
        f"- K线区间: {ctx.get('kline_range', '')}，最近 {len(kline)} 根"
        f"{ctx.get('period', 'daily')}K",
        f"- 指标最新值: {json.dumps({k: (round(v, 2) if isinstance(v, float) else v) for k, v in indicators.items()}, ensure_ascii=False)}",
    ]
    # 信号与触发证据（结构化，避免模型编造价位/均线细节）
    signals = ctx.get("signals") or []
    if not isinstance(signals, list):
        signals = []
    if signals:
        lines.append(f"- 命中信号: {len(signals)} 条")
        for sg in signals:
            if not isinstance(sg, dict):
                continue
            ev = sg.get("evidence") or {}
            if not isinstance(ev, dict):
                ev = {}
            ev = ev.copy()
            ev.pop("_logic", None)
            detail = (
                "; ".join(f"{k}: {v}" for k, v in ev.items()) if ev else "（无证据）"
            )
            lines.append(f"  · 信号 {sg.get('signals')} → 证据: {detail}")
    if ctx.get("fundamental"):
        f = ctx["fundamental"]
        lines.append(
            f"- 基本面: PE {f.get('pe')} PB {f.get('pb')} 总市值 {f.get('market_cap')}亿"
        )
    if ctx.get("selected_stocks"):
        lines.append(
            f"- 选中股票列表: {json.dumps(ctx['selected_stocks'], ensure_ascii=False)}"
        )
    _append_page_snapshot(lines, ctx)
    return "\n".join(lines)


def build_research_context(ctx: dict) -> str:
    lines = [
        "# 因子研究上下文",
        f"- 数据集: {ctx.get('dataset', '')}，股票数 {ctx.get('stock_count', '?')}",
    ]
    # 空值省略：date_range/features/operators 无实际数据时不上送，避免占位空值污染 prompt
    if ctx.get("date_range"):
        lines.append(f"- 时间范围: {ctx['date_range']}")
    if ctx.get("features"):
        lines.append(f"- 特征字段: {ctx['features']}")
    if ctx.get("operators"):
        lines.append(f"- 算子集合: {ctx['operators']}")
    if ctx.get("expression"):
        lines.append(f"- 当前公式: `{ctx['expression']}`")
    if ctx.get("metrics"):
        lines.append(f"- 评估指标: {json.dumps(ctx['metrics'], ensure_ascii=False)}")
    _append_page_snapshot(lines, ctx)
    return "\n".join(lines)


def _run_readonly_tool(name: str, args: dict) -> str | None:
    """执行白名单只读查询工具（jobs.status），返回结果文本。

    懒导入避免循环依赖；查询失败返回简短错误文本（绝不抛异常、绝不做写操作）。
    """
    if name == "jobs.status":
        try:
            from ...core.tasks.runner import list_jobs

            limit = args.get("limit")
            limit = (
                min(max(int(limit), 1), 10) if isinstance(limit, (int, float)) else 5
            )
            jobs = list_jobs(limit)
            if not jobs:
                return "[任务状态] 暂无后台任务"
            rows = []
            for j in jobs:
                label = j.get("label") or ""
                rows.append(
                    f"- #{j.get('id')} {j.get('job_type') or ''}（{j.get('status') or ''}）{label}".strip()
                )
            return "[任务状态] 最近任务:\n" + "\n".join(rows)
        except Exception as e:
            logger.warning("jobs.status 查询失败: %s", e)
            return "[任务状态] 查询失败"
    return None


async def stream_chat(messages: list[dict], tools: list | None = None):
    """流式调用 OpenAI 兼容 API。

    yield 契约（dict）：
    - {"kind": "text", "text": str}                                  文本增量
    - {"kind": "action", "action": {...}}   完成的动作工具调用（筛选命令 / 个股跳转）

    动作工具白名单：apply_signal_center_filters（FilterCommand 严格校验：extra=forbid/
    枚举/信号白名单，参数非法/注入一律不产出 action，只保留文本流）、stock.navigate
    （code 经 _STOCK_CODE_RE 格式校验）。只读查询工具 jobs.status
    由服务端执行真实查询，结果以文本事件注入流末尾（防模型编造数据）。
    文本流受 max_tokens 与 MAX_RESPONSE_CHARS 双重截断。
    """
    if not settings.llm_api_key:
        yield {
            "kind": "text",
            "text": "[LLM 未配置] 请在系统设置或环境变量 SR_LLM_API_KEY 中配置 OpenAI 兼容 API Key。",
        }
        return
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": True,
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    # index → {id, name, arguments}：跨分片累积 choices[0].delta.tool_calls 增量
    tool_calls: dict[int, dict] = {}
    # 服务端字符级截断：累积文本超 MAX_RESPONSE_CHARS 后停发（防御异常超长输出）
    acc_chars = 0
    text_truncated = False
    trunc_mark = "\n\n（回复已截断）"
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {
                        "kind": "text",
                        "text": f"[LLM 错误 {resp.status_code}] {body.decode(errors='replace')[:200]}",
                    }
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"]
                        text = delta.get("content", "")
                        if text and not text_truncated:
                            acc_chars += len(text)
                            if acc_chars > MAX_RESPONSE_CHARS:
                                text_truncated = True
                                # 截断标记计入上限预留，保证产出总字符 ≤ MAX_RESPONSE_CHARS
                                cut = (
                                    MAX_RESPONSE_CHARS
                                    - (acc_chars - len(text))
                                    - len(trunc_mark)
                                )
                                if cut > 0:
                                    yield {
                                        "kind": "text",
                                        "text": text[:cut] + trunc_mark,
                                    }
                                else:
                                    yield {"kind": "text", "text": trunc_mark}
                            else:
                                yield {"kind": "text", "text": text}
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            entry = tool_calls.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.get("id"):
                                entry["id"] += tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                # 赋值而非累加：部分提供方会在多个分片中重复下发完整
                                # name（arguments 才是增量分片），累加会拼成重复串导致
                                # 工具名校验失败；id/arguments 保持增量拼接。
                                entry["name"] = fn["name"]
                            if fn.get("arguments"):
                                entry["arguments"] += fn["arguments"]
                    except Exception:
                        continue
    except Exception as e:
        yield {"kind": "text", "text": f"[LLM 连接错误] {str(e)[:200]}"}
        return

    # 流结束后处理工具调用（按 index 顺序）：白名单筛选/导航产出 action，
    # 只读查询工具执行真实查询并把结果注入文本流末尾（防模型编造数据）。
    # 未完成调用（有增量但无合法参数，如仅 name 无 args）流末统一显式反馈，
    # 避免「模型要调工具却静默跳过」让用户无感知（T-72）。
    unfinished: list[tuple[str, str]] = []
    for idx in sorted(tool_calls):
        call = tool_calls[idx]
        name = call.get("name") or ""
        args_text = (call.get("arguments") or "").strip()
        if not args_text:
            # 空参数 = 未完成的工具调用（arguments 分片从未下发），记录并流末反馈
            unfinished.append((name, call.get("arguments") or ""))
            continue
        try:
            args = json.loads(args_text)
            if not isinstance(args, dict):
                unfinished.append((name, args_text))
                continue
        except Exception:
            unfinished.append((name, args_text))
            continue
        if name == "apply_signal_center_filters":
            # 工具 parameters 即 CommandFilters 结构，arguments 只含 filters 字段；
            # command/mode 为服务端字面量，包裹后经 FilterCommand 严格校验
            # （extra=forbid/枚举/信号白名单由模型保证，注入一律拒绝）
            try:
                cmd = FilterCommand.model_validate(
                    {
                        "command": "signal_center.apply_filters",
                        "mode": "replace",
                        "filters": args,
                    }
                )
            except Exception as e:
                logger.info("工具参数校验失败（不产出 action）: %s", e)
                continue
            yield {
                "kind": "action",
                "action": {
                    "command": cmd.command,
                    "mode": cmd.mode,
                    "filters": cmd.filters.model_dump(),
                },
            }
        elif name == "stock.navigate":
            code = str(args.get("code") or "").strip()
            if not _STOCK_CODE_RE.match(code):
                logger.info("stock.navigate 参数非法（不产出 action）: %r", code)
                continue
            yield {
                "kind": "action",
                "action": {"command": "stock.navigate", "code": code},
            }
        elif name in _READONLY_TOOLS:
            result = _run_readonly_tool(name, args)
            if result:
                yield {"kind": "text", "text": f"\n\n{result}"}

    # 兜底（T-72）：存在未完成工具调用时在文本流末尾追加显式提示行，
    # 并记录 warning（含模型名、工具名、原始增量摘要），不再静默丢弃。
    if unfinished:
        for name, raw in unfinished:
            logger.warning(
                "工具调用未完成（无合法参数，未产出 action）: model=%s tool=%s raw=%r",
                settings.llm_model,
                name or "<unknown>",
                raw[:200],
            )
        yield {
            "kind": "text",
            "text": "\n\n⚠ 工具调用未完成（模型仅返回工具名，未提供参数）",
        }
