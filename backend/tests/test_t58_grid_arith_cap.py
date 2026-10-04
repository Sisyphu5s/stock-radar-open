"""T-58 factor_tune 网格物化前算术上限检查。

修复前 `vals = [pmin + i * pstep for i in range(int((pmax - pmin) / pstep) + 1)]`
先完整物化再检查 50 点上限——step 极小时列表可达上亿元素（≈800MB）直接 OOM。
修复后先用纯算术判断点数，超限立即拒绝，不做任何物化。
"""

from __future__ import annotations

import threading

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（worker 与查询走同一临时库）。"""
    import app.core.tasks.runner as Q

    engine = create_engine(
        f"sqlite:///{tmp_path / 'tune.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    monkeypatch.setattr(Q, "init_backend", lambda: None)
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()
    yield Maker
    engine.dispose()


def _install_tune_fakes(monkeypatch):
    """打桩 operators/evaluate/dataset（网格上限检查发生前的路径 + 正常完成路径）。"""
    import app.core.datasets as AD
    import app.lib.alpha.evaluate as AE
    import app.lib.alpha.operators as AO
    import app.core.tasks.runner as Q

    def fake_load_panel(ds_id, features=None):
        return {"panel": {"close": np.zeros((5, 40))}, "dates": ["2026-01-01"] * 40}

    def fake_compile_rpn(expr):
        return {"expr": expr}

    def fake_collect_tunable_params(rpn):
        return [{"name": "ts_mean.window"}]

    def fake_set_tunable_param(rpn, name, value):
        return {"expr": f"{rpn['expr']}-{value}"}

    def fake_evaluate_rpn(rpn_i, panel):
        return np.zeros((5, 32))

    def fake_evaluate_rpn_memo(rpn_i, panel, memo=None):
        return fake_evaluate_rpn(rpn_i, panel)

    def fake_evaluate_factor(rpn_i, panel, fwd, horizon=5, _memo=None):
        v = float(str(rpn_i["expr"]).rsplit("-", 1)[1])
        return {
            "ic": 0.01 * v,
            "rank_ic": 0.02 * v,
            "ic_series": [0.01] * 10,
            "long_short_annual": 0.1,
            "stability": 0.5,
            "turnover": 0.3,
        }

    def fake_forward_returns(close, horizon=5):
        return close

    def fake_expr_str(rpn_i):
        return str(rpn_i["expr"])

    def fake_stability(vals):
        return 0.5

    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AO, "collect_tunable_params", fake_collect_tunable_params)
    monkeypatch.setattr(AO, "set_tunable_param", fake_set_tunable_param)
    monkeypatch.setattr(AO, "evaluate_rpn", fake_evaluate_rpn)
    monkeypatch.setattr(AO, "evaluate_rpn_memo", fake_evaluate_rpn_memo)
    monkeypatch.setattr(AO, "expr_str", fake_expr_str)
    monkeypatch.setattr(AE, "evaluate_factor", fake_evaluate_factor)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(AE, "stability", fake_stability)
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "load_panel", fake_load_panel)


def _run_tune_job(maker, param_range) -> dict:
    import app.core.tasks.runner as Q

    params = {
        "dataset_id": 1,
        "expression": "ts_mean(close,5)",
        "target": "ic",
        "horizon": 5,
        "param": {"name": "ts_mean.window", **param_range},
    }
    db = maker()
    job = ExperimentJob(job_type="factor_tune", params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    t = threading.Thread(target=Q._run_factor_tune, args=(jid, params))
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "任务线程未及时退出（疑似仍在物化网格）"

    db = maker()
    row = db.get(ExperimentJob, jid)
    db.close()
    return {"status": row.status, "error": row.error, "result": row.result}


def test_factor_tune_tiny_step_rejected_before_materialization(temp_db, monkeypatch):
    """step=1e-7 网格点数 1e14：修复前物化即 OOM；修复后算术检查立即 failed。"""
    _install_tune_fakes(monkeypatch)
    out = _run_tune_job(temp_db, {"min": 0, "max": 1e7, "step": 1e-7, "is_int": False})
    assert out["status"] == "failed"
    assert "超过上限 50" in out["error"]


def test_factor_tune_oversize_grid_rejected(temp_db, monkeypatch):
    """常规步长但点数超 50：同样拒绝（不误伤边界内的网格）。"""
    _install_tune_fakes(monkeypatch)
    out = _run_tune_job(temp_db, {"min": 0, "max": 100, "step": 1, "is_int": True})
    assert out["status"] == "failed"
    assert "超过上限 50" in out["error"]


def test_factor_tune_valid_grid_completes(temp_db, monkeypatch):
    """合法网格（≤50 点）正常跑完：预检查不误伤。"""
    _install_tune_fakes(monkeypatch)
    out = _run_tune_job(temp_db, {"min": 1, "max": 5, "step": 1, "is_int": True})
    assert out["status"] == "done", out["error"]
    grid = out["result"]["grid"]
    assert len(grid) == 5
    assert sorted(g["param_value"] for g in grid) == [1, 2, 3, 4, 5]
