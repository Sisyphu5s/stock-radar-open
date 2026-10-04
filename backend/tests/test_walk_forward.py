"""T-03 Walk-forward 滚动验证测试。

覆盖:
- walk_forward_split 窗边界(anchored 等分切分/clamp);
- run_walk_forward 输出结构、每窗 IC 与汇总、OOS 拼接序列;
- 空窗(段过短)与 NaN 边界不抛错;
- handler 端到端(monkeypatch load_panel)→ done,result 结构;
- 任务注册表与 API 白名单含 walk_forward。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.lib.alpha.walk_forward import run_walk_forward, walk_forward_split
from app.lib.alpha.operators import compile_rpn
from app.lib.alpha import backend as _B
from app.storage.models import ExperimentJob  # noqa: F401  导入即注册 models 到 Base(create_all 建表)


@pytest.fixture(autouse=True)
def _init_backend():
    _B.init_backend()


# ---------------------------------------------------------------------------
# 窗切分
# ---------------------------------------------------------------------------


def test_split_even_boundaries():
    assert walk_forward_split(100, 3) == [(0, 33), (33, 66), (66, 100)]
    assert walk_forward_split(90, 2) == [(0, 45), (45, 90)]
    # 连续不重叠
    splits = walk_forward_split(120, 4)
    assert splits == [(0, 30), (30, 60), (60, 90), (90, 120)]


def test_split_clamp():
    assert len(walk_forward_split(100, 1)) == 2  # clamp 下界 2
    assert len(walk_forward_split(100, 99)) == 10  # clamp 上界 10
    assert walk_forward_split(100, 3)[0][1] < 100


# ---------------------------------------------------------------------------
# 核心计算
# ---------------------------------------------------------------------------


def _panel(S=50, T=120, seed=3):
    rng = np.random.RandomState(seed)
    rets = rng.randn(S, T) * 0.02
    close = np.cumprod(1.0 + rets, axis=1) * 100.0
    return {
        "close": close.astype(np.float32),
        "volume": (rng.rand(S, T) * 1e6 + 1e5).astype(np.float32),
    }


def _rpn():
    """简单表达式:rank(5 日动量)(compile_rpn 出扁平 RPN 后缀式)。"""
    return compile_rpn("rank(ts_mean(close,5) - ts_delay(close,5))")


def test_run_walk_forward_structure():
    panel = _panel()
    out = run_walk_forward(
        _rpn(), panel, horizon=5, n_windows=3, dates=[f"D{i}" for i in range(120)]
    )
    assert out["n_windows"] == 3
    assert out["horizon"] == 5
    assert len(out["windows"]) == 3
    for w in out["windows"]:
        assert set(w) == {
            "window",
            "oos_start",
            "oos_end",
            "ic",
            "stability",
            "ic_positive_ratio",
            "n_days",
        }
        assert w["window"] in (1, 2, 3)
        assert w["ic"] is None or (np.isfinite(w["ic"]) and -1 <= w["ic"] <= 1)
        assert w["n_days"] > 0
    # 拼接 OOS 序列:总长度 = 各窗有效期数之和,与日期轴一致
    assert len(out["oos_ic_series"]) == sum(w["n_days"] for w in out["windows"])
    assert len(out["oos_dates"]) == len(out["oos_ic_series"])
    assert all(np.isfinite(v) for v in out["oos_ic_series"])
    # 汇总
    s = out["summary"]
    assert s["mean_ic"] is not None and np.isfinite(s["mean_ic"])
    assert 0 <= s["stability"] <= 1
    assert s["n_days"] == len(out["oos_ic_series"])
    # 各窗 IC 按有效期数加权平均 = 拼接序列均值(同口径)
    w_ics = [(w["ic"], w["n_days"]) for w in out["windows"] if w["ic"] is not None]
    wmean = sum(ic * n for ic, n in w_ics) / sum(n for _, n in w_ics)
    assert abs(wmean - s["mean_ic"]) < 1e-12


def test_run_walk_forward_tiny_panel_no_crash():
    """极小面板(窗内不足 2 列)→ 空窗,不抛错。"""
    panel = {"close": np.ones((10, 4), dtype=np.float32)}
    out = run_walk_forward(compile_rpn("close"), panel, horizon=2, n_windows=3)
    assert out["n_windows"] == 3
    assert out["summary"]["mean_ic"] is None  # 无有效 IC


def test_run_walk_forward_nan_factor():
    """因子全 NaN(退化)→ 各窗 IC None,汇总 None,不抛错。"""
    panel = _panel()
    out = run_walk_forward(compile_rpn("close/close"), panel, horizon=5, n_windows=3)
    assert out["n_windows"] == 3


def test_run_walk_forward_reuses_evaluate_ic_path():
    """与 evaluate.ic_series 口径一致:每窗 OOS IC = 该段独立 ic_series 的均值。

    每窗 OOS 段独立 forward_returns(窗尾部 horizon NaN 边界与整段不同),故不做
    整段均值相等断言;验证单窗均值与独立 ic_series 逐位一致(复用同一计算路径)。
    """
    from app.lib.alpha.evaluate import evaluate_rpn, forward_returns, ic_series

    panel = _panel()
    f = evaluate_rpn(_rpn(), panel)
    out = run_walk_forward(_rpn(), panel, horizon=5, n_windows=2)
    assert out["n_windows"] == 2
    # 窗 2(后半段):独立 ic_series 有效均值 == run_walk_forward 窗 2 的 ic
    seg_fwd = forward_returns(panel["close"][:, 60:], 5)
    seg_ics = ic_series(f[:, 60:], seg_fwd)
    seg_valid = seg_ics[np.isfinite(seg_ics)]
    assert out["windows"][1]["ic"] == pytest.approx(float(seg_valid.mean()))
    assert out["windows"][1]["n_days"] == len(seg_valid)
    # 拼接序列 == 各窗 ic 序列的逐段拼接(元素级一致)
    seg1_fwd = forward_returns(panel["close"][:, :60], 5)
    seg1_ics = ic_series(f[:, :60], seg1_fwd)
    joined = [round(v, 6) for s in (seg1_ics, seg_ics) for v in s if np.isfinite(v)]
    np.testing.assert_allclose(np.asarray(out["oos_ic_series"]), joined, rtol=1e-9)


# ---------------------------------------------------------------------------
# handler 端到端 + 注册表 + 白名单
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite:替换 runner.SessionLocal(worker 与查询同一临时库)。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    from app.storage.db import Base

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.tasks.runner as Q

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()
    yield Maker
    engine.dispose()


def _install_panel_fake(monkeypatch, panel):
    import app.core.tasks.runner as Q

    monkeypatch.setattr(
        Q,
        "load_panel",
        lambda ds_id, features=None: {
            "panel": panel,
            "dates": [str(i) for i in range(panel["close"].shape[1])],
        },
    )


def _spawn_job(maker, params=None, job_type="walk_forward") -> int:
    from app.storage.models import ExperimentJob

    db = maker()
    job = ExperimentJob(job_type=job_type, params=params or {}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


def test_walk_forward_job_done(temp_db, monkeypatch):
    """walk_forward 任务端到端:_run_walk_forward → done,result 含 walk_forward 结构。"""
    import app.core.tasks.runner as Q

    _install_panel_fake(monkeypatch, _panel())
    params = {
        "dataset_id": 1,
        "expression": "rank(close)",
        "horizon": 5,
        "n_windows": 3,
    }
    jid = _spawn_job(temp_db, params)
    Q._run_walk_forward(
        jid, params
    )  # 注册表分派路径由 test_registry_and_whitelist 覆盖

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    assert set(row.result) == {"walk_forward", "meta"}
    wf = row.result["walk_forward"]
    assert wf["n_windows"] == 3
    assert len(wf["windows"]) == 3
    assert wf["summary"]["n_days"] == len(wf["oos_ic_series"])
    assert row.result["meta"]["stocks"] == 50


def test_walk_forward_job_failed_bad_expression(temp_db, monkeypatch):
    """非法表达式 → failed(compile_rpn 抛错),错误透传。"""
    import app.core.tasks.runner as Q

    _install_panel_fake(monkeypatch, _panel())
    params = {"dataset_id": 1, "expression": "??not-an-expr??", "horizon": 5}
    jid = _spawn_job(temp_db, params)
    Q._run_walk_forward(jid, params)

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "failed"


def test_registry_and_whitelist(monkeypatch):
    """registry 含 walk_forward 处理器;API 白名单接受 walk_forward。"""
    import app.core.tasks.runner  # noqa: F401  导入即触发 registry 注册(模块末尾循环)
    from app.core.tasks import registry

    assert "walk_forward" in registry.HANDLERS
    import app.api.experiments as E

    monkeypatch.setattr(E, "submit", lambda job_type, params: 99)
    assert E.create_experiment({"job_type": "walk_forward", "params": {}}) == {
        "job_id": 99,
        "status": "pending",
    }
    with pytest.raises(Exception):
        E.create_experiment({"job_type": "not_a_job_type", "params": {}})
    with pytest.raises(Exception):
        E.create_experiment({"job_type": "not_a_job_type", "params": {}})
