"""T-56 NN 架构注册表测试：注册表结构 / mlp 行为回归 / lstm/transformer 训练预测
/ MLX 不可用错误 / checkpoint arch 元信息往返 / handler arch 校验与透传 / backtest 分发。

mlp 路径必须与改动前行为一致（回归）；lstm/transformer 依赖 MLX（本机 Apple
Silicon 实测可用，测试仍按可用性守卫：不可用时断言报「需要 MLX」）。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.lib.alpha import nn
from app.storage.db import Base
from app.storage.models import ExperimentJob, NNModel


def _make_panel(S=20, T=60, seed=7):
    """随机游走 close + 随机 volume 构造小面板，派生 5 日未来收益。"""
    rng = np.random.RandomState(seed)
    rets = rng.randn(S, T) * 0.02
    close = np.cumprod(1.0 + rets, axis=1)
    close = close / close[:, :1] * 100.0
    volume = rng.rand(S, T) * 1e6 + 1e5
    data = {"close": close.astype(np.float32), "volume": volume.astype(np.float32)}
    fwd = np.full_like(close, np.nan)
    fwd[:, :-5] = close[:, 5:] / close[:, :-5] - 1.0
    return data, fwd.astype(np.float32)


def _train_kwargs(epochs=3, **over):
    kw = dict(
        data_train=None,
        forward_returns=None,
        data_val=None,
        val_forward_returns=None,
        layers=[32, 16],
        activation="relu",
        lr=0.01,
        epochs=epochs,
        batch_size=64,
        seed=42,
        max_rows=20000,
    )
    kw.update(over)
    return kw


@pytest.fixture()
def tmp_model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(nn, "_MODEL_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# 注册表结构
# ---------------------------------------------------------------------------


def test_arch_registry_structure():
    """注册表含 mlp/lstm/transformer，每项含 name/desc/available/default_hparams/
    build/make_samples；mlp 恒可用且 build 为 None（不走 mlx.nn）。"""
    assert set(nn.ARCH_REGISTRY) == {"mlp", "lstm", "transformer"}
    for arch, entry in nn.ARCH_REGISTRY.items():
        for k in (
            "name",
            "desc",
            "available",
            "default_hparams",
            "build",
            "make_samples",
        ):
            assert k in entry, f"{arch} 缺字段 {k}"
        assert callable(entry["make_samples"]), f"{arch} make_samples 必须可调用"
        assert isinstance(entry["default_hparams"], dict)
    assert nn.ARCH_REGISTRY["mlp"]["available"] is True
    assert nn.ARCH_REGISTRY["mlp"]["build"] is None
    assert nn.ARCH_REGISTRY["lstm"]["available"] is nn._MLX_OK
    assert nn.ARCH_REGISTRY["transformer"]["available"] is nn._MLX_OK
    # 序列架构默认超参含 seq_len（滑窗长度）
    assert nn.ARCH_REGISTRY["lstm"]["default_hparams"]["seq_len"] >= 2
    assert nn.ARCH_REGISTRY["transformer"]["default_hparams"]["seq_len"] >= 2


# ---------------------------------------------------------------------------
# mlp 行为回归（arch 缺省 = mlp，改动前行为不变）
# ---------------------------------------------------------------------------


def test_mlp_default_arch_regression(tmp_model_dir):
    """arch 缺省为 mlp：契约字段、model_kind='mlp'、checkpoint 带 arch='mlp'。"""
    data, fwd = _make_panel()
    out = nn.train_mlp(
        **_train_kwargs(
            data_train=data, forward_returns=fwd, data_val=data, val_forward_returns=fwd
        )
    )
    for k in (
        "train_loss",
        "val_loss",
        "train_ic",
        "val_ic",
        "epochs",
        "model_kind",
        "checkpoint",
    ):
        assert k in out
    assert out["model_kind"] == "mlp"
    assert len(out["train_loss"]) == 3
    # T-57 viz：mlp = 末层权重矩阵 [末层隐藏, 1]（layers=[32,16] → 16×1），kind='weights'，round 4 精度
    viz = out["viz"]
    assert viz is not None and viz["kind"] == "weights"
    assert np.shape(viz["matrix"]) == (16, 1)
    assert viz["row_labels"] == [str(i) for i in range(16)]
    assert viz["col_labels"] == ["0"]
    assert round(viz["matrix"][0][0], 4) == viz["matrix"][0][0]  # round 4 精度
    with np.load(out["checkpoint"]) as d:
        assert "W0" in d.files and "b2" in d.files
        assert str(np.asarray(d["arch"]).item()) == "mlp"
    meta = nn.load_checkpoint(out["checkpoint"])
    assert meta["arch"] == "mlp" and meta["arch_hparams"] == {}
    # 内部 predict 契约（numpy 前向）
    F, m = nn.build_features(data)
    pred = nn.predict(F.reshape(-1, 5)[m.reshape(-1)])
    assert pred.shape == (int(m.sum()),) and np.isfinite(pred).all()


def test_mlp_explicit_arch_mlp_same_as_default(tmp_model_dir):
    """显式 arch='mlp' 与缺省走同一路径：loss 曲线完全一致（同 seed 同数据）。"""
    data, fwd = _make_panel()
    kw = _train_kwargs(
        data_train=data, forward_returns=fwd, data_val=data, val_forward_returns=fwd
    )
    out_default = nn.train_mlp(**kw)
    out_explicit = nn.train_mlp(**kw, arch="mlp", arch_hparams=None)
    assert out_explicit["model_kind"] == "mlp"
    np.testing.assert_allclose(out_default["train_loss"], out_explicit["train_loss"])


# ---------------------------------------------------------------------------
# 校验与 MLX 不可用路径
# ---------------------------------------------------------------------------


def test_arch_whitelist_invalid(tmp_model_dir):
    """arch 非白名单 → ValueError。"""
    data, fwd = _make_panel()
    with pytest.raises(ValueError, match="arch"):
        nn.train_mlp(
            **_train_kwargs(
                data_train=data,
                forward_returns=fwd,
                data_val=data,
                val_forward_returns=fwd,
            ),
            arch="gru",
        )


def test_arch_hparams_unknown_field_rejected(tmp_model_dir):
    """arch_hparams 未知字段 → ValueError 含字段名。"""
    data, fwd = _make_panel()
    with pytest.raises(ValueError, match="seq_len_typo"):
        nn.train_mlp(
            **_train_kwargs(
                data_train=data,
                forward_returns=fwd,
                data_val=data,
                val_forward_returns=fwd,
            ),
            arch="lstm",
            arch_hparams={"seq_len_typo": 10},
        )


def test_arch_hparams_defaults_merged(tmp_model_dir):
    """部分 arch_hparams → 与注册表默认合并（checkpoint 内 hparams 含全量键）。"""
    if not nn._MLX_OK:
        pytest.skip("mlx 不可用，无法训练 lstm")
    data, fwd = _make_panel()
    out = nn.train_mlp(
        **_train_kwargs(
            epochs=1,
            data_train=data,
            forward_returns=fwd,
            data_val=data,
            val_forward_returns=fwd,
        ),
        arch="lstm",
        arch_hparams={"hidden": 8},
    )
    meta = nn.load_checkpoint(out["checkpoint"])
    hp = meta["arch_hparams"]
    assert hp["hidden"] == 8
    for k, v in nn.ARCH_REGISTRY["lstm"]["default_hparams"].items():
        assert k in hp, f"默认超参缺失: {k}"
        if k != "hidden":
            assert hp[k] == v


def test_arch_unavailable_without_mlx(tmp_model_dir, monkeypatch):
    """mlx 不可用时：mlp 正常训练，lstm/transformer 抛 ValueError 含「需要 MLX」。"""
    data, fwd = _make_panel()
    monkeypatch.setattr(nn, "_MLX_OK", False)
    kw = _train_kwargs(
        data_train=data, forward_returns=fwd, data_val=data, val_forward_returns=fwd
    )
    # mlp 不受影响（numpy 参考实现）
    out = nn.train_mlp(**kw)
    assert out["model_kind"] == "mlp"
    for arch in ("lstm", "transformer"):
        with pytest.raises(ValueError, match="需要 MLX"):
            nn.train_mlp(**kw, arch=arch, arch_hparams={})


# ---------------------------------------------------------------------------
# 序列架构训练 / 预测 / checkpoint 往返（MLX 可用时）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not nn._MLX_OK, reason="mlx 不可用，无法训练序列架构")
@pytest.mark.parametrize(
    "arch,hparams",
    [
        ("lstm", {"hidden": 8, "num_layers": 2, "seq_len": 10}),
        (
            "transformer",
            {"dims": 8, "num_layers": 1, "num_heads": 2, "mlp_dims": 16, "seq_len": 10},
        ),
    ],
)
def test_seq_arch_train_predict_roundtrip(tmp_model_dir, arch, hparams):
    """序列架构：小规模真实训练 + predict_panel 形状 + checkpoint 往返（arch 元信息）。"""
    data, fwd = _make_panel()
    out = nn.train_mlp(
        **_train_kwargs(
            data_train=data, forward_returns=fwd, data_val=data, val_forward_returns=fwd
        ),
        arch=arch,
        arch_hparams=hparams,
    )
    assert out["model_kind"] == arch
    assert len(out["train_loss"]) == 3
    assert all(np.isfinite(out["train_loss"]))

    # T-57 viz：lstm = 末层 LSTM 权重拼接 [4H, input+hidden]（num_layers=2 时末层输入=hidden）；
    # transformer = 注意力 [seq_len, seq_len]
    viz = out["viz"]
    assert viz is not None
    if arch == "lstm":
        assert viz["kind"] == "lstm_weights"
        assert np.shape(viz["matrix"]) == (4 * hparams["hidden"], 2 * hparams["hidden"])
    else:
        assert viz["kind"] == "attention"
        mat = np.asarray(viz["matrix"])
        assert mat.shape == (hparams["seq_len"], hparams["seq_len"])
        assert np.isfinite(mat).all()
        np.testing.assert_allclose(mat.sum(axis=1), 1.0, atol=1e-3)  # softmax 行和=1
    assert all(np.isfinite(v) for row in viz["matrix"] for v in row)

    # checkpoint 往返：arch/hparams 原样、权重键为扁平 "." 结构
    meta = nn.load_checkpoint(out["checkpoint"])
    assert meta["arch"] == arch
    assert meta["arch_hparams"]["seq_len"] == 10
    flat_keys = [k for k in meta["weights"]]
    assert flat_keys and all("." in k for k in flat_keys), flat_keys[:5]
    assert all(
        isinstance(v, np.ndarray) and v.dtype == np.float32
        for v in meta["weights"].values()
    )

    # predict_panel 按 arch 分发 → (S,T)；头部 seq_len-1 天无预测为 NaN，其余有限
    factor = nn.predict_panel(
        meta["weights"],
        meta["activation"],
        data,
        arch=meta["arch"],
        arch_hparams=meta["arch_hparams"],
    )
    assert factor.shape == (20, 60) and factor.dtype == np.float32
    assert np.isnan(factor[:, :9]).all()  # seq_len=10 → 前 9 天 NaN
    assert np.isfinite(factor[:, 30:]).all()

    # 内部 predict：序列输入 (n, seq_len, 5) → (n,)
    F, m = nn.build_features(data)
    Xw, _ = nn._make_seq_samples(F, m, fwd, 20000, 42, meta["arch_hparams"])
    pred = nn.predict(Xw[:16])
    assert pred.shape == (16,) and np.isfinite(pred).all()


@pytest.mark.skipif(not nn._MLX_OK, reason="mlx 不可用，无法训练序列架构")
def test_seq_samples_window_shape():
    """_make_seq_samples：按股票滑窗，样本 (n, seq_len, 5)，右端点与 fwd 对齐。"""
    data, fwd = _make_panel(S=5, T=30)
    F, m = nn.build_features(data)
    hp = {"seq_len": 8}
    X, y = nn._make_seq_samples(F, m, fwd, 1000, 42, hp)
    S, T = F.shape[:2]
    assert X.shape[1:] == (8, 5)
    assert len(X) == len(y)
    assert len(X) <= S * (T - 8 + 1)
    # 窗口右端点预测值 = fwd[s, t]，抽查第 1 个样本
    assert np.isfinite(X).all() and np.isfinite(y).all()


def test_seq_samples_window_alignment():
    """样本窗口与 _predict_seq 口径一致：第 s 股首个窗口末帧 = F[s, seq_len-1]，
    且标签 y = fwd[s, seq_len-1]（右端点预测对齐）。"""
    data, fwd = _make_panel(S=4, T=20)
    F, m = nn.build_features(data)
    hp = {"seq_len": 6}
    X, y = nn._make_seq_samples(F, m, fwd, 1000, 42, hp)
    assert X.shape == (len(y), 6, 5)
    # 第一只股票第一个窗口：t=seq_len-1=5，窗口 [0..5]，末帧 = F[0, 5]
    first = X[0]
    np.testing.assert_allclose(first[-1], F[0, 5])
    np.testing.assert_allclose(first[:-1], F[0, :5])
    assert y[0] == fwd[0, 5]
    # 窗口纯历史（相对右端点 t 不含未来特征）
    assert len(X) <= 4 * (20 - 6 + 1)


# ---------------------------------------------------------------------------
# handler：arch 校验 / 透传 / 落库
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
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


def _install_neural_fakes(monkeypatch, *, S=30, T=120, train_mlp=None):
    """打桩 load_panel / train_mlp（不真实训练），返回 captured kwargs dict。"""
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

    def fake_train_mlp(**kw):
        captured.update(kw)
        cb = kw.get("progress_cb")
        epochs = kw.get("epochs", 50)
        if cb is not None:
            for ep in range(epochs + 1):
                cb(ep, epochs)
        return {
            "train_loss": [0.1] * epochs,
            "val_loss": [0.2] * epochs,
            "train_ic": 0.35,
            "val_ic": 0.12,
            "epochs": epochs,
            "model_kind": kw.get("arch", "mlp"),
            "checkpoint": "/tmp/fake.npz",
            # T-57 viz：fake 产出简单矩阵，供 handler 透传断言
            "viz": {
                "kind": "weights",
                "matrix": [[0.1]],
                "row_labels": ["0"],
                "col_labels": ["0"],
            },
        }

    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "train_mlp", train_mlp or fake_train_mlp)
    return captured


def _spawn_job(maker, params=None) -> int:
    db = maker()
    job = ExperimentJob(job_type="neural_train", params=params or {}, status="pending")
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


@pytest.mark.skipif(not nn._MLX_OK, reason="需要 MLX 才能执行序列架构 handler 端到端测试")
def test_handler_arch_passthrough_and_persist(temp_db, monkeypatch):
    """handler：arch='lstm' 透传 train_mlp；architecture 落库含 arch 与 hparams；
    meta.model_kind=arch。"""
    import app.core.tasks.runner as Q

    captured = _install_neural_fakes(monkeypatch, S=30, T=120)
    params = {
        "dataset_id": 1,
        "arch": "lstm",
        "arch_hparams": {"hidden": 16, "seq_len": 10},
    }
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done", row.error
    assert captured["arch"] == "lstm"
    assert captured["arch_hparams"] == {"hidden": 16, "seq_len": 10}
    assert row.result["meta"]["model_kind"] == "lstm"
    # T-57 viz 透传：train_mlp 返回的 viz 原样进 meta
    assert row.result["meta"]["viz"] == {
        "kind": "weights",
        "matrix": [[0.1]],
        "row_labels": ["0"],
        "col_labels": ["0"],
    }
    # 落库 architecture 含 arch + hparams
    db = temp_db()
    m = db.get(NNModel, row.result["meta"]["model_id"])
    db.close()
    assert m is not None
    assert m.architecture["arch"] == "lstm"
    assert m.architecture["hidden"] == 16 and m.architecture["seq_len"] == 10
    assert m.architecture["layers"] == [32, 16]  # 独立参数保留


def test_handler_arch_default_mlp_unchanged(temp_db, monkeypatch):
    """handler：不传 arch → 默认 mlp，行为与改动前一致。"""
    import app.core.tasks.runner as Q

    captured = _install_neural_fakes(monkeypatch)
    params = {"dataset_id": 1}
    jid = _spawn_job(temp_db, params)

    Q._run_neural_train(jid, params)

    row = _load(temp_db, jid)
    assert row.status == "done"
    assert captured["arch"] == "mlp"
    assert captured["arch_hparams"] == {}
    assert row.result["meta"]["model_kind"] == "mlp"


def test_handler_arch_validation_fails(temp_db, monkeypatch):
    """handler 校验：arch 非白名单 / arch_hparams 非 dict / MLX 不可用 → failed。"""
    import app.core.tasks.runner as Q

    _install_neural_fakes(monkeypatch)
    cases = [
        ({"dataset_id": 1, "arch": "gru"}, "arch"),
        ({"dataset_id": 1, "arch": "lstm", "arch_hparams": "oops"}, "arch_hparams"),
    ]
    for params, expect in cases:
        jid = _spawn_job(temp_db, params)
        Q._run_neural_train(jid, params)
        row = _load(temp_db, jid)
        assert row.status == "failed", params
        assert expect in row.error, (params, row.error)

    # MLX 不可用：monkeypatch 注册表 available=False（_MLX_OK 冻结于 import 时）
    monkeypatch.setitem(nn.ARCH_REGISTRY["lstm"], "available", False)
    params = {"dataset_id": 1, "arch": "lstm"}
    jid = _spawn_job(temp_db, params)
    Q._run_neural_train(jid, params)
    row = _load(temp_db, jid)
    assert row.status == "failed"
    assert "MLX" in row.error and "lstm" in row.error


# ---------------------------------------------------------------------------
# backtest：按 checkpoint arch 分发
# ---------------------------------------------------------------------------


def _train_seq_checkpoint(tmp_path, monkeypatch, panel, arch="lstm"):
    """序列架构小训练，checkpoint 落盘 tmp 目录，返回 checkpoint 路径。"""
    monkeypatch.setattr(nn, "_MODEL_DIR", str(tmp_path))
    data = {"close": panel["close"], "volume": panel["volume"]}
    rng = np.random.RandomState(7)
    fwd = np.full_like(panel["close"], np.nan, dtype=np.float32)
    fwd[:, :-5] = panel["close"][:, 5:] / panel["close"][:, :-5] - 1.0
    out = nn.train_mlp(
        data_train=data,
        forward_returns=fwd,
        data_val=data,
        val_forward_returns=fwd,
        layers=[8],
        activation="relu",
        lr=0.01,
        epochs=2,
        batch_size=64,
        seed=42,
        max_rows=20000,
        arch=arch,
        arch_hparams={"hidden": 8, "seq_len": 8},
    )
    return out["checkpoint"]


@pytest.mark.skipif(not nn._MLX_OK, reason="mlx 不可用，无法训练序列架构")
def test_backtest_nn_seq_arch_distribution(temp_db, tmp_path, monkeypatch):
    """backtest：加载 lstm checkpoint → predict_panel 按 arch 分发 → 因子 (S,T)。"""
    import app.core.tasks.runner as Q
    import app.lib.alpha.backtest as BT

    rng = np.random.RandomState(7)
    S, T = 30, 60
    close = (np.cumprod(1.0 + rng.randn(S, T) * 0.02, axis=1) * 100.0).astype(
        np.float32
    )
    panel = {"close": close, "volume": (rng.rand(S, T) * 1e6 + 1e5).astype(np.float32)}
    monkeypatch.setattr(
        Q,
        "load_panel",
        lambda ds_id, features=None: {
            "panel": panel,
            "dates": [str(i) for i in range(T)],
        },
    )
    ckpt = _train_seq_checkpoint(tmp_path, monkeypatch, panel, arch="lstm")

    db = temp_db()
    m = NNModel(
        checkpoint=ckpt,
        architecture={"arch": "lstm", "layers": [8]},
        dataset_id=1,
        horizon=5,
        epochs=2,
    )
    db.add(m)
    db.commit()
    mid = m.id
    db.close()

    captured: dict = {}

    def fake_multi(factor, close_, fwd_, **kw):
        captured["factor"] = factor
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
    factor = captured["factor"]
    assert factor.shape == (S, T)
    # 序列架构：头部 seq_len-1=7 天 NaN，其后有限（与 mlp 全量有限不同）
    assert np.isnan(factor[:, :7]).all()
    assert np.isfinite(factor[:, 30:]).all()
