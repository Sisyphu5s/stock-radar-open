"""组合优化回测处理器(_run_backtest_opt, T-10)。

流程:表达式信号 → top N 选股 → 收益协方差(训练窗口内近 horizon 个月)→
优化得权重(均值方差/风险平价/最小方差,仅多 + 单股权重上限)→ 按固定权重
调仓回测(仅回测窗口,样本外)。

窗口规则(P1-59 前视声明):训练窗口 [0, train_end) 与回测窗口
[train_end, n_t) 严格分离,默认 train_ratio=0.7(前 70% 训练、后 30% 回测,
参数可调)。top_n 选股与协方差估计**只使用训练窗口内数据**(选股取训练窗口
末端信号,协方差取训练窗口内尾部窗口);权重由训练窗口决定;回测与
optimize 绩效只使用回测窗口收益(权重 t 日决定、t+1 日生效)。不存在
「用 t 时刻之后数据算 t 时刻信号」的路径。

回测引擎:复用 lib/alpha/backtest 的调仓纯函数(_daily_and_bench/_metrics/
_to_series/_suspended_day/_tradability_masks/resolve_cost_rate/limit_pct_by_code),
固定权重重建 _rebuild_fixed 语义与 run_backtest 完全一致(权重 t 日决定、
t+1 日生效、成本 t 日算出 t+1 日扣除、涨跌停/停牌约束);结果复用 BTResult
结构(modes/bench_nav/bench_metrics/params,params 携带 method/max_weight/
weights/optimize 绩效)。

runner 依赖经 _deps._dyn 动态转发(测试 monkeypatch Q.SessionLocal/Q.load_panel
/Q._update 等穿透),与 handlers/backtest.py 同构。
"""

from __future__ import annotations

import numpy as np

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


def _fixed_weights_target(
    factor: np.ndarray,
    daily_ret: np.ndarray,
    method: str,
    max_weight: float,
    top_n: int,
    horizon: int,
    risk_aversion: float,
    train_end: int,
) -> tuple[np.ndarray, np.ndarray]:
    """优化权重计算(与 /alpha/optimize 端点同路径,供 handler 独立复用)。

    P1-59 前视声明:本函数只使用训练窗口 [0, train_end) 的数据——
    top_n 选股取训练窗口末端的信号列,协方差估计取训练窗口内尾部窗口
    (近 max(60, horizon*21) 天),均不含回测窗口(未来)信息。调用方
    (handler)传入回测窗口起点 train_end,回测时点之前的数据计算权重。

    返回 (target_weights(S,), top_idx)。top_idx 为入选股票的全局下标。
    """
    from ....lib.alpha.optimize import (
        mean_variance,
        min_variance,
        risk_parity,
    )

    n_s, n_t = daily_ret.shape
    train_end = int(train_end)
    if not (2 <= train_end < n_t):
        raise ValueError(
            f"训练窗口划分无效: train_end={train_end} 需满足 2 <= train_end < n_t={n_t}"
            "(否则无样本外回测窗口,静默退化会造成前视)"
        )
    top_idx: np.ndarray | None = None
    # 选股只遍历训练窗口 [0, train_end),取其中最后一列信号充分的时刻
    for t in range(train_end - 1, -1, -1):
        x = factor[:, t]
        mask = np.isfinite(x)
        if mask.sum() >= top_n:
            order = np.argsort(x[mask])
            idx = np.where(mask)[0]
            top_idx = idx[order[-top_n:]]
            break
    if top_idx is None or len(top_idx) == 0:
        raise ValueError(f"信号有效股票数不足 {top_n},请换数据集或调小 top_n")

    win = max(60, int(horizon) * 21)
    start = max(0, train_end - win)
    sub = daily_ret[np.asarray(top_idx), start:train_end]
    means = np.nanmean(sub, axis=1)
    means = np.where(np.isfinite(means), means, 0.0)
    filled = np.where(np.isfinite(sub), sub, means[:, None])
    cov = np.cov(filled)
    if method == "mv":
        w = mean_variance(
            means, cov, risk_aversion=risk_aversion, max_weight=max_weight
        )
    elif method == "risk_parity":
        w = risk_parity(cov, max_weight=max_weight)
    else:
        w = min_variance(cov, max_weight=max_weight)
    target = np.zeros(n_s)
    target[np.asarray(top_idx)] = w
    return target, np.asarray(top_idx, dtype=np.int64)


def _rebuild_fixed(
    prev: np.ndarray | None,
    target_w: np.ndarray,
    buy_ok: np.ndarray | None,
    sell_ok: np.ndarray | None,
) -> np.ndarray:
    """固定权重重建:prev 持仓今日不可卖(跌停/停牌/一字板)→ 保留原权重,
    剩余权重在可买目标成员上按目标权重比例分配(总权重 <= 1,受阻不强制平仓)。

    语义与 backtest._rebuild_weights 的保留策略一致,只是目标权重从「等权」
    换成优化权重。
    """
    n_s = len(target_w)
    new_w = np.zeros(n_s)
    sel = (target_w > 1e-12) & (buy_ok if buy_ok is not None else True)
    if prev is not None and sell_ok is not None:
        hold = (prev > 1e-12) & ~sell_ok
        if hold.any():
            held = float(prev[hold].sum())
            new_w[hold] = prev[hold]
            sel = sel & ~hold
            if sel.any() and held < 1.0 - 1e-12:
                tw = target_w[sel]
                new_w[sel] = tw / tw.sum() * (1.0 - held)
            return new_w
    if sel.any():
        tw = target_w[sel]
        new_w[sel] = tw / tw.sum()
    return new_w


def _run_fixed_weight_backtest(
    target_w: np.ndarray,
    close: np.ndarray,
    daily_ret: np.ndarray,
    trade_interval: int,
    cost_rate_eff: float,
    ohlc: dict | None = None,
    codes: list[str] | np.ndarray | None = None,
    max_series: int = 600,
) -> dict:
    """固定权重组合回测(核心引擎,纯 numpy 可测)。

    每 trade_interval 日调仓回目标权重(T-02 涨跌停/停牌约束经 _rebuild_fixed),
    换手成本 = |Δw|·cost_rate_eff/2(与 run_backtest 组合级成本口径一致);
    收益后移一天(权重 t 日决定、t+1 日生效,避免前视偏差)。
    返回 BTResult 子结构 {modes, bench_nav, bench_metrics, params}。
    """
    from ....lib.alpha.backtest import (
        _daily_and_bench,
        _metrics,
        _suspended_day,
        _to_series,
        _tradability_masks,
        limit_pct_by_code,
    )
    from ...indicators.risk import risk_metrics as _risk_metrics

    c = np.asarray(close, dtype=np.float64)
    n_s, n_t = c.shape
    if codes is not None and np.asarray(codes).shape[0] == n_s:
        limit_pct = np.asarray(
            [limit_pct_by_code(str(x)) for x in np.asarray(codes, dtype=object)],
            dtype=np.float64,
        )
    else:
        limit_pct = None

    daily_ret_b, bench_ret = _daily_and_bench(c)
    w = np.zeros((n_s, n_t))
    turnover_cost = np.zeros(n_t)
    prev: np.ndarray | None = None
    for t in range(0, n_t):
        if t % trade_interval != 0:
            if prev is not None:
                w[:, t] = prev
            continue
        if limit_pct is not None and _suspended_day(ohlc, t):
            if prev is not None:
                w[:, t] = prev
            continue
        if limit_pct is not None and ohlc is not None:
            buy_ok, sell_ok = _tradability_masks(c, ohlc, t, limit_pct)
            new_w = _rebuild_fixed(prev, target_w, buy_ok, sell_ok)
        else:
            new_w = target_w.copy()
        if prev is not None:
            turnover_cost[t] = np.abs(new_w - prev).sum() * cost_rate_eff / 2
        prev = new_w
        w[:, t] = new_w

    combo_ret = np.full(n_t, np.nan)
    combo_ret[1:] = (
        np.nansum(w[:, :-1] * daily_ret_b[:, 1:], axis=0) - turnover_cost[1:]
    )
    combo_ret[0] = 0.0
    risk = _risk_metrics(np.cumprod(1 + np.nan_to_num(combo_ret)) * 100)
    return {
        "modes": [
            {
                "mode": "long",
                "combo_nav": _to_series(combo_ret, max_series),
                "metrics": _metrics(combo_ret),
                "risk": risk,
            }
        ],
        "bench_nav": _to_series(bench_ret, max_series),
        "bench_metrics": _metrics(bench_ret),
        "params": {},
    }


def _run_backtest_opt(job_id: int, params: dict):
    """组合优化回测(后台任务):优化权重 + 固定权重调仓,结果复用 BTResult 结构。"""
    try:
        if not _start_running(job_id, 10.0):
            return  # 已被取消
        _track(job_id)
        init_backend()
        from ....lib.alpha.backtest import (
            downsample_indices,
            resolve_cost_rate,
        )
        from ....lib.alpha.operators import compile_rpn, evaluate_rpn
        from ....lib.alpha.optimize import portfolio_metrics, turnover_vs_equal

        rpn = compile_rpn(params.get("expression", ""))
        need = _rpn_feature_names(rpn) | {"close", "open", "high", "low", "volume"}
        panel_info = load_panel(int(params.get("dataset_id", 0)), features=sorted(need))
        if panel_info is None:
            raise RuntimeError("数据集不可用")
        _checkpoint(job_id)
        panel = panel_info["panel"]
        dates = panel_info["dates"]
        codes = panel_info.get("codes")
        ohlc = {k: panel[k] for k in ("open", "high", "low", "volume") if k in panel}
        _set_phase(job_id, "数据准备", 30.0)
        factor = evaluate_rpn(rpn, panel)
        close = panel["close"]
        n_s, n_t = close.shape
        daily_ret = np.full((n_s, n_t), np.nan)
        daily_ret[:, 1:] = close[:, 1:] / close[:, :-1] - 1.0

        method = str(params.get("method") or "mv")
        if method not in ("mv", "risk_parity", "min_var"):
            raise ValueError(f"method 不受支持: {method}")
        max_weight = float(params.get("max_weight") or 0.1)
        if max_weight <= 0:
            raise ValueError("max_weight 必须为正数")
        top_n = max(2, min(200, int(params.get("top_n") or 20)))
        if max_weight * top_n < 1.0:
            raise ValueError(
                f"max_weight={max_weight} 与 top_n={top_n} 不可行(上限×数量需 >= 1)"
            )
        horizon = max(1, min(250, int(params.get("horizon") or 5)))
        risk_aversion = float(params.get("risk_aversion") or 1.0)
        trade_interval = max(1, int(params.get("trade_interval") or 5))
        cost_rate_eff, _ = resolve_cost_rate(
            float(params.get("cost_rate") or 0.001),
            params.get("slippage_bps"),
            params.get("commission_bps"),
            params.get("stamp_tax_bps"),
        )

        # P1-59 前视隔离:训练/回测窗口按 train_ratio 划分(默认前 70% 训练、
        # 后 30% 回测,参数可调)。训练窗口必须至少留出回测窗口 ≥10 天(绩效
        # 可算),且回测窗口非空;其余全部进入训练窗口。
        train_ratio = min(0.95, max(0.3, float(params.get("train_ratio") or 0.7)))
        train_end = max(2, int(n_t * train_ratio))
        if n_t - train_end < 10:
            train_end = max(2, n_t - 10)
        if n_t - train_end < 1:
            raise ValueError(f"样本过短: n_t={n_t} 不足以划分训练/回测窗口")
        train_end = min(train_end, n_t - 1)

        _set_phase(job_id, "权重优化", 50.0)
        target_w, top_idx = _fixed_weights_target(
            factor,
            daily_ret,
            method,
            max_weight,
            top_n,
            horizon,
            risk_aversion,
            train_end=train_end,
        )
        _checkpoint(job_id)

        _set_phase(job_id, "回测计算")
        # 回测只跑样本外窗口 [train_end, n_t):权重由训练窗口决定,t=train_end
        # 起生效,收益序列/涨跌停约束(ohlc)同步截断,保证无 t 之后数据参与。
        bt = _run_fixed_weight_backtest(
            target_w,
            close[:, train_end:],
            daily_ret[:, train_end:],
            trade_interval=trade_interval,
            cost_rate_eff=cost_rate_eff,
            ohlc={k: v[:, train_end:] for k, v in ohlc.items()} if ohlc else None,
            codes=codes,
        )
        # 优化绩效只使用回测窗口收益(样本外)
        perf = portfolio_metrics(
            daily_ret[np.asarray(top_idx), train_end:], target_w[top_idx]
        )
        turnover = turnover_vs_equal(target_w[top_idx])
        w_items = [
            {
                "code": str(codes[i]) if codes else f"#{i}",
                "weight": round(float(target_w[i]), 6),
            }
            for i in top_idx
            if target_w[i] > 1e-12
        ]
        bt["params"] = {
            "cost_rate": float(params.get("cost_rate") or 0.001),
            "trade_interval": trade_interval,
            "mode": "long",
            "direction": "long",
            "method": method,
            "max_weight": round(float(max_weight), 6),
            "top_n": int(len(top_idx)),
            "train_ratio": round(train_ratio, 4),
            "train_end": int(train_end),
            "weights": w_items,
            "optimize": {
                "perf": perf,
                "turnover": round(float(turnover), 6),
            },
        }
        _set_phase(job_id, "结果整理", 60.0)

        nav = (bt.get("modes") or [{}])[0].get("combo_nav") or []
        dts = np.asarray(dates, dtype=object)[train_end:]
        nav_dates = dts[downsample_indices(len(dts), 600)]
        meta: dict = {
            "stocks": n_s,
            "days": int(n_t - train_end),
            "dates": [str(x) for x in nav_dates][: len(nav)],
            "optimize": True,
            "train_ratio": round(train_ratio, 4),
            "train_end": int(train_end),
        }
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "backtest": bt,
                "meta": meta,
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消:状态已由 delete_job 写入
    except Exception as e:
        logger.exception("组合优化回测失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
