"""任务类型注册表(core/tasks/registry):数据驱动的任务分派。

场景:9 种任务类型(gp_run/evaluate/neural_train/backtest/alpha101_score/
factor_tune/dataset_build/market_scan/paper_experiment)的处理器经 register()
注册;runner.submit 的 worker 分派只查 registry,未知类型由调用方兜底置
failed——新增任务类型 = 新增注册项,不改分派代码。

job_type 字符串与既有 API/前端契约完全一致(任务 ID 编号与 job_type 不变)。
"""

from __future__ import annotations

from typing import Callable

# job_type -> 处理器(job_id, params)。模块级唯一事实源;同类型重复注册覆盖旧项。
HANDLERS: dict[str, Callable[[int, dict], None]] = {}

import logging

logger = logging.getLogger("stockradar.core.tasks.registry")


def register(job_type: str, fn: Callable[[int, dict], None]) -> None:
    """注册任务类型处理器;重复注册覆盖旧处理器(最后注册生效)并告警。

    P2-51:重复注册多为跨层顺序依赖误覆盖,静默替换难定位,故登记后若原处理器
    非同一对象则 warning(覆盖语义保留:最后注册生效)。
    """
    prev = HANDLERS.get(job_type)
    if prev is not None and prev is not fn:
        logger.warning(
            "任务类型 %s 重复注册:原有处理器 %r 将被 %r 覆盖",
            job_type,
            prev,
            fn,
        )
    HANDLERS[job_type] = fn


def run(job_type: str, job_id: int, params: dict) -> bool:
    """分派入口:查 registry 执行处理器,返回是否找到该类型的处理器。

    未注册类型返回 False,由调用方(submit worker)置 failed;registry 不写终态。
    """
    handler = HANDLERS.get(job_type)
    if handler is None:
        return False
    handler(job_id, params)
    return True
