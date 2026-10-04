"""/api/v1/system/* : 系统控制（优雅退出等）。"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from typing import Any, Optional

from ..storage.db import engine

logger = logging.getLogger("stockradar.system")

router = APIRouter(prefix="/system", tags=["system"])

_exiting = threading.Event()


class DeployState(BaseModel):
    """T-81 部署状态聚合(T-112 契约硬化)。"""

    deployed_commit: Optional[str] = None
    dist_commit: Optional[str] = None
    dist_built_at: Optional[str] = None
    last_build_ok: Optional[bool] = None
    last_build_at: Optional[str] = None
    last_build_error_tail: Optional[str] = None


class DeployStatusResponse(DeployState):
    frontend_synced: Optional[bool] = None


class RuntimeConfigItem(BaseModel):
    """运行时配置项(与前端 RuntimeConfigItem 对齐)。"""

    key: str
    label: str
    type: str
    value: Any
    default: Any
    description: str
    apply: str


class RuntimeConfigList(BaseModel):
    data: list[RuntimeConfigItem]


class RuntimeConfigSetResponse(BaseModel):
    ok: bool
    key: str
    value: Any
    default: Any
    apply: str


class CacheStatsResponse(BaseModel):
    data: dict[str, Any]


class ExitResponse(BaseModel):
    ok: bool
    status: str
    reason: str = "user"


def read_deploy_state() -> dict:
    """T-81 部署状态单一事实源：读部署侧文件聚合（deployed-commit / build-manifest / last-build）。

    部署环境外（dev 直跑）文件缺失 → 各字段为 None；任何文件损坏/不可读不影响启动。
    路径约定：BASE_DIR=backend/，部署根=BASE_DIR.parent（.run/ 与 frontend/dist 均在部署根下）。"""
    from ..config import BASE_DIR, settings

    root = BASE_DIR.parent
    out = {
        "deployed_commit": None,
        "dist_commit": None,
        "dist_built_at": None,
        "last_build_ok": None,
        "last_build_at": None,
        "last_build_error_tail": None,
    }
    try:
        p = root / ".run" / "deployed-commit"
        if p.is_file():
            out["deployed_commit"] = p.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    try:
        raw = settings.frontend_dist_dir.strip()
        dist = Path(raw).expanduser() if raw else root / "frontend" / "dist"
        mf = dist / "build-manifest.json"
        if mf.is_file():
            m = json.loads(mf.read_text(encoding="utf-8"))
            out["dist_commit"] = m.get("commit")
            out["dist_built_at"] = m.get("builtAt")
    except (OSError, ValueError):
        pass
    try:
        lb = root / ".run" / "last-build.json"
        if lb.is_file():
            m = json.loads(lb.read_text(encoding="utf-8"))
            out["last_build_ok"] = m.get("ok")
            out["last_build_at"] = m.get("at")
            out["last_build_error_tail"] = m.get("error_tail")
    except (OSError, ValueError):
        pass
    return out


@router.get("/deploy-status", response_model=DeployStatusResponse)
def deploy_status():
    """T-81 部署管道状态聚合：deployed_commit / dist_commit / dist_built_at / last_build_*。

    前端版本自检横幅与 sr-service.sh status 共用（部署诊断单一出口）；
    frontend_synced = deployed 与 dist 都有值时的相等判定，缺任一为 null（未知）。"""
    st = read_deploy_state()
    synced = None
    if st["deployed_commit"] and st["dist_commit"]:
        synced = st["deployed_commit"] == st["dist_commit"]
    return {**st, "frontend_synced": synced}


@router.get("/config/runtime", response_model=RuntimeConfigList)
def list_runtime_config():
    """运行时配置清单（T-77）：key/label/type/value/default/description/apply。

    value = 当前生效值（AppParam 显式覆盖优先，无覆盖回落 env 基线）；
    default = env 基线（backend/.env）；apply = immediate（立即生效）| restart
    （重启生效）。"""
    from ..config import settings
    from ..core.runtime_config import RUNTIME_SPECS, env_default, get_runtime

    return {
        "data": [
            {
                "key": spec.key,
                "label": spec.label,
                "type": spec.type,
                "value": get_runtime(spec.key, env_default(spec.key)),
                "default": env_default(spec.key),
                "description": spec.description,
                "apply": spec.apply,
            }
            for spec in RUNTIME_SPECS.values()
        ]
    }


@router.put("/config/runtime", response_model=RuntimeConfigSetResponse)
def put_runtime_config(payload: dict):
    """更新运行时配置（T-77）：body {key, value}。

    白名单 + 类型/范围校验，非法 400；成功返回保存后的生效值（仍走覆盖优先）。"""
    from ..config import settings
    from ..core.runtime_config import (
        RUNTIME_SPECS,
        env_default,
        get_runtime,
        set_runtime,
    )

    key = payload.get("key")
    if key not in RUNTIME_SPECS:
        raise HTTPException(400, f"未知配置项 {key}，可选: {sorted(RUNTIME_SPECS)}")
    try:
        set_runtime(key, payload.get("value"))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        "key": key,
        "value": get_runtime(key, env_default(key)),
        "default": env_default(key),
        "apply": RUNTIME_SPECS[key].apply,
    }


@router.get("/cache-stats", response_model=CacheStatsResponse)
def cache_stats():
    """各全局缓存实例命中统计（TTL 内存缓存，进程内累计）。"""
    from ..storage.cache import all_cache_stats

    return {"data": all_cache_stats()}


def _require_exit_token(x_api_token: str = Header(default="")):
    """exit 端点独立鉴权（P1-58）：复用全站 X-API-Token 约定与 settings.api_token。

    全站中间件仅在 SR_API_TOKEN 非空时启用（空 token = 全站不鉴权）；exit 属
    远程杀进程级操作，必须单独强化：未配置 token 或 token 不匹配一律 401。
    """
    from ..config import settings

    if not settings.api_token:
        raise HTTPException(
            401, "远程退出未启用：服务端未配置 SR_API_TOKEN，请先配置后再调用"
        )
    if x_api_token != settings.api_token:
        raise HTTPException(401, "Unauthorized")


def _graceful_exit():
    """正常退出路径（替代 os._exit(0)）：发 SIGTERM 触发框架级 shutdown。

    SIGTERM → uvicorn 优雅关闭 → lifespan shutdown（runtime.stop() 逆拓扑序停
    全部后台组件）→ 进程正常退出 → atexit 执行（TTLCache flush 落盘）。
    os._exit 会跳过 atexit，丢未落盘缓存，且违反图审计 B4 铁律（os_exit），
    故不设 os._exit 兜底：SIGTERM 发送失败仅记日志（该场景在 posix 上基本
    不可达），退出交由外层服务管理。
    """
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception as e:
        logger.warning("SIGTERM 发送失败，进程退出交由外层服务处理: %s", e)


@router.post("/exit", dependencies=[Depends(_require_exit_token)], response_model=ExitResponse)
def exit_system(reason: str = "user"):
    """优雅退出：停止市场扫描调度 → 关闭数据库 → 落盘缓存 → 正常退出进程。

    鉴权（P1-58）：路由级依赖 _require_exit_token，未配置 SR_API_TOKEN 或
    X-API-Token 不匹配一律 401（全站中间件仅在 token 非空时启用，此处独立兜底）。
    退出走 SIGTERM 框架级 shutdown（正常关闭，atexit 落盘缓存），不再 os._exit。

    前端调用后进入退出完成页；可通过 README 中的启动命令再次启动。
    """
    if _exiting.is_set():
        return {"ok": True, "status": "exiting", "reason": reason}

    def _shutdown():
        if _exiting.is_set():
            return
        _exiting.set()
        try:
            # 1. 停止市场扫描调度器
            from ..core.scanning import stop_scanner

            stop_scanner()
            logger.info("已停止市场扫描调度")
        except Exception as e:
            logger.warning("停止调度器失败: %s", e)
        try:
            # 2. 关闭数据库连接池
            engine.dispose()
            logger.info("数据库连接已释放")
        except Exception as e:
            logger.warning("关闭数据库失败: %s", e)
        try:
            # 3. 落盘磁盘缓存：os._exit 跳过 atexit（TTLCache 的 flush 注册），
            #    手动 flush 各持久化缓存实例，避免未落盘队列丢失（尽力而为，失败仅日志）
            from ..storage.cache import _CACHE_INSTANCES

            for _, cache in _CACHE_INSTANCES:
                cache.flush()
            logger.info("磁盘缓存已落盘")
        except Exception as e:
            logger.warning("磁盘缓存落盘失败: %s", e)
        try:
            # 4. 优雅退出（延迟 0.5s 确保本请求响应已发出）：发 SIGTERM → uvicorn
            #    框架级关闭 → lifespan shutdown（runtime.stop() 逆拓扑停全部后台组件）
            #    → 进程正常退出 → atexit 落盘缓存。os._exit 会跳过 atexit 丢未落盘
            #    数据，故不设兜底（B4 铁律）：SIGTERM 发送失败仅记日志，缓存已手动
            #    flush 兜数据（见 _graceful_exit）
            threading.Timer(0.5, _graceful_exit).start()
        except Exception:
            _graceful_exit()

    threading.Thread(target=_shutdown, daemon=True).start()
    return {"ok": True, "status": "exiting", "reason": reason}
