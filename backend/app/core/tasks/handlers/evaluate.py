"""单因子完整评估处理器(_run_evaluate)。

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
_set_phase = _dyn("_set_phase")
_terminal = _dyn("_terminal")
_rpn_feature_names = _dyn("_rpn_feature_names")
# runner 顶层 import 的业务符号:动态转发
init_backend = _dyn("init_backend")
load_panel = _dyn("load_panel")
split_panel = _dyn("split_panel")
evaluate_factor = _dyn("evaluate_factor")
utcnow = _dyn("utcnow")


def _run_evaluate(job_id: int, params: dict):
    """单因子完整评估实验（含 IC 序列与分层收益，供评估页可视化）。"""
    try:
        if not _start_running(job_id, 10.0):
            return  # 已被取消
        _track(job_id)
        init_backend()
        from ....lib.alpha.operators import compile_rpn
        from ....lib.alpha.evaluate import evaluate_factor_full

        rpn = compile_rpn(params.get("expression", ""))
        # P2-15: 按表达式实际特征子集加载（+close 供 forward_returns）
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
        n_days = panel["close"].shape[1]
        # P2-16: 统一 split_panel（训练 80% / 样本外 20%，无验证段）
        sp = split_panel(panel, horizon, 0.8, 0.0)
        train_panel, oos_panel = sp["train"], sp["oos"]
        train_fwd, oos_fwd = sp["fwd_train"], sp["fwd_oos"]
        t2 = sp["t2"]
        res = evaluate_factor_full(
            rpn, train_panel, train_fwd, horizon=horizon, dates=dates[:t2]
        )
        _set_phase(job_id, "数据准备", 40.0)
        _set_phase(job_id, "因子评估")
        oos_res = evaluate_factor(rpn, oos_panel, oos_fwd, horizon=horizon)
        _set_phase(job_id, "结果整理", 70.0)
        # 附加 LaTeX 公式（KaTeX 渲染）
        from ....lib.alpha.latex import cached_latex

        res["latex"] = cached_latex(res["expression"])
        oos_res["latex"] = cached_latex(oos_res["expression"])
        # 附加日期轴：res["dates"] 已由 evaluate_factor_full 按 ic_series 抽稀对齐
        res["oos_dates"] = dates[t2:]
        oos_res["dates"] = dates[t2:]
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "result": res,
                "oos": oos_res,
                "meta": {
                    "stocks": panel["close"].shape[0],
                    "days": n_days,
                    "split_date": dates[t2] if t2 < len(dates) else None,
                },
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("评估实验失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
