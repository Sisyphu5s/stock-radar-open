"""运行时配置覆盖（T-77）：AppParam 显式覆盖优先，env 为基线。

背景：11+ 项 env 配置（SR_SCAN_SCHEDULE / SR_SCAN_MAX_STOCKS / SR_QUOTE_CACHE_TTL /
SR_JOB_CONCURRENCY / SR_LLM_TIMEOUT 等）前端零暴露，用户裁决「很多选项只能看
不能改」。本模块把可运行时生效的项做成可编辑：

- Settings(config.py) 对下列 key 的**读取**（settings.<key>）经 __getattribute__
  hook 动态查 AppParam：AppParam 显式覆盖优先（用户主动改的），无覆盖回落 env 基线。
- API 层（api/system.py）提供 GET/PUT /system/config/runtime 供前端读写。

生效语义（如实标注，不硬做热更新）：
- immediate（立即生效）：market_scan_limit / quote_cache_ttl / llm_timeout /
  llm_model —— 消费方在函数内读取 settings 属性，hook 每次动态查 AppParam。
- restart（重启生效）：scan_schedule（scheduler 启动时注册固定 interval job，
  见 core/scanning.start_scanner）/ job_concurrency（并发闸门为模块级 Semaphore，
  见 core/tasks/runner._CONCURRENCY）——改动存库后重启后端生效。

边界：AppParam 查询失败（表未建 / DB 未就绪，如模块导入期）静默回落默认值；
非法覆盖值（类型不匹配 / 超出范围）同样回落默认值，不阻塞消费方。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("stockradar.core.runtime_config")

# AppParam 键前缀：运行时覆盖全部落在此前缀下，与指标全局参数（ind:）等互不干扰
KEY_PREFIX = "rt:"


@dataclass(frozen=True)
class RuntimeSpec:
    """单条运行时配置项定义（key 与 Settings 字段名一致）。"""

    key: str
    label: str
    type: str  # int | float | str | json
    description: str
    apply: str  # immediate | restart（生效语义，见模块 docstring）
    min: float | None = None
    max: float | None = None


RUNTIME_SPECS: dict[str, RuntimeSpec] = {
    "scan_schedule": RuntimeSpec(
        key="scan_schedule",
        label="扫描调度（周期 → 间隔秒）",
        type="json",
        description=(
            'JSON 表，如 {"daily": 300, "1": 60, "5": 120, "15": 300, '
            '"30": 600, "60": 1200, "weekly": 86400, "monthly": 86400}；'
            "间隔 0 = 不调度该周期。scheduler 启动时注册固定间隔，修改后需重启后端生效。"
        ),
        apply="restart",
    ),
    "market_scan_limit": RuntimeSpec(
        key="market_scan_limit",
        label="扫描股票池上限",
        type="int",
        description="扫描股票池上限（scanner 按成交额排序取前 N）；修改后下次扫描生效。",
        apply="immediate",
        min=1,
    ),
    "quote_cache_ttl": RuntimeSpec(
        key="quote_cache_ttl",
        label="行情缓存 TTL（秒）",
        type="int",
        description="实时快照缓存秒数（配合磁盘离线快照，减少重复拉取）；修改后下次拉取生效。",
        apply="immediate",
        min=1,
    ),
    "job_concurrency": RuntimeSpec(
        key="job_concurrency",
        label="任务并发",
        type="int",
        description="同时运行的重计算任务上限（其余保持 pending 排队）；并发闸门为启动时创建的信号量，修改后需重启后端生效。",
        apply="restart",
        min=1,
        max=64,
    ),
    "job_io_concurrency": RuntimeSpec(
        key="job_io_concurrency",
        label="IO 任务并发",
        type="int",
        description="IO 密集任务（dataset_build/market_scan/panel_build）独立并发上限（与重计算任务分闸限流）；信号量为启动时创建，修改后需重启后端生效。",
        apply="restart",
        min=1,
        max=64,
    ),
    "job_queue_max": RuntimeSpec(
        key="job_queue_max",
        label="任务队列上限",
        type="int",
        description="任务队列总在飞上限（pending+running 合计），超限新提交返回 429；信号量为启动时创建，修改后需重启后端生效。",
        apply="restart",
        min=1,
        max=10000,
    ),
    "llm_timeout": RuntimeSpec(
        key="llm_timeout",
        label="LLM 超时（秒）",
        type="float",
        description="LLM 请求超时秒数；修改后下次 LLM 调用生效。",
        apply="immediate",
        min=1,
        max=600,
    ),
    "llm_model": RuntimeSpec(
        key="llm_model",
        label="LLM 模型名",
        type="str",
        description="OpenAI 兼容模型名；修改后下次 LLM 调用生效（如连接测试可立即验证）。",
        apply="immediate",
    ),
}


def _coerce(spec: RuntimeSpec, value: Any) -> Any:
    """按 spec.type 校验并转换覆盖值；非法抛 ValueError。"""
    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{spec.key} 必须为整数")
        out = value
    elif spec.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{spec.key} 必须为数字")
        out = float(value)
    elif spec.type == "str":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{spec.key} 必须为非空字符串")
        out = value.strip()
    elif spec.type == "json":
        if not isinstance(value, dict):
            raise ValueError(f"{spec.key} 必须为 JSON 对象")
        for k, v in value.items():
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{spec.key} 的 {k!r} 必须为非负整数（0 = 不调度）")
        out = value
    else:  # pragma: no cover - 注册表内联类型，不会走到
        raise ValueError(f"未知类型 {spec.type!r}")
    if spec.min is not None and out < spec.min:
        raise ValueError(f"{spec.key} 不能小于 {spec.min}")
    if spec.max is not None and out > spec.max:
        raise ValueError(f"{spec.key} 不能大于 {spec.max}")
    return out


def _db_key(key: str) -> str:
    return KEY_PREFIX + key


def get_runtime(key: str, default: Any = None) -> Any:
    """读取运行时覆盖值：AppParam 显式覆盖优先，无覆盖/非法值/DB 不可用回落 default。

    调用方（Settings hook / API / scanning）传入的 default 为 env 基线。
    """
    spec = RUNTIME_SPECS.get(key)
    if spec is None:
        return default
    try:
        from ..storage.appparams import get_app_param

        raw = get_app_param(_db_key(key))
    except Exception:
        # DB 未就绪（模块导入期 / 测试无表）→ 回落基线
        return default
    if raw is None:
        return default
    try:
        if spec.type == "json":
            return _coerce(spec, json.loads(raw))
        if spec.type == "int":
            return _coerce(spec, int(raw))
        if spec.type == "float":
            return _coerce(spec, float(raw))
        return _coerce(spec, raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.warning("运行时覆盖 %s 值非法，回落默认: %r", key, raw)
        return default


def set_runtime(key: str, value: Any) -> Any:
    """写入运行时覆盖（白名单 + 类型/范围校验，非法抛 ValueError）。

    返回规范化后的值（int/float 已转换、str 已 strip、json 已校验）。
    """
    spec = RUNTIME_SPECS.get(key)
    if spec is None:
        raise ValueError(f"未知配置项 {key}，可选: {sorted(RUNTIME_SPECS)}")
    out = _coerce(spec, value)
    stored = json.dumps(out, ensure_ascii=False) if spec.type == "json" else str(out)
    from ..storage.appparams import set_app_params

    set_app_params({_db_key(key): stored})
    return out


def env_default(key: str) -> Any:
    """Settings 的 env 基线值（绕过运行时覆盖 hook，供 API 展示 default 字段）。"""
    from ..config import settings

    return object.__getattribute__(settings, key)
