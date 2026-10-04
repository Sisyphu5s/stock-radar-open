"""Alpha101 全库评分处理器(_run_alpha101_score)。

自 runner.py 逐字搬移,仅 import 路径调整;runner 依赖经 _deps._dyn 动态转发
(测试 monkeypatch Q.ThreadPoolExecutor/Q._parallel_workers/Q.backend_name 等穿透),
_ProgressThrottle 类直接绑定。
"""

from __future__ import annotations

from ..errors import JobCancelled
from ..runner import _ProgressThrottle, logger
from ._deps import _dyn

# runner 辅助/模块状态:动态转发(测试 monkeypatch 穿透)
_start_running = _dyn("_start_running")
_track = _dyn("_track")
_checkpoint = _dyn("_checkpoint")
_update = _dyn("_update")
_set_phase = _dyn("_set_phase")
_terminal = _dyn("_terminal")
_parallel_workers = _dyn("_parallel_workers")
_rpn_feature_names = _dyn("_rpn_feature_names")
# runner 顶层 import 的业务符号:动态转发(ThreadPoolExecutor 测试会 patch)
init_backend = _dyn("init_backend")
backend_name = _dyn("backend_name")
ThreadPoolExecutor = _dyn("ThreadPoolExecutor")
as_completed = _dyn("as_completed")
utcnow = _dyn("utcnow")


def _run_alpha101_score(job_id: int, params: dict):
    """Alpha101 全库评分：逐因子评估（每个因子计一次进度），排序输出库评分。

    numpy 后端按因子并行（每 job 一个 ThreadPoolExecutor）；mlx 后端保持串行。
    并行 worker 内先过协作控制点，暂停/取消时因子求值立即停摆；
    取消时挂起 futures 用 cancel 尽力回收，不等待长任务。
    """
    try:
        if not _start_running(job_id, 1.0):
            return  # 已被取消
        _track(job_id, 1800)
        init_backend()
        import numpy as np
        from ....lib.alpha.alpha101 import list_alpha101
        from ....core.datasets import load_panel
        from ....lib.alpha.evaluate import evaluate_factor, forward_returns
        from ....lib.alpha.operators import compile_rpn

        horizon = int(params.get("horizon", 5))
        limit = int(params.get("limit", 0)) or None
        items = list_alpha101()[:limit]
        # P2-15: 按公式实际特征子集加载（全库公式特征 ∪ close 供 forward_returns）
        need = {"close"}
        for a in items:
            try:
                need |= _rpn_feature_names(compile_rpn(a["formula"]))
            except Exception:
                continue
        panel_info = load_panel(int(params.get("dataset_id", 0)), features=sorted(need))
        if panel_info is None:
            raise RuntimeError("数据集不可用")
        _checkpoint(job_id)
        panel = panel_info["panel"]
        fwd = forward_returns(panel["close"], horizon=horizon)
        _set_phase(job_id, "数据准备")
        total = len(items)
        scored = []

        def _score_one(a: dict) -> dict:
            # 协作控制点：暂停阻塞/取消抛错（在 worker 线程内，保证并行求值及时停摆）
            _checkpoint(job_id)
            try:
                rpn = compile_rpn(a["formula"])
                # P2-16: 显式传 horizon，不再依赖 _infer_horizon（其 clamp 60 会让
                # horizon>60 的年化系数失真）
                ev = evaluate_factor(rpn, panel, fwd, horizon=horizon)
                ic_mean = (
                    float(ev["ic"])
                    if ev.get("ic") is not None and np.isfinite(ev["ic"])
                    else None
                )
                stability = (
                    float(ev["stability"])
                    if ev.get("stability") is not None and np.isfinite(ev["stability"])
                    else None
                )
                score = (
                    abs(ic_mean or 0) * 100 * (0.5 + min(max(stability or 0, 0), 1))
                    if ic_mean is not None
                    else None
                )
                return {
                    "id": a["id"],
                    "name": a["name"],
                    "ic_mean": ic_mean,
                    "stability": stability,
                    "score": round(score, 4),
                }
            except JobCancelled:
                raise
            except Exception:
                return {
                    "id": a["id"],
                    "name": a["name"],
                    "ic_mean": None,
                    "stability": None,
                    "score": None,
                }

        n_workers = _parallel_workers()
        _set_phase(job_id, "因子评估")
        if backend_name() != "numpy" or n_workers <= 1:
            # mlx 后端 / 单线程回退：保持原串行语义（每因子一个控制点 + 进度更新）
            for i, a in enumerate(items):
                _checkpoint(job_id)
                scored.append(_score_one(a))
                _update(job_id, progress=1 + 95 * (i + 1) / max(total, 1))
        else:
            throttle = _ProgressThrottle()
            done = 0
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(_score_one, a): a for a in items}
                try:
                    for fut in as_completed(futures):
                        _checkpoint(job_id)  # 主线程控制点：暂停/取消立即生效
                        scored.append(fut.result())
                        done += 1
                        progress = 1 + 95 * done / max(total, 1)
                        if throttle.should_update(progress, force=(done == total)):
                            _update(job_id, progress=progress)
                except BaseException:
                    for f in futures:
                        f.cancel()
                    raise
        scored.sort(
            key=lambda x: x["score"] if x["score"] is not None else -1e9, reverse=True
        )
        _set_phase(job_id, "结果整理", 96.0)
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "results": scored,
                "meta": {
                    "stocks": panel["close"].shape[0],
                    "days": panel["close"].shape[1],
                    "scored": len(scored),
                },
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("Alpha101 全库评分失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
