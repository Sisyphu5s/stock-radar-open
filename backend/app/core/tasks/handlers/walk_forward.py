"""Walk-forward 滚动验证处理器(_run_walk_forward)。

自 handlers/evaluate.py 同构搬移:runner 依赖经 _deps._dyn 动态转发(测试
monkeypatch 穿透)。任务类型 'walk_forward',注册于 registry。
"""

from __future__ import annotations

from ..errors import JobCancelled
from ..runner import logger
from ._deps import _dyn

# runner 辅助/模块状态:动态转发(测试 monkeypatch 穿透)
_start_running = _dyn("_start_running")
_track = _dyn("_track")
_checkpoint = _dyn("_checkpoint")
_set_phase = _dyn("_set_phase")
_terminal = _dyn("_terminal")
_rpn_feature_names = _dyn("_rpn_feature_names")
# runner 顶层 import 的业务符号:动态转发
init_backend = _dyn("init_backend")
load_panel = _dyn("load_panel")
utcnow = _dyn("utcnow")


def _run_walk_forward(job_id: int, params: dict):
    """Walk-forward 滚动验证实验:多窗 OOS IC 拼接 + 分窗汇总。"""
    try:
        if not _start_running(job_id, 10.0):
            return  # 已被取消
        _track(job_id)
        init_backend()
        from ....lib.alpha.operators import compile_rpn
        from ....lib.alpha.walk_forward import run_walk_forward

        rpn = compile_rpn(params.get("expression", ""))
        # 表达式特征子集 + close(供前向收益)
        panel_info = load_panel(
            int(params.get("dataset_id", 0)),
            features=sorted(_rpn_feature_names(rpn) | {"close"}),
        )
        if panel_info is None:
            raise RuntimeError("数据集不可用")
        _checkpoint(job_id)
        panel = panel_info["panel"]
        dates = panel_info["dates"]
        horizon = int(params.get("horizon", 5))
        n_windows = int(params.get("n_windows", 3))
        _set_phase(job_id, "滚动验证")
        wf = run_walk_forward(
            rpn,
            panel,
            horizon=horizon,
            n_windows=n_windows,
            dates=dates,
        )
        _set_phase(job_id, "结果整理", 70.0)
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "walk_forward": wf,
                "meta": {
                    "stocks": panel["close"].shape[0],
                    "days": panel["close"].shape[1],
                    "n_windows": wf["n_windows"],
                },
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("Walk-forward 验证失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
