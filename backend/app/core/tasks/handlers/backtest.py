"""因子回测处理器(_run_backtest)。

自 runner.py 逐字搬移,仅 import 路径调整;runner 依赖经 _deps._dyn 动态转发
(测试 monkeypatch Q.SessionLocal/Q.load_panel/Q._update 等穿透)。
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
SessionLocal = _dyn("SessionLocal")
utcnow = _dyn("utcnow")
# NNModel 作为 SQLAlchemy 实体类传给 db.get 必须为真实类(不能 _dyn 包装),
# 测试不 patch Q.NNModel 的 backtest 路径;neural_train 的构造路径单独保留 _dyn。
from ..runner import NNModel  # noqa: E402


def _run_backtest(job_id: int, params: dict):
    """因子回测实验（多使用方式同时输出，供前端叠加展示）。"""
    try:
        if not _start_running(job_id, 10.0):
            return  # 已被取消
        _track(job_id)
        init_backend()
        from ....lib.alpha.backtest import (
            downsample_indices,
            run_backtest_multi,
            run_backtest_quantile,
        )
        from ....lib.alpha.evaluate import forward_returns
        from ....lib.alpha.operators import compile_rpn, evaluate_rpn
        import numpy as np

        # 因子来源二选一：model_id（NN checkpoint 预测截面因子）或 expression（符号表达式）；
        # 无 model_id 时行为与现状完全一致；非数值 model_id → ValueError → failed。
        raw_model_id = params.get("model_id")
        model_id = None if raw_model_id is None else int(raw_model_id)
        # P2-15: 按任务实际特征子集加载(NN 路径 close/volume；表达式路径 rpn 特征 + close)
        # T-02: 恒加 open/high/low/volume —— 涨跌停/停牌约束与成本建模依赖 OHLCV
        if model_id is None:
            rpn = compile_rpn(params.get("expression", ""))
            need = _rpn_feature_names(rpn) | {"close", "open", "high", "low", "volume"}
        else:
            rpn = None
            need = {"close", "volume", "open", "high", "low"}

        panel_info = load_panel(int(params.get("dataset_id", 0)), features=sorted(need))
        if panel_info is None:
            raise RuntimeError("数据集不可用")
        _checkpoint(job_id)
        panel = panel_info["panel"]
        dates = panel_info["dates"]
        codes = panel_info.get("codes")
        # T-02:OHLCV 子集(面板缺 open/high/low 的旧缓存场景由 _tradability_masks 降级)
        ohlc = {k: panel[k] for k in ("open", "high", "low", "volume") if k in panel}
        _set_phase(job_id, "数据准备", 30.0)
        if model_id is not None:
            # NN 因子路径：加载 checkpoint 权重 → 对面板（close/volume 子集）预测因子
            from ....lib.alpha import nn

            db = SessionLocal()
            try:
                m = db.get(NNModel, model_id)
            finally:
                db.close()
            if m is None:
                raise RuntimeError(f"NN 模型 {model_id} 不存在")
            meta = nn.load_checkpoint(m.checkpoint)
            nn_panel = {k: panel[k] for k in ("close", "volume") if k in panel}
            # T-56: 按 checkpoint 的 arch 分发（mlp 展平 / lstm/transformer 滑窗序列）
            factor = nn.predict_panel(
                meta["weights"],
                meta["activation"],
                nn_panel,
                arch=meta["arch"],
                arch_hparams=meta["arch_hparams"],
            )
        else:
            factor = evaluate_rpn(rpn, panel)
        close = panel["close"]
        fwd = forward_returns(close, int(params.get("horizon", 5)))
        modes = params.get("modes") or ["long_short", "long", "short"]
        bt_modes = [m for m in modes if m != "quantile"]
        run_quantile = "quantile" in modes
        direction = params.get("direction", "auto")
        # T-02 成本拆分(基点):任一显式 → 拆分模型(未传按 0);全缺省 → 旧 cost_rate 路径
        slippage_bps = params.get("slippage_bps")
        commission_bps = params.get("commission_bps")
        stamp_tax_bps = params.get("stamp_tax_bps")

        result: dict = {}
        _set_phase(job_id, "回测计算")
        if bt_modes:
            result = run_backtest_multi(
                factor,
                close,
                fwd,
                top_pct=float(params.get("top_pct", 0.2)),
                bottom_pct=float(params.get("bottom_pct", 0.2)),
                cost_rate=float(params.get("cost_rate", 0.001)),
                trade_interval=int(params.get("trade_interval", 5)),
                modes=bt_modes,
                direction=direction,
                slippage_bps=(
                    float(slippage_bps) if slippage_bps is not None else None
                ),
                commission_bps=(
                    float(commission_bps) if commission_bps is not None else None
                ),
                stamp_tax_bps=(
                    float(stamp_tax_bps) if stamp_tax_bps is not None else None
                ),
                codes=codes,
                ohlc=ohlc or None,
            )
        _set_phase(job_id, "结果整理", 60.0)
        if run_quantile:
            q_res = run_backtest_quantile(
                factor,
                close,
                fwd,
                n_q=int(params.get("n_q", 5)),
                cost_rate=float(params.get("cost_rate", 0.001)),
                trade_interval=int(params.get("trade_interval", 5)),
                direction=direction,
                slippage_bps=(
                    float(slippage_bps) if slippage_bps is not None else None
                ),
                commission_bps=(
                    float(commission_bps) if commission_bps is not None else None
                ),
                stamp_tax_bps=(
                    float(stamp_tax_bps) if stamp_tax_bps is not None else None
                ),
                codes=codes,
                ohlc=ohlc or None,
            )
            result["quantiles"] = q_res["quantiles"]
            if bt_modes:
                # 保留多方式参数（modes/top_pct 等），叠加分位参数（n_q）
                result["params"] = {**result["params"], **q_res["params"]}
            else:
                # 仅分层任务：modes 恒为数组（前端依赖），基准与参数取自分位结果
                result["modes"] = []
                result["bench_nav"] = q_res["bench_nav"]
                result["bench_metrics"] = q_res["bench_metrics"]
                result["params"] = q_res["params"]
        nav = (
            (result.get("modes") or [{}])[0].get("combo_nav")
            if result.get("modes")
            else (
                (result.get("quantiles") or [{}])[0].get("combo_nav")
                if result.get("quantiles")
                else []
            )
        )
        # 日期轴与 combo_nav 用同一抽稀规则（downsample_indices，max_series=600 与
        # run_backtest 默认一致），保证日期与净值逐点对齐
        dts = np.asarray(dates, dtype=object)
        nav_dates = dts[downsample_indices(len(dts), 600)]
        meta: dict = {
            "stocks": close.shape[0],
            "days": close.shape[1],
            "dates": [str(x) for x in nav_dates][: len(nav)],
        }
        if model_id is not None:
            meta["model_id"] = model_id
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "backtest": result,
                "meta": meta,
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("回测实验失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
