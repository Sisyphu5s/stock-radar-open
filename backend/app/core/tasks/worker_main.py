"""任务 worker 进程入口（T-122/P1-65 持久任务执行与恢复）。

Web 进程（uvicorn --reload）与任务执行解耦：部署以 SR_TASKS_EMBEDDED=0 启动
本进程（由独立进程启动），任务由本进程轮询
认领执行——Web reload 不再中断运行中任务；进程自身重启时遗留任务由
recover_stale_jobs 重新排队（不被无条件置 failed）。

运行：PYTHONPATH=. .venv/bin/python -m app.core.tasks.worker_main
"""

from __future__ import annotations

import logging
import time

from ...storage.db import init_db
from .runner import _ensure_timeout_monitor, recover_stale_jobs, worker_poll_once

logger = logging.getLogger("stockradar.tasks.worker")

# 轮询间隔（秒）：认领频率；认领后任务异步执行，间隔不影响任务吞吐
POLL_INTERVAL_SEC = 0.5
# 单轮认领上限：控制线程生成节奏（实际并发由 _run_task 内闸门限制）
POLL_BATCH = 4


def worker_tick() -> int:
    """单轮认领并返回本轮认领数（测试可调用）。"""
    return worker_poll_once(POLL_BATCH)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_db()
    recovered = recover_stale_jobs()
    if recovered:
        logger.info("worker 启动：恢复 %d 个遗留任务为重新排队（pending）", recovered)
    _ensure_timeout_monitor()
    logger.info("task worker 已启动（轮询间隔 %.1fs，单轮上限 %d）", POLL_INTERVAL_SEC, POLL_BATCH)
    while True:
        try:
            worker_tick()
        except Exception as e:  # noqa: BLE001 单轮异常不退出进程
            logger.error("worker 轮询异常: %s", str(e)[:200])
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()