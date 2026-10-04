"""P1-59 回测优化前视偏差测试:训练/回测窗口严格分离。

覆盖:
- 纯函数 _fixed_weights_target:选股只用训练窗口信号(回测窗口信号即使
  相反也不影响 top_idx);协方差/均值只取训练窗口尾部——修改回测窗口
  收益完全不影响权重,修改训练窗口收益改变权重;
- train_end 守卫(train_end < 2 拒绝);
- handler 端到端(_run_backtest_opt):train_ratio 划分窗口、回测净值与
  日期只从回测窗口开始、optimize 绩效只反映回测窗口(构造「训练涨/回测跌
  被选中 vs 训练跌/回测涨未被选中」,断言组合绩效为负——旧代码遍历全样本
  会选中回测暴涨的股票得到正收益,即前视虚高)。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# runner 模块末尾转发 handlers(import 顺序依赖:必须先行完整初始化 runner,
# 否则直接 import backtest_opt 触发循环导入)
import app.core.tasks.runner  # noqa: F401
from app.core.tasks.handlers.backtest_opt import _fixed_weights_target  # noqa: E402


# ---------------------------------------------------------------------------
# 纯函数:训练窗口前视隔离
# ---------------------------------------------------------------------------


def _base_series(S=6, T=150, seed=3):
    """S 只股票 × T 天。

    信号:0..2 训练窗口内高(5.0)、3..5 低(1.0);回测窗口信号相反
    (3..5 高、0..2 低)——若选股遍历全样本会选中 3..5,正确实现选 0..2。
    收益:0..2 训练窗口 +1%/+0.5%/+0.2%(均值有区分,协方差非退化)、
    3..5 -1%;回测窗口 0..2 跌、3..5 涨。
    train_ratio=0.7 → train_end=105。
    """
    rng = np.random.RandomState(seed)
    train_end = int(T * 0.7)  # 105
    factor = np.full((S, T), np.nan)
    factor[0:3, :train_end] = 5.0
    factor[3:6, :train_end] = 1.0
    factor[0:3, train_end:] = 1.0
    factor[3:6, train_end:] = 5.0
    daily_ret = rng.normal(0.0, 0.001, size=(S, T))
    daily_ret[0:3, :train_end] += np.array([0.01, 0.005, 0.002])[:, None]
    daily_ret[3:6, :train_end] -= 0.01
    daily_ret[0:3, train_end:] -= 0.02
    daily_ret[3:6, train_end:] += 0.02
    return factor, daily_ret, train_end


def test_fixed_weights_selection_ignores_backtest_signal():
    """选股只遍历训练窗口:回测窗口信号相反也不改变 top_idx。"""
    factor, daily_ret, train_end = _base_series()
    _, top_idx = _fixed_weights_target(
        factor,
        daily_ret,
        method="mv",
        max_weight=0.5,
        top_n=3,
        horizon=5,
        risk_aversion=1.0,
        train_end=train_end,
    )
    assert set(top_idx.tolist()) == {0, 1, 2}  # 未选中回测窗口高信号股票


def test_backtest_window_returns_do_not_affect_weights():
    """未来(回测窗口)收益不影响训练窗口权重:协方差/均值切片截止 train_end。"""
    factor, daily_ret, train_end = _base_series()
    w_a, top_idx = _fixed_weights_target(
        factor,
        daily_ret,
        method="mv",
        max_weight=0.5,
        top_n=3,
        horizon=5,
        risk_aversion=1.0,
        train_end=train_end,
    )
    # 把回测窗口收益整体改写(极端值)→ 权重必须逐位不变
    ret_b = daily_ret.copy()
    ret_b[:, train_end:] = np.where(
        np.isfinite(factor[:, train_end:]),
        factor[:, train_end:] * 1e3,  # 若被误用,均值/协方差被彻底改变
        ret_b[:, train_end:],
    )
    w_b, top_idx_b = _fixed_weights_target(
        factor,
        ret_b,
        method="mv",
        max_weight=0.5,
        top_n=3,
        horizon=5,
        risk_aversion=1.0,
        train_end=train_end,
    )
    assert np.array_equal(top_idx, top_idx_b)
    assert np.allclose(w_a, w_b, atol=1e-12)
    assert set(top_idx_b.tolist()) == {0, 1, 2}


def test_training_window_returns_do_affect_weights():
    """训练窗口收益改变 → 权重改变(协方差/均值确实取自训练窗口)。"""
    factor, daily_ret, train_end = _base_series()
    w_a, _ = _fixed_weights_target(
        factor,
        daily_ret,
        method="mv",
        max_weight=0.5,
        top_n=3,
        horizon=5,
        risk_aversion=1.0,
        train_end=train_end,
    )
    ret_c = daily_ret.copy()
    ret_c[0:3, :train_end] = -0.01  # 训练窗口均值/协方差反转
    ret_c[3:6, :train_end] = 0.01
    w_c, _ = _fixed_weights_target(
        factor,
        ret_c,
        method="mv",
        max_weight=0.5,
        top_n=3,
        horizon=5,
        risk_aversion=1.0,
        train_end=train_end,
    )
    assert not np.allclose(w_a, w_c, atol=1e-9)


def test_fixed_weights_target_train_end_guard():
    """train_end < 2 必须拒绝(训练窗口不可用)。"""
    factor, daily_ret, _ = _base_series()
    with pytest.raises(ValueError):
        _fixed_weights_target(
            factor,
            daily_ret,
            method="mv",
            max_weight=0.5,
            top_n=3,
            horizon=5,
            risk_aversion=1.0,
            train_end=1,
        )


def test_fixed_weights_target_train_end_must_leave_backtest_window():
    """train_end 必须 < n_t(无回测窗口则拒绝,防静默退化全样本前视)。"""
    factor, daily_ret, train_end = _base_series()
    with pytest.raises(ValueError):
        _fixed_weights_target(
            factor,
            daily_ret,
            method="mv",
            max_weight=0.5,
            top_n=3,
            horizon=5,
            risk_aversion=1.0,
            train_end=train_end + 1000,  # 超样本长度 → 拒绝而非 clamp 全样本
        )
    with pytest.raises(ValueError):
        _fixed_weights_target(
            factor,
            daily_ret,
            method="mv",
            max_weight=0.5,
            top_n=3,
            horizon=5,
            risk_aversion=1.0,
            train_end=150,  # 等于 n_t → 无回测窗口
        )


# ---------------------------------------------------------------------------
# handler 端到端:窗口划分 + 样本外回测(monkeypatch load_panel)
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite:替换 runner.SessionLocal(worker 与查询同一临时库)。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    from app.storage.db import Base
    from app.storage.models import ExperimentJob  # noqa: F401  注册 models 建表

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


def _oos_panel(S=8, T=160, seed=7):
    """训练窗口:前 4 只涨(+1%/天)后 4 只跌;回测窗口:前 4 只跌后 4 只涨。

    rank(close) 在训练窗口末端选中前 4 只;若选股看了全样本(回测窗口
    后 4 只涨)→ 会选中后 4 只得到正绩效——正确实现选前 4 只 → 绩效为负。
    """
    rng = np.random.RandomState(seed)
    train_end = int(T * 0.7)  # 112
    rets = np.zeros((S, T))
    rets[0:4, :train_end] = 0.01
    rets[4:8, :train_end] = -0.01
    rets[0:4, train_end:] = -0.02
    rets[4:8, train_end:] = 0.02
    close = np.cumprod(1.0 + rets, axis=1) * 100.0
    open_ = close * (1.0 + rng.randn(S, T) * 0.005)
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    volume = rng.rand(S, T) * 1e6 + 1e5
    return {
        "close": close.astype(np.float64),
        "open": open_.astype(np.float64),
        "high": high.astype(np.float64),
        "low": low.astype(np.float64),
        "volume": volume.astype(np.float64),
    }


def _install_panel_fake(monkeypatch, panel):
    import app.core.tasks.runner as Q

    T = panel["close"].shape[1]
    monkeypatch.setattr(
        Q,
        "load_panel",
        lambda ds_id, features=None: {
            "panel": panel,
            "dates": [f"d{i:04d}" for i in range(T)],
            "codes": [f"{600000 + i}.SH" for i in range(panel["close"].shape[0])],
        },
    )


def _spawn_job(maker, params):
    from app.storage.models import ExperimentJob

    db = maker()
    job = ExperimentJob(job_type="backtest_opt", params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


def test_handler_oos_window_and_negative_perf(temp_db, monkeypatch):
    """端到端:回测只跑回测窗口,组合绩效为负(未选中回测暴涨股票)。"""
    import app.core.tasks.runner as Q

    panel = _oos_panel()
    _install_panel_fake(monkeypatch, panel)
    params = {
        "dataset_id": 1,
        "expression": "rank(close)",
        "method": "mv",
        "max_weight": 0.5,
        "top_n": 4,
        "horizon": 5,
        "trade_interval": 5,
        "train_ratio": 0.7,
    }
    jid = _spawn_job(temp_db, params)
    Q._run_backtest_opt(jid, params)

    from app.storage.models import ExperimentJob

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    bt = row.result["backtest"]
    meta = row.result["meta"]

    # 窗口划分:160 天 × 0.7 → train_end=112,回测 48 天
    assert bt["params"]["train_ratio"] == pytest.approx(0.7)
    assert bt["params"]["train_end"] == 112
    assert meta["train_end"] == 112
    assert meta["days"] == 48
    assert meta["stocks"] == 8
    # 日期轴从回测窗口起点开始,与净值对齐
    assert meta["dates"][0] == "d0112"
    nav = bt["modes"][0]["combo_nav"]
    assert len(meta["dates"]) == len(nav)
    # 选中了训练窗口涨幅领先的前 4 只,而它们在回测窗口下跌 → 样本外绩效为负
    assert bt["modes"][0]["metrics"]["annual_return"] < 0
    assert bt["modes"][0]["metrics"]["trading_days"] == pytest.approx(48)
    # optimize 绩效同样只反映回测窗口
    assert bt["params"]["optimize"]["perf"]["annual_return"] < 0
    # 选股成员:前 4 只(训练窗口高 close)
    assert len(bt["params"]["weights"]) == 4
    assert all(item["code"].startswith("60000") for item in bt["params"]["weights"])


def test_handler_train_ratio_param(temp_db, monkeypatch):
    """train_ratio 参数生效:0.5 → train_end=80,回测 80 天。"""
    import app.core.tasks.runner as Q

    panel = _oos_panel()
    _install_panel_fake(monkeypatch, panel)
    params = {
        "dataset_id": 1,
        "expression": "rank(close)",
        "method": "min_var",
        "max_weight": 0.5,
        "top_n": 4,
        "train_ratio": 0.5,
    }
    jid = _spawn_job(temp_db, params)
    Q._run_backtest_opt(jid, params)

    from app.storage.models import ExperimentJob

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    bt = row.result["backtest"]
    meta = row.result["meta"]
    assert bt["params"]["train_ratio"] == pytest.approx(0.5)
    assert bt["params"]["train_end"] == 80
    assert meta["days"] == 80
    assert meta["dates"][0] == "d0080"
    assert bt["modes"][0]["metrics"]["trading_days"] == pytest.approx(80)


def test_handler_default_train_ratio(temp_db, monkeypatch):
    """缺省 train_ratio → 0.7(默认前 70% 训练)。"""
    import app.core.tasks.runner as Q

    panel = _oos_panel()
    _install_panel_fake(monkeypatch, panel)
    params = {
        "dataset_id": 1,
        "expression": "rank(close)",
        "method": "min_var",
        "max_weight": 0.5,
        "top_n": 4,
    }
    jid = _spawn_job(temp_db, params)
    Q._run_backtest_opt(jid, params)

    from app.storage.models import ExperimentJob

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    assert row.result["backtest"]["params"]["train_ratio"] == pytest.approx(0.7)
