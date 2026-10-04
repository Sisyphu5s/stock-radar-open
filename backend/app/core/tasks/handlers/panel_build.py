"""面板重建处理器(_run_panel_build)。

C11a:冷缓存 miss 时后台重建面板(拉成分 K 线 + spot 注入 + align,数十秒),
完成后落 npz 磁盘冷缓存 + 内存热缓存,后续请求直接命中。与 _run_dataset_build
(新建数据集)语义区分:本处理器只对已存在 dataset_id 走 load_panel 重建路径,
不新建 Dataset 行。runner 依赖经 _deps._dyn 动态转发(与同包其它 handler 一致)。
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


def _run_panel_build(job_id: int, params: dict):
    """面板重建(load_panel 冷缓存 miss 后台兜底):重建全量面板并落盘/入内存缓存。"""
    try:
        if not _start_running(job_id, 1.0):
            return  # 已被取消
        _track(job_id, 3600)
        from ....core.datasets import load_panel

        dataset_id = int(params.get("dataset_id") or 0)
        if dataset_id <= 0:
            raise ValueError("dataset_id 无效")
        _set_phase(job_id, "加载面板")
        # 重建全量特征:写无后缀全量 npz,特征子集请求经 _load_panel_disk
        # 子集 miss → 回退全量文件直接命中,无需为每种子集单独重建
        panel_info = load_panel(dataset_id)
        if panel_info is None:
            raise ValueError("数据集未构建或无成分数据，无法重建面板")
        _checkpoint(job_id)
        _set_phase(job_id, "结果整理", 96.0)
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "dataset_id": dataset_id,
                "stock_count": panel_info.get("stock_count"),
                "days": len(panel_info.get("dates") or []),
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("面板重建任务失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
