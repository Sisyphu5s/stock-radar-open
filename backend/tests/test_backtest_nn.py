"""回测任务支持 NN 因子（model_id）测试：checkpoint 端到端预测因子走回测流水线、
model_id 异常路径（不存在/非数值/checkpoint 缺失）、无 model_id 时现有 expression 路径回归。

模式参照 test_neural_train_job.py（monkeypatch load_panel / 临时 SQLite 库）；
NN 因子端到端真实调用 load_checkpoint + predict_panel：权重由 train_mlp 在
60×100 小数据训 3 epoch 的产物，checkpoint 落盘到 tmp 目录（monkeypatch
nn._MODEL_DIR），load_panel 的 close/volume 与训练数据同源。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, NNModel
from app.lib.alpha import nn


def _make_panel(S=60, T=100, seed=7):
    """面板数据（close/volume 同源随机游走，供训练与回测共用）。"""
    rng = np.random.RandomState(seed)
    rets = rng.randn(S, T) * 0.02
    close = np.cumprod(1.0 + rets, axis=1)
    close = close / close[:, :1] * 100.0
    volume = rng.rand(S, T) * 1e6 + 1e5
    return {
        "close": close.astype(np.float32),
        "volume": volume.astype(np.float32),
    }


def _make_fwd(panel, horizon=5):
    fwd = np.full_like(panel["close"], np.nan, dtype=np.float32)
    fwd[:, :-horizon] = panel["close"][:, horizon:] / panel["close"][:, :-horizon] - 1.0
    return fwd


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite：替换 queue.SessionLocal（worker 与查询同一临时库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.tasks.runner as Q

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    # 注意：不 monkeypatch init_backend——真实计算路径（evaluate_rpn/回测）依赖
    # backend._T 张量模块初始化，no-op 会导致 _safe_div 等算子 AttributeError。

    # 清理跨测试残留的模块级状态
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


def _install_panel_fake(monkeypatch, panel):
    """打桩 queue.load_panel：返回固定面板与日期轴（features 参数为 P2-15 调用点新增）。"""
    import app.core.tasks.runner as Q

    monkeypatch.setattr(
        Q,
        "load_panel",
        lambda ds_id, features=None: {
            "panel": panel,
            "dates": [str(i) for i in range(panel["close"].shape[1])],
        },
    )


def _train_checkpoint(tmp_path, monkeypatch, panel):
    """train_mlp 在 60×100 小数据训 3 epoch，checkpoint 落盘 tmp 目录。

    强制 numpy 参考实现（monkeypatch _MLX_OK），返回 checkpoint 绝对路径。
    """
    monkeypatch.setattr(nn, "_MLX_OK", False)
    monkeypatch.setattr(nn, "_MODEL_DIR", str(tmp_path))
    data = {"close": panel["close"], "volume": panel["volume"]}
    fwd = _make_fwd(panel)
    out = nn.train_mlp(
        data_train=data,
        forward_returns=fwd,
        data_val=data,
        val_forward_returns=fwd,
        layers=[32, 16],
        activation="relu",
        lr=0.01,
        epochs=3,
        batch_size=256,
        seed=42,
        max_rows=20000,
    )
    assert out["checkpoint"] and out["checkpoint"].startswith(str(tmp_path))
    return out["checkpoint"]


def _seed_model(temp_db, **over) -> int:
    db = temp_db()
    m = NNModel(
        architecture={"layers": [32, 16], "activation": "relu"},
        dataset_id=1,
        horizon=5,
        epochs=3,
        train_ic=0.3,
        val_ic=0.1,
        train_loss=[0.1],
        val_loss=[0.2],
        **over,
    )
    if m.checkpoint is None:  # 未显式覆盖时用默认假路径
        m.checkpoint = "/tmp/fake.npz"
    db.add(m)
    db.commit()
    db.refresh(m)
    mid = m.id
    db.close()
    return mid


def _spawn_job(maker, params=None, job_type="backtest") -> int:
    db = maker()
    job = ExperimentJob(job_type=job_type, params=params or {}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


def _load(maker, jid):
    db = maker()
    row = db.get(ExperimentJob, jid)
    db.close()
    return row


# ---------------------------------------------------------------------------
# NN 因子全路径（真实 load_checkpoint + predict_panel + 真实回测）
# ---------------------------------------------------------------------------


def test_backtest_nn_done_full_path(temp_db, tmp_path, monkeypatch):
    """NN 因子端到端：checkpoint 加载 → predict_panel 出 (60,100) 因子 → 真实回测
    done；result 结构与现有 backtest 一致（含指标键）；阶段点与 expression 路径一致。"""
    import app.core.tasks.runner as Q

    panel = _make_panel()
    _install_panel_fake(monkeypatch, panel)
    ckpt = _train_checkpoint(tmp_path, monkeypatch, panel)
    mid = _seed_model(temp_db, checkpoint=ckpt)

    # 记录阶段点（模型有 phase 列，直接透传真实 _update）
    phases: list[str] = []
    real_update = Q._update

    def patched(job_id, **fields):
        if fields.get("phase"):
            phases.append(fields["phase"])
        return real_update(job_id, **fields)

    monkeypatch.setattr(Q, "_update", patched)

    params = {"dataset_id": 1, "model_id": mid, "horizon": 5}
    jid = _spawn_job(temp_db, params)
    Q._run_backtest(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done", row.error
    assert row.progress == 100.0
    # 阶段上报：与 expression 路径一致的数据准备/回测计算/结果整理
    assert phases[0] == "数据准备"
    assert "回测计算" in phases
    assert phases[-1] == "结果整理"
    assert row.phase == "结果整理"

    # result 结构与现有 backtest 一致（backtest 指标键齐全）
    assert set(row.result) == {"backtest", "meta"}
    assert row.result["meta"]["model_id"] == mid
    assert row.result["meta"]["stocks"] == 60
    assert row.result["meta"]["days"] == 100
    bt = row.result["backtest"]
    assert {"modes", "bench_nav", "bench_metrics", "params"} <= set(bt)
    assert [m["mode"] for m in bt["modes"]] == ["long_short", "long", "short"]
    for m in bt["modes"]:
        assert "combo_nav" in m and "risk" in m
        for k in ("annual_return", "sharpe", "max_drawdown", "win_rate"):
            assert k in m["metrics"]
    assert bt["params"]["top_pct"] == 0.2
    assert bt["params"]["trade_interval"] == 5
    assert bt["params"]["cost_rate"] == 0.001


def test_backtest_nn_factor_shape_captured(temp_db, tmp_path, monkeypatch):
    """run_backtest_multi 捕获参数：factor 为 (60,100)（与 close 面板同源）。

    queue._run_backtest 内为局部 import（from ..alpha.backtest import ...），
    故 monkeypatch alpha.backtest 模块属性而非 Q。
    """
    import app.lib.alpha.backtest as BT
    import app.core.tasks.runner as Q

    panel = _make_panel()
    _install_panel_fake(monkeypatch, panel)
    ckpt = _train_checkpoint(tmp_path, monkeypatch, panel)
    mid = _seed_model(temp_db, checkpoint=ckpt)

    captured: dict = {}

    def fake_multi(factor, close, fwd, **kw):
        captured["factor"] = factor
        captured["close"] = close
        captured["fwd"] = fwd
        captured["kw"] = kw
        return {
            "modes": [],
            "bench_nav": [],
            "bench_metrics": {},
            "params": {"top_pct": kw.get("top_pct")},
        }

    monkeypatch.setattr(BT, "run_backtest_multi", fake_multi)

    params = {"dataset_id": 1, "model_id": mid, "horizon": 5}
    jid = _spawn_job(temp_db, params)
    Q._run_backtest(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done", row.error
    assert captured["factor"].shape == (60, 100)
    assert captured["factor"].dtype == np.float32
    assert captured["factor"].shape == captured["close"].shape
    assert np.array_equal(captured["close"], panel["close"])
    # 预测因子非全 NaN：第 20 天（roll_mean 窗口后）起应为有限值
    assert np.isfinite(captured["factor"][:, 30:]).sum() > 0
    assert captured["kw"]["modes"] == ["long_short", "long", "short"]
    assert captured["kw"]["direction"] == "auto"


# ---------------------------------------------------------------------------
# model_id 异常路径
# ---------------------------------------------------------------------------


def test_backtest_nn_model_missing_fails(temp_db, monkeypatch):
    """model_id 不存在 → failed，error 含「不存在」。"""
    import app.core.tasks.runner as Q

    _install_panel_fake(monkeypatch, _make_panel())
    params = {"dataset_id": 1, "model_id": 9999}
    jid = _spawn_job(temp_db, params)

    Q._run_backtest(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "failed"
    assert "不存在" in row.error


def test_backtest_nn_bad_model_id_fails(temp_db, monkeypatch):
    """model_id 非数值 → failed（与现有参数校验风格一致）。"""
    import app.core.tasks.runner as Q

    _install_panel_fake(monkeypatch, _make_panel())
    params = {"dataset_id": 1, "model_id": "abc"}
    jid = _spawn_job(temp_db, params)

    Q._run_backtest(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "failed"
    assert "abc" in row.error


def test_backtest_nn_checkpoint_missing_fails(temp_db, tmp_path, monkeypatch):
    """checkpoint 缺失 → load_checkpoint 抛 ValueError → failed。"""
    import app.core.tasks.runner as Q

    _install_panel_fake(monkeypatch, _make_panel())
    mid = _seed_model(temp_db, checkpoint=str(tmp_path / "nope.npz"))
    params = {"dataset_id": 1, "model_id": mid}
    jid = _spawn_job(temp_db, params)

    Q._run_backtest(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "failed"
    assert "checkpoint" in row.error


# ---------------------------------------------------------------------------
# 无 model_id 回归：现有 expression 路径不被破坏
# ---------------------------------------------------------------------------


def test_backtest_without_model_id_regression(temp_db, monkeypatch):
    """无 model_id → 现有 expression 流程（compile + evaluate + 真实回测）照常，
    result.meta 不带 model_id 键。"""
    import app.core.tasks.runner as Q

    panel = _make_panel()
    _install_panel_fake(monkeypatch, panel)
    params = {"dataset_id": 1, "expression": "close/volume", "horizon": 5}
    jid = _spawn_job(temp_db, params)

    Q._run_backtest(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done", row.error
    assert row.result["meta"]["stocks"] == 60
    assert "model_id" not in row.result["meta"]
    bt = row.result["backtest"]
    assert bt["modes"] and bt["modes"][0]["mode"] == "long_short"
    assert bt["bench_nav"]


# ---------------------------------------------------------------------------
# P1-38：方向探测 horizon 参数化（fwd_ret 生效）+ 多 mode 基准共享
# ---------------------------------------------------------------------------


def _sine_panel(S=200, T=1000):
    """周期 20 列的正弦价格面板：每股相位均匀错开。

    用于构造方向翻转因子（见 test_probe_direction_horizon_flips_direction）。
    """
    phases = np.linspace(0, 2 * np.pi, S, endpoint=False)[:, None]
    theta = (2 * np.pi * np.arange(T) / 20)[None, :]
    return (100.0 + 2.0 * np.sin(theta + phases)).astype(np.float64)


def test_probe_direction_reads_fwd_ret_columns(monkeypatch):
    """P1-38: _probe_direction 逐期收益取自 fwd_ret[:, t]（h 日前向收益），
    而非硬编码 close[:, t+5]——spy 验证每个采样点 y 与 fwd_ret 对应列一致。"""
    import app.lib.alpha.backtest as BT
    from app.lib.alpha.evaluate import forward_returns

    rng = np.random.RandomState(7)
    S, T = 40, 300
    close = np.cumprod(1.0 + rng.randn(S, T) * 0.01, axis=1)
    f = rng.randn(S, T)
    fwd = forward_returns(close, 10)
    real = BT.spearman_corr
    seen: list[tuple[int, np.ndarray]] = []

    def spy(x, y):
        seen.append((len(seen), y.copy()))
        return real(x, y)

    monkeypatch.setattr(BT, "spearman_corr", spy)
    d = BT._probe_direction(f, fwd, T)
    assert d in (1, -1)
    assert len(seen) > 0
    # 探测采样 t = 30 + 5*i（horizon=10 → 上界 min(T-10, 260)=260）
    for i, (_, y) in enumerate(seen):
        t = 30 + 5 * i
        np.testing.assert_allclose(y, fwd[:, t], equal_nan=True)


def test_probe_direction_horizon_flips_direction():
    """P1-38: 同一因子在不同 horizon 的 fwd_ret 下方向相反（horizon 真正生效）。

    因子 = 5 日前向动量 + 0.75×(10 日反转)：截面相关 corr(f,fwd5)≈+0.32、
    corr(f,fwd10)≈-0.45（周期 20 正弦面板，S=200 使误差 ~1e-2，30σ 以上稳健）。
    """
    from app.lib.alpha.backtest import _probe_direction
    from app.lib.alpha.evaluate import forward_returns

    close = _sine_panel()
    T = close.shape[1]
    f = np.full_like(close, np.nan)
    f[:, :-5] = close[:, 5:] - close[:, :-5]
    f[:, :-10] += 0.75 * (close[:, :-10] - close[:, 10:])

    fwd10 = forward_returns(close, 10)
    fwd5 = forward_returns(close, 5)
    assert _probe_direction(f, fwd10, T) == -1  # 与 10 日收益反向
    assert _probe_direction(f, fwd5, T) == 1  # 与 5 日收益同向
    # 旧实现硬编码 5 日收益：无论传哪个 fwd 都返回 fwd5 语义的结果
    assert _probe_direction(f, fwd10, T) != _probe_direction(f, fwd5, T)


def test_run_backtest_forwards_fwd_ret_to_probe(monkeypatch):
    """P1-38: run_backtest 把 fwd_ret（而非 close）传入方向探测——
    修复前 fwd_ret 是死参数，方向由硬编码 5 日收益决定。"""
    import app.lib.alpha.backtest as BT
    from app.lib.alpha.evaluate import forward_returns

    rng = np.random.RandomState(9)
    S, T = 60, 300
    close = np.cumprod(1.0 + rng.randn(S, T) * 0.01, axis=1)
    fwd = forward_returns(close, 10)
    f = rng.randn(S, T)
    seen: dict = {}

    def spy(factor, fwd_ret, n_t, horizon=None):
        seen["fwd"] = fwd_ret
        seen["n_t"] = n_t
        return 1

    monkeypatch.setattr(BT, "_probe_direction", spy)
    BT.run_backtest(f, close, fwd, direction="auto")
    assert seen["fwd"] is fwd  # 同一对象：方向探测消费传入的 fwd_ret
    assert seen["n_t"] == T


def test_run_backtest_multi_bench_computed_once(monkeypatch):
    """P1-38: run_backtest_multi 基准/日收益只算一次（此前 3 个 mode 各重算一遍）。"""
    import app.lib.alpha.backtest as BT
    from app.lib.alpha.evaluate import forward_returns

    rng = np.random.RandomState(3)
    S, T = 30, 120
    close = np.cumprod(1.0 + rng.randn(S, T) * 0.01, axis=1)
    fwd = forward_returns(close, 5)
    f = rng.randn(S, T)
    calls = {"n": 0}
    real = BT._daily_and_bench

    def spy(c):
        calls["n"] += 1
        return real(c)

    monkeypatch.setattr(BT, "_daily_and_bench", spy)
    out = BT.run_backtest_multi(f, close, fwd, modes=["long_short", "long", "short"])
    assert calls["n"] == 1
    assert [m["mode"] for m in out["modes"]] == ["long_short", "long", "short"]
