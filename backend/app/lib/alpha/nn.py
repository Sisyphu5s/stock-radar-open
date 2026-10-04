"""神经网络训练模块：mlp（numpy 参考实现 + mlx 加速）+ 序列架构（lstm/transformer，mlx.nn 自动微分）。

作为因子挖掘 "neural" 算法的训练后端：输入 5 维横截面 rank 特征
（ret1/ret5/mom20/vol_ratio/vol_share），训练小回归器预测未来收益，
输出每 epoch 损失曲线 + 训练/验证 IC，训练完成后 checkpoint 落盘（仅网络权重）。

架构分发（ARCH_REGISTRY）：
- mlp：mlx 可用时走 mlx（GPU），任何异常自动回退 numpy 参考实现；两实现算法一致
  （同权重初始化、同 mini-batch 切分顺序、同前向/反向公式），仅浮点顺序与精度差异。
- lstm/transformer：mlx.nn 模块 + mx.value_and_grad 自动微分，仅 mlx 可用（不可用时
  明确报错，不静默回退）；样本按股票滑窗（seq_len），窗口右端点预测（无未来泄漏）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time

import numpy as np

from ...storage.paths import CACHE_DIR, MODEL_DIR
from .backend import mlx_probe

logger = logging.getLogger("stockradar.alpha.nn")

# 单一事实源：backend/cache/models（lib 层 checkpoint I/O，S4 卡记账例外；
# test_backtest_nn 以 monkeypatch 覆盖 _MODEL_DIR 走 tmp 目录）
_MODEL_DIR = str(MODEL_DIR)
# 历史遗留兼容符号：backend 根目录
_BACKEND_ROOT = str(CACHE_DIR.parent)

# T-120/P1-63:mlx 顶层导入改造——无 Metal 环境 import mlx 可能进程级 abort(except 拦不住),
# 先经 backend.mlx_probe() 子进程隔离探测(abort 只杀子进程),确认可用才在主进程 import。
# SR_MLX_OFF=1 / SR_GP_BACKEND=numpy 可显式跳过(零 MLX 触发,纯 numpy 路径)。
mx = None
nn = None
_MLX_OK = False
if mlx_probe():
    try:
        import mlx.core as mx
        import mlx.nn as nn

        mx.array(np.zeros(2))  # 冒烟测试(探测已通过,此处防御性兜底)
        _MLX_OK = True
    except Exception as e:  # noqa: BLE001
        mx = None
        nn = None
        _MLX_OK = False
        logger.warning("mlx 导入失败(%s)，neural 训练走 numpy 参考实现", str(e)[:80])

_ACTIVATIONS = ("relu", "tanh", "sigmoid")

# 架构白名单（ARCH_REGISTRY 的键，train_mlp/handler 校验共用）
_ARCH_WHITELIST = ("mlp", "lstm", "transformer")

# 最近一次训练的权重快照，供模块级 predict 使用（内部契约，权重由 train_mlp 维护）
_MODEL_STATE: dict = {
    "weights": None,
    "activation": "relu",
    "arch": "mlp",
    "arch_hparams": {},
}
# 保护 _MODEL_STATE 读写：并发 train_mlp 竞态覆盖、predict 读到半写状态。
# train_mlp 持锁仅覆盖 _MODEL_STATE 整体赋值（训练过程本身不加锁），predict 锁内
# 只取引用、锁外计算——训练期间 predict 返回旧权重，完成后读到新权重。
_MODEL_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 特征构建（自 regressors.run_neural 的局部函数提取复用；契约：F 为 float32）
# ---------------------------------------------------------------------------
def build_features(data: dict) -> tuple[np.ndarray, np.ndarray]:
    """从面板提取 5 特征（ret1/ret5/mom20/vol_ratio/vol_share，横截面 rank 0-1）。

    返回 (F, valid_mask)：F shape (S,T,5) float32；valid_mask shape (S,T) 全有限掩码。
    """

    def _cs_rank(a: np.ndarray) -> np.ndarray:
        order = np.argsort(np.argsort(a, axis=0), axis=0)
        return order / max(a.shape[0] - 1, 1)

    def _roll_mean(a: np.ndarray, w: int) -> np.ndarray:
        cs = np.cumsum(a, axis=1)
        out = np.full_like(a, np.nan)
        if w <= 0:
            return out
        for t in range(w, a.shape[1] + 1):
            # t=w 时 cs[:, -1] 会越界取最后一列，用 0（窗口 [0..w-1] 前缀和）
            prev = cs[:, t - 1 - w] if (t - 1 - w) >= 0 else 0.0
            out[:, t - 1] = (cs[:, t - 1] - prev) / w
        p = min(w - 1, a.shape[1])
        if p > 0:
            out[:, :p] = cs[:, :p] / np.arange(1, p + 1)
        return out

    close = np.asarray(data["close"], dtype=np.float64)
    ret1 = np.full_like(close, np.nan)
    ret1[:, 1:] = close[:, 1:] / close[:, :-1] - 1.0
    ret5 = np.full_like(close, np.nan)
    ret5[:, 5:] = close[:, 5:] / close[:, :-5] - 1.0
    mom20 = close / _roll_mean(close, 20) - 1.0
    vol = np.asarray(data.get("volume", np.ones_like(close)), dtype=np.float64)
    vol_ratio = vol / _roll_mean(vol, 20)
    vol_share = vol / np.nanmean(vol, axis=0, keepdims=True)
    feats = [_cs_rank(f) for f in (ret1, ret5, mom20, vol_ratio, vol_share)]
    F = np.stack(feats, axis=-1).astype(np.float32)
    return F, np.all(np.isfinite(F), axis=-1)


def _prepare_samples(
    F: np.ndarray,
    valid_mask: np.ndarray,
    y: np.ndarray,
    max_rows: int,
    seed: int,
    arch_hparams: dict | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """面板展平 + 有效性过滤 + max_rows 抽样（RandomState(seed)）。

    返回 (X, y)：X (n,5)、y (n,)，均为 float32。
    arch_hparams 为注册表 make_samples 统一接口的占位参数（mlp 忽略）。
    """
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    flat_mask = valid_mask.reshape(-1) & np.isfinite(y)
    X = F.reshape(-1, F.shape[-1])[flat_mask]
    y = y[flat_mask]
    if max_rows and len(y) > max_rows:
        idx = np.random.RandomState(seed).choice(len(y), max_rows, replace=False)
        X, y = X[idx], y[idx]
    return X, y


def _make_seq_samples(
    F: np.ndarray,
    valid_mask: np.ndarray,
    y: np.ndarray,
    max_rows: int,
    seed: int,
    arch_hparams: dict | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """序列架构样本组织：按股票分组滑动窗口，保留序列结构。

    对每只股票 s，右端点 t（t = seq_len-1 .. T-1）取窗口 [t-seq_len+1, t]：
    窗口内特征全部有效（valid_mask 全真）且 y[s, t] 有限才成样本。
    返回 (X, y)：X (n, seq_len, F) float32、y (n,) float32——窗口右端点即
    预测时点（LSTM 因果；Transformer 窗口限定在历史段，均无未来泄漏）。
    """
    seq_len = int((arch_hparams or {})["seq_len"])
    y = np.asarray(y, dtype=np.float32)
    S, T, D = F.shape
    rows: list[tuple[int, int]] = []
    for s in range(S):
        # 向量化窗口有效性：cumsum 差判定 [t-seq_len+1, t] 内全 True
        cs = np.concatenate([[0], np.cumsum(valid_mask[s])])
        window_ok = cs[seq_len:] - cs[:-seq_len] == seq_len
        y_ok = np.isfinite(y[s, seq_len - 1 :])
        for t in np.where(window_ok & y_ok)[0]:
            rows.append((s, t + seq_len - 1))
    if not rows:
        return np.zeros((0, seq_len, D), dtype=np.float32), np.zeros(
            0, dtype=np.float32
        )
    X = np.stack([F[s, t - seq_len + 1 : t + 1] for s, t in rows]).astype(np.float32)
    yr = np.asarray([y[s, t] for s, t in rows], dtype=np.float32)
    if max_rows and len(yr) > max_rows:
        idx = np.random.RandomState(seed).choice(len(yr), max_rows, replace=False)
        X, yr = X[idx], yr[idx]
    return X, yr


# ---------------------------------------------------------------------------
# 网络结构与训练（numpy 参考实现）
# ---------------------------------------------------------------------------
def _init_weights(sizes: list[int], seed: int) -> dict[str, np.ndarray]:
    """He 初始化；numpy 生成后 mlx 实现原样转换，保证双实现起点一致。"""
    rng = np.random.RandomState(seed)
    weights: dict[str, np.ndarray] = {}
    for i in range(len(sizes) - 1):
        fan_in = max(sizes[i], 1)
        scale = float(np.sqrt(2.0 / fan_in))
        weights[f"W{i}"] = (rng.randn(sizes[i], sizes[i + 1]) * scale).astype(
            np.float32
        )
        weights[f"b{i}"] = np.zeros(sizes[i + 1], dtype=np.float32)
    return weights


def _act(z: np.ndarray, activation: str) -> np.ndarray:
    if activation == "relu":
        return np.maximum(z, 0.0)
    if activation == "tanh":
        return np.tanh(z)
    return 1.0 / (1.0 + np.exp(-z))  # sigmoid


def _dact(h: np.ndarray, activation: str) -> np.ndarray:
    """激活函数对 post-activation 输入的导数（与 pre-activation 逐元素等价）。"""
    if activation == "relu":
        return (h > 0.0).astype(np.float32)
    if activation == "tanh":
        return 1.0 - h * h
    return h * (1.0 - h)  # sigmoid


def _forward_np(X: np.ndarray, weights: dict, activation: str) -> list[np.ndarray]:
    """numpy 前向传播，返回每层输出 hs（hs[-1] 为网络输出，shape (n,1)）。"""
    hs = [np.asarray(X, dtype=np.float32)]
    h = hs[0]
    n_layers = len(weights) // 2
    for i in range(n_layers):
        z = h @ weights[f"W{i}"] + weights[f"b{i}"]
        h = z if i == n_layers - 1 else _act(z, activation)
        hs.append(h)
    return hs


def _fit_numpy(
    X: np.ndarray,
    y: np.ndarray,
    *,
    layers: list[int],
    activation: str,
    lr: float,
    epochs: int,
    batch_size: int,
    seed: int,
    progress_cb=None,
    on_epoch_loss=None,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
) -> tuple[list[float], list[float], dict[str, np.ndarray]]:
    """mini-batch SGD（不 shuffle，双实现共享同一 batch 顺序）。

    返回 (train_losses, val_losses, weights)；X_val/y_val 非 None 时每 epoch 评估一次。
    on_epoch_loss（T-57 可选）：每 epoch 回调 (epoch, total, train_loss, val_loss)，
    供任务层发布实时损失增量事件；不影响 progress_cb 旧契约。
    """
    sizes = [X.shape[1]] + list(layers) + [1]
    weights = _init_weights(sizes, seed)
    n = X.shape[0]
    n_layers = len(weights) // 2
    losses: list[float] = []
    val_losses: list[float] = []
    if progress_cb:
        progress_cb(0, epochs)
    for epoch in range(1, epochs + 1):
        epoch_loss = 0.0
        n_batches = 0
        for start in range(0, n, batch_size):
            xb = X[start : start + batch_size]
            yb = y[start : start + batch_size]
            hs = _forward_np(xb, weights, activation)
            diff = hs[-1] - yb[:, None]
            # backward（先算梯度、更新参数，再记录本 batch 前向 loss）
            g = 2.0 * diff / xb.shape[0]
            for i in range(n_layers - 1, -1, -1):
                gW = hs[i].T @ g
                gb = g.sum(axis=0)
                if i > 0:
                    g = (g @ weights[f"W{i}"].T) * _dact(hs[i], activation)
                weights[f"W{i}"] = weights[f"W{i}"] - lr * gW
                weights[f"b{i}"] = weights[f"b{i}"] - lr * gb
            epoch_loss += float(np.mean(diff * diff))
            n_batches += 1
        losses.append(epoch_loss / max(n_batches, 1))
        if X_val is not None and len(y_val):
            pred = _forward_np(X_val, weights, activation)[-1].reshape(-1)
            val_losses.append(float(np.mean((pred - y_val) ** 2)))
        if on_epoch_loss:
            on_epoch_loss(
                epoch, epochs, losses[-1], val_losses[-1] if val_losses else None
            )
        if progress_cb:
            progress_cb(epoch, epochs)
    return losses, val_losses, weights


# ---------------------------------------------------------------------------
# mlx 加速实现（与 numpy 参考实现算法一致，仅浮点实现差异）
# ---------------------------------------------------------------------------
def _act_mlx(z, activation):
    if activation == "relu":
        return mx.maximum(z, 0.0)
    if activation == "tanh":
        return mx.tanh(z)
    return 1.0 / (1.0 + mx.exp(-z))  # sigmoid


def _dact_mlx(h, activation):
    if activation == "relu":
        return (h > 0.0).astype(mx.float32)
    if activation == "tanh":
        return 1.0 - h * h
    return h * (1.0 - h)  # sigmoid


def _fit_mlx(
    X: np.ndarray,
    y: np.ndarray,
    *,
    layers: list[int],
    activation: str,
    lr: float,
    epochs: int,
    batch_size: int,
    seed: int,
    progress_cb=None,
    on_epoch_loss=None,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
) -> tuple[list[float], list[float], dict[str, np.ndarray]]:
    """mlx 版 mini-batch SGD；每 epoch 结束转回 numpy 计算 loss，返回 numpy 权重。
    on_epoch_loss 语义同 _fit_numpy（T-57 实时损失增量回调）。"""
    sizes = [X.shape[1]] + list(layers) + [1]
    init = _init_weights(sizes, seed)
    Ws = [mx.array(init[f"W{i}"]) for i in range(len(sizes) - 1)]
    bs = [mx.array(init[f"b{i}"]) for i in range(len(sizes) - 1)]
    Xa, ya = mx.array(X), mx.array(y)
    n = X.shape[0]
    n_layers = len(Ws)
    losses: list[float] = []
    val_losses: list[float] = []
    if progress_cb:
        progress_cb(0, epochs)
    for epoch in range(1, epochs + 1):
        epoch_loss = 0.0
        n_batches = 0
        for start in range(0, n, batch_size):
            xb = Xa[start : start + batch_size]
            yb = ya[start : start + batch_size]
            # forward
            hs = [xb]
            h = xb
            for i in range(n_layers):
                z = h @ Ws[i] + bs[i]
                h = z if i == n_layers - 1 else _act_mlx(z, activation)
                hs.append(h)
            diff = hs[-1] - mx.expand_dims(yb, 1)
            # backward
            g = 2.0 * diff / hs[-1].shape[0]
            for i in range(n_layers - 1, -1, -1):
                gW = hs[i].T @ g
                gb = g.sum(axis=0)
                if i > 0:
                    g = (g @ Ws[i].T) * _dact_mlx(hs[i], activation)
                Ws[i] = Ws[i] - lr * gW
                bs[i] = bs[i] - lr * gb
            epoch_loss += float(np.array(mx.mean(diff * diff)))
            n_batches += 1
        losses.append(epoch_loss / max(n_batches, 1))
        if X_val is not None and len(y_val):
            val_w = {f"W{i}": np.array(Ws[i]) for i in range(n_layers)} | {
                f"b{i}": np.array(bs[i]) for i in range(n_layers)
            }
            pred = _forward_np(X_val, val_w, activation)[-1].reshape(-1)
            val_losses.append(float(np.mean((pred - y_val) ** 2)))
        if on_epoch_loss:
            on_epoch_loss(
                epoch, epochs, losses[-1], val_losses[-1] if val_losses else None
            )
        if progress_cb:
            progress_cb(epoch, epochs)
    weights: dict[str, np.ndarray] = {}
    for i in range(n_layers):
        weights[f"W{i}"] = np.array(Ws[i])
        weights[f"b{i}"] = np.array(bs[i])
    return losses, val_losses, weights


# ---------------------------------------------------------------------------
# 序列架构（lstm / transformer）：mlx.nn 模块 + mx.value_and_grad 自动微分
# 训练走 _fit_seq（与 MLP 相同语义：MSE loss、SGD、mini-batch、epoch 结构），
# 模块权重为 mlx 嵌套参数 dict，checkpoint 前 _flatten_params 展平为 npz 键。
# 类定义受 _MLX_OK 保护：mlx 不可用时仅占位（不会实例化，调用方先查 available）。
# ---------------------------------------------------------------------------
if _MLX_OK:

    class _LSTMRegressor(nn.Module):
        """LSTM 回归器：堆叠 LSTM 编码序列，取最后一步隐状态过线性头。"""

        def __init__(
            self, input_dim: int, hidden: int, num_layers: int = 1, dropout: float = 0.0
        ):
            super().__init__()
            self.lstms = [
                nn.LSTM(input_dim if i == 0 else hidden, hidden)
                for i in range(max(1, num_layers))
            ]
            self.dropout = nn.Dropout(dropout) if dropout > 0 else None
            self.head = nn.Linear(hidden, 1)

        def __call__(self, x):
            h = x
            for i, lstm in enumerate(self.lstms):
                h, _ = lstm(h)
                if i < len(self.lstms) - 1 and self.dropout is not None:
                    h = self.dropout(h)
            return self.head(h[:, -1, :])

    class _TransformerRegressor(nn.Module):
        """Transformer 编码器回归器：线性嵌入 + 正弦位置编码 + 编码器，取末位过线性头。"""

        def __init__(
            self,
            input_dim: int,
            dims: int,
            num_layers: int = 2,
            num_heads: int = 4,
            mlp_dims: int | None = None,
            dropout: float = 0.0,
        ):
            super().__init__()
            self.embed = nn.Linear(input_dim, dims)
            self.pos = nn.SinusoidalPositionalEncoding(dims)
            self.enc = nn.TransformerEncoder(
                max(1, num_layers), dims, num_heads, mlp_dims, dropout=dropout
            )
            self.head = nn.Linear(dims, 1)

        def __call__(self, x):
            pos = self.pos(mx.arange(x.shape[-2])[None, :])
            h = self.embed(x) + pos
            h = self.enc(h, mask=None)  # mlx 0.32 TransformerEncoder 需显式 mask
            return self.head(h[:, -1, :])

else:

    class _LSTMRegressor:  # 占位：mlx 不可用时不可实例化
        def __init__(self, *a, **k):
            raise ValueError("arch 'lstm' 需要 MLX（当前环境 mlx 不可用）")

    class _TransformerRegressor:  # 占位：mlx 不可用时不可实例化
        def __init__(self, *a, **k):
            raise ValueError("arch 'transformer' 需要 MLX（当前环境 mlx 不可用）")


def _build_arch_module(input_dim: int, arch: str, arch_hparams: dict):
    """按 arch/arch_hparams 构建 mlx.nn 模块（mlp 返回 None——走 numpy/mlx 双实现）。"""
    if arch == "lstm":
        return _LSTMRegressor(
            input_dim,
            hidden=arch_hparams["hidden"],
            num_layers=arch_hparams["num_layers"],
            dropout=arch_hparams["dropout"],
        )
    if arch == "transformer":
        return _TransformerRegressor(
            input_dim,
            dims=arch_hparams["dims"],
            num_layers=arch_hparams["num_layers"],
            num_heads=arch_hparams["num_heads"],
            mlp_dims=arch_hparams["mlp_dims"],
            dropout=arch_hparams["dropout"],
        )
    return None


def _flatten_params(params, prefix: str = "") -> dict[str, np.ndarray]:
    """mlx 嵌套参数（dict/list 混合）→ 扁平 dict（键 "." 连接），值转 numpy float32。

    list 项以数字索引为键段（如 lstms.0.Wx），与 mlx parameters() 的容器结构对应。
    """
    flat: dict[str, np.ndarray] = {}
    items = (
        params.items()
        if isinstance(params, dict)
        else enumerate(params)
        if isinstance(params, (list, tuple))
        else []
    )
    if not items and not isinstance(params, (dict, list, tuple)):
        return {prefix: np.asarray(params, dtype=np.float32)}
    for k, v in items:
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, (dict, list, tuple)):
            flat.update(_flatten_params(v, key))
        else:
            flat[key] = np.asarray(v, dtype=np.float32)
    return flat


def _unflatten_params(flat: dict) -> dict:
    """扁平参数 dict（"." 连接键）→ mlx 嵌套结构（dict/list，module.update 可用）。

    段序判定容器：list 节点按数字索引定位；dict 节点看下一段是否纯数字——
    是则本段建 list（如 lstms.0.Wx → lstms 为 list），否则建 dict。
    叶子值由 numpy float32 转回 mx.array（仅供 mlx 路径调用，mlx 不可用时
    该路径已被 available 拦截）。
    """
    root: dict = {}

    def _leaf(v):
        return mx.array(np.asarray(v, dtype=np.float32))

    def _place(node, parts, i, v):
        seg = parts[i]
        last = i == len(parts) - 1
        if isinstance(node, list):
            idx = int(seg)
            while len(node) <= idx:
                node.append({})
            if last:
                node[idx] = _leaf(v)
            else:
                _place(node[idx], parts, i + 1, v)
            return
        if last:
            node[seg] = _leaf(v)
            return
        if parts[i + 1].isdigit():  # 下一段是索引 → 本段为 list
            lst = node.get(seg)
            if lst is None:
                lst = []
                node[seg] = lst
            _place(lst, parts, i + 1, v)
        else:
            child = node.get(seg)
            if child is None:
                child = {}
                node[seg] = child
            _place(child, parts, i + 1, v)

    for key, v in flat.items():
        _place(root, str(key).split("."), 0, v)
    return root


def _fit_seq(
    X: np.ndarray,
    y: np.ndarray,
    *,
    arch: str,
    arch_hparams: dict,
    lr: float,
    epochs: int,
    batch_size: int,
    seed: int,
    progress_cb=None,
    on_epoch_loss=None,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
) -> tuple[list[float], list[float], dict[str, np.ndarray]]:
    """序列架构训练：mlx.nn 模块 + mx.value_and_grad 自动微分 + SGD（Adam 语义对齐 MLP 的 SGD）。

    loss = mean((pred - y)^2)，与 MLP 双实现的损失语义一致（梯度路径不同，
    数值精度略异）。返回 (train_losses, val_losses, flat_weights dict[str, np.ndarray])。
    on_epoch_loss 语义同 _fit_numpy（T-57 实时损失增量回调）。
    """
    import mlx.optimizers as opt

    module = _build_arch_module(X.shape[-1], arch, arch_hparams)
    params = module.parameters()
    mx.eval(params)
    optimizer = opt.SGD(learning_rate=lr)

    def loss_fn(p, x, y):
        module.update(p)
        return mx.mean((module(x) - y[:, None]) ** 2)

    n = X.shape[0]
    losses: list[float] = []
    val_losses: list[float] = []
    if progress_cb:
        progress_cb(0, epochs)
    for epoch in range(1, epochs + 1):
        epoch_loss = 0.0
        n_batches = 0
        for start in range(0, n, batch_size):
            xb = mx.array(X[start : start + batch_size])
            yb = mx.array(y[start : start + batch_size])
            loss, grads = mx.value_and_grad(loss_fn)(module.parameters(), xb, yb)
            optimizer.update(module, grads)
            mx.eval(module.parameters(), optimizer.state)
            epoch_loss += float(loss.item())
            n_batches += 1
        losses.append(epoch_loss / max(n_batches, 1))
        if X_val is not None and len(y_val):
            module.eval()
            pred = module(mx.array(X_val)).reshape(-1)
            val_losses.append(float(mx.mean((pred - mx.array(y_val)) ** 2).item()))
            module.train()
        if on_epoch_loss:
            on_epoch_loss(
                epoch, epochs, losses[-1], val_losses[-1] if val_losses else None
            )
        if progress_cb:
            progress_cb(epoch, epochs)
    return losses, val_losses, _flatten_params(module.parameters())


# 架构注册表（仿 regressors.REGRESSORS 范式；available=False 的架构在任务提交时
# 给出明确错误）。build: 输入维度/超参 → mlx.nn 模块；make_samples: 特征/标签 →
# 训练样本（mlp 展平、lstm/transformer 按股票滑窗）。mlp 的 build 为 None——
# 走现有 numpy/mlx 双实现，不构建 mlx.nn 模块。
ARCH_REGISTRY: dict[str, dict] = {
    "mlp": {
        "name": "MLP 多层感知机",
        "desc": "全连接回归器：numpy 参考实现 + mlx 加速双实现",
        "available": True,
        "default_hparams": {},
        "build": None,
        "make_samples": _prepare_samples,
    },
    "lstm": {
        "name": "LSTM 长短期记忆",
        "desc": "堆叠 LSTM 回归器：mlx.nn 自动微分（需要 MLX）",
        "available": _MLX_OK,
        "default_hparams": {
            "hidden": 32,
            "num_layers": 1,
            "seq_len": 20,
            "dropout": 0.0,
        },
        "build": _build_arch_module,
        "make_samples": _make_seq_samples,
    },
    "transformer": {
        "name": "Transformer 编码器",
        "desc": "Transformer 编码器回归器：mlx.nn 自动微分（需要 MLX）",
        "available": _MLX_OK,
        "default_hparams": {
            "dims": 32,
            "num_layers": 2,
            "num_heads": 4,
            "mlp_dims": 64,
            "seq_len": 20,
            "dropout": 0.0,
        },
        "build": _build_arch_module,
        "make_samples": _make_seq_samples,
    },
}


def _resolve_arch_hparams(arch: str, arch_hparams) -> dict:
    """合并注册表默认超参与校验（未知字段/数值类型/依赖约束），返回完整 hparams。

    mlp 无额外超参（layers/activation 为独立参数），仅做 dict 类型检查、返回 {}。
    """
    if arch == "mlp":
        if arch_hparams is not None and not isinstance(arch_hparams, dict):
            raise ValueError("arch_hparams 必须是 dict")
        return {}
    defaults = dict(ARCH_REGISTRY[arch]["default_hparams"])
    if arch_hparams is not None:
        if not isinstance(arch_hparams, dict):
            raise ValueError("arch_hparams 必须是 dict")
        for k, v in arch_hparams.items():
            if k not in defaults:
                raise ValueError(
                    f"arch_hparams 未知字段 {k!r}（可用: {sorted(defaults)}）"
                )
            defaults[k] = v
    seq_len = int(defaults["seq_len"])
    if seq_len < 2:
        raise ValueError(f"seq_len 必须 ≥ 2，实际 {seq_len}")
    dropout = float(defaults["dropout"])
    if not 0.0 <= dropout < 1.0:
        raise ValueError(f"dropout 必须在 [0, 1)，实际 {dropout}")
    if arch == "lstm":
        hidden, num_layers = int(defaults["hidden"]), int(defaults["num_layers"])
        if hidden < 1 or num_layers < 1:
            raise ValueError("lstm hidden/num_layers 必须 ≥ 1")
        defaults["hidden"], defaults["num_layers"] = hidden, num_layers
    else:  # transformer
        dims, num_heads, num_layers = (
            int(defaults["dims"]),
            int(defaults["num_heads"]),
            int(defaults["num_layers"]),
        )
        if dims < 2 or dims % 2 != 0:
            raise ValueError(
                f"transformer dims 必须为正偶数（Sinusoidal 位置编码要求），实际 {dims}"
            )
        if num_heads < 1 or dims % num_heads != 0:
            raise ValueError(
                f"transformer num_heads 必须能整除 dims（{dims} % {num_heads} != 0）"
            )
        if num_layers < 1:
            raise ValueError("transformer num_layers 必须 ≥ 1")
        mlp_dims = defaults.get("mlp_dims")
        defaults["dims"], defaults["num_heads"], defaults["num_layers"] = (
            dims,
            num_heads,
            num_layers,
        )
        defaults["mlp_dims"] = None if mlp_dims is None else int(mlp_dims)
    defaults["seq_len"], defaults["dropout"] = seq_len, dropout
    return defaults


# ---------------------------------------------------------------------------
# 评估与 checkpoint
# ---------------------------------------------------------------------------
def _predict_seq(
    F: np.ndarray,
    valid_mask: np.ndarray,
    weights: dict,
    arch: str,
    arch_hparams: dict,
) -> np.ndarray:
    """序列架构面板预测：按股票滑窗重建序列，返回 (S,T) float32 因子。

    每只股票独立处理：右端点 t 的窗口 [t-seq_len+1, t] 内特征全有效才预测；
    头部 t < seq_len-1 与窗口不完整的时点保持 NaN（与训练样本口径一致）。
    """
    module = _build_arch_module(F.shape[-1], arch, arch_hparams)
    module.update(_unflatten_params(weights))
    module.eval()
    seq_len = int(arch_hparams["seq_len"])
    S, T = F.shape[:2]
    factor = np.full((S, T), np.nan, dtype=np.float32)
    for s in range(S):
        valid = valid_mask[s]
        cs = np.concatenate([[0], np.cumsum(valid)])
        window_ok = cs[seq_len:] - cs[:-seq_len] == seq_len
        ts = np.where(window_ok)[0] + (seq_len - 1)
        if not len(ts):
            continue
        Xw = np.stack([F[s, t - seq_len + 1 : t + 1] for t in ts]).astype(np.float32)
        pred = module(mx.array(Xw))
        factor[s, ts] = np.asarray(pred, dtype=np.float32).reshape(-1)
    return factor


def _predict_factor(
    F: np.ndarray,
    valid_mask: np.ndarray,
    weights: dict,
    activation: str,
    arch: str = "mlp",
    arch_hparams: dict | None = None,
) -> np.ndarray:
    """按 arch 生成 (S,T) float32 因子：mlp 展平前向 / 序列按股票滑窗重建。"""
    if arch != "mlp":
        return _predict_seq(F, valid_mask, weights, arch, arch_hparams or {})
    factor = np.full(valid_mask.shape, np.nan, dtype=np.float32)
    flat = valid_mask.reshape(-1)
    if flat.any():
        X = F.reshape(-1, F.shape[-1])[flat]
        factor[valid_mask] = _forward_np(X, weights, activation)[-1].reshape(-1)
    return factor


def _eval_ic(
    F: np.ndarray,
    valid_mask: np.ndarray,
    fwd: np.ndarray,
    weights: dict,
    activation: str,
    arch: str = "mlp",
    arch_hparams: dict | None = None,
) -> float:
    """预测还原到 (S,T) 有效位置，与真实 forward_returns 算 Spearman IC。"""
    from .evaluate import compute_ic

    factor = _predict_factor(F, valid_mask, weights, activation, arch, arch_hparams)
    return compute_ic(factor, np.asarray(fwd, dtype=np.float32))


# ---------------------------------------------------------------------------
# 权重/注意力可视化（T-57）：训练完成后生成 viz 供前端 heatmap 展示
# ---------------------------------------------------------------------------
_VIZ_MAX_SIDE = 64  # 可视化矩阵最大边长（超过按行列等距抽样）


def _resample_matrix(
    mat: np.ndarray, max_side: int = _VIZ_MAX_SIDE
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """矩阵行列等距抽样至边长 ≤ max_side，返回 (抽样矩阵, 行索引, 列索引)。"""
    r, c = mat.shape
    ridx = (
        np.linspace(0, r - 1, min(r, max_side)).astype(int)
        if r > max_side
        else np.arange(r)
    )
    cidx = (
        np.linspace(0, c - 1, min(c, max_side)).astype(int)
        if c > max_side
        else np.arange(c)
    )
    return mat[np.ix_(ridx, cidx)], ridx, cidx


def _viz_matrix(mat: np.ndarray, kind: str) -> dict:
    """矩阵 → viz dict（精度截断 round 4；labels 为抽样后的行列索引，可省略）。

    float64 先转再 round：float32 直接 tolist 会带尾数（如 0.6861000061035156）。
    """
    mat, ridx, cidx = _resample_matrix(np.asarray(mat, dtype=np.float32))
    return {
        "kind": kind,
        "matrix": np.round(mat.astype(np.float64), 4).tolist(),
        "row_labels": [str(int(i)) for i in ridx],
        "col_labels": [str(int(j)) for j in cidx],
    }


def _viz_mlp(weights: dict) -> dict:
    """mlp 末层权重矩阵 [hidden, 1]（输出投影层 W_{n-1}）：行=隐藏单元、列=输出。"""
    n_layers = len(weights) // 2
    return _viz_matrix(weights[f"W{n_layers - 1}"], "weights")


def _viz_lstm(module) -> dict:
    """末层 LSTM 权重：Wx(输入投影) 与 Wh(隐态投影) 水平拼接 [4H, input+hidden]。

    拼接而非分开，单矩阵一次渲染；行序为 ifgo 门块（4×H），前端标题标注 Wx/Wh。
    """
    lstm = module.lstms[-1]
    wx = np.asarray(lstm.Wx, dtype=np.float32)  # (4H, input)
    wh = np.asarray(lstm.Wh, dtype=np.float32)  # (4H, hidden)
    return _viz_matrix(np.concatenate([wx, wh], axis=1), "lstm_weights")


def _viz_transformer(module, xb) -> dict:
    """最后一层注意力矩阵 [seq_len, seq_len]：query/key 投影后缩放点积 softmax，mean over heads。

    与 mlx.nn.MultiHeadAttention 前向逐式一致（query_proj/key_proj → unflatten 分头 →
    transpose → 缩放点积 → softmax），无 mask（本项目 enc(mask=None)）。
    取训练最后一批的单个样本（xb 为 (1, seq_len, dims)），注意力输入遵循
    TransformerEncoderLayer.norm_first 语义（默认 True：ln1 之后进 attention）。
    """
    seq_len = xb.shape[-2]
    pos = module.pos(mx.arange(seq_len)[None, :])
    h = module.embed(xb) + pos
    enc = module.enc
    for layer in enc.layers[:-1]:
        h = layer(h, mask=None)
    last = enc.layers[-1]
    y = last.ln1(h) if getattr(last, "norm_first", True) else h
    attn = last.attention
    num_heads = attn.num_heads
    q = mx.unflatten(attn.query_proj(y), -1, (num_heads, -1)).transpose(0, 2, 1, 3)
    k = mx.unflatten(attn.key_proj(y), -1, (num_heads, -1)).transpose(0, 2, 1, 3)
    scale = math.sqrt(1 / q.shape[-1])
    scores = q @ k.transpose(0, 1, 3, 2) * scale
    w = mx.softmax(scores, axis=-1)  # (n, heads, seq, seq)
    mat = np.asarray(mx.mean(w, axis=1)[0], dtype=np.float32)  # 单样本 mean over heads
    return _viz_matrix(mat, "attention")


def _build_viz(
    arch: str, weights: dict, Xtr: np.ndarray, arch_hparams: dict
) -> dict | None:
    """按 arch 生成权重/注意力可视化 dict；任何异常返回 None（不阻断训练任务）。

    viz 数据量控制：矩阵边长 ≤ 64（_resample_matrix）、float 精度 round 4。
    """
    try:
        if arch == "mlp":
            return _viz_mlp(weights)
        module = _build_arch_module(Xtr.shape[-1], arch, arch_hparams)
        module.update(_unflatten_params(weights))
        module.eval()
        if arch == "lstm":
            return _viz_lstm(module)
        return _viz_transformer(module, mx.array(Xtr[-1:]))
    except Exception as e:  # noqa: BLE001
        logger.warning("权重可视化生成失败（viz=null，不阻断任务）: %s", str(e)[:80])
        return None


def _save_checkpoint(
    weights: dict,
    *,
    layers: list[int],
    activation: str,
    engine: str,
    arch: str = "mlp",
    arch_hparams: dict | None = None,
) -> str:
    """权重落盘 backend/cache/models/nn_{int(time.time())}.npz（仅权重与轻量元信息）。

    arch/arch_hparams 为 T-56 新增元信息（序列架构权重键为 "." 连接的扁平参数）。
    """
    os.makedirs(_MODEL_DIR, exist_ok=True)
    path = os.path.join(_MODEL_DIR, f"nn_{int(time.time())}.npz")
    np.savez(
        path,
        **weights,
        layers=np.asarray(list(layers)),
        activation=str(activation),
        engine=str(engine),
        arch=str(arch),
        arch_hparams=json.dumps(arch_hparams or {}, ensure_ascii=False),
    )
    return path


def _infer_layers(weights: dict) -> list[int]:
    """旧格式 checkpoint（仅权重）推导隐藏层：由 W0..W_{n-2} 的输出维度构成。"""
    ws = sorted((k for k in weights if k.startswith("W")), key=lambda k: int(k[1:]))
    if not ws:
        raise ValueError("checkpoint 缺少权重矩阵（未找到 W0）")
    return [int(weights[w].shape[1]) for w in ws[:-1]]


def _validate_weights(weights: dict, layers: list[int]) -> None:
    """权重结构校验：W/b 成对、形状链自洽（输入 dim → layers → 输出 1）。"""
    ws = sorted((k for k in weights if k.startswith("W")), key=lambda k: int(k[1:]))
    bs = sorted((k for k in weights if k.startswith("b")), key=lambda k: int(k[1:]))
    if len(ws) != len(bs) or [int(k[1:]) for k in ws] != list(range(len(ws))):
        raise ValueError(f"checkpoint 权重不完整（W/b 不配对: {sorted(weights)}）")
    if len(ws) != len(layers) + 1:
        raise ValueError(f"checkpoint 权重层数({len(ws)})与架构 layers({layers})不匹配")
    dims = [int(weights[ws[0]].shape[0])] + [int(x) for x in layers] + [1]
    for i, w in enumerate(ws):
        if weights[w].shape != (dims[i], dims[i + 1]):
            raise ValueError(
                f"checkpoint 权重 {w} 形状 {weights[w].shape} 与架构不符"
                f"（期望 {(dims[i], dims[i + 1])}）"
            )
        if weights[f"b{i}"].shape != (dims[i + 1],):
            raise ValueError(f"checkpoint 偏置 b{i} 形状 {weights[f'b{i}'].shape} 异常")


def load_checkpoint(path: str) -> dict:
    """加载 checkpoint npz，返回 {"layers": [...], "activation": str, "weights": dict,
    "arch": str, "arch_hparams": dict}。

    兼容三种格式：
    - 新格式（含 arch）：arch/arch_hparams 原样读回；
    - 旧新格式（layers/activation，无 arch）：arch 默认 mlp、arch_hparams 空；
    - 旧格式：仅权重数组（W0/b0/W1/b1...），layers 由 W 形状推导，activation 默认 relu。
    文件不存在 / 损坏 / 结构非法时抛 ValueError（带明确信息）。
    """
    if not os.path.exists(path):
        raise ValueError(f"checkpoint 不存在: {path}")
    try:
        with np.load(path, allow_pickle=False) as d:
            meta_keys = {"layers", "activation", "engine", "arch", "arch_hparams"}
            weights = {k: d[k] for k in d.files if k not in meta_keys}
            if "layers" in d.files:
                layers = [int(x) for x in np.asarray(d["layers"]).reshape(-1)]
            else:
                layers = _infer_layers(weights)
            if "activation" in d.files:
                activation = str(np.asarray(d["activation"]).item())
            else:
                activation = "relu"
            arch = str(np.asarray(d["arch"]).item()) if "arch" in d.files else "mlp"
            if "arch_hparams" in d.files:
                arch_hparams = json.loads(str(np.asarray(d["arch_hparams"]).item()))
            else:
                arch_hparams = {}
    except ValueError as e:
        # np.load 对非 npz 文件（如 pickle 数据）也抛 ValueError，统一包装为损坏信息
        raise ValueError(f"checkpoint 损坏或无法读取: {path}（{str(e)[:100]}）") from e
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"checkpoint 损坏或无法读取: {path}（{str(e)[:100]}）") from e
    if arch == "mlp":
        _validate_weights(weights, layers)
    elif not weights:
        raise ValueError(f"checkpoint 缺少权重: {path}")
    return {
        "layers": layers,
        "activation": activation,
        "weights": {k: np.asarray(v, dtype=np.float32) for k, v in weights.items()},
        "arch": arch,
        "arch_hparams": arch_hparams,
    }


def predict_panel(
    weights: dict,
    activation: str,
    data: dict,
    arch: str = "mlp",
    arch_hparams: dict | None = None,
) -> np.ndarray:
    """对面板数据预测，返回 (S,T) float32 因子（与面板同形）。

    按 arch 分发：mlp 展平为 (S*T,5) 仅有效行预测（valid_mask），无效行保持 NaN；
    lstm/transformer 按股票滑窗重建序列（头部 seq_len-1 天无预测为 NaN）。
    数值边界与 train_mlp 内部（_eval_ic 还原路径）一致。
    """
    F, valid_mask = build_features(data)
    return _predict_factor(F, valid_mask, weights, activation, arch, arch_hparams)


def predict(features: np.ndarray) -> np.ndarray:
    """内部使用：基于最近一次 train_mlp 的权重前向预测，返回 (n,) 或 (n,1)。

    mlp：numpy 前向，输入 (n,5)；序列架构：mlx.nn 前向，输入 (n, seq_len, 5)。
    """
    with _MODEL_LOCK:
        if _MODEL_STATE["weights"] is None:
            raise RuntimeError("nn.predict 需先调用 train_mlp")
        weights = _MODEL_STATE["weights"]
        activation = _MODEL_STATE["activation"]
        arch = _MODEL_STATE["arch"]
        arch_hparams = _MODEL_STATE["arch_hparams"]
    if arch == "mlp":
        return _forward_np(features, weights, activation)[-1].reshape(-1)
    module = _build_arch_module(features.shape[-1], arch, arch_hparams)
    module.update(_unflatten_params(weights))
    module.eval()
    out = module(mx.array(np.asarray(features, dtype=np.float32)))
    return np.asarray(out, dtype=np.float32).reshape(-1)


# ---------------------------------------------------------------------------
# 训练入口（固定契约；T-56 扩展 arch 分发）
# ---------------------------------------------------------------------------
def train_mlp(
    *,
    data_train: dict,
    forward_returns: np.ndarray,
    data_val: dict,
    val_forward_returns: np.ndarray,
    layers: list[int],
    activation: str,
    lr: float,
    epochs: int,
    batch_size: int,
    seed: int,
    max_rows: int,
    progress_cb=None,
    on_epoch_loss=None,
    arch: str = "mlp",
    arch_hparams: dict | None = None,
) -> dict:
    """训练神经网络回归器（arch 分发）。

    arch='mlp' 走现有 numpy/mlx 双实现路径（行为与改动前完全一致）；
    arch='lstm'/'transformer' 走 mlx.nn 模块 + mx.value_and_grad 自动微分，
    mlx 不可用时抛 ValueError（arch 需要 MLX）。

    返回契约（固定）：
    {"train_loss": list[float]（每 epoch 一个，样本内小批 MSE 均值）,
     "val_loss": list[float], "train_ic": float|None, "val_ic": float|None,
     "epochs": int, "model_kind": arch, "checkpoint": str|None}

    progress_cb(epoch, total) 每 epoch 调一次（0 起始，末次 (epochs, epochs)）。
    on_epoch_loss（T-57 可选）：每 epoch 回调 (epoch, total, train_loss, val_loss)，
    值即该 epoch 最终 loss（val_loss 无验证集时为 None）——供任务层发布实时损失
    增量事件，不影响 progress_cb 旧契约。
    样本组织：mlp 展平为 (S*T, 5) 按 valid_mask 过滤；序列架构按股票滑窗
    （seq_len 窗口，右端点预测）。数据切分由调用方完成。
    """
    layers = list(layers)
    activation = (activation or "relu").lower()
    if activation not in _ACTIVATIONS:
        raise ValueError(f"activation 必须是 {_ACTIVATIONS} 之一，实际 {activation}")
    if len(layers) < 1:
        raise ValueError("layers 至少需要 1 个隐藏层")
    if arch not in _ARCH_WHITELIST:
        raise ValueError(f"arch 必须是 {_ARCH_WHITELIST} 之一，实际 {arch}")
    arch_hparams = _resolve_arch_hparams(arch, arch_hparams)
    lr = float(lr)
    epochs = int(epochs)
    batch_size = int(batch_size)
    seed = int(seed)
    max_rows = int(max_rows) if max_rows else 0

    Ftr, mtr = build_features(data_train)
    make_samples = ARCH_REGISTRY[arch]["make_samples"]
    Xtr, ytr = make_samples(Ftr, mtr, forward_returns, max_rows, seed, arch_hparams)
    Fvl, mvl = build_features(data_val)
    Xvl, yvl = make_samples(Fvl, mvl, val_forward_returns, max_rows, seed, arch_hparams)
    if len(ytr) < 10:
        raise RuntimeError(f"neural 训练样本不足（有效样本 {len(ytr)} < 10）")
    if len(yvl) < 10:
        raise RuntimeError(f"neural 验证样本不足（有效样本 {len(yvl)} < 10）")

    if arch == "mlp":
        # 原双实现路径（零行为变更）
        fit_kwargs = dict(
            layers=layers,
            activation=activation,
            lr=lr,
            epochs=epochs,
            batch_size=batch_size,
            seed=seed,
            progress_cb=progress_cb,
            on_epoch_loss=on_epoch_loss,
            X_val=Xvl,
            y_val=yvl,
        )
        engine = "numpy"
        if _MLX_OK:
            try:
                train_loss, val_loss, weights = _fit_mlx(Xtr, ytr, **fit_kwargs)
                engine = "mlx"
            except Exception as e:  # noqa: BLE001
                logger.warning("mlx 训练失败(%s)，回退 numpy 参考实现", str(e)[:80])
                train_loss, val_loss, weights = _fit_numpy(Xtr, ytr, **fit_kwargs)
        else:
            train_loss, val_loss, weights = _fit_numpy(Xtr, ytr, **fit_kwargs)
    else:
        # 序列架构：仅 mlx（不静默回退——mlx 不可用即明确失败）
        if not _MLX_OK:
            raise ValueError(f"arch '{arch}' 需要 MLX（当前环境 mlx 不可用）")
        train_loss, val_loss, weights = _fit_seq(
            Xtr,
            ytr,
            arch=arch,
            arch_hparams=arch_hparams,
            lr=lr,
            epochs=epochs,
            batch_size=batch_size,
            seed=seed,
            progress_cb=progress_cb,
            on_epoch_loss=on_epoch_loss,
            X_val=Xvl,
            y_val=yvl,
        )
        engine = "mlx"

    with _MODEL_LOCK:
        _MODEL_STATE["weights"] = weights
        _MODEL_STATE["activation"] = activation
        _MODEL_STATE["arch"] = arch
        _MODEL_STATE["arch_hparams"] = arch_hparams

    tr_ic = _eval_ic(Ftr, mtr, forward_returns, weights, activation, arch, arch_hparams)
    vl_ic = _eval_ic(
        Fvl, mvl, val_forward_returns, weights, activation, arch, arch_hparams
    )
    ckpt = _save_checkpoint(
        weights,
        layers=layers,
        activation=activation,
        engine=engine,
        arch=arch,
        arch_hparams=arch_hparams,
    )
    viz = _build_viz(arch, weights, Xtr, arch_hparams)

    return {
        "train_loss": [float(v) for v in train_loss],
        "val_loss": [float(v) for v in val_loss],
        "train_ic": float(tr_ic) if np.isfinite(tr_ic) else None,
        "val_ic": float(vl_ic) if np.isfinite(vl_ic) else None,
        "epochs": epochs,
        "model_kind": arch,
        "checkpoint": ckpt,
        "viz": viz,
    }
