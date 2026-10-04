"""应用配置：环境变量驱动。"""

import os

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})

# T-77 可运行时覆盖的配置 key（白名单，与 core/runtime_config.RUNTIME_SPECS 一致）。
# 读取顺序：env 基线 → AppParam 显式覆盖（优先——用户主动改的）。消费方读
# settings.<key> 时经 __getattribute__ 动态查 AppParam；DB 不可用/值非法静默回落 env。
_RUNTIME_OVERRIDABLE = frozenset(
    {
        "scan_schedule",
        "market_scan_limit",
        "quote_cache_ttl",
        "job_concurrency",
        "job_io_concurrency",
        "job_queue_max",
        "llm_timeout",
        "llm_model",
    }
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "app" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SR_")

    app_name: str = "Stock Radar"
    api_prefix: str = "/api/v1"
    host: str = "127.0.0.1"  # Explicitly set SR_HOST for network access.
    database_url: str = f"sqlite:///{DATA_DIR / 'stock_radar.db'}"

    # K 线缓存独立库（T-103）：与业务主库分文件，避免 1.4GB klines 拖累主库备份/迁移
    klines_db_url: str = f"sqlite:///{DATA_DIR / 'kline_cache.db'}"

    # 可选 API 鉴权：设置后 /api/v1/*（除 health/openapi.json）需携带 X-API-Token 头
    api_token: str = ""

    # CORS 允许来源（逗号分隔；"*" 允许所有）。收敛在 Settings：
    # main.py 不得绕过 Settings 直读环境变量，保证配置单一事实源（SR_CORS_ORIGINS）
    cors_origins: str = "*"

    # 前端构建产物目录(生产单服务托管;T-B 单一 HTTP 服务)。空字符串 = 自动探测
    # <backend>/../frontend/dist;非空 = 显式路径(支持 SR_FRONTEND_DIST_DIR 覆盖)。
    frontend_dist_dir: str = ""

    # LLM (OpenAI 兼容)
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-v4-flash"
    llm_timeout: float = 60.0

    # 行情数据
    data_provider: str = (
        "auto"  # auto | akshare | sina | tencent（sources.py 的 VALID 集合）
    )
    market_scan_limit: int = 500  # 扫描股票池上限（scanner.py 按成交额排序取前 N）
    scan_max_stocks: int = 2000  # 信号扫描上限（按成交额前 N + 自选；雷达快照仍全市场）
    quote_cache_ttl: int = 60  # 实时快照缓存秒数（配合磁盘离线快照，减少重复拉取）

    # 指标接口（P2-35）：GET /indicators/{code} 的 days 上限，防极值触发全量重算 + 缓存膨胀
    max_indicator_days: int = 1000

    # /market/snapshot（P2-37）：默认字段子集（逗号分隔列白名单；空 = 全列，兼容旧契约）；
    # 单次返回行数上限（limit 钳制上界）
    market_snapshot_default_fields: str = ""
    market_snapshot_max_rows: int = 5000

    # 面板内存热缓存字节上界（P2-41）：条目数 + 字节双上限，防 limit=0 全市场面板（≈200MB/条）
    # 撑爆内存；0 = 仅条目数上限（旧行为）
    panel_cache_max_bytes: int = 512 * 1024 * 1024

    # 请求健壮性上限（P2-44/P2-45）：超限返回 400，防止畸形请求拖垮递归/任务队列
    screener_tree_max_depth: int = 50  # 条件树最大嵌套层数（校验+求值双重递归共用）
    scan_codes_max: int = 200  # /signals/scan codes 长度上限（防数 MB params 落库）
    scan_top_n_max: int = 200  # /signals/scan top_n 上限（成交额前 N 扫描）

    # 扫描调度注册表：周期 → 间隔秒（0 = 不调度）。单一事实源，scanner.start_scanner
    # 按表驱动逐周期注册后台 interval job（env SR_SCAN_SCHEDULE 传 JSON 可覆盖）。
    # daily 盘中 5min；分钟周期仅扫自选股（低功耗），间隔按带宽权衡放大；周/月 24h 一次。
    scan_schedule: dict[str, int] = {
        "daily": 300,
        "1": 60,
        "5": 120,
        "15": 300,
        "30": 600,
        "60": 1200,
        "weekly": 86400,
        "monthly": 86400,
    }

    # GP / Alpha
    gp_backend: str = "auto"  # auto | mlx | numpy

    # 任务队列并发闸门：同时运行的重计算任务数上限（其余保持 pending 排队）
    job_concurrency: int = 2

    # IO 密集任务（dataset_build/market_scan/panel_build）独立并发闸门：与 CPU 重计算
    # 任务分闸限流，避免长 IO 阻塞重计算任务的同时又让 IO 并发无上限（P2-31）。
    # 0/负值按 1 兜底（与 job_concurrency 同语义：闸门恒 ≥1）。
    job_io_concurrency: int = 2

    # 任务队列总在飞上限（pending+running 合计）：超限 submit 抛 QueueFullError，
    # API 层返回 429。每任务一个 daemon 线程，上限同时约束线程数与 DB 行数
    # （P2-31，修复千级任务=千级线程+千级 DB 行）。0/负值按 1 兜底。
    job_queue_max: int = 100

    # 运行时组件启停（T-107,core/runtime.py 编排器消费）：空 = 全部启用；非空时
    # 未列出的组件默认启用，列出的按 value 决定（大小写不敏感匹配组件名）。
    # 两种设置形式：SR_RUNTIME_COMPONENTS='{"SourceRecovery": false}'（JSON 整体
    # 覆盖），或独立变量 SR_RUNTIME_SOURCE_RECOVERY=false（逐组件覆盖，合并进
    # runtime_components）。组件的默认行为是启用——「关闭某组件」即显式置 false。
    runtime_components: dict[str, bool] = {}

    # 域插件启停(T-110,core/plugins.py 消费):逗号分隔插件名,空 = 全开(默认);
    # 非空 = 仅启用列出的域(system 域强制启用,不参与关闭);未知插件名警告跳过。
    plugins: str = ""

    # uvicorn reload 开关与排除项(C5):默认与旧行为一致(reload 开、排除 tests/)。
    # 排除项逗号分隔;uvicorn 仅监控 *.py,tests 目录无必要纳入。消费方
    # (main.py __main__)必须读本字段,不得再硬编码 --reload。
    uvicorn_reload: bool = True
    uvicorn_reload_exclude: str = "tests"

    def model_post_init(self, __context) -> None:
        """合并逐组件形式的环境变量（SR_RUNTIME_<NAME>=<bool>）进 runtime_components。

        整体 JSON（SR_RUNTIME_COMPONENTS）已由 pydantic-settings 解析进字段；
        此处只合并独立变量（跳过 SR_RUNTIME_COMPONENTS 自身），实现两种形式共存。
        """
        merged = dict(self.runtime_components)
        prefix = "SR_RUNTIME_"
        for k, v in os.environ.items():
            if k.startswith(prefix) and k != f"{prefix}COMPONENTS":
                merged[k[len(prefix) :]] = str(v).strip().lower() in _BOOL_TRUE
        self.runtime_components = merged

    def __getattribute__(self, name: str):
        """运行时配置覆盖（T-77）：env 基线 → AppParam 显式覆盖（优先）。

        对 _RUNTIME_OVERRIDABLE 内的 key，读 AppParam 中 rt:<key> 覆盖值；
        无覆盖 / DB 不可用 / 值非法 → 回落 env 基线（消费方不感知，不阻塞）。
        """
        if name in _RUNTIME_OVERRIDABLE:
            env_value = object.__getattribute__(self, name)
            try:
                from .core.runtime_config import get_runtime

                return get_runtime(name, env_value)
            except Exception:
                return env_value
        return object.__getattribute__(self, name)


settings = Settings()
