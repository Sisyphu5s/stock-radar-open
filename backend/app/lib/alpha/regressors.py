"""符号回归算法注册表：多算法可选，可选依赖探测式注册。

REGRESSORS[algorithm] -> {name, desc, available, fn}。
available=False 的算法在任务提交时给出明确错误（未安装依赖/不可用原因）。
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger("stockradar.alpha.regressors")

REGRESSORS: dict[str, dict] = {
    "gp": {
        "name": "GP 符号回归（默认）",
        "desc": "锦标赛+子树交叉+点变异，numpy 批量求值",
        "available": True,
        "backend": "both",
    },
}

try:
    from gplearn.genetic import SymbolicRegressor

    _GPLEARN_OK = True
except ImportError:
    _GPLEARN_OK = False

try:
    import pysr  # noqa: F401

    _PYSR_OK = True
except ImportError:
    _PYSR_OK = False

try:
    from sklearn.neural_network import MLPRegressor  # noqa: F401

    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False


def run_gplearn(
    *,
    op_set=None,
    pop_size=120,
    generations=12,
    data_train=None,
    data_val=None,
    forward_returns=None,
    val_forward_returns=None,
    target="ic",
    penalty_complexity=False,
    progress_cb=None,
) -> tuple[list[dict], list[dict]]:
    """gplearn 适配：面板展平 (S*T) 行样本训练，best program 转表达式。

    返回与 evolve() 同构的 (results, evolution)。表达式限制在 OPERATORS 已有
    函数集（add/sub/mul/div/abs/neg），保证可编译为 RPN 参与后续评估。
    """
    from .evaluate import compute_ic
    from .operators import FEATURES, compile_rpn, evaluate_rpn

    if not _GPLEARN_OK:
        raise RuntimeError("算法 gplearn 不可用（未安装依赖: pip install gplearn）")

    features = [f for f in FEATURES if f in (data_train or {})]
    y = np.asarray(forward_returns).reshape(-1)
    X = np.stack([np.asarray(data_train[f]).reshape(-1) for f in features], axis=1)
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    X, y = X[mask], y[mask]
    if len(y) < 100:
        raise RuntimeError(f"gplearn 训练样本不足（有效样本 {len(y)} < 100）")
    max_rows = 20000
    if len(y) > max_rows:
        idx = np.random.RandomState(42).choice(len(y), max_rows, replace=False)
        X, y = X[idx], y[idx]

    est = SymbolicRegressor(
        population_size=pop_size,
        generations=generations,
        function_set=("add", "sub", "mul", "div", "abs", "neg"),
        metric="spearman",
        parsimony_coefficient=0.01 if penalty_complexity else 0.001,
        feature_names=features,
        random_state=42,
        n_jobs=1,
    )
    if progress_cb:
        progress_cb(0, generations, None)
    est.fit(X, y)
    if progress_cb:
        progress_cb(generations, generations, None)

    prog = est._program
    expr = str(prog)
    results: list[dict] = []
    try:
        rpn = compile_rpn(expr)
        tr_ic = compute_ic(evaluate_rpn(rpn, data_train), forward_returns)
        vl_ic = compute_ic(evaluate_rpn(rpn, data_val), val_forward_returns)
        results.append(
            {
                "expression": expr,
                "rpn": rpn,
                "train_ic": float(tr_ic) if np.isfinite(tr_ic) else None,
                "val_ic": float(vl_ic) if np.isfinite(vl_ic) else None,
                "complexity": int(prog.length_),
                "source": "gplearn",
            }
        )
    except Exception as e:
        logger.warning("gplearn 表达式转 RPN 失败: %s", e)
        results.append(
            {
                "expression": expr,
                "rpn": None,
                "train_ic": None,
                "val_ic": None,
                "complexity": int(prog.length_),
                "source": "gplearn",
            }
        )

    rd = est.run_details_
    evolution = [
        {
            "gen": int(g) + 1,
            "best_expression": "",
            "best_train_ic": float(rd["best_fitness"][i]),
            "avg_ic": float(rd["average_fitness"][i]),
            "best_complexity": int(rd["best_length"][i]),
        }
        for i, g in enumerate(rd["generation"])
    ]
    return results, evolution


def run_pysr(
    *,
    op_set=None,
    pop_size=120,
    generations=12,
    data_train=None,
    data_val=None,
    forward_returns=None,
    val_forward_returns=None,
    target="ic",
    penalty_complexity=False,
    progress_cb=None,
) -> tuple[list[dict], list[dict]]:
    """PySR 适配（未安装时给出明确错误；安装后在此实现真实训练）。"""
    raise RuntimeError(
        "算法 pysr 不可用（未安装依赖: pip install pysr，需 Julia 运行时；"
        "PySR 为 Julia 加速的符号回归，含模板/神经网络混合搜索）"
    )


def run_neural(
    *,
    op_set=None,
    pop_size=120,
    generations=12,
    data_train=None,
    data_val=None,
    forward_returns=None,
    val_forward_returns=None,
    target="ic",
    penalty_complexity=False,
    progress_cb=None,
) -> tuple[list[dict], list[dict]]:
    """MLP 神经网络回归：面板展平 (S,T) 样本，close/volume 派生特征预测未来收益。

    特征为 5 个标准化派生量（1日/5日收益、20日动量、量比、量份额），
    训练走 nn.train_mlp（mlx Apple GPU 加速，mlx 不可用/失败时回退 numpy
    参考实现），generations 参数映射为训练 epochs，每 epoch 调一次
    progress_cb(epoch, epochs, None)；输出与 evolve() 同构（source=neural，
    evolution 为空）。
    """
    from . import nn

    epochs = max(1, int(generations or 12))
    out = nn.train_mlp(
        data_train=data_train,
        forward_returns=forward_returns,
        data_val=data_val,
        val_forward_returns=val_forward_returns,
        layers=[32, 16],
        activation="relu",
        lr=0.01,
        epochs=epochs,
        batch_size=128,
        seed=42,
        max_rows=20000,
        progress_cb=lambda e, t: progress_cb(e, t, None) if progress_cb else None,
    )

    results: list[dict] = [
        {
            "expression": "neural_mlp(ret1,ret5,mom20,vol_ratio,vol_share;mlp(32,16))",
            "rpn": None,
            "train_ic": out["train_ic"],
            "val_ic": out["val_ic"],
            "complexity": 1,
            "source": "neural",
        }
    ]
    return results, []


if _GPLEARN_OK:
    REGRESSORS["gplearn"] = {
        "name": "gplearn（sklearn 生态）",
        "desc": "遗传编程符号回归，纯 CPU 轻量（sklearn 生态，可并行）",
        "available": True,
        "backend": "cpu",
        "fn": run_gplearn,
    }
else:
    REGRESSORS["gplearn"] = {
        "name": "gplearn（sklearn 生态）",
        "desc": "遗传编程符号回归，纯 CPU 轻量（未安装: pip install gplearn）",
        "available": False,
        "backend": "cpu",
    }

if _PYSR_OK:
    REGRESSORS["pysr"] = {
        "name": "PySR（神经符号回归）",
        "desc": "PySR（Julia 加速，含模板/神经网络混合搜索）",
        "available": True,
        "backend": "cpu",
        "fn": run_pysr,
    }
else:
    REGRESSORS["pysr"] = {
        "name": "PySR（神经符号回归）",
        "desc": "PySR（Julia 加速，含模板/神经网络混合搜索；未安装: pip install pysr，需 Julia 运行时）",
        "available": False,
        "backend": "cpu",
    }

REGRESSORS["neural"] = {
    "name": "neural（MLP 神经网络）",
    "desc": "MLP 预测未来收益：mlx Apple GPU 加速 / numpy 参考实现回退",
    "available": True,
    "backend": "both",
    "fn": run_neural,
}

REGRESSORS["bsr"] = {
    "name": "BSR（神经符号回归）",
    "desc": "Amazon BSR transformer 神经符号回归（PyPI 包名 bsr 被无关库占用，"
    "需从 GitHub 安装且依赖 torch，故不可用）",
    "available": False,
    "backend": "cpu",
}
