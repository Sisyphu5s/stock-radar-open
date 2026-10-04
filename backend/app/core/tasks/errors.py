"""任务执行器共享异常与错误文案常量(core/tasks/errors)。

JobCancelled 供任务处理器内部可中断循环/进度回调抛出,由 runner 的 _terminal
守卫识别(向上传播保持取消语义);scanner 等未来共享方直接 import 本模块,
斩断 queue ↔ scanner 的跨域 import 环。
"""

# 用户取消的任务错误文案（DB 中 cancelled/failed 共用；旧数据 failed+此文案 → 输出归一 cancelled）
_CANCELLED_ERR = "已被用户取消"


class JobCancelled(Exception):
    """任务已被用户取消：可中断循环/进度回调处抛出，立即中止计算。"""


class QueueFullError(RuntimeError):
    """任务队列已满：submit 拒绝入队（超 job_queue_max 上限），不创建任务行。

    message 携带队列上限与重试语义，由 API 层转换为 429 响应。
    """


def _timeout_error(timeout_sec: int) -> str:
    return (
        f"任务超时(>{timeout_sec}s)，已强制结束；"
        "计算线程可能仍在运行，建议重启后端或调小参数"
    )
