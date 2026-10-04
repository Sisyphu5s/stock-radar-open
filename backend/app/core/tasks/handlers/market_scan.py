"""全市场信号扫描处理器(_run_market_scan)。

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
_terminal = _dyn("_terminal")
utcnow = _dyn("utcnow")


def _run_market_scan(job_id: int, params: dict):
    """全市场信号扫描（后台任务，带进度；手动触发 /signals/scan 时使用）。"""
    try:
        if not _start_running(job_id, 1.0):
            return  # 已被取消
        _track(job_id, 3600)
        from ....core.scanning import scan_once

        def cb(pct: float, msg: str) -> None:
            _checkpoint(job_id)  # 进度回调即协作控制点
            _update(job_id, progress=round(pct, 1))

        result = scan_once(
            period=str(params.get("period", "daily")),
            full_universe=bool(params.get("full_universe", True)),
            universe=str(params.get("universe", "watchlist")),
            top_n=params.get("top_n"),
            codes=params.get("codes"),
            progress_cb=cb,
        )
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={"scan": result},
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("市场扫描任务失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
