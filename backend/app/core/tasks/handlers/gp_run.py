"""GP 符号回归实验处理器(_run_gp)。

自 runner.py 逐字搬移,仅 import 路径调整;runner 依赖经 _deps._dyn 动态转发
(运行时解析 runner 模块属性,测试 monkeypatch 穿透),锁/常量直接绑定。
"""

from __future__ import annotations

from ..errors import JobCancelled
from ..runner import DEFAULT_TIMEOUT_SEC, REGRESSORS, logger
from ._deps import _dyn

# runner 辅助/模块状态:动态转发(测试 monkeypatch Q._update 等穿透)
_start_running = _dyn("_start_running")
_track = _dyn("_track")
_apply_backend = _dyn("_apply_backend")
_set_phase = _dyn("_set_phase")
_checkpoint = _dyn("_checkpoint")
_update = _dyn("_update")
_terminal = _dyn("_terminal")
# runner 顶层 import 的业务符号:动态转发(测试 monkeypatch Q.load_panel/evolve 等穿透)
load_panel = _dyn("load_panel")
split_panel = _dyn("split_panel")
evaluate_factor = _dyn("evaluate_factor")
compile_rpn = _dyn("compile_rpn")
evolve = _dyn("evolve")
utcnow = _dyn("utcnow")


def _run_gp(job_id: int, params: dict):
    """GP 符号回归实验。"""
    backend_mode = params.get("backend", "auto")
    try:
        if not _start_running(job_id, 2.0):
            return  # 已被取消/终态（delete_job 已写取消终态）
        _track(job_id, int(params.get("timeout_sec", DEFAULT_TIMEOUT_SEC)))
        active_backend = _apply_backend(backend_mode)

        # 配置白名单校验（features/op_set/op_config）：非法配置抛 ValueError →
        # 任务 failed 并给出明确原因，不静默回退默认。
        from ....lib.alpha.gp import DEFAULT_GP_FEATURES, DEFAULT_GP_OP_SET
        from ....lib.alpha.operators import (
            validate_features,
            validate_op_config,
            validate_op_set,
        )

        raw_features = params.get("features")
        raw_op_set = params.get("op_set")
        features = (
            list(DEFAULT_GP_FEATURES)
            if raw_features is None
            else validate_features(raw_features) or []
        )
        op_set = (
            list(DEFAULT_GP_OP_SET)
            if raw_op_set is None
            else validate_op_set(raw_op_set) or []
        )
        op_config = validate_op_config(params.get("op_config"))
        if not features:
            raise ValueError("features 不能为空，请至少选择一个特征字段")
        if not op_set:
            raise ValueError("op_set 不能为空，请至少选择一个算子")

        # 数据集
        ds_id = params.get("dataset_id")
        # P2-15: 只加载选中特征 + close（forward_returns 需要），不传全量 7 特征
        panel_info = load_panel(ds_id, features=sorted(set(features) | {"close"}))
        if panel_info is None:
            raise RuntimeError(f"数据集 {ds_id} 不可用")
        panel_full, dates = panel_info["panel"], panel_info["dates"]
        n_stocks, n_days = panel_full["close"].shape
        # 求值只使用选中 features：面板按 features 子集投影（含 oos 评估）
        missing = [f for f in features if f not in panel_full]
        if missing:
            raise ValueError(
                f"所选特征在数据集 {ds_id} 中缺失: {missing}（面板可用: {sorted(panel_full)}）"
            )
        panel = {f: panel_full[f] for f in features}
        _set_phase(job_id, "数据准备", 8.0)

        # 时间切分: 训练 60% / 验证 20% / 样本外 20%（P2-16 统一 split_panel）
        horizon = int(params.get("horizon", 5))
        sp = split_panel(panel_full, horizon, 0.6, 0.2)
        train = {k: sp["train"][k] for k in features}
        val = {k: sp["val"][k] for k in features}
        oos = {k: sp["oos"][k] for k in features}
        fwd_train, fwd_val, fwd_oos = sp["fwd_train"], sp["fwd_val"], sp["fwd_oos"]
        t1, t2 = sp["t1"], sp["t2"]

        pop_size = int(params.get("pop_size", 120))
        gens = int(params.get("generations", 12))
        target = params.get("target", "ic")
        penalty_complexity = bool(params.get("penalty_complexity", False))

        def progress_cb(gen, total, best):
            _checkpoint(job_id)
            _update(
                job_id,
                progress=10 + 80 * gen / total,
                phase=f"进化搜索 {gen}/{total} 代",
            )

        algorithm = params.get("algorithm", "gp")
        if algorithm == "gp":
            results, evolution = evolve(
                op_set=op_set,
                features=features,
                op_config=op_config,
                pop_size=pop_size,
                generations=gens,
                data_train=train,
                data_val=val,
                forward_returns=fwd_train,
                val_forward_returns=fwd_val,
                target=target,
                penalty_complexity=penalty_complexity,
                progress_cb=progress_cb,
            )
        else:
            info = REGRESSORS.get(algorithm)
            if info is None:
                raise RuntimeError(
                    f"算法 {algorithm} 不可用（未注册，可选: {', '.join(REGRESSORS)}）"
                )
            if not info.get("available"):
                raise RuntimeError(
                    f"算法 {algorithm} 不可用（未安装依赖: {info.get('desc', '')}）"
                )
            results, evolution = info["fn"](
                op_set=op_set,
                pop_size=pop_size,
                generations=gens,
                data_train=train,
                data_val=val,
                forward_returns=fwd_train,
                val_forward_returns=fwd_val,
                target=target,
                penalty_complexity=penalty_complexity,
                progress_cb=progress_cb,
            )

        # 样本外评估 Top 因子
        _set_phase(job_id, "结果整理")
        top = results[: int(params.get("top_n", 8))]
        for r in top:
            _checkpoint(job_id)
            rpn = r.get("rpn")
            if rpn is None and r.get("source") == "neural":
                continue
            try:
                rpn = rpn or compile_rpn(r["expression"])
                oos_eval = evaluate_factor(rpn, oos, fwd_oos, horizon=horizon)
                r["oos"] = oos_eval
                r.pop("tree", None)
                r.pop("rpn", None)
            except Exception as e:
                r["oos"] = {"error": str(e)[:100]}

        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "results": results[:20],
                "evolution": evolution,
                "meta": {
                    "stocks": n_stocks,
                    "days": n_days,
                    "split": {"train": t1, "val": t2 - t1, "oos": n_days - t2},
                    "dates": {"train": dates[0], "oos_end": dates[-1]},
                    "backend": active_backend,
                    "config": {
                        "features": list(features),
                        "op_set": list(op_set),
                        "op_config": op_config,
                    },
                },
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：取消状态已由 delete_job 写入，禁止覆盖
    except Exception as e:
        logger.exception("GP 实验失败")
        err = str(e)[:2000]
        if backend_mode == "gpu":
            err = f"[GPU_FAILED] {err}（GPU 计算失败，建议用 CPU 后端重试）"[:2000]
        _terminal(job_id, status="failed", error=err, finished_at=utcnow())
