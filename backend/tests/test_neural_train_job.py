"""neural_train 任务队列接入测试：进度上报（phase）/ 结果结构 / 参数切分 / 异常路径。

phase 列由集成阶段添加到 ExperimentJob 模型；当前模型尚无该列，
直接 `_update(phase=...)` 会抛 CompileError（Unconsumed column names: phase），
故测试通过包装 Q._update 剥离 phase 字段（记录到列表）后转真实写入。
不真实训练：load_panel / forward_returns / train_mlp 全部打桩。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（worker 与查询走同一临时库）。"""
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

    # 清理跨测试残留的模块级状态
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


@pytest.fixture()
def phase_capture(temp_db, monkeypatch):
    """包装 Q._update：剥离 phase 字段（模型无该列，直接写会 CompileError），
    记录 phase 文案，其余字段转真实 _update。返回记录到的 phase 列表。"""
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


def _install_neural_fakes(monkeypatch, *, S=60, T=100, train_mlp=None, fail=None):
    """打桩 load_panel / forward_returns / train_mlp（不真实训练）。

    默认 stub 记录调用参数、按契约调 progress_cb（0 起始，末次 (epochs, epochs)）
    并返回标准结果 dict；fail 非空时抛 RuntimeError。返回 captured 参数 dict。
    """
    import app.core.tasks.runner as Q

    rng = np.random.RandomState(0)
    close = (np.abs(rng.randn(S, T)) + 10.0).astype(np.float32)
    vol = (rng.rand(S, T) + 1.0).astype(np.float32)

    captured: dict = {}

    def fake_load_panel(ds_id, features=None):
        return {
            "panel": {"close": close, "volume": vol},
            "dates": [str(i) for i in range(T)],
        }

    def fake_forward_returns(c, horizon=5):
        return c

    def fake_train_mlp(**kw):
        captured.update(kw)
        cb = kw.get("progress_cb")
        epochs = kw.get("epochs", 50)
        if fail is not None:
            raise RuntimeError(fail)
        if cb is not None:
            for ep in range(epochs + 1):
                cb(ep, epochs)
        return {
            "train_loss": [0.1] * epochs,
            "val_loss": [0.2] * epochs,
            "train_ic": 0.35,
            "val_ic": 0.12,
            "epochs": epochs,
            "model_kind": "mlp",
            "checkpoint": "/tmp/fake.npz",
        }

    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(Q, "train_mlp", train_mlp or fake_train_mlp)
    return captured


def _spawn_job(maker, params=None, status="pending") -> int:
    db = maker()
    job = ExperimentJob(job_type="neural_train", params=params or {}, status=status)
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


def _clear_event_ring():
    """清空事件总线 ring（模块级全局，测试用 job id 从 1 重启会串 ring 键）"""
    from app.core import events

    with events._lock:
        events._jobs.clear()
        events._subs.clear()


# ---------------------------------------------------------------------------
# 全路径 done
# ---------------------------------------------------------------------------


def test_neural_train_done_full_path(temp_db, phase_capture, monkeypatch):
    """全路径：60/20 切分、train_mlp 参数正确、进度 1→100、phase 逐 epoch 上报、
    结果结构符合契约。"""
    import app.core.tasks.runner as Q

    captured = _install_neural_fakes(monkeypatch, S=60, T=100)
    params = {"dataset_id": 1, "epochs": 10, "horizon": 5}
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done"
    assert row.progress == 100.0
    assert set(row.result) == {"results", "meta"}
    assert row.result["results"] == []
    meta = row.result["meta"]
    assert set(meta) == {
        "train_loss",
        "val_loss",
        "train_ic",
        "val_ic",
        "epochs",
        "model_kind",
        "checkpoint",
        "model_id",
        # T-57 权重/注意力可视化：train_mlp 未产出时为 None（fake 无 viz 键）
        "viz",
    }
    assert meta["viz"] is None
    assert isinstance(meta["model_id"], int) and meta["model_id"] > 0
    assert meta["train_ic"] == 0.35 and meta["val_ic"] == 0.12
    assert meta["epochs"] == 10 and meta["model_kind"] == "mlp"
    assert meta["checkpoint"] == "/tmp/fake.npz"

    # 参数透传与 60/20 切分（T=100 → t1=60, t2=80）
    assert set(captured["data_train"]) == {"close", "volume"}
    assert captured["data_train"]["close"].shape == (60, 60)
    assert captured["data_val"]["close"].shape == (60, 20)
    assert captured["forward_returns"].shape == (60, 60)
    assert captured["val_forward_returns"].shape == (60, 20)
    assert captured["layers"] == [32, 16]
    assert captured["activation"] == "relu"
    assert captured["lr"] == 0.01
    assert captured["epochs"] == 10
    assert captured["batch_size"] == 256
    assert captured["seed"] == 42
    assert captured["max_rows"] == 20000

    # phase 逐 epoch 上报（契约：0 起始，末次 (epochs, epochs)）
    assert phase_capture[0] == "数据准备"
    assert "训练中 1/10" in phase_capture
    assert phase_capture[-2:] == ["评估", "结果整理"]
    # progress_cb 契约 0 起始、末次 (epochs, epochs) → 训练中文案 11 条
    assert len(phase_capture) == 1 + 11 + 2  # 数据准备 + 训练中 + 评估/结果整理


def test_neural_train_defaults_and_custom_params(temp_db, phase_capture, monkeypatch):
    """默认参数生效；自定义 layers/activation/lr/batch_size/seed/max_rows 透传。"""
    import app.core.tasks.runner as Q

    captured = _install_neural_fakes(monkeypatch, S=30, T=120)
    params = {
        "dataset_id": 2,
        "layers": [64, 32, 8],
        "activation": "tanh",
        "lr": 0.001,
        "epochs": 3,
        "batch_size": 128,
        "seed": 7,
        "max_rows": 5000,
        "horizon": 10,
    }
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done"
    # T=120 → t1=72, t2=96
    assert captured["data_train"]["close"].shape == (30, 72)
    assert captured["data_val"]["close"].shape == (30, 24)
    assert captured["layers"] == [64, 32, 8]
    assert captured["activation"] == "tanh"
    assert captured["lr"] == 0.001
    assert captured["batch_size"] == 128
    assert captured["seed"] == 7
    assert captured["max_rows"] == 5000
    assert row.result["meta"]["epochs"] == 3


def test_neural_train_ic_none_passthrough(temp_db, phase_capture, monkeypatch):
    """train_ic/val_ic 为 None 时原样透传（不误转 0）。"""
    import app.core.tasks.runner as Q

    def stub(**kw):
        return {
            "train_loss": [],
            "val_loss": [],
            "train_ic": None,
            "val_ic": None,
            "epochs": 1,
            "model_kind": "mlp",
            "checkpoint": None,
        }

    _install_neural_fakes(monkeypatch, train_mlp=stub)
    params = {"dataset_id": 1}
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done"
    assert row.result["meta"]["train_ic"] is None
    assert row.result["meta"]["val_ic"] is None
    assert row.result["meta"]["checkpoint"] is None


# ---------------------------------------------------------------------------
# T-57 实时损失增量事件（on_epoch_loss → progress 事件 payload 扩展）
# ---------------------------------------------------------------------------


def test_neural_train_loss_progress_events(temp_db, monkeypatch):
    """T-57：train_mlp 调 on_epoch_loss 时，progress 事件携带 epoch/train_loss/val_loss
    增量；phase/进度等旧字段保留（MonitorPanel 兼容）。"""
    import app.core.tasks.runner as Q
    from app.core import events

    _clear_event_ring()

    def fake_train_mlp(**kw):
        cb = kw.get("progress_cb")
        ol = kw.get("on_epoch_loss")
        epochs = kw.get("epochs", 50)
        if cb is not None:
            cb(0, epochs)
        for ep in range(1, epochs + 1):
            if ol is not None:
                ol(ep, epochs, 0.1 * ep, 0.2 * ep)
            if cb is not None:
                cb(ep, epochs)
        return {
            "train_loss": [0.1 * ep for ep in range(1, epochs + 1)],
            "val_loss": [0.2 * ep for ep in range(1, epochs + 1)],
            "train_ic": 0.35,
            "val_ic": 0.12,
            "epochs": epochs,
            "model_kind": "mlp",
            "checkpoint": "/tmp/fake.npz",
        }

    _install_neural_fakes(monkeypatch, S=30, T=120, train_mlp=fake_train_mlp)
    params = {"dataset_id": 1, "epochs": 3}
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done", row.error
    loss_events = [
        e
        for e in events.snapshot(jid)
        if e.get("epoch") is not None and e.get("train_loss") is not None
    ]
    assert [(e["epoch"], e["train_loss"], e["val_loss"]) for e in loss_events] == [
        (1, 0.1, 0.2),
        (2, 0.2, 0.4),
        (3, 0.3, 0.6),
    ]
    # 事件仍为 progress 类型且带旧字段（前端/MonitorPanel 向后兼容）
    assert all(
        e["type"] == "progress" and e["progress"] is not None for e in loss_events
    )
    assert all(e["phase"] for e in loss_events)


def test_neural_train_no_loss_events_with_legacy_cb(temp_db, monkeypatch):
    """T-57 兼容：旧契约 train_mlp 仅调 progress_cb(epoch, total) 时，
    不产生损失增量事件（事件总线只有进度/阶段/终态事件）。"""
    import app.core.tasks.runner as Q
    from app.core import events

    _clear_event_ring()
    _install_neural_fakes(monkeypatch, S=30, T=120)  # 默认 stub 仅调 cb(ep, epochs)
    params = {"dataset_id": 1, "epochs": 2}
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    assert _load(temp_db, jid).status == "done"
    snap = events.snapshot(jid)
    assert snap, "任务应发布事件（进度/终态）"
    assert all(e.get("train_loss") is None for e in snap)


# ---------------------------------------------------------------------------
# 校验与异常路径
# ---------------------------------------------------------------------------


def test_neural_train_validation_errors(temp_db, monkeypatch):
    """非法参数 → failed，error 含明确原因（epochs 越界 / layers 非法）。"""
    import app.core.tasks.runner as Q

    _install_neural_fakes(monkeypatch)
    for params, expect in [
        ({"dataset_id": 1, "epochs": 1000}, "epochs"),  # 越界
        ({"dataset_id": 1, "epochs": 0}, "epochs"),  # 越界（下界）
        ({"dataset_id": 1, "layers": [32, "a"]}, "layers"),
        ({"dataset_id": 1, "layers": 3}, "layers"),
        ({"dataset_id": 1, "layers": []}, "layers"),
        ({"dataset_id": 1, "layers": [True]}, "layers"),  # bool 不是 int
    ]:
        jid = _spawn_job(temp_db, params)
        Q._run_neural_train(jid, params)
        row = _load(temp_db, jid)
        assert row.status == "failed", params
        assert expect in row.error, (params, row.error)


def test_neural_train_exception_fails_job(temp_db, monkeypatch):
    """train_mlp 抛异常 → failed，error 携带异常消息。"""
    import app.core.tasks.runner as Q

    _install_neural_fakes(monkeypatch, fail="训练崩了")
    params = {"dataset_id": 1}
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "failed" and "训练崩了" in row.error


def test_neural_train_dataset_missing_fails(temp_db, monkeypatch):
    """数据集不可用（load_panel 返回 None）→ failed。"""
    import app.core.tasks.runner as Q

    _install_neural_fakes(monkeypatch)

    def none_panel(ds_id, features=None):
        return None

    monkeypatch.setattr(Q, "load_panel", none_panel)
    params = {"dataset_id": 999}
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "failed" and "不可用" in row.error


# ---------------------------------------------------------------------------
# 序列化：get_job / 列表的 phase 字段
# ---------------------------------------------------------------------------


def test_neural_train_phase_in_get_job_and_list(temp_db, monkeypatch):
    """get_job 与列表序列化含 phase 字段（模型无该列时 getattr 兜底空串），
    列表 label 使用中文映射。"""
    from app.core.tasks.runner import get_job, list_jobs

    db = temp_db()
    job = ExperimentJob(
        job_type="neural_train",
        params={"dataset_id": 1},
        status="done",
        progress=100.0,
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    detail = get_job(jid)
    assert detail is not None
    assert detail["phase"] == ""
    items = list_jobs(50)
    row = [it for it in items if it["id"] == jid][0]
    assert row["phase"] == ""
    assert row["label"] == "神经网络训练"
