"""因子参数网格调优处理器(_run_factor_tune)。

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
# runner 顶层 import 的业务符号:动态转发
init_backend = _dyn("init_backend")
backend_name = _dyn("backend_name")
ThreadPoolExecutor = _dyn("ThreadPoolExecutor")
as_completed = _dyn("as_completed")
sharpe_ratio = _dyn("sharpe_ratio")
max_drawdown = _dyn("max_drawdown")
win_rate = _dyn("win_rate")
utcnow = _dyn("utcnow")


def _run_factor_tune(job_id: int, params: dict):
    """因子参数网格搜索调优（逻辑自 api/alpha.py /tune 迁移，转为后台任务）。"""
    try:
        if not _start_running(job_id, 2.0):
            return  # 已被取消
        _track(job_id, 1800)
        init_backend()
        import numpy as np
        from ....core.datasets import load_panel
        from ....lib.alpha.evaluate import (
            evaluate_factor,
            forward_returns,
            split_panel,
            stability as stab_fn,
        )
        from ....lib.alpha.operators import (
            collect_tunable_params,
            compile_rpn,
            evaluate_rpn_memo,
            expr_str,
            set_tunable_param,
        )

        expression = str(params.get("expression", "")).strip()
        try:
            rpn = compile_rpn(expression)
        except Exception as e:
            raise RuntimeError(f"表达式解析失败: {str(e)[:120]}")

        available = collect_tunable_params(rpn)
        available_names = [a["name"] for a in available]
        p = params.get("param") or {}
        pname = str(p.get("name", ""))
        if pname not in available_names:
            raise RuntimeError(f"参数 {pname} 不可调，可用: {available_names}")

        # P2-15: 按表达式实际特征子集加载（+close 供 forward_returns）
        panel_info = load_panel(
            int(params.get("dataset_id", 0)),
            features=sorted(_rpn_feature_names(rpn) | {"close"}),
        )
        if panel_info is None:
            raise RuntimeError("数据集不可用")
        panel = panel_info["panel"]
        horizon = int(params.get("horizon", 5))
        target = params.get("target", "ic")
        n_days = panel["close"].shape[1]
        # P1-31/P2-16: 统一 split_panel（训练 80% / 样本外 20%，无验证段）——
        # 修复原「完整面板 fwd 再切片」把 oos 价格泄入训练标签尾部的问题
        sp = split_panel(panel, horizon, 0.8, 0.0)
        train, oos = sp["train"], sp["oos"]
        fwd_train, fwd_oos = sp["fwd_train"], sp["fwd_oos"]
        t2 = sp["t2"]
        _set_phase(job_id, "数据准备")

        pmin = float(p.get("min", 1))
        pmax = float(p.get("max", 20))
        pstep = float(p.get("step", 1))
        is_int = bool(p.get("is_int", True))
        if pmin > pmax or pstep <= 0:
            raise RuntimeError("参数范围非法")
        # 先算术检查再物化网格：step 极小可产生上亿点（≈800MB 列表），
        # 物化前的 50 点上限检查必须用纯算术，避免内存耗尽
        n_raw = int((pmax - pmin) / pstep) + 1
        if n_raw > 50:
            raise RuntimeError(f"网格 {n_raw} 点超过上限 50，请增大 step 或收窄范围")
        vals = [pmin + i * pstep for i in range(n_raw)]
        if is_int:
            vals = sorted({round(v) for v in vals})
        if len(vals) > 50:
            raise RuntimeError(f"网格 {len(vals)} 点超过上限 50")

        def _ls_metrics(vals):
            """由因子值序列计算实操类指标（多空日收益 → 夏普/回撤/胜率）。

            P1-39 向量化：top/bottom 掩码全矩阵计算（单次 argsort×2 替代逐日
            python 循环）；语义与原逐列版逐位一致（NaN 列排最后，rank 覆盖
            双有限掩码）。
            """
            m = np.isfinite(vals) & np.isfinite(fwd_train)
            n_valid = m.sum(axis=0)
            # 显式 float32 的 inf/nan 参与 np.where，避免 dtype 提升产生全量 float64 拷贝
            ff = np.where(m, vals, np.float32(np.inf))
            order = np.argsort(np.argsort(ff, axis=0), axis=0)
            n_top = np.maximum(1, (n_valid * 0.2).astype(np.int64))
            top = order >= (n_valid - n_top)[None, :]
            bot = order < n_top[None, :]
            ls_ret = np.nanmean(
                np.where(top, fwd_train, np.float32(np.nan)), axis=0
            ) - np.nanmean(np.where(bot, fwd_train, np.float32(np.nan)), axis=0)
            ls_ret[n_valid < 10] = np.nan
            v = ls_ret[np.isfinite(ls_ret)]
            if len(v) < 10:
                return None
            daily = v / horizon if horizon else v  # h 日收益折算日等效
            sharpe = sharpe_ratio(daily)
            mdd = max_drawdown(daily)
            win = win_rate(daily)
            return {
                "ls_sharpe": round(sharpe, 3),
                "max_drawdown": round(mdd, 4),
                "win_rate": round(win, 4),
            }

        # P1-39: 节点级求值缓存——网格点共享子表达式（set_tunable_param 只改单个
        # 节点 params，其余子树 key 不变直接命中）；train/oos 分桶缓存，防止特征
        # 张量跨段串用
        memo_train: dict = {}
        memo_oos: dict = {}

        def score_of(rpn_i):
            vals = evaluate_rpn_memo(rpn_i, train, memo_train)
            ev = evaluate_factor(
                rpn_i, train, fwd_train, horizon=horizon, _memo=memo_train
            )
            oos_ev = evaluate_factor(
                rpn_i, oos, fwd_oos, horizon=horizon, _memo=memo_oos
            )
            ic = ev.get("ic")
            if ic is None:
                return None
            ics = ev.get("ic_series") or []
            icir = (
                float(np.mean(ics) / np.std(ics))
                if len(ics) > 1 and np.std(ics) > 0
                else None
            )
            ls = _ls_metrics(vals) or {}
            return {
                "train_ic": round(float(ic), 5),
                "rank_ic": round(float(ev.get("rank_ic") or 0), 5),
                "icir": round(icir, 4) if icir is not None else None,
                "ls_annual": round(float(ev.get("long_short_annual") or 0), 5),
                "stability": round(float(ev.get("stability") or 0), 4),
                "oos_ic": round(float(oos_ev.get("ic") or 0), 5),
                "turnover": round(float(ev.get("turnover") or 0), 4),
                **ls,
            }

        grid = []
        total = len(vals)

        def _score_point(v: float):
            # 协作控制点：暂停阻塞/取消抛错（worker 线程内，并行求值及时停摆）
            _checkpoint(job_id)
            try:
                rpn_i = set_tunable_param(rpn, pname, v)
                s = score_of(rpn_i)
                if s is not None:
                    s["param_value"] = v
                    return v, s
                return None
            except JobCancelled:
                raise
            except Exception:
                return None

        n_workers = _parallel_workers()
        _set_phase(job_id, "网格搜索")
        if backend_name() != "numpy" or n_workers <= 1:
            # mlx 后端 / 单线程回退：保持原串行语义（每网格点一个控制点 + 进度更新）
            for i, v in enumerate(vals):
                _checkpoint(job_id)
                r = _score_point(v)
                if r is not None:
                    grid.append(r[1])
                _update(job_id, progress=2 + 90 * (i + 1) / max(total, 1))
        else:
            throttle = _ProgressThrottle()
            done = 0
            picked: dict[float, dict] = {}
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(_score_point, v): v for v in vals}
                try:
                    for fut in as_completed(futures):
                        _checkpoint(job_id)  # 主线程控制点：暂停/取消立即生效
                        r = fut.result()
                        if r is not None:
                            picked[r[0]] = r[1]
                        done += 1
                        progress = 2 + 90 * done / max(total, 1)
                        if throttle.should_update(progress, force=(done == total)):
                            _update(job_id, progress=progress)
                except BaseException:
                    for f in futures:
                        f.cancel()
                    raise
            # 按参数值恢复串行顺序，保证与串行路径输出一致
            grid = [picked[v] for v in vals if v in picked]

        order = {
            "ic": "train_ic",
            "ic_abs": "train_ic",
            "icir": "icir",
            "rank_ic": "rank_ic",
            "ls_annual": "ls_annual",
            "sharpe": "ls_sharpe",
            "max_drawdown": "max_drawdown",
            "win_rate": "win_rate",
            "turnover": "turnover",
            "stability": "stability",
        }
        key = order.get(target, "train_ic")
        if target == "ic_abs":
            grid.sort(key=lambda x: abs(x.get("train_ic") or 0), reverse=True)
        elif target == "max_drawdown":
            # 回撤越小越好（min_drawdown 绝对值）
            grid.sort(key=lambda x: abs(x.get("max_drawdown") or 0))
        elif target == "composite":

            def _comp(x):
                ic = abs(x.get("train_ic") or 0)
                st = x.get("stability") or 0
                sh = x.get("ls_sharpe") or 0
                return ic * (0.4 + st * 0.3) + min(max(sh, -1), 1) * 0.3

            grid.sort(key=_comp, reverse=True)
        else:
            grid.sort(
                key=lambda x: x.get(key) if x.get(key) is not None else -1e9,
                reverse=True,
            )
        best = grid[0] if grid else None
        best_expr = None
        if best is not None:
            best_expr = expr_str(set_tunable_param(rpn, pname, best["param_value"]))
        _set_phase(job_id, "结果整理", 96.0)

        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={
                "expression": expression,
                "param": p,
                "grid": grid,
                "best": best,
                "best_expression": best_expr,
                "available_params": list(available),
                "target": target,
                "horizon": horizon,
                "meta": {
                    "stocks": panel["close"].shape[0],
                    "days": n_days,
                    "split": {"train": t2, "oos": n_days - t2},
                },
            },
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入
    except Exception as e:
        logger.exception("因子调优任务失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
