"""GP 符号回归：锦标赛选择 + 子树交叉 + 点变异，适应度在 GPU 批量求值。"""

from __future__ import annotations

import logging
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from .operators import (
    OPERATORS,
    evaluate_rpn,
    expr_str,
    param_candidates,
    sample_op_params,
    tunable_param_key,
    validate_features,
    validate_op_config,
    validate_op_set,
)

logger = logging.getLogger("stockradar.alpha.gp")

MAX_DEPTH = 6
POP_SIZE_DEFAULT = 120
GENS_DEFAULT = 12

_TERMINALS = ["close", "open", "high", "low", "volume", "amount"]
# 未传 op_set 时的默认算子集合（与队列层历史默认一致）
DEFAULT_GP_OP_SET = [
    "add",
    "sub",
    "mul",
    "div",
    "neg",
    "abs",
    "ts_mean",
    "ts_std",
    "ts_delay",
    "delta",
    "rank",
]
DEFAULT_GP_FEATURES = list(_TERMINALS)


def _fitness_workers() -> int:
    """代内 fitness 并行度：min(os.cpu_count(), 8)；≤1 时纯串行。仅 numpy 后端启用。"""
    return max(1, min(os.cpu_count() or 1, 8))


# ---------- 表达式树表示：嵌套 dict ----------
def random_tree(
    depth: int,
    op_set: list[str],
    max_depth: int = MAX_DEPTH,
    features: list[str] | None = None,
    op_config: dict | None = None,
) -> dict:
    if depth >= max_depth or random.random() < 0.25:
        return {"name": random.choice(features or _TERMINALS)}
    op = random.choice(op_set)
    meta = OPERATORS[op]
    params = sample_op_params(op, op_config)
    return {
        "name": op,
        "params": params,
        "args": [
            random_tree(depth + 1, op_set, max_depth, features, op_config)
            for _ in range(meta["arity"])
        ],
    }


def tree_size(tree: dict) -> int:
    if "name" in tree:
        return 1 + sum(tree_size(a) for a in tree.get("args", []))
    return 1


def tree_depth(tree: dict) -> int:
    if "name" not in tree or not tree.get("args"):
        return 1
    return 1 + max(tree_depth(a) for a in tree["args"])


def all_nodes(tree: dict):
    yield tree
    for a in tree.get("args", []):
        yield from all_nodes(a)


def _replace_at(tree: dict, path: tuple[int, ...], new: dict) -> dict:
    if not path:
        return new
    idx, rest = path[0], path[1:]
    args = list(tree.get("args", []))
    args[idx] = _replace_at(args[idx], rest, new)
    return {**tree, "args": args}


def get_at(tree: dict, path: tuple[int, ...]) -> dict:
    node = tree
    for idx in path:
        node = node["args"][idx]
    return node


def random_path(tree: dict) -> tuple[int, ...]:
    path: list[int] = []
    node = tree
    while "args" in node and node["args"] and random.random() < 0.8:
        idx = random.randrange(len(node["args"]))
        path.append(idx)
        node = node["args"][idx]
    return tuple(path)


def mutate(
    tree: dict,
    op_set: list[str],
    max_depth: int = MAX_DEPTH,
    features: list[str] | None = None,
    op_config: dict | None = None,
) -> dict:
    """点变异：随机替换一个节点为同构新子树，或重采样节点参数（候选来自 op_config/默认网格）。"""
    if random.random() < 0.5:
        # 替换叶子为浅子树
        path = random_path(tree)
        sub = random_tree(0, op_set, min(max_depth, 3), features, op_config)
        return _replace_at(tree, path, sub)
    path = random_path(tree)
    node = get_at(tree, path)
    name = node.get("name", "")
    if name not in OPERATORS:
        return tree
    new_params = dict(node.get("params", {}))
    key = tunable_param_key(name)
    if key is not None:
        cands = param_candidates(name, key, op_config)
        if cands:
            new_params[key] = random.choice(cands)
    return _replace_at(tree, path, {**node, "params": new_params})


def crossover(a: dict, b: dict) -> tuple[dict, dict]:
    if random.random() < 0.7:
        return a, b
    pa, pb = random_path(a), random_path(b)
    na, nb = get_at(a, pa), get_at(b, pb)
    if tree_depth(nb) + len(pa) > MAX_DEPTH + 2:
        return a, b
    if tree_depth(na) + len(pb) > MAX_DEPTH + 2:
        return a, b
    return _replace_at(a, pa, nb), _replace_at(b, pb, na)


def rpn_of(tree: dict) -> list[dict]:
    from .operators import _tree_to_rpn

    return _tree_to_rpn(tree)


def tree_expression(tree: dict) -> str:
    return expr_str(rpn_of(tree))


# ---------- 进化循环 ----------
def evolve(
    op_set: list[str] | None = None,
    features: list[str] | None = None,
    op_config: dict | None = None,
    pop_size: int = POP_SIZE_DEFAULT,
    generations: int = GENS_DEFAULT,
    data_train: dict[str, np.ndarray] | None = None,
    data_val: dict[str, np.ndarray] | None = None,
    forward_returns: np.ndarray | None = None,
    val_forward_returns: np.ndarray | None = None,
    target: str = "ic",
    penalty_complexity: bool = False,
    progress_cb=None,
    seed: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """主进化入口。返回 (按验证 IC 降序的个体列表, 每代进化轨迹)。

    op_set: 参与符号回归的算子白名单（None 用默认集合；非法值抛 ValueError）。
    features: 可用终端字段白名单，表达式生成/求值只使用这些字段（None 用默认）。
    op_config: {算子: {参数键: [候选值]}}，真正进入随机生成与变异采样；
               未传时用 EDITABLE_OPS/TS_PARAM_GRID 默认候选（保持现有默认）。

    data_train/val: feature -> (S, T)。forward_returns: (S, T) 未来收益。
    target: 适应度目标——
      "ic": 训练 IC（默认，可正可负，方向由选择自然决定）;
      "ic_abs": |IC|（只追求幅度，不惩罚负 IC）;
      "icir": 滚动 IC 均值/标准差（强调稳健性）;
      "ls_annual": 多空年化收益（IC 不可用时回退 IC）;
      "composite": 0.5*|IC| + 0.3*稳定性 + 0.2*min(ICIR/2,1)，分量均 clip 0~1。
    penalty_complexity: True 时适应度扣 0.02*max(tree_size-8,0)。
    seed: 传入时重置全局 random 流，同输入完全可复现（随机树/选择/交叉/变异
    全部走模块级 random）；不传保持默认随机。注意会改写模块级 random 状态。
    """
    from .evaluate import compute_ic, ic_series, factor_to_returns

    if op_set is None:
        op_set = list(DEFAULT_GP_OP_SET)
    else:
        op_set = validate_op_set(op_set) or []
    if features is None:
        features = list(_TERMINALS)
    else:
        features = validate_features(features) or []
    op_config = validate_op_config(op_config)
    if not op_set:
        raise ValueError("op_set 不能为空，请至少选择一个算子")
    if not features:
        raise ValueError("features 不能为空，请至少选择一个特征字段")

    if seed is not None:
        random.seed(seed)

    pop = [
        random_tree(1, op_set, features=features, op_config=op_config)
        for _ in range(pop_size)
    ]

    # 未来收益按横截面排名（去除市场共同因子影响简化版）
    score_cache: dict[str, float] = {}
    raw_ic_cache: dict[str, float] = {}

    def fitness(tree) -> float:
        expr = tree_expression(tree)
        if expr in score_cache:
            return score_cache[expr]
        try:
            vals = evaluate_rpn(rpn_of(tree), data_train)
        except Exception as e:
            logger.warning("因子表达式计算异常（记 -9.0）: %s | %s", expr, e)
            score_cache[expr] = -9.0
            raw_ic_cache[expr] = float("nan")
            return -9.0
        ics = ic_series(vals, forward_returns)
        valid = ics[np.isfinite(ics)]
        if len(valid) < 5:
            score_cache[expr] = -8.0
            raw_ic_cache[expr] = float("nan")
            return -8.0
        ic = float(valid.mean())
        raw_ic_cache[expr] = ic
        std = float(valid.std())
        icir = ic / std if std >= 1e-9 else 0.0
        if target == "ic":
            score = ic
        elif target == "ic_abs":
            score = abs(ic)
        elif target == "icir":
            score = icir
        elif target == "ls_annual":
            _, ls_ann, _ = factor_to_returns(vals, forward_returns)
            score = float(ls_ann) if np.isfinite(ls_ann) else ic
        elif target == "composite":
            stability = float((valid > 0).mean())
            score = (
                0.5 * min(abs(ic), 1.0)
                + 0.3 * min(stability, 1.0)
                + 0.2 * min(icir / 2.0, 1.0)
            )
        else:
            raise ValueError(f"未知 target: {target}")
        if penalty_complexity:
            score -= 0.02 * max(tree_size(tree) - 8, 0)
        score_cache[expr] = float(score)
        return float(score)

    best: list[dict] = []
    evolution: list[dict] = []
    # 代内 fitness 并行（仅 numpy 后端；mlx 串行）。score_cache/raw_ic_cache
    # 为进程内 dict，GIL 下读写原子，无需额外锁（同一表达式可能被两个线程重复计算，
    # 结果一致，后写覆盖无害）。
    n_fit = _fitness_workers()
    parallel_fitness = False
    if n_fit > 1:
        try:
            from .backend import backend_name

            parallel_fitness = backend_name() == "numpy"
        except Exception:
            parallel_fitness = False
    for gen in range(generations):
        # 适应度
        if parallel_fitness:
            fitnesses = [0.0] * len(pop)
            with ThreadPoolExecutor(max_workers=n_fit) as pool:
                futures = {pool.submit(fitness, t): i for i, t in enumerate(pop)}
                for fut in as_completed(futures):
                    fitnesses[futures[fut]] = fut.result()
        else:
            fitnesses = [fitness(t) for t in pop]
        ranked = sorted(zip(pop, fitnesses), key=lambda x: -x[1])

        # 精英保留 10%
        elite = [t for t, _ in ranked[: max(1, pop_size // 10)]]
        best = ranked[: max(20, pop_size // 4)]

        # 进化轨迹：每代 best 表达式 / 原始 IC / 种群均值
        best_tree = ranked[0][0]
        best_expr = tree_expression(best_tree)
        best_raw = raw_ic_cache[best_expr]
        raw_ics = [raw_ic_cache[tree_expression(t)] for t in pop]
        finite_raw = [v for v in raw_ics if np.isfinite(v)]
        evolution.append(
            {
                "gen": gen + 1,
                "best_expression": best_expr,
                "best_train_ic": float(best_raw) if np.isfinite(best_raw) else None,
                "avg_ic": float(np.mean(finite_raw)) if finite_raw else None,
                "best_complexity": tree_size(best_tree),
            }
        )

        # 锦标赛选择 + 交叉变异
        new_pop = list(elite)
        while len(new_pop) < pop_size:

            def tourney(k=3):
                return min(random.sample(ranked, k), key=lambda x: -x[1])[0]

            p1, p2 = tourney(), tourney()
            c1, c2 = crossover(p1, p2)
            c1 = (
                mutate(c1, op_set, features=features, op_config=op_config)
                if random.random() < 0.3
                else c1
            )
            c2 = (
                mutate(c2, op_set, features=features, op_config=op_config)
                if random.random() < 0.3
                else c2
            )
            new_pop.append(c1)
            if len(new_pop) < pop_size:
                new_pop.append(c2)
        pop = new_pop
        if progress_cb:
            progress_cb(gen + 1, generations, ranked[0])

    # 最终：计算验证集 IC 排序。best 已按训练适应度降序（ranked 切片），
    # 元组第二项即 fitness——直接复用，避免对已算过的个体再调 fitness() 重算。
    results: list[dict] = []
    seen: set[str] = set()
    for tree, _train_ic in best:
        expr = tree_expression(tree)
        if expr in seen:
            continue
        seen.add(expr)
        try:
            vals = evaluate_rpn(rpn_of(tree), data_train)
            val_vals = evaluate_rpn(rpn_of(tree), data_val)
        except Exception:
            continue
        tr_ic = compute_ic(vals, forward_returns)
        vl_ic = compute_ic(val_vals, val_forward_returns)
        results.append(
            {
                "tree": tree,
                "rpn": rpn_of(tree),
                "expression": expr,
                "train_ic": float(tr_ic) if np.isfinite(tr_ic) else None,
                "val_ic": float(vl_ic) if np.isfinite(vl_ic) else None,
                "complexity": tree_size(tree),
            }
        )
    results.sort(key=lambda r: -(r["val_ic"] if r["val_ic"] is not None else -99))
    return results, evolution
