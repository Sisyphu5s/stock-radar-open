"""FastAPI 入口：挂载 /api/v1，初始化数据库，启动扫描调度。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import BASE_DIR, settings
from .core import plugins as plugins_mod
from .core.runtime import Runtime, setup_runtime

logger = logging.getLogger("stockradar.main")

# T-123 三类健康：运行时实例由 lifespan 持有，供 /health/ready 读取组件状态
_runtime: Runtime | None = None
from .storage.db import init_db

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("stockradar")

# 前端静态资源缓存策略（生产单服务托管，单一事实源）：
# hash 命名的 assets 一年 immutable；index.html 不缓存（保证新发布立即生效）；其余静态 1 天。
_ASSET_CACHE = "public, max-age=31536000, immutable"
_INDEX_CACHE = "no-cache"
_STATIC_CACHE = "public, max-age=86400"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 运行时编排（T-107）：按组件注册表启动全部后台机制（任务重启恢复/后端初始化/
    # 扫描调度/预热/源恢复/任务超时巡检/事件 GC）。任一组件失败仅 warning，不阻塞
    # 服务（组件级容错，语义与旧逐个 try/except 一致）；shutdown 时逆拓扑序停止。
    global _runtime
    _runtime = runtime = setup_runtime()
    runtime.start()
    # T-123 安全默认值提示：显式配置才算批准暴露（不改变默认行为，仅启动可观测）
    if not settings.api_token:
        logger.warning("SR_API_TOKEN 未设置——API 无鉴权（局域网部署请显式配置）")
    if settings.cors_origins.strip() in ("", "*"):
        logger.warning("SR_CORS_ORIGINS 为 '%s'——跨源放行宽泛，生产建议显式白名单", settings.cors_origins)
    yield
    runtime.stop()
    _runtime = None


app = FastAPI(title=settings.app_name, lifespan=lifespan)

API = settings.api_prefix  # 常量上移：api_token_auth 中间件/路由挂载/health 均引用

# 可选鉴权：SR_API_TOKEN 非空时，除 health/openapi.json 外所有 /api/v1/* 校验 X-API-Token
if settings.api_token:

    @app.middleware("http")
    async def api_token_auth(request, call_next):
        path = request.url.path
        # T-123:health 家族（/health、/health/live、/health/ready）恒豁免——探活端点
        # 必须无鉴权可达，否则带 Token 部署时探活/编排层无法工作
        if path.startswith(API) and not path.startswith(f"{API}/health") and path != "/openapi.json":
            if request.headers.get("X-API-Token") != settings.api_token:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


# CORS：来源白名单收敛到 Settings（settings.cors_origins，SR_CORS_ORIGINS 环境变量），
# 不在此直读环境变量，避免配置双入口。默认 "*"（前端走 vite 代理为同源，跨源直连
# 场景靠此处放行；需收紧设 SR_CORS_ORIGINS="http://a,http://b"）。
# 必须注册在 auth 之后（Starlette add_middleware 后注册者在外层）：
# 使 CORSMiddleware 成为最外层，OPTIONS 预检直接 200，不落入 api_token_auth。
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 域插件挂载(T-110):路由由插件注册表驱动,SR_PLUGINS(逗号分隔)控制启用域,
# 空 = 全开(默认);system 域强制启用。新增域只需改 core/plugins.py register_builtin(),
# main.py 不再改动;api 层冻结,只读引用 router 对象,挂载顺序/前缀语义与旧静态
# include_router 等价(13 组前缀互不重叠,无路径匹配依赖)。
plugins_mod.register_builtin()
plugins_mod.load_plugins_from_config(settings.plugins)
for _plugin in plugins_mod.get_enabled_plugins():
    # T-121 惰性解析:仅启用域才 importlib 其 api 模块(禁用域重依赖零加载)
    for _router in _plugin.resolve():
        app.include_router(_router, prefix=API)


@app.get(f"{API}/health/live")
def health_live():
    """Liveness（T-123）：进程活着即 200，不探任何依赖。"""
    return {"status": "ok"}


@app.get(f"{API}/health/ready")
def health_ready():
    """Readiness（T-123）：DB 可访问 + 运行时组件状态可观测。

    关键依赖（DB）不可用 → 503（不接流量）；组件级失败（源/预热等，设计上
    容错不阻塞服务）→ 200 + degraded 标记，供编排层区分。
    """
    from .storage.db import SessionLocal

    db_ok = True
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        db_ok = False

    rt = _runtime
    comp_status = rt.status() if rt is not None else {}
    failed = [name for name, st in comp_status.items() if not st.get("running")]
    body = {
        "status": "ok" if db_ok else "unavailable",
        "db": "ok" if db_ok else "unreachable",
        "components": comp_status,
        "degraded": failed,
    }
    if not db_ok:
        return JSONResponse(body, status_code=503)
    return body


@app.get(f"{API}/health")
def health():
    from .lib.alpha.backend import backend_name
    from .lib.alpha import native_ops
    from .api.system import read_deploy_state

    deploy = read_deploy_state()
    backend_commit = deploy["deployed_commit"]
    frontend_commit = deploy["dist_commit"]
    synced = None
    if backend_commit and frontend_commit:
        synced = backend_commit == frontend_commit
    return {
        "status": "ok",
        "app": settings.app_name,
        "gp_backend": backend_name(),
        "native_available": native_ops.native_available(),
        # T-81 版本自检字段（dev 环境无部署文件 → null；前端按 frontend_synced 显横幅）
        "backend_commit": backend_commit,
        "frontend_commit": frontend_commit,
        "frontend_synced": synced,
    }


def _resolve_frontend_dist() -> Path | None:
    """解析前端构建产物目录；不存在则返回 None（跳过托管，保持纯 API）。"""
    raw = settings.frontend_dist_dir.strip()
    dist = Path(raw).expanduser() if raw else BASE_DIR.parent / "frontend" / "dist"
    return dist if dist.is_dir() else None


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """挂载前端构建产物 + SPA fallback（生产单服务托管）。

    机制化设计：
    - /assets 挂到带长缓存 Cache-Control 的 StaticFiles（hash 文件可 immutable）；
    - catch-all 路由兜底所有未命中路径：命中 api/* 直接 404，防止把 API 404 误吞成 index.html；
    - is_relative_to 防路径穿越（../ 等越界访问一律回退 index.html）；
    - 命中真实文件按文件类别套用对应缓存策略，否则回退 index.html（no-cache）。
    """

    class _CacheStatic(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = _ASSET_CACHE
            return resp

    if (dist / "assets").is_dir():
        app.mount(
            "/assets", _CacheStatic(directory=dist / "assets"), name="frontend-assets"
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise StarletteHTTPException(status_code=404, detail="Not Found")
        dist_root = dist.resolve()
        if full_path:
            candidate = (dist / full_path).resolve()
        else:
            candidate = dist_root / "index.html"
        if candidate.is_file() and candidate.is_relative_to(dist_root):
            if "assets" in candidate.parts:
                cache = _ASSET_CACHE
            elif candidate.name == "index.html":
                cache = _INDEX_CACHE
            else:
                cache = _STATIC_CACHE
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(
            dist_root / "index.html", headers={"Cache-Control": _INDEX_CACHE}
        )


if (_dist := _resolve_frontend_dist()) is not None:
    _mount_frontend(app, _dist)


if __name__ == "__main__":
    import uvicorn

    # reload 开关与排除项收敛在 Settings(C5):SR_UVICORN_RELOAD /
    # SR_UVICORN_RELOAD_EXCLUDE(逗号分隔),与 start.sh 同读单一事实源,禁止硬编码。
    _reload_excludes = [
        _p.strip() for _p in settings.uvicorn_reload_exclude.split(",") if _p.strip()
    ]
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=8000,
        reload=settings.uvicorn_reload,
        reload_excludes=_reload_excludes,
    )
