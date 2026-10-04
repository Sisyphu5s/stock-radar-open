"""T-58 DQN 个股仓位模型测试：奖励纯函数 / 参数校验 / mlx 可用性守卫 / 小面板短训练
端到端 / checkpoint 往返 / handler 接入（done + meta + 落库 / 校验失败 / 数据集缺失）。

lib 层测试 mlx 不可用路径用 monkeypatch dqn._MLX_OK 模拟（本机 Apple Silicon 实测
mlx 0.32 可用，真实训练路径仍按可用性守卫执行）；handler 端到端真实跑小训练
（3 股 × 40 天、2 episode），与 test_neural_train_job 同源打桩 load_panel。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.lib.alpha import dqn
from app.lib.alpha.dqn import compute_reward, train_dqn
from app.storage.db import Base
from app.storage.models import ExperimentJob, NNModel

_DEFAULT_COST = 0.001


# ---------------------------------------------------------------------------
# 通用 fixtures / 工具
# ---------------------------------------------------------------------------
@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 runner.SessionLocal（worker 与查询走同一临时库）。"""
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

    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


@pytest.fixture()
def tmp_model_dir(tmp_path, monkeypatch):
    """dqn checkpoint 落盘目录 → tmp（不写真 cache/models）。"""
    monkeypatch.setattr(dqn, "_MODEL_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def phase_capture(temp_db, monkeypatch):
    """包装 Q._update：剥离 phase 字段（模型无该列，直接写会 CompileError）。"""
    import app.core.tasks.runner as Q

    phases: list[str] = []
    real_update = Q._update

    def patched(job_id, **fields):
        if "phase" in fields:
            phases.append(fields["phase"])
            fields = {k: v for k, v in fields.items() if k != "phase"}
        return real_update(job_id, **fields)

    monkeypatch.setattr(Q, "_update", patched)
    return phases


def _make_panel(S=3, T=40, seed=7):
    """随机游走 close + 随机 volume 构造小面板（float32，与面板加载一致）。"""
    rng = np.random.RandomState(seed)
    rets = rng.randn(S, T) * 0.02
    close = np.cumprod(1.0 + rets, axis=1)
    close = close / close[:, :1] * 100.0
    volume = rng.rand(S, T) * 1e6 + 1e5
    return {
        "close": close.astype(np.float32),
        "volume": volume.astype(np.float32),
    }


def _spawn_job(maker, params=None, status="pending") -> int:
    db = maker()
    job = ExperimentJob(job_type="rl_train", params=params or {}, status=status)
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
# 奖励纯函数（pos 变化成本）
# ---------------------------------------------------------------------------
def test_compute_reward():
    """r = pos·ret − cost·|pos−pos_prev|：持仓收益 + 换仓成本惩罚。"""
    # 持仓不变：只有持仓收益
    assert compute_reward(1.0, 1.0, 0.01, _DEFAULT_COST) == pytest.approx(0.01)
    # 空仓：无收益无成本
    assert compute_reward(0.0, 0.0, 0.01, _DEFAULT_COST) == 0.0
    # 半仓→满仓（成本 0.001×0.5）：收益 0.02×1.0 − 0.0005
    assert compute_reward(0.5, 1.0, 0.02, _DEFAULT_COST) == pytest.approx(0.02 - 0.0005)
    # 满仓→空仓：负收益部分也按变化计成本
    assert compute_reward(1.0, 0.0, -0.01, _DEFAULT_COST) == pytest.approx(-0.001)
    # 空仓→满仓：成本 = cost × 1.0
    assert compute_reward(0.0, 1.0, 0.0, _DEFAULT_COST) == pytest.approx(-0.001)


def test_position_mapping_is_three_buckets():
    """动作空间三档：0 空仓 / 1 半仓 / 2 满仓（仓位比例 0/0.5/1.0）。"""
    assert dqn._ACTIONS == (0, 1, 2)
    assert dqn._POSITION == (0.0, 0.5, 1.0)
    assert dqn._N_ACTIONS == 3


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
def test_train_dqn_validation(tmp_model_dir):
    data = _make_panel()
    for kw, expect in [
        ({"seq_len": 1}, "seq_len"),
        ({"episodes": 0}, "episodes"),
        ({"episodes": 501}, "episodes"),
        ({"gamma": 1.0}, "gamma"),
        ({"gamma": -0.1}, "gamma"),
        ({"lr": 0.0}, "lr"),
        ({"cost": -0.1}, "cost"),
        ({"hidden": 0}, "hidden"),
        ({"batch_size": 0}, "batch_size"),
        ({"capacity": 0}, "capacity"),
        ({"target_sync_steps": 0}, "target_sync_steps"),
    ]:
        with pytest.raises(ValueError, match=expect):
            train_dqn(
                data_train=data, data_val=data, **{"seq_len": 5, "episodes": 2, **kw}
            )


def test_train_dqn_short_panel(tmp_model_dir):
    """时间轴不足 seq_len+1 → 明确报错。"""
    data = _make_panel(T=10)
    with pytest.raises(ValueError, match="时间轴不足"):
        train_dqn(data_train=data, data_val=data, seq_len=20, episodes=1)


# ---------------------------------------------------------------------------
# mlx 可用性守卫（不静默回退）
# ---------------------------------------------------------------------------
def test_train_dqn_requires_mlx(tmp_model_dir, monkeypatch):
    """mlx 不可用（monkeypatch _MLX_OK=False）→ ValueError 含「需要 MLX」。"""
    data = _make_panel()
    monkeypatch.setattr(dqn, "_MLX_OK", False)
    with pytest.raises(ValueError, match="需要 MLX"):
        train_dqn(data_train=data, data_val=data, seq_len=5, episodes=1)


# ---------------------------------------------------------------------------
# 端到端训练（mlx 可用时；2 episode 不收敛仅验证链路与 meta 结构）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not dqn._MLX_OK, reason="需要 MLX（当前环境 mlx 不可用）")
def test_train_dqn_small_panel_end_to_end(tmp_model_dir):
    """小面板（3 股 × 40 天）短训练（2 episode）跑通 + meta 结构 + checkpoint 往返。"""
    data = _make_panel()
    out = train_dqn(
        data_train=data,
        data_val=data,
        seq_len=5,
        episodes=2,
        seed=42,
        capacity=500,
        batch_size=16,
        progress_cb=lambda *a: None,
    )
    assert out["model_kind"] == "dqn"
    assert out["episodes"] == 2
    assert len(out["train_rewards"]) == 2
    assert all(np.isfinite(v) for v in out["train_rewards"])
    ev = out["eval"]
    for k in ("equity", "total_return", "sharpe", "max_drawdown", "trades", "avg_pos"):
        assert k in ev, f"eval 缺字段 {k}"
    assert len(ev["equity"]) > 0 and np.isfinite(ev["total_return"])
    assert isinstance(ev["trades"], int) and 0 <= ev["avg_pos"] <= 1.0

    # checkpoint 往返：扁平参数 + arch/arch_hparams + 特征统计
    meta = dqn.load_checkpoint(out["checkpoint"])
    assert meta["arch"] == "dqn"
    assert meta["arch_hparams"]["seq_len"] == 5
    assert meta["arch_hparams"]["cost"] == _DEFAULT_COST
    assert len(meta["weights"]) == 6  # 3 层 Linear × (W, b)
    assert meta["feat_mean"].shape == (5,) and meta["feat_std"].shape == (5,)


@pytest.mark.skipif(not dqn._MLX_OK, reason="需要 MLX（当前环境 mlx 不可用）")
def test_train_dqn_progress_contract(tmp_model_dir):
    """progress_cb 契约：0 起始，末次 (episodes, episodes)，每次递增。"""
    data = _make_panel()
    seen = []

    train_dqn(
        data_train=data,
        data_val=data,
        seq_len=5,
        episodes=3,
        seed=1,
        capacity=256,
        batch_size=8,
        progress_cb=lambda ep, total: seen.append((ep, total)),
    )

    assert seen[0] == (0, 3) and seen[-1] == (3, 3)
    assert seen == sorted(seen) and len(seen) == 4


@pytest.mark.skipif(not dqn._MLX_OK, reason="需要 MLX（当前环境 mlx 不可用）")
def test_evaluate_policy_empty_inputs():
    """评估空输入（无有效 step）→ 全零默认结构（不炸）。"""
    data = _make_panel(S=2, T=8)
    net = dqn._DQNet(5 * 5, 8)  # seq_len=5, 特征 5 维
    res = dqn.evaluate_policy(
        net,
        np.zeros((2, 8, 5), np.float32),
        np.zeros((2, 8), bool),
        data["close"],
        0.001,
        5,
    )
    assert res["equity"] == [] and res["trades"] == 0 and res["avg_pos"] == 0.0


# ---------------------------------------------------------------------------
# handler 接入（temp_db + 打桩 load_panel，真实 split/train）
# ---------------------------------------------------------------------------
def _install_panel_fake(monkeypatch, data=None):
    """打桩 runner.load_panel：返回固定面板与日期轴。"""
    import app.core.tasks.runner as Q

    data = data or _make_panel()

    def fake_load_panel(ds_id, features=None):
        return {"panel": data, "dates": [str(i) for i in range(data["close"].shape[1])]}

    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    return data


@pytest.mark.skipif(not dqn._MLX_OK, reason="需要 MLX（当前环境 mlx 不可用）")
def test_rl_train_done_full_path(temp_db, tmp_model_dir, phase_capture, monkeypatch):
    """rl_train 端到端 done：meta 结构 + 进度 + NNModel 落库。"""
    import app.core.tasks.runner as Q

    data = _install_panel_fake(monkeypatch)
    # 面板 60/20 切分后训练段 24 天、验证段 8 天 → seq_len=5 够用
    params = {
        "dataset_id": 1,
        "seq_len": 5,
        "episodes": 2,
        "batch_size": 16,
        "capacity": 500,
    }
    jid = _spawn_job(temp_db, params)

    Q._run_rl_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done", row.error
    assert row.progress == 100.0
    assert any("训练中" in p for p in phase_capture)
    meta = row.result["meta"]
    assert meta["model_kind"] == "dqn"
    assert meta["episodes"] == 2
    assert len(meta["train_rewards"]) == 2
    assert set(meta["eval"]) == {
        "equity",
        "total_return",
        "sharpe",
        "max_drawdown",
        "trades",
        "avg_pos",
    }
    assert meta["checkpoint"]
    assert meta["model_id"] > 0

    # 落库记录：architecture={"arch": "dqn", **超参}，train_loss 承载奖励曲线
    db = temp_db()
    m = db.get(NNModel, meta["model_id"])
    db.close()
    assert m is not None and m.architecture["arch"] == "dqn"
    assert m.architecture["seq_len"] == 5
    assert m.epochs == 2 and m.train_loss == meta["train_rewards"]


def test_rl_train_validation_errors(temp_db, tmp_model_dir, monkeypatch):
    """非法参数 → failed，error 含明确原因。"""
    import app.core.tasks.runner as Q

    _install_panel_fake(monkeypatch)
    for params, expect in [
        ({"dataset_id": 0}, "dataset_id"),
        ({"dataset_id": 1, "seq_len": 1}, "seq_len"),
        ({"dataset_id": 1, "episodes": 0}, "episodes"),
        ({"dataset_id": 1, "episodes": 1000}, "episodes"),
        ({"dataset_id": 1, "gamma": 1.5}, "gamma"),
        ({"dataset_id": 1, "lr": -1}, "lr"),
        ({"dataset_id": 1, "cost": -0.1}, "cost"),
    ]:
        jid = _spawn_job(temp_db, params)
        Q._run_rl_train(jid, params)
        row = _load(temp_db, jid)
        assert row.status == "failed", params
        assert expect in row.error, (params, row.error)


def test_rl_train_requires_mlx_fails(temp_db, tmp_model_dir, monkeypatch):
    """mlx 不可用（monkeypatch dqn._MLX_OK=False）→ handler failed，error 含「需要 MLX」。"""
    import app.core.tasks.runner as Q

    _install_panel_fake(monkeypatch)
    monkeypatch.setattr(dqn, "_MLX_OK", False)
    params = {"dataset_id": 1, "episodes": 1}
    jid = _spawn_job(temp_db, params)

    Q._run_rl_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "failed" and "需要 MLX" in row.error


def test_rl_train_dataset_missing_fails(temp_db, tmp_model_dir, monkeypatch):
    """数据集不可用（load_panel 返回 None）→ failed。"""
    import app.core.tasks.runner as Q

    def none_panel(ds_id, features=None):
        return None

    monkeypatch.setattr(Q, "load_panel", none_panel)
    params = {"dataset_id": 999, "episodes": 1}
    jid = _spawn_job(temp_db, params)

    Q._run_rl_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "failed" and "不可用" in row.error


# ---------------------------------------------------------------------------
# 注册 / 白名单
# ---------------------------------------------------------------------------
def test_rl_train_registered_and_whitelisted(monkeypatch):
    """_RUNNERS 注册 + 白名单：rl_train 可提交；未知类型 400。"""
    import app.core.tasks.runner as Q
    from app.api import experiments as E
    from app.api.experiments import create_experiment
    from app.core.tasks import registry
    from fastapi import HTTPException

    assert Q._RUNNERS["rl_train"] == "_run_rl_train"
    assert "rl_train" in registry.HANDLERS

    # experiments 模块静态绑定的 submit（monkeypatch E.submit，仿 test_optimize 范式）
    called = {}

    def fake_submit(job_type, params):
        called["type"] = job_type
        return 42

    monkeypatch.setattr(E, "submit", fake_submit)
    res = create_experiment({"job_type": "rl_train", "params": {"dataset_id": 1}})
    assert called["type"] == "rl_train" and res["job_id"] == 42

    with pytest.raises(HTTPException) as exc:
        create_experiment({"job_type": "not_a_job", "params": {}})
    assert exc.value.status_code == 400
