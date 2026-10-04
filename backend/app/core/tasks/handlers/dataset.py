"""数据集构建处理器(_run_dataset_build)。

自 runner.py 逐字搬移,仅 import 路径调整;runner 依赖经 _deps._dyn 动态转发。
"""

from __future__ import annotations

from ..errors import JobCancelled
from ..runner import logger
from ._deps import _dyn

# runner 辅助/模块状态:动态转发(测试 monkeypatch 穿透)
_start_running = _dyn("_start_running")
_track = _dyn("_track")
_checkpoint = _dyn("_checkpoint")
_update = _dyn("_update")
_set_phase = _dyn("_set_phase")
_terminal = _dyn("_terminal")
utcnow = _dyn("utcnow")


def _run_dataset_build(job_id: int, params: dict):
    """数据集构建（拉取成分股 K 线 + 落库），逐股票进度。"""
    try:
        if not _start_running(job_id, 1.0):
            return  # 已被取消
        _track(job_id, 3600)
        from ....core.datasets import build_dataset, default_range

        _set_phase(job_id, "数据准备")
        phase_mid = {"done": False}

        def cb(done: int, total: int) -> None:
            _checkpoint(job_id)  # 逐股票进度即协作控制点
            if not phase_mid["done"]:
                phase_mid["done"] = True
                _set_phase(job_id, "拉取行情")
            _update(job_id, progress=1 + 95 * done / max(total, 1))

        res = build_dataset(
            name=str(params.get("name", "未命名数据集")),
            universe=str(params.get("universe", "custom")),
            start_date=str(params.get("start_date") or default_range()[0]),
            end_date=str(params.get("end_date") or default_range()[1]),
            limit=int(params.get("limit", 300)),
            custom_codes=params.get("custom_codes"),
            progress_cb=cb,
        )
        _set_phase(job_id, "结果整理", 96.0)
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={"dataset": res},
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("数据集构建任务失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
