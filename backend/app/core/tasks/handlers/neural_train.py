"""MLP 神经网络训练处理器(_run_neural_train + _persist_nn_model)。

自 runner.py 逐字搬移,仅 import 路径调整;runner 依赖经 _deps._dyn 动态转发
(测试 monkeypatch Q.SessionLocal/Q.NNModel/Q.load_panel/Q.train_mlp 等穿透),
GPU 互斥锁 _GPU_LOCK 直接绑定(runner 同一锁对象)。
"""

from __future__ import annotations

from ..errors import JobCancelled
from ..runner import DEFAULT_TIMEOUT_SEC, _GPU_LOCK, logger
from ._deps import _dyn
from ...events import publish as _publish_event
from ....lib.alpha.nn import ARCH_REGISTRY

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
train_mlp = _dyn("train_mlp")
SessionLocal = _dyn("SessionLocal")
NNModel = _dyn("NNModel")
utcnow = _dyn("utcnow")


def _persist_nn_model(
    res: dict,
    layers: list[int],
    activation: str,
    dataset_id: int,
    horizon: int,
    arch: str = "mlp",
    arch_hparams: dict | None = None,
) -> int | None:
    """NN 训练产物落库 nn_models，返回新行 id。

    落库失败（DB 异常等）仅 logger.warning 不抛错——训练任务照常 done，
    返回 None 由调用方决定不给 meta 挂 model_id。
    """
    db = SessionLocal()
    try:
        m = NNModel(
            checkpoint=res.get("checkpoint"),
            architecture={
                "arch": arch,
                "layers": list(layers),
                "activation": activation,
                **(arch_hparams or {}),
            },
            dataset_id=dataset_id,
            horizon=horizon,
            epochs=res.get("epochs"),
            train_ic=res.get("train_ic"),
            val_ic=res.get("val_ic"),
            train_loss=res.get("train_loss"),
            val_loss=res.get("val_loss"),
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    except Exception:  # noqa: BLE001
        logger.warning("NN 模型落库失败（任务照常 done，无 model_id）", exc_info=True)
        return None
    finally:
        db.close()


def _run_neural_train(job_id: int, params: dict):
    """神经网络训练实验（arch 分发：mlp 双实现 / lstm/transformer 需 MLX）。

    数据准备（面板加载 + 60/20 训练/验证切分 + forward returns）→ train_mlp
    逐 epoch 进度上报（phase 文案）→ 结果落库。不调用 _apply_backend：
    train_mlp 内部自行探测 mlx 并回退 numpy（mlp 路径），不扰动全局后端切换锁。
    """
    try:
        if not _start_running(job_id, 1.0):
            return  # 已被取消/终态（delete_job 已写取消终态）
        _track(job_id, int(params.get("timeout_sec", DEFAULT_TIMEOUT_SEC)))

        # 参数校验：非法配置抛 ValueError → 任务 failed 并给出明确原因
        raw_layers = params.get("layers", [32, 16])
        if (
            not isinstance(raw_layers, (list, tuple))
            or len(raw_layers) < 1
            or not all(
                isinstance(x, int) and not isinstance(x, bool) and x > 0
                for x in raw_layers
            )
        ):
            raise ValueError(f"layers 必须是正整数列表，实际 {raw_layers!r}")
        layers = [int(x) for x in raw_layers]
        activation = str(params.get("activation", "relu"))
        # T-56: 架构注册表分发——arch 白名单 + MLX 依赖预校验（不静默）
        arch = str(params.get("arch", "mlp"))
        if arch not in ARCH_REGISTRY:
            raise ValueError(f"arch 必须是 {sorted(ARCH_REGISTRY)} 之一，实际 {arch}")
        raw_arch_hparams = params.get("arch_hparams") or {}
        if not isinstance(raw_arch_hparams, dict):
            raise ValueError(f"arch_hparams 必须是 dict，实际 {raw_arch_hparams!r}")
        arch_hparams = {str(k): v for k, v in raw_arch_hparams.items()}
        if not ARCH_REGISTRY[arch]["available"]:
            raise ValueError(
                f"架构 {arch} 需要 MLX（当前环境 mlx 不可用），"
                f"可用架构: {sorted(k for k, v in ARCH_REGISTRY.items() if v['available'])}"
            )
        lr = float(params.get("lr", 0.01))
        epochs = int(params.get("epochs", 50))
        if not 1 <= epochs <= 500:
            raise ValueError(f"epochs 必须在 1..500 之间，实际 {epochs}")
        batch_size = int(params.get("batch_size", 256))
        seed = int(params.get("seed", 42))
        horizon = int(params.get("horizon", 5))
        max_rows = int(params.get("max_rows", 20000))

        # 数据集
        ds_id = int(params.get("dataset_id", 0))
        # P2-15: 神经网络只用 close/volume，不传全量 7 特征
        panel_info = load_panel(ds_id, features=["close", "volume"])
        if panel_info is None:
            raise RuntimeError(f"数据集 {ds_id} 不可用")
        panel_full = panel_info["panel"]
        missing = [f for f in ("close", "volume") if f not in panel_full]
        if missing:
            raise ValueError(f"数据集 {ds_id} 面板缺少字段: {missing}")
        n_stocks, n_days = panel_full["close"].shape
        _set_phase(job_id, "数据准备", 3.0)

        # 时间切分: 训练 60% / 验证 20%（不做样本外，IC 只报 train/val）
        # P1-31: split_panel 先切分再各自 forward_returns——修复原「完整面板 fwd
        # 再切片」把 val 段价格泄入训练标签尾部（horizon 天）的问题，与 _run_gp 对齐
        sp = split_panel(panel_full, horizon, 0.6, 0.2)
        data_train, data_val = sp["train"], sp["val"]
        fwd_train, fwd_val = sp["fwd_train"], sp["fwd_val"]

        def progress_cb(epoch, total):
            # 协作式控制点：暂停阻塞 / 取消抛 JobCancelled
            _checkpoint(job_id)
            p = 5 + 90 * (epoch + 1) / total
            _update(
                job_id,
                progress=round(p, 1),
                phase=f"训练中 {epoch + 1}/{total}",
            )

        def on_epoch_loss(epoch, total, train_loss, val_loss):
            # T-57 实时损失增量事件：progress 事件 payload 扩展 epoch/train_loss/
            # val_loss（旧字段 progress/phase 保留，向后兼容）。独立于 _update 的
            # 进度事件发布——runner._update 只透传 progress/phase 两键，loss 增量
            # 由本回调直发事件总线；前端据 epoch+train_loss 键解析流式曲线，
            # done 后以 meta 全量校准。train_loss 不可得（旧契约）时不发。
            if train_loss is None:
                return
            p = 5 + 90 * epoch / total
            _publish_event(
                job_id,
                "progress",
                progress=round(p, 1),
                phase=f"训练中 {epoch}/{total}",
                epoch=int(epoch),
                train_loss=round(float(train_loss), 6),
                val_loss=None if val_loss is None else round(float(val_loss), 6),
            )

        # GPU 互斥：train_mlp 独占 MLX 显存，等待期间任务保持 running（不计超时
        # 已由 _track 登记，此处仅排队获取锁，不阻塞其他 CPU 任务闸门）。
        # 等待循环每 1s 重试获取，每轮先过协作控制点（_checkpoint）：暂停时阻塞
        # 不占锁（resume 后继续排队）、取消时抛 JobCancelled 立即退出——修复
        # 原无限 acquire 导致排队中的 neural 任务不可中断、后续任务无限等待。
        # 不引入独立等待超时：仍由既有超时监控（_running 登记 + 监控线程置
        # failed）按任务 deadline 接管，语义与其余任务一致。
        while True:
            _checkpoint(job_id)
            if _GPU_LOCK.acquire(timeout=1.0):
                break
        try:
            res = train_mlp(
                data_train=data_train,
                forward_returns=fwd_train,
                data_val=data_val,
                val_forward_returns=fwd_val,
                layers=layers,
                activation=activation,
                lr=lr,
                epochs=epochs,
                batch_size=batch_size,
                seed=seed,
                max_rows=max_rows,
                progress_cb=progress_cb,
                on_epoch_loss=on_epoch_loss,
                arch=arch,
                arch_hparams=arch_hparams,
            )
        finally:
            _GPU_LOCK.release()
        _set_phase(job_id, "评估", 96.0)
        _set_phase(job_id, "结果整理")
        # 训练产物落库 nn_models（失败不阻塞，meta 无 model_id）
        model_id = _persist_nn_model(
            res, layers, activation, ds_id, horizon, arch, arch_hparams
        )
        meta = {
            "train_loss": res.get("train_loss"),
            "val_loss": res.get("val_loss"),
            "train_ic": res.get("train_ic"),
            "val_ic": res.get("val_ic"),
            "epochs": res.get("epochs"),
            "model_kind": res.get("model_kind"),
            "checkpoint": res.get("checkpoint"),
            # T-57 权重/注意力可视化（mlp 末层权重 / lstm 末层权重 / transformer 注意力）；
            # 计算失败或 mlx 不可用时为 null，前端显示占位（不阻断任务）
            "viz": res.get("viz"),
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
        logger.exception("neural 训练实验失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
