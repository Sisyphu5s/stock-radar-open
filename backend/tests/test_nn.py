"""MLP 神经网络训练模块测试：双实现一致性 / 返回契约与 checkpoint / 进度回调 / 抽样。"""

import os

import numpy as np
import pytest

from app.lib.alpha import nn
from app.lib.alpha.evaluate import compute_ic


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


def _train_kwargs(epochs=10, **over):
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


def test_build_features_shape_and_dtype():
    data, _ = _make_panel(S=20, T=60)
    F, mask = nn.build_features(data)
    assert F.shape == (20, 60, 5)
    assert F.dtype == np.float32
    assert mask.shape == (20, 60)
    assert mask.dtype == bool
    assert mask.any()  # 横截面 rank 后 F 无 NaN，掩码恒有效（与原 regressors 行为一致）


@pytest.mark.skipif(not nn._MLX_OK, reason="mlx 不可用，无法对比双实现")
def test_mlx_vs_numpy_loss_curve():
    """同 seed 同数据：mlx 与 numpy 训练 loss 曲线形状一致（末值相对误差 < 1e-1）。"""
    data, fwd = _make_panel()
    kw = _train_kwargs(
        epochs=10,
        data_train=data,
        forward_returns=fwd,
        data_val=data,
        val_forward_returns=fwd,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(nn, "_MLX_OK", False)
        out_np = nn.train_mlp(**kw)
    out_mx = nn.train_mlp(**kw)

    assert len(out_np["train_loss"]) == len(out_mx["train_loss"]) == 10
    a, b = out_np["train_loss"][-1], out_mx["train_loss"][-1]
    rel = abs(a - b) / max(abs(b), 1e-12)
    assert rel < 1e-1, f"numpy {a} vs mlx {b} 相对误差 {rel}"
    # 两实现 IC 也应接近（同一训练目标）
    assert out_np["val_ic"] is None or abs(out_np["val_ic"] - out_mx["val_ic"]) < 0.2


def test_contract_and_checkpoint():
    data, fwd = _make_panel()
    out = nn.train_mlp(
        **_train_kwargs(
            epochs=5,
            data_train=data,
            forward_returns=fwd,
            data_val=data,
            val_forward_returns=fwd,
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
        assert k in out, f"契约字段缺失: {k}"
    assert out["model_kind"] == "mlp"
    assert out["epochs"] == 5
    assert len(out["train_loss"]) == 5
    assert len(out["val_loss"]) == 5
    assert isinstance(out["checkpoint"], str)
    assert os.path.exists(out["checkpoint"])
    with np.load(out["checkpoint"]) as d:
        assert "W0" in d.files and "b0" in d.files
        assert "W2" in d.files  # 3 个权重矩阵（2 隐藏 + 1 输出）
        assert d["layers"].tolist() == [32, 16]
    assert out["val_ic"] is None or isinstance(out["val_ic"], float)
    # predict 内部契约：预测 shape 与有效样本一致且全有限
    F, m = nn.build_features(data)
    pred = nn.predict(F.reshape(-1, 5)[m.reshape(-1)])
    assert pred.shape == (int(m.sum()),)
    assert np.isfinite(pred).all()
    # IC 计算路径可跑（compute_ic 直接复用）
    factor = np.full(m.shape, np.nan, dtype=np.float32)
    factor[m] = pred
    assert np.isfinite(compute_ic(factor, fwd))


def test_progress_cb_called_epochs_plus_one():
    data, fwd = _make_panel()
    calls = []
    out = nn.train_mlp(
        **_train_kwargs(
            epochs=10,
            data_train=data,
            forward_returns=fwd,
            data_val=data,
            val_forward_returns=fwd,
            progress_cb=lambda e, t: calls.append((e, t)),
        )
    )
    assert len(calls) == 11  # epochs + 1
    assert calls[0] == (0, 10)
    assert calls[-1] == (10, 10)
    assert [e for e, _ in calls] == list(range(11))
    assert all(t == 10 for _, t in calls)


def test_on_epoch_loss_receives_per_epoch_loss():
    """T-57：on_epoch_loss(epoch, total, train_loss, val_loss) 每 epoch 回调，
    值与返回曲线逐点一致；progress_cb 旧契约不受影响。"""
    data, fwd = _make_panel()
    seen = []
    out = nn.train_mlp(
        **_train_kwargs(
            epochs=4,
            data_train=data,
            forward_returns=fwd,
            data_val=data,
            val_forward_returns=fwd,
            progress_cb=lambda e, t: None,  # 旧契约回调并存不冲突
            on_epoch_loss=lambda e, t, tr, va: seen.append((e, t, tr, va)),
        )
    )
    assert len(seen) == 4
    assert [e for e, *_ in seen] == [1, 2, 3, 4]
    assert all(t == 4 for _, t, _, _ in seen)
    for i, (e, t, tr, va) in enumerate(seen):
        assert (e, t) == (i + 1, 4)
        assert tr == out["train_loss"][i]
        assert va == out["val_loss"][i]
    assert all(np.isfinite(tr) and np.isfinite(va) for _, _, tr, va in seen)


def test_subsample_when_over_max_rows():
    """有效样本 > max_rows 时按 seed 抽样，训练不崩溃。"""
    data, fwd = _make_panel(S=20, T=60)
    out = nn.train_mlp(
        **_train_kwargs(
            epochs=2,
            max_rows=50,
            data_train=data,
            forward_returns=fwd,
            data_val=data,
            val_forward_returns=fwd,
        )
    )
    assert out["epochs"] == 2
    assert len(out["train_loss"]) == 2
