"""DQN 个股仓位模型：状态=NN 因子同源面板特征窗口，动作=离散三档仓位，奖励=持仓收益-交易成本。

与 lib/alpha/nn.py 同源：build_features 产出 (S,T,5) 横截面 rank 特征（ret1/ret5/
mom20/vol_ratio/vol_share），状态 s_t 为窗口 [t-seq_len+1, t] 的特征块；DQN 网络
（mlx.nn）输入展平窗口（seq_len×5）输出 3 个 Q 值；ε-greedy 探索（线性衰减）+
回放缓冲 + target 网络（周期性同步）；训练后冻结策略在验证段仿真评估（净值曲线/
总收益/夏普/最大回撤/交易次数/平均持仓）。

奖励口径（单一事实源 _DEFAULT_COST）：r_t = pos_t * ret_{t+1} - cost * |pos_t - pos_{t-1}|，
pos ∈ {0, 0.5, 1.0} 对应动作 {空仓, 半仓, 满仓}；ret 按 close 自行计算（build_features
输出的 ret1 已横截面 rank，不可还原为收益率，故不取自特征矩阵）。

仅 mlx 可用（与 nn.ARCH_REGISTRY 中 lstm/transformer 的 available 语义一致）：
mlx 不可用时训练直接报「需要 MLX」，不写 numpy 参考实现。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque

import numpy as np

from ...storage.paths import MODEL_DIR
from .nn import _flatten_params, _unflatten_params, build_features

logger = logging.getLogger("stockradar.alpha.dqn")

# 单一事实源：backend/cache/models（与 nn.py 同目录，checkpoint 前缀 dqn_ 区分）
_MODEL_DIR = str(MODEL_DIR)

_MLX_DISABLED = os.environ.get("SR_MLX_OFF") == "1" or os.environ.get("SR_GP_BACKEND") == "numpy"
if _MLX_DISABLED:
    mx = None
    nn = None
    opt = None
    _MLX_OK = False
else:
    try:
        import mlx.core as mx
        import mlx.nn as nn
        import mlx.optimizers as opt

        mx.array(np.zeros(2))  # 冒烟测试
        _MLX_OK = True
    except Exception as e:  # noqa: BLE001
        mx = None
        nn = None
        opt = None
        _MLX_OK = False
        logger.warning("mlx 不可用(%s)，dqn 训练不可用（需要 MLX）", str(e)[:80])

# 动作空间：0 空仓 / 1 半仓 / 2 满仓；_POSITION 为动作→仓位比例（奖励/评估口径）
_ACTIONS = (0, 1, 2)
_POSITION = (0.0, 0.5, 1.0)
_N_ACTIONS = len(_ACTIONS)

# 单边交易成本（与回测成本口径一致，显式常量单一事实源；10bps/次）
_DEFAULT_COST = 0.001

# 训练默认超参（handler 缺省复用，单一事实源）
_DEFAULT_HPARAMS = {
    "seq_len": 20,
    "episodes": 50,
    "lr": 1e-3,
    "gamma": 0.99,
    "cost": _DEFAULT_COST,
    "hidden": 64,
    "capacity": 20000,
    "batch_size": 32,
    "target_sync_steps": 50,
    "epsilon_start": 1.0,
    "epsilon_end": 0.05,
}


def compute_reward(pos_prev: float, pos: float, ret: float, cost: float) -> float:
    """单步奖励：r = pos * ret - cost * |pos - pos_prev|（持仓收益 − 换仓成本）。

    纯函数（单元测试点）：pos 为仓位比例（0/0.5/1.0），ret 为下一期收益，
    cost 为单边交易成本（与回测成本口径一致）。
    """
    return pos * ret - cost * abs(pos - pos_prev)


def _daily_returns(close: np.ndarray) -> np.ndarray:
    """面板日收益 (S, T-1)：ret[s,t] = close[s,t+1]/close[s,t] - 1（float64）。"""
    close = np.asarray(close, dtype=np.float64)
    return close[:, 1:] / close[:, :-1] - 1.0


# ---------------------------------------------------------------------------
# 网络与回放缓冲（仅 mlx 路径；类定义受 _MLX_OK 保护，不可用时仅占位）
# ---------------------------------------------------------------------------
if _MLX_OK:

    class _DQNet(nn.Module):
        """DQN 网络：输入展平窗口 (seq_len×5) → MLP(64, 64) → 3 个 Q 值。"""

        def __init__(
            self, input_dim: int, hidden: int = 64, n_actions: int = _N_ACTIONS
        ):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(input_dim, hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.ReLU(),
                nn.Linear(hidden, n_actions),
            )

        def __call__(self, x):
            # 输入 (B, seq_len, 5) → 展平 (B, seq_len*5)（调用方统一传窗口块）
            b = x.shape[0]
            return self.net(x.reshape(b, -1))

else:

    class _DQNet:  # 占位：mlx 不可用时不可实例化
        def __init__(self, *a, **k):
            raise ValueError("arch 'dqn' 需要 MLX（当前环境 mlx 不可用）")


class _ReplayBuffer:
    """回放缓冲（deque 循环缓冲，capacity 超限自动丢最旧）。"""

    def __init__(self, capacity: int):
        self._buf: deque = deque(maxlen=int(capacity))

    def push(self, state, action, reward, next_state, done) -> None:
        self._buf.append((state, int(action), float(reward), next_state, bool(done)))

    def sample(self, batch_size: int, rng: np.random.RandomState):
        """均匀采样（允许重复），返回 (states, actions, rewards, next_states, dones)。"""
        n = min(batch_size, len(self._buf))
        idx = rng.randint(len(self._buf), size=n)
        states = np.stack([self._buf[i][0] for i in idx])
        actions = np.asarray([self._buf[i][1] for i in idx], dtype=np.int32)
        rewards = np.asarray([self._buf[i][2] for i in idx], dtype=np.float32)
        next_states = np.stack([self._buf[i][3] for i in idx])
        dones = np.asarray([self._buf[i][4] for i in idx], dtype=np.float32)
        return states, actions, rewards, next_states, dones

    def __len__(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------------------
# 训练
# ---------------------------------------------------------------------------
def _train_step(
    net, target, optimizer, replay: _ReplayBuffer, batch_size: int, gamma: float, rng
) -> float:
    """单步 DQN 更新：TD loss = mean((Q(s,a) − (r + γ·max_a' Q_target(s',a')))²)。

    target 网络参数不参与梯度（mx.stop_gradient）；返回本 batch TD loss。
    """
    states, actions, rewards, next_states, dones = replay.sample(batch_size, rng)
    X = mx.array(states)
    A = mx.array(actions)
    R = mx.array(rewards)
    Xn = mx.array(next_states)
    D = mx.array(dones)

    def loss_fn(p):
        net.update(p)
        q = net(X)
        q_sa = mx.take_along_axis(q, A[:, None], axis=1).reshape(-1)
        q_next = target(Xn)
        tgt = mx.stop_gradient(R + gamma * mx.max(q_next, axis=-1) * (1.0 - D))
        return mx.mean((q_sa - tgt) ** 2)

    loss, grads = mx.value_and_grad(loss_fn)(net.parameters())
    optimizer.update(net, grads)
    mx.eval(net.parameters(), optimizer.state)
    return float(loss.item())


def _window_ok(valid_mask: np.ndarray, seq_len: int) -> list[np.ndarray]:
    """每只股票窗口有效性（右端点 t=seq_len-1..T-1）：[t-seq_len+1, t] 内特征全有效。

    与 nn.py 序列架构样本口径一致（cumsum 差判定）；返回长度 T-seq_len+1 的 bool 数组列表。
    """
    out = []
    for s in range(valid_mask.shape[0]):
        cs = np.concatenate([[0], np.cumsum(valid_mask[s])])
        out.append(cs[seq_len:] - cs[:-seq_len] == seq_len)
    return out


def _eps_schedule(
    epsilon_start: float, epsilon_end: float, episode: int, episodes: int
) -> float:
    """ε 线性衰减：episode 0 起 epsilon_start → 末 episode epsilon_end（episodes=1 恒为 start）。"""
    if episodes <= 1:
        return float(epsilon_start)
    return float(
        epsilon_start + (epsilon_end - epsilon_start) * episode / (episodes - 1)
    )


def _run_episode(
    net,
    replay: _ReplayBuffer,
    F: np.ndarray,
    window_ok: list[np.ndarray],
    rets: np.ndarray,
    *,
    s: int,
    seq_len: int,
    cost: float,
    eps: float,
    rng: np.random.RandomState,
) -> tuple[float, int]:
    """单 episode：股票 s 沿时间顺序决策，产生经验入回放。

    窗口有效（特征全有限）且 ret 有限才 step；无效时点维持仓位不动作（无奖励无成本）。
    返回 (累计奖励, step 数)。
    """
    pos = 0.0
    total = 0.0
    n_steps = 0
    ok = window_ok[s]
    T = F.shape[1]
    for t in range(seq_len - 1, T - 1):
        if not ok[t - (seq_len - 1)] or not np.isfinite(rets[s, t]):
            continue
        state = F[s, t - seq_len + 1 : t + 1].astype(np.float32)
        if rng.rand() < eps:
            a = int(rng.randint(_N_ACTIONS))
        else:
            q = net(mx.array(state[None, ...]))
            a = int(np.asarray(q).argmax())
        new_pos = _POSITION[a]
        r = compute_reward(pos, new_pos, float(rets[s, t]), cost)
        nxt_t = t + 1
        done = True
        next_state = state
        if nxt_t < T - 1 and ok[nxt_t - (seq_len - 1)] and np.isfinite(rets[s, nxt_t]):
            next_state = F[s, nxt_t - seq_len + 1 : nxt_t + 1].astype(np.float32)
            done = False
        replay.push(state, a, r, next_state, done)
        pos = new_pos
        total += r
        n_steps += 1
    return total, n_steps


def evaluate_policy(
    net,
    F: np.ndarray,
    valid_mask: np.ndarray,
    close: np.ndarray,
    cost: float,
    seq_len: int,
) -> dict:
    """冻结策略（ε=0 贪心）在验证段仿真评估。

    每只股票独立从空仓沿时间顺序决策；返回：
    {"equity": 净值曲线（cumprod(1+r)，r=pos·ret 不含换仓成本）,
     "total_return": 期末累计收益, "sharpe": 夏普, "max_drawdown": 最大回撤,
     "trades": 动作变化次数, "avg_pos": 平均持仓比例}
    """
    from ...lib.metrics import max_drawdown, sharpe_ratio

    rets = _daily_returns(close)
    window_ok = _window_ok(valid_mask, seq_len)
    returns: list[float] = []
    trades = 0
    pos_sum = 0.0
    n_steps = 0
    for s in range(F.shape[0]):
        pos = 0.0
        ok = window_ok[s]
        T = F.shape[1]
        for t in range(seq_len - 1, T - 1):
            if not ok[t - (seq_len - 1)] or not np.isfinite(rets[s, t]):
                continue
            state = F[s, t - seq_len + 1 : t + 1].astype(np.float32)
            q = net(mx.array(state[None, ...]))
            a = int(np.asarray(q).argmax())
            new_pos = _POSITION[a]
            if new_pos != pos:
                trades += 1
            returns.append(new_pos * float(rets[s, t]))
            pos_sum += new_pos
            n_steps += 1
            pos = new_pos
    arr = np.asarray(returns, dtype=np.float64)
    if len(arr) == 0:
        return {
            "equity": [],
            "total_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "trades": 0,
            "avg_pos": 0.0,
        }
    equity = np.cumprod(1.0 + arr)
    return {
        "equity": [round(float(v), 6) for v in equity],
        "total_return": round(float(equity[-1] - 1.0), 6),
        "sharpe": round(float(sharpe_ratio(arr)), 6),
        "max_drawdown": round(float(max_drawdown(arr)), 6),
        "trades": int(trades),
        "avg_pos": round(float(pos_sum / n_steps), 6),
    }


def _save_checkpoint(
    weights: dict, hparams: dict, feat_mean: np.ndarray, feat_std: np.ndarray
) -> str:
    """网络参数 + 超参 + 特征统计落盘 backend/cache/models/dqn_{ts}.npz。"""
    os.makedirs(_MODEL_DIR, exist_ok=True)
    path = os.path.join(_MODEL_DIR, f"dqn_{int(time.time())}.npz")
    np.savez(
        path,
        **weights,
        arch="dqn",
        arch_hparams=json.dumps(hparams, ensure_ascii=False),
        feat_mean=np.asarray(feat_mean, dtype=np.float32),
        feat_std=np.asarray(feat_std, dtype=np.float32),
    )
    return path


def train_dqn(
    *,
    data_train: dict,
    data_val: dict,
    seq_len: int = _DEFAULT_HPARAMS["seq_len"],
    episodes: int = _DEFAULT_HPARAMS["episodes"],
    lr: float = _DEFAULT_HPARAMS["lr"],
    gamma: float = _DEFAULT_HPARAMS["gamma"],
    cost: float = _DEFAULT_HPARAMS["cost"],
    hidden: int = _DEFAULT_HPARAMS["hidden"],
    capacity: int = _DEFAULT_HPARAMS["capacity"],
    batch_size: int = _DEFAULT_HPARAMS["batch_size"],
    target_sync_steps: int = _DEFAULT_HPARAMS["target_sync_steps"],
    epsilon_start: float = _DEFAULT_HPARAMS["epsilon_start"],
    epsilon_end: float = _DEFAULT_HPARAMS["epsilon_end"],
    seed: int = 42,
    progress_cb=None,
) -> dict:
    """训练 DQN 个股仓位模型（mlx.nn 自动微分，仅 mlx 可用）。

    状态：build_features 的 (S,T,5) 特征，窗口 [t-seq_len+1, t]（右端点即决策时点，
    无未来泄漏）；动作 {0,1,2} → 仓位 {0, 0.5, 1.0}；奖励见 compute_reward。
    训练：episodes × 随机选股沿时间顺序 step；ε-greedy（线性衰减）；回放缓冲采样
    mini-batch 更新，target 网络每 target_sync_steps 步同步。

    返回契约：
    {"train_rewards": list[float]（每 episode 平均奖励）,
     "eval": {"equity": [...], "total_return", "sharpe", "max_drawdown", "trades", "avg_pos"},
     "episodes": int, "model_kind": "dqn", "checkpoint": str}

    progress_cb(episode, total) 每 episode 调一次（0 起始，末次 (episodes, episodes)）。
    """
    seq_len = int(seq_len)
    episodes = int(episodes)
    hidden = int(hidden)
    capacity = int(capacity)
    batch_size = int(batch_size)
    target_sync_steps = int(target_sync_steps)
    lr = float(lr)
    gamma = float(gamma)
    cost = float(cost)
    seed = int(seed)
    if seq_len < 2:
        raise ValueError(f"seq_len 必须 ≥ 2，实际 {seq_len}")
    if not 1 <= episodes <= 500:
        raise ValueError(f"episodes 必须在 1..500 之间，实际 {episodes}")
    if hidden < 1:
        raise ValueError(f"hidden 必须 ≥ 1，实际 {hidden}")
    if capacity < 1:
        raise ValueError(f"capacity 必须 ≥ 1，实际 {capacity}")
    if batch_size < 1:
        raise ValueError(f"batch_size 必须 ≥ 1，实际 {batch_size}")
    if target_sync_steps < 1:
        raise ValueError(f"target_sync_steps 必须 ≥ 1，实际 {target_sync_steps}")
    if lr <= 0:
        raise ValueError(f"lr 必须 > 0，实际 {lr}")
    if not 0 <= gamma < 1:
        raise ValueError(f"gamma 必须在 [0, 1)，实际 {gamma}")
    if cost < 0:
        raise ValueError(f"cost 必须 ≥ 0，实际 {cost}")

    F_tr, m_tr = build_features(data_train)
    F_vl, m_vl = build_features(data_val)
    close_tr = np.asarray(data_train["close"], dtype=np.float64)
    close_vl = np.asarray(data_val["close"], dtype=np.float64)
    rets_tr = _daily_returns(close_tr)
    if F_tr.shape[1] < seq_len + 1 or F_vl.shape[1] < seq_len + 1:
        raise ValueError(
            f"面板时间轴不足（训练 {F_tr.shape[1]} 天 / 验证 {F_vl.shape[1]} 天），"
            f"需要 ≥ seq_len+1 = {seq_len + 1}"
        )

    if not _MLX_OK:
        raise ValueError("arch 'dqn' 需要 MLX（当前环境 mlx 不可用）")

    input_dim = seq_len * F_tr.shape[-1]
    net = _DQNet(input_dim, hidden)
    target = _DQNet(input_dim, hidden)
    target.update(net.parameters())
    mx.eval(target.parameters())
    optimizer = opt.Adam(learning_rate=lr)
    replay = _ReplayBuffer(capacity)
    rng = np.random.RandomState(seed)

    window_ok_tr = _window_ok(m_tr, seq_len)
    n_stocks = F_tr.shape[0]
    train_rewards: list[float] = []
    global_step = 0
    if progress_cb:
        progress_cb(0, episodes)
    for ep in range(episodes):
        eps = _eps_schedule(epsilon_start, epsilon_end, ep, episodes)
        s = int(rng.randint(n_stocks))
        total, n_steps = _run_episode(
            net,
            replay,
            F_tr,
            window_ok_tr,
            rets_tr,
            s=s,
            seq_len=seq_len,
            cost=cost,
            eps=eps,
            rng=rng,
        )
        if len(replay) >= batch_size:
            for _ in range(max(1, n_steps // batch_size)):
                _train_step(net, target, optimizer, replay, batch_size, gamma, rng)
                global_step += 1
                if global_step % target_sync_steps == 0:
                    target.update(net.parameters())
                    mx.eval(target.parameters())
        train_rewards.append(round(total / max(n_steps, 1), 6))
        if progress_cb:
            progress_cb(ep + 1, episodes)

    eval_res = evaluate_policy(net, F_vl, m_vl, close_vl, cost, seq_len)
    hparams = {
        "seq_len": seq_len,
        "episodes": episodes,
        "lr": lr,
        "gamma": gamma,
        "cost": cost,
        "hidden": hidden,
        "capacity": capacity,
        "batch_size": batch_size,
        "target_sync_steps": target_sync_steps,
        "epsilon_start": float(epsilon_start),
        "epsilon_end": float(epsilon_end),
        "seed": seed,
    }
    flat = _flatten_params(net.parameters())
    valid = m_tr
    feat_rows = F_tr.reshape(-1, F_tr.shape[-1])[valid.reshape(-1)]
    if len(feat_rows) == 0:
        # 空有效行（valid 全 False）：np.nanmean/np.nanstd 对空数组返回 NaN，
        # 落盘会让 checkpoint 携带非法特征统计；兜底 mean=0/std=1，与归一化
        # 无操作语义一致（(x-0)/1），保证加载侧不产生 NaN。
        feat_mean = np.zeros(F_tr.shape[-1], dtype=np.float32)
        feat_std = np.ones(F_tr.shape[-1], dtype=np.float32)
    else:
        feat_mean = np.nanmean(feat_rows, axis=0)
        feat_std = np.nanstd(feat_rows, axis=0)
    ckpt = _save_checkpoint(flat, hparams, feat_mean, feat_std)
    return {
        "train_rewards": train_rewards,
        "eval": eval_res,
        "episodes": episodes,
        "model_kind": "dqn",
        "checkpoint": ckpt,
    }


def load_checkpoint(path: str) -> dict:
    """加载 dqn checkpoint npz：{"arch": "dqn", "arch_hparams": dict, "weights": dict,
    "feat_mean"/"feat_std": np.ndarray}。文件不存在/损坏时抛 ValueError。"""
    if not os.path.exists(path):
        raise ValueError(f"checkpoint 不存在: {path}")
    try:
        with np.load(path, allow_pickle=False) as d:
            meta_keys = {"arch", "arch_hparams", "feat_mean", "feat_std"}
            weights = {k: d[k] for k in d.files if k not in meta_keys}
            arch = str(np.asarray(d["arch"]).item())
            arch_hparams = json.loads(str(np.asarray(d["arch_hparams"]).item()))
            feat_mean = np.asarray(d["feat_mean"], dtype=np.float32)
            feat_std = np.asarray(d["feat_std"], dtype=np.float32)
    except ValueError as e:
        raise ValueError(f"checkpoint 损坏或无法读取: {path}（{str(e)[:100]}）") from e
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"checkpoint 损坏或无法读取: {path}（{str(e)[:100]}）") from e
    if arch != "dqn":
        raise ValueError(f"checkpoint 非 dqn 模型（arch={arch}）: {path}")
    if not weights:
        raise ValueError(f"checkpoint 缺少权重: {path}")
    return {
        "arch": arch,
        "arch_hparams": arch_hparams,
        "weights": {k: np.asarray(v, dtype=np.float32) for k, v in weights.items()},
        "feat_mean": feat_mean,
        "feat_std": feat_std,
    }
