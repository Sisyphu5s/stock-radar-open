"""DQN 个股仓位训练处理器(_run_rl_train + _persist_dqn_model)。

仿 neural_train.py 范式:参数校验 → 面板加载(与 neural_train 同源 dataset 面板)→
时间切分(60/20)→ GPU 互斥锁(_GPU_LOCK)→ train_dqn 逐 episode 进度上报 →
落库 NNModel(architecture={"arch": "dqn", ...}) → meta 组装。runner 依赖经
_deps._dyn 动态转发(测试 monkeypatch Q.SessionLocal/Q.NNModel/Q.load_panel/
Q.train_dqn 等穿透)。
"""

from __future__ import annotations

from ..errors import JobCancelled
from ..runner import DEFAULT_TIMEOUT_SEC, _GPU_LOCK, logger
from ._deps import _dyn
from ....lib.alpha.dqn import _MLX_OK as DQN_MLX_OK
from ....lib.alpha.dqn import train_dqn

# runner 辅助/模块状态:动态转发(测试 monkeypatch 穿透)
_start_running = _dyn("_start_running")
_track = _dyn("_track")
_set_phase = _dyn("_set_phase")
_checkpoint = _dyn("_checkpoint")
_update = _dyn("_update")
_terminal = _dyn("_terminal")
# runner 顶层 import 的业务符号:动态转发
load_panel = _dyn("load_panel")
split_panel = _dyn("split_panel")
SessionLocal = _dyn("SessionLocal")
NNModel = _dyn("NNModel")
utcnow = _dyn("utcnow")


def _persist_dqn_model(res: dict, params: dict, dataset_id: int) -> int | None:
    """DQN 产物落库 nn_models，返回新行 id。

    复用 NNModel:architecture={"arch": "dqn", **超参};epochs=episodes;
    train_loss 字段承载每 episode 平均奖励曲线(前端训练曲线复用,字段名历史遗留);
    train_ic/val_ic 为回归指标,DQN 无此语义,留 None。
    落库失败仅 logger.warning 不抛错——训练任务照常 done,返回 None 由调用方
    决定不给 meta 挂 model_id。
    """
    db = SessionLocal()
    try:
        m = NNModel(
            checkpoint=res.get("checkpoint"),
            architecture={"arch": "dqn", **params},
            dataset_id=dataset_id,
            horizon=1,  # DQN 奖励口径为日收益(持仓期 1 天)
            epochs=res.get("episodes"),
            train_loss=res.get("train_rewards"),
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    except Exception:  # noqa: BLE001
        logger.warning("DQN 模型落库失败（任务照常 done，无 model_id）", exc_info=True)
        return None
    finally:
        db.close()


def _run_rl_train(job_id: int, params: dict):
    """DQN 个股仓位训练实验（仅 mlx 可用，不静默回退）。

    数据准备（面板加载 + 60/20 训练/验证切分）→ train_dqn 逐 episode 进度上报
    → 落库 NNModel。GPU 互斥复用 runner._GPU_LOCK（MLX 独占，与 neural_train 排队
    语义一致：等待期间任务保持 running，每 1s 重试过协作控制点）。
    """
    try:
        if not _start_running(job_id, 1.0):
            return  # 已被取消/终态（delete_job 已写取消终态）
        _track(job_id, int(params.get("timeout_sec", DEFAULT_TIMEOUT_SEC)))

        # 参数校验：非法配置抛 ValueError → 任务 failed 并给出明确原因
        ds_id = int(params.get("dataset_id", 0))
        if ds_id <= 0:
            raise ValueError(f"dataset_id 必须是正整数，实际 {ds_id}")
        seq_len = int(params.get("seq_len", 20))
        if seq_len < 2:
            raise ValueError(f"seq_len 必须 ≥ 2，实际 {seq_len}")
        episodes = int(params.get("episodes", 50))
        if not 1 <= episodes <= 500:
            raise ValueError(f"episodes 必须在 1..500 之间，实际 {episodes}")
        lr = float(params.get("lr", 1e-3))
        if lr <= 0:
            raise ValueError(f"lr 必须 > 0，实际 {lr}")
        gamma = float(params.get("gamma", 0.99))
        if not 0 <= gamma < 1:
            raise ValueError(f"gamma 必须在 [0, 1)，实际 {gamma}")
        cost = float(params.get("cost", 0.001))
        if cost < 0:
            raise ValueError(f"cost 必须 ≥ 0，实际 {cost}")
        hidden = int(params.get("hidden", 64))
        if hidden < 1:
            raise ValueError(f"hidden 必须 ≥ 1，实际 {hidden}")
        batch_size = int(params.get("batch_size", 32))
        if batch_size < 1:
            raise ValueError(f"batch_size 必须 ≥ 1，实际 {batch_size}")
        capacity = int(params.get("capacity", 20000))
        if capacity < 1:
            raise ValueError(f"capacity 必须 ≥ 1，实际 {capacity}")
        target_sync_steps = int(params.get("target_sync_steps", 50))
        if target_sync_steps < 1:
            raise ValueError(f"target_sync_steps 必须 ≥ 1，实际 {target_sync_steps}")
        seed = int(params.get("seed", 42))
        # MLX 依赖预校验（与 nn.ARCH_REGISTRY available 语义一致：不静默回退）
        if not DQN_MLX_OK:
            raise ValueError(
                "任务 rl_train（DQN）需要 MLX（当前环境 mlx 不可用），"
                "可用任务类型: neural_train(mlp)/gp_run 等"
            )
        train_hparams = {
            "seq_len": seq_len,
            "episodes": episodes,
            "lr": lr,
            "gamma": gamma,
            "cost": cost,
            "hidden": hidden,
            "batch_size": batch_size,
            "capacity": capacity,
            "target_sync_steps": target_sync_steps,
            "seed": seed,
        }

        # 数据集（与 neural_train 同源：面板加载，仅 close/volume）
        panel_info = load_panel(ds_id, features=["close", "volume"])
        if panel_info is None:
            raise RuntimeError(f"数据集 {ds_id} 不可用")
        panel_full = panel_info["panel"]
        missing = [f for f in ("close", "volume") if f not in panel_full]
        if missing:
            raise ValueError(f"数据集 {ds_id} 面板缺少字段: {missing}")
        n_stocks, n_days = panel_full["close"].shape
        _set_phase(job_id, "数据准备", 3.0)

        # 时间切分:训练 60% / 验证 20%（horizon=1 日收益，与 DQN 奖励口径一致；
        # 切分机制与 neural_train 同源，先切分后独立构造 fwd，无未来泄漏）
        sp = split_panel(panel_full, 1, 0.6, 0.2)
        data_train, data_val = sp["train"], sp["val"]

        def progress_cb(episode, total):
            # 协作式控制点：暂停阻塞 / 取消抛 JobCancelled
            _checkpoint(job_id)
            p = 5 + 90 * (episode + 1) / total
            _update(
                job_id,
                progress=round(p, 1),
                phase=f"训练中 {episode + 1}/{total}",
            )

        # GPU 互斥：train_dqn 独占 MLX，等待循环语义与 neural_train 一致
        while True:
            _checkpoint(job_id)
            if _GPU_LOCK.acquire(timeout=1.0):
                break
        try:
            res = train_dqn(
                data_train=data_train,
                data_val=data_val,
                seq_len=seq_len,
                episodes=episodes,
                lr=lr,
                gamma=gamma,
                cost=cost,
                hidden=hidden,
                capacity=capacity,
                batch_size=batch_size,
                target_sync_steps=target_sync_steps,
                seed=seed,
                progress_cb=progress_cb,
            )
        finally:
            _GPU_LOCK.release()
        _set_phase(job_id, "评估", 96.0)
        _set_phase(job_id, "结果整理")
        # 训练产物落库 nn_models（失败不阻塞，meta 无 model_id）
        model_id = _persist_dqn_model(res, train_hparams, ds_id)
        meta = {
            "episodes": res.get("episodes"),
            "train_rewards": res.get("train_rewards"),
            "eval": res.get("eval"),
            "model_kind": res.get("model_kind"),
            "checkpoint": res.get("checkpoint"),
        }
        if model_id is not None:
            meta["model_id"] = model_id
        _terminal(
            job_id,
            status="done",
            progress=100.0,
            result={"results": [], "meta": meta},
            finished_at=utcnow(),
        )
    except JobCancelled:
        return  # 用户取消：取消状态已由 delete_job 写入，禁止覆盖
    except Exception as e:
        logger.exception("DQN 训练实验失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
