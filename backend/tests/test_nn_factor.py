"""NN 因子产物化测试：checkpoint 加载/面板预测、neural_train 落库 nn_models、
kind='nn' 因子创建 API、/alpha/nn-models 列表。

覆盖任务卡 T1：load_checkpoint 新旧格式兼容、predict_panel 契约、
_run_neural_train 落库（monkeypatch 训练）、POST /factors kind=nn 校验链、
传统 expr 因子回归。
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, NNModel
from app.lib.alpha import nn


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_panel(S=20, T=60, seed=7):
    rng = np.random.RandomState(seed)
    rets = rng.randn(S, T) * 0.02
    close = np.cumprod(1.0 + rets, axis=1)
    close = close / close[:, :1] * 100.0
    volume = rng.rand(S, T) * 1e6 + 1e5
    data = {"close": close.astype(np.float32), "volume": volume.astype(np.float32)}
    fwd = np.full_like(close, np.nan)
    fwd[:, :-5] = close[:, 5:] / close[:, :-5] - 1.0
    return data, fwd.astype(np.float32)


def _train(epochs=2, S=20, T=60):
    """最小训练，返回 train_mlp 结果 dict（_MODEL_STATE 同步为训练权重）。"""
    data, fwd = _make_panel(S=S, T=T)
    return nn.train_mlp(
        data_train=data,
        forward_returns=fwd,
        data_val=data,
        val_forward_returns=fwd,
        layers=[32, 16],
        activation="relu",
        lr=0.01,
        epochs=epochs,
        batch_size=64,
        seed=42,
        max_rows=20000,
    )


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite：queue / factors.service / database 三处 SessionLocal 统一替换。"""
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

    import app.storage.db as DB
    import app.core.tasks.runner as Q
    import app.core.factors as FS

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    monkeypatch.setattr(FS, "SessionLocal", Maker)
    monkeypatch.setattr(DB, "SessionLocal", Maker)

    # 清理跨测试残留的模块级状态
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


def _install_neural_fakes(monkeypatch, *, S=60, T=100, checkpoint="/tmp/fake.npz"):
    """打桩 load_panel / forward_returns / train_mlp（不真实训练，模式同 test_neural_train_job）。"""
    import app.core.tasks.runner as Q

    rng = np.random.RandomState(0)
    close = (np.abs(rng.randn(S, T)) + 10.0).astype(np.float32)
    vol = (rng.rand(S, T) + 1.0).astype(np.float32)

    def fake_load_panel(ds_id, features=None):
        return {
            "panel": {"close": close, "volume": vol},
            "dates": [str(i) for i in range(T)],
        }

    def fake_forward_returns(c, horizon=5):
        return c

    def fake_train_mlp(**kw):
        epochs = kw.get("epochs", 50)
        return {
            "train_loss": [0.1] * epochs,
            "val_loss": [0.2] * epochs,
            "train_ic": 0.35,
            "val_ic": 0.12,
            "epochs": epochs,
            "model_kind": "mlp",
            "checkpoint": checkpoint,
        }

    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(Q, "train_mlp", fake_train_mlp)


def _client():
    from app.api.alpha import router as alpha_router
    from app.api.factors import router as factors_router

    app = FastAPI()
    app.include_router(factors_router, prefix="/api/v1")
    app.include_router(alpha_router, prefix="/api/v1")
    return TestClient(app)


# ---------------------------------------------------------------------------
# load_checkpoint：往返一致 / 旧格式兼容 / 异常
# ---------------------------------------------------------------------------


def test_load_checkpoint_roundtrip():
    """train_mlp 产出 → load_checkpoint：架构/激活/权重与训练一致。"""
    out = _train(epochs=2)
    ck = nn.load_checkpoint(out["checkpoint"])
    assert ck["layers"] == [32, 16]
    assert ck["activation"] == "relu"
    # 读出的权重与模块级训练权重预测完全一致（同权重数组）
    data, fwd = _make_panel()
    pred_ck = nn.predict_panel(ck["weights"], ck["activation"], data)
    pred_state = nn.predict_panel(nn._MODEL_STATE["weights"], ck["activation"], data)
    assert np.allclose(pred_ck, pred_state, equal_nan=True)


def test_load_checkpoint_old_format(tmp_path):
    """旧格式（仅权重，无 layers/activation 元数据）：layers 由 W 形状推导，activation 默认 relu。"""
    rng = np.random.RandomState(1)
    weights = {
        "W0": (rng.randn(5, 16) * 0.6).astype(np.float32),
        "b0": np.zeros(16, dtype=np.float32),
        "W1": (rng.randn(16, 1) * 0.6).astype(np.float32),
        "b1": np.zeros(1, dtype=np.float32),
    }
    path = tmp_path / "old.npz"
    np.savez(path, **weights)

    ck = nn.load_checkpoint(str(path))
    assert ck["layers"] == [16]
    assert ck["activation"] == "relu"
    for k, v in weights.items():
        assert np.array_equal(ck["weights"][k], v)


def test_load_checkpoint_missing_and_corrupt(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        nn.load_checkpoint(str(tmp_path / "nope.npz"))
    bad = tmp_path / "bad.npz"
    bad.write_bytes(b"this is not an npz archive")
    with pytest.raises(ValueError, match="损坏"):
        nn.load_checkpoint(str(bad))
    # 权重与架构不匹配（形状 chain 断裂）也报 ValueError
    rng = np.random.RandomState(2)
    path = tmp_path / "mismatch.npz"
    np.savez(
        path,
        W0=rng.randn(5, 16).astype(np.float32),
        b0=np.zeros(16, dtype=np.float32),
        layers=[32],  # 声明 1 个隐藏层但只有 W0（缺输出层）
    )
    with pytest.raises(ValueError, match="不匹配"):
        nn.load_checkpoint(str(path))


# ---------------------------------------------------------------------------
# predict_panel：形状 / 有效位置与 predict 一致 / NaN 位置
# ---------------------------------------------------------------------------


def test_predict_panel_contract():
    _train(epochs=2)
    data, _ = _make_panel()
    F, mask = nn.build_features(data)

    factor = nn.predict_panel(nn._MODEL_STATE["weights"], "relu", data)
    assert factor.shape == (20, 60)
    assert factor.dtype == np.float32
    # 有效位置与直接 predict 一致
    flat = mask.reshape(-1)
    direct = nn.predict(F.reshape(-1, 5)[flat])
    assert np.array_equal(factor[mask], direct)
    # NaN 位置一致（rank 后全有效；显式注入 NaN 验证还原）
    assert np.isnan(factor[~mask]).all()


def test_predict_panel_nan_positions_preserved(monkeypatch):
    """无效行（valid_mask=False）保持 NaN：monkeypatch build_features 注入无效列。
    注：真实 build_features 的横截面 rank 会消化 NaN 使掩码恒真，此处直接测
    predict_panel 自身的 NaN 保持契约。"""
    _train(epochs=2)
    data, _ = _make_panel()
    S, T = 20, 60
    F = np.random.RandomState(0).randn(S, T, 5).astype(np.float32)
    mask = np.ones((S, T), dtype=bool)
    mask[:, 3] = False  # 第 3 列整体无效
    monkeypatch.setattr(nn, "build_features", lambda d: (F, mask))

    factor = nn.predict_panel(nn._MODEL_STATE["weights"], "relu", data)
    assert factor.shape == (S, T)
    assert np.isnan(factor[:, 3]).all()  # 无效行保持 NaN
    assert np.isfinite(factor[:, 4]).all()  # 有效行有预测值


# ---------------------------------------------------------------------------
# _run_neural_train 落库 nn_models
# ---------------------------------------------------------------------------


def test_neural_train_persists_model(temp_db, monkeypatch):
    """训练 done 后 nn_models 有行、meta.model_id 正确、architecture/指标落库正确。"""
    import app.core.tasks.runner as Q

    _install_neural_fakes(monkeypatch, checkpoint="/tmp/fake.npz")
    params = {"dataset_id": 7, "epochs": 10, "horizon": 5, "layers": [32, 16]}
    db = temp_db()
    job = ExperimentJob(job_type="neural_train", params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    Q._run_neural_train(jid, params)

    db = temp_db()
    job_row = db.get(ExperimentJob, jid)
    meta = job_row.result["meta"]
    db.close()
    assert job_row.status == "done"

    db = temp_db()
    models = db.query(NNModel).order_by(NNModel.id.desc()).all()
    assert len(models) == 1
    m = models[0]
    assert meta["model_id"] == m.id
    assert m.checkpoint == "/tmp/fake.npz"
    # T-56: architecture 扩展含 arch（缺省 mlp），hparams 空展开不出现
    assert m.architecture == {"arch": "mlp", "layers": [32, 16], "activation": "relu"}
    assert m.dataset_id == 7
    assert m.horizon == 5
    assert m.epochs == 10
    assert m.train_ic == 0.35
    assert m.val_ic == 0.12
    assert m.train_loss == [0.1] * 10
    assert m.val_loss == [0.2] * 10
    db.close()


def test_neural_train_persist_failure_keeps_done(temp_db, monkeypatch):
    """落库失败（NNModel 构造抛错）不阻塞任务：done 照写、meta 无 model_id。"""
    import app.core.tasks.runner as Q

    _install_neural_fakes(monkeypatch)
    params = {"dataset_id": 1, "epochs": 5}

    class _Boom:
        def __init__(self, **kw):
            raise RuntimeError("nn_models 写入失败")

    monkeypatch.setattr(Q, "NNModel", _Boom)
    db = temp_db()
    job = ExperimentJob(job_type="neural_train", params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    Q._run_neural_train(jid, params)

    db = temp_db()
    job_row = db.get(ExperimentJob, jid)
    db.close()
    assert job_row.status == "done"
    assert "model_id" not in job_row.result["meta"]


# ---------------------------------------------------------------------------
# POST /factors kind=nn（TestClient）
# ---------------------------------------------------------------------------


def _seed_model(temp_db, **over):
    db = temp_db()
    m = NNModel(
        checkpoint="/tmp/fake.npz",
        architecture={"layers": [32, 16], "activation": "relu"},
        dataset_id=1,
        epochs=10,
        train_ic=0.3,
        val_ic=0.1,
        train_loss=[0.1],
        val_loss=[0.2],
        **over,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    mid = m.id
    db.close()
    return mid


def test_create_nn_factor_via_api(temp_db):
    mid = _seed_model(temp_db)
    resp = _client().post(
        "/api/v1/factors",
        json={"name": "NN 因子", "kind": "nn", "model_id": mid, "dataset_id": 3},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["factor"]["kind"] == "nn"
    assert body["factor"]["expression"] == f"neural_mlp(model#{mid})"
    assert body["version"]["model_ref"] == mid
    assert body["version"]["expression"] == f"neural_mlp(model#{mid})"

    # 列表字段带出 kind / model_ref
    lst = _client().get("/api/v1/factors").json()["data"]
    assert lst[0]["kind"] == "nn"
    assert lst[0]["versions"][0]["model_ref"] == mid


def test_create_nn_factor_duplicate_400(temp_db):
    mid = _seed_model(temp_db)
    c = _client()
    payload = {"name": "A", "kind": "nn", "model_id": mid}
    assert c.post("/api/v1/factors", json=payload).status_code == 200
    resp = c.post("/api/v1/factors", json=payload)
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]


def test_create_nn_factor_missing_model_400(temp_db):
    resp = _client().post(
        "/api/v1/factors", json={"name": "A", "kind": "nn", "model_id": 9999}
    )
    assert resp.status_code == 400
    assert "模型不存在" in resp.json()["detail"]


def test_create_nn_factor_requires_model_id(temp_db):
    c = _client()
    for payload in [
        {"name": "A", "kind": "nn"},  # 缺 model_id
        {"name": "A", "kind": "nn", "model_id": "abc"},  # 非整数
        {"name": "A", "kind": "nn", "model_id": 0},  # 非正整数
    ]:
        resp = c.post("/api/v1/factors", json=payload)
        assert resp.status_code == 400, payload
        assert "model_id" in resp.json()["detail"]


def test_create_expr_factor_regression(temp_db):
    """传统 expr 因子：不带 kind 行为不变。"""
    resp = _client().post(
        "/api/v1/factors",
        json={"name": "传统因子", "expression": "close/volume"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["factor"]["kind"] == "expr"
    assert body["version"]["model_ref"] is None
    assert body["factor"]["expression"] == "close/volume"


# ---------------------------------------------------------------------------
# GET /alpha/nn-models
# ---------------------------------------------------------------------------


def test_nn_models_list_desc(temp_db):
    id1 = _seed_model(temp_db, horizon=5)
    id2 = _seed_model(temp_db, horizon=10)
    resp = _client().get("/api/v1/alpha/nn-models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [m["id"] for m in data] == [id2, id1]  # id 降序
    m = data[0]
    for k in (
        "id",
        "checkpoint",
        "architecture",
        "dataset_id",
        "horizon",
        "epochs",
        "train_ic",
        "val_ic",
        "train_loss",
        "val_loss",
        "created_at",
    ):
        assert k in m
    assert m["architecture"] == {"layers": [32, 16], "activation": "relu"}
    assert m["created_at"] is not None


# ---------------------------------------------------------------------------
# P1-31：neural fwd 无跨切分泄漏（对照 gp 语义：先 split 再各自 forward_returns）
# ---------------------------------------------------------------------------


def test_neural_fwd_no_cross_segment_leak(temp_db, monkeypatch):
    """neural_train 传给 train_mlp 的 fwd_train/fwd_val 与段内独立 forward_returns
    逐位一致，段尾部 horizon 列 NaN——修复前「完整面板 fwd 再切片」会用 val 段
    价格给 train 尾部标签算出实值（val 段首列相对其起点有值即泄漏判据失效处）。"""
    import app.core.tasks.runner as Q

    S, T = 20, 60
    rng = np.random.RandomState(5)
    close = (np.abs(rng.randn(S, T)) + 10.0).astype(np.float32)
    vol = (rng.rand(S, T) + 1.0).astype(np.float32)
    panel = {"close": close, "volume": vol}

    def fake_load_panel(ds_id, features=None):
        return {"panel": panel, "dates": [str(i) for i in range(T)]}

    captured: dict = {}

    def fake_train_mlp(**kw):
        captured.update(kw)
        return {
            "train_loss": [0.1] * 2,
            "val_loss": [0.2] * 2,
            "train_ic": 0.3,
            "val_ic": 0.1,
            "epochs": 2,
            "model_kind": "mlp",
            "checkpoint": "/tmp/fake.npz",
        }

    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "train_mlp", fake_train_mlp)
    # 不打桩 forward_returns：验证真实 split_panel 语义（P1-31 修复后的独立切分）

    params = {"dataset_id": 1, "epochs": 2, "horizon": 5}
    db = temp_db()
    job = ExperimentJob(job_type="neural_train", params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    Q._run_neural_train(jid, params)

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error

    from app.lib.alpha.evaluate import forward_returns

    t1, t2 = int(T * 0.6), int(T * 0.8)
    expect_train = forward_returns(close[:, :t1], 5)
    expect_val = forward_returns(close[:, t1:t2], 5)
    np.testing.assert_allclose(
        captured["forward_returns"], expect_train, equal_nan=True
    )
    np.testing.assert_allclose(
        captured["val_forward_returns"], expect_val, equal_nan=True
    )
    assert captured["forward_returns"].shape == (S, t1)
    assert captured["val_forward_returns"].shape == (S, t2 - t1)
    # 泄漏判据：train/val 段尾部 horizon 列全 NaN（旧语义此处为实值）
    assert np.isnan(captured["forward_returns"][:, -1]).all()
    assert np.isnan(captured["val_forward_returns"][:, -1]).all()
    # 对照：完整面板 fwd 再切（旧写法）末列非 NaN，证明判据确实区分新旧语义
    leaky = forward_returns(close, 5)[:, :t1]
    assert not np.isnan(leaky[:, -1]).all()
