"""WorldQuant 风格算子集合：设备无关定义 + RPN 编译 + 批量张量求值。"""

from __future__ import annotations

import random
import re as _re

import numpy as np

from . import backend as B

# 算子表: name -> (arity, fn)
# fn 接收 (args: list[array], params) 返回 array；所有数组形状 (P, S, T)
OPERATORS: dict[str, dict] = {}


def _op(name: str, arity: int, display: str = ""):
    def deco(fn):
        OPERATORS[name] = {"arity": arity, "fn": fn, "display": display or name}
        return fn

    return deco


# ---------- 一元算子 ----------
@_op("log", 1, "{x}")
def _log(args, params):
    return B._T.where(
        args[0] > 0, B._T.log(B._T.abs(args[0]) + 1e-8), B._T.full_like(args[0], np.nan)
    )


@_op("abs", 1, "abs({x})")
def _abs(args, params):
    return B._T.abs(args[0])


@_op("sign", 1, "sign({x})")
def _sign(args, params):
    return B._T.sign(args[0])


@_op("neg", 1, "-{x}")
def _neg(args, params):
    return -args[0]


@_op("signed_power", 1, "signed_pow({x},{w})")
def _signed_power(args, params):
    a = args[0]
    return B._T.sign(a) * B._T.power(B._T.abs(a), float(params.get("power", 2)))


# ---------- 二元算子 ----------
@_op("add", 2, "({x}+{y})")
def _add(args, params):
    return args[0] + args[1]


@_op("sub", 2, "({x}-{y})")
def _sub(args, params):
    return args[0] - args[1]


@_op("mul", 2, "({x}*{y})")
def _mul(args, params):
    return args[0] * args[1]


@_op("div", 2, "({x}/{y})")
def _div(args, params):
    return B._safe_div(args[0], args[1])


@_op("max2", 2, "max({x},{y})")
def _max2(args, params):
    return B._T.maximum(args[0], args[1])


@_op("min2", 2, "min({x},{y})")
def _min2(args, params):
    return B._T.minimum(args[0], args[1])


# ---------- 时间序列算子 ----------
@_op("ts_mean", 1, "ts_mean({x},{w})")
def _ts_mean(args, params):
    return B.ts_mean(args[0], int(params.get("window", 5)))


@_op("ts_std", 1, "ts_std({x},{w})")
def _ts_std(args, params):
    return B.ts_std(args[0], int(params.get("window", 5)))


@_op("ts_delay", 1, "ts_delay({x},{k})")
def _ts_delay(args, params):
    # ts_delay(x, k)[t] = x[t-k]，滞后（仅用过去值，避免 look-ahead）
    return B.shift(args[0], int(params.get("delay", 1)))


@_op("ts_rank", 1, "ts_rank({x},{w})")
def _ts_rank(args, params):
    return B.ts_rank(args[0], int(params.get("window", 5)))


@_op("ts_corr", 2, "ts_corr({x},{y},{w})")
def _ts_corr(args, params):
    return B.ts_corr(args[0], args[1], int(params.get("window", 5)))


@_op("delta", 1, "delta({x},{k})")
def _delta(args, params):
    k = int(params.get("delay", 1))
    # delta(x, k)[t] = x[t] - x[t-k]，差分（仅用过去值，避免 look-ahead）
    return B.shift(args[0], 0) - B.shift(args[0], k)


# ---------- 扩展时间序列算子 ----------
@_op("ts_min", 1, "ts_min({x},{w})")
def _ts_min(args, params):
    return B.ts_min(args[0], int(params.get("window", 5)))


@_op("ts_max", 1, "ts_max({x},{w})")
def _ts_max(args, params):
    return B.ts_max(args[0], int(params.get("window", 5)))


@_op("ts_sum", 1, "ts_sum({x},{w})")
def _ts_sum(args, params):
    return B.ts_sum(args[0], int(params.get("window", 5)))


@_op("ts_product", 1, "ts_product({x},{w})")
def _ts_product(args, params):
    return B.ts_product(args[0], int(params.get("window", 5)))


@_op("ts_skew", 1, "ts_skew({x},{w})")
def _ts_skew(args, params):
    return B.ts_skew(args[0], int(params.get("window", 5)))


@_op("ts_kurt", 1, "ts_kurt({x},{w})")
def _ts_kurt(args, params):
    return B.ts_kurt(args[0], int(params.get("window", 5)))


@_op("ts_count", 1, "ts_count({x},{w})")
def _ts_count(args, params):
    return B.ts_count(args[0], int(params.get("window", 5)))


@_op("ts_argmax", 1, "ts_argmax({x},{w})")
def _ts_argmax(args, params):
    return B.ts_argmax(args[0], int(params.get("window", 5)))


@_op("ts_argmin", 1, "ts_argmin({x},{w})")
def _ts_argmin(args, params):
    return B.ts_argmin(args[0], int(params.get("window", 5)))


@_op("decay_linear", 1, "decay_linear({x},{w})")
def _decay_linear(args, params):
    return B.decay_linear(args[0], int(params.get("window", 5)))


@_op("winsorize", 1, "winsorize({x})")
def _winsorize(args, params):
    return B.winsorize(args[0], float(params.get("pct", 0.01)))


@_op("scale", 1, "scale({x})")
def _scale(args, params):
    return B.scale(args[0])


# ---------- 横截面算子 ----------
@_op("rank", 1, "rank({x})")
def _rank(args, params):
    return B.rank(args[0])


# 叶子：特征字段（T-07 特征终端扩展：7 个 OHLCV 行情特征 + 6 个估值特征 + 行业分类特征）
# - 行情特征：来自 K 线缓存（open/high/low/close/volume/amount/pct_change）
# - 估值特征（pe/pb/ps/market_cap/float_cap/turnover）：来自每日快照（get_spot 链路），
#   面板构建时按交易日与 K 线对齐注入，非快照日缺失为 NaN（不回填历史，避免前视）；
#   ps 依赖数据源提供，源无市销率字段时记缺失 NaN
# - 行业特征（industry）：分类特征，面板矩阵存行业名整数编码（编码表由面板构建方
#   从候选集合构建，0=未知/缺失），数值面板可参与 RPN
FEATURES = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_change",
    "pe",
    "pb",
    "ps",
    "market_cap",
    "float_cap",
    "turnover",
    "industry",
]

# 可用于符号回归的算子（默认集合）
DEFAULT_OP_SET = [
    "add",
    "sub",
    "mul",
    "div",
    "neg",
    "abs",
    "sign",
    "ts_mean",
    "ts_std",
    "ts_delay",
    "ts_rank",
    "delta",
    "rank",
    "log",
]

# 每个时间序列算子支持的参数候选
TS_PARAM_GRID = {
    "window": [3, 5, 10, 20],
    "delay": [1, 2, 3, 5],
}

# 可编辑算子参数（前端算子面板可调整每个算子的参数候选）
EDITABLE_OPS = {
    "ts_mean": {"window": [3, 5, 10, 20]},
    "ts_std": {"window": [3, 5, 10, 20]},
    "ts_rank": {"window": [3, 5, 10, 20]},
    "ts_min": {"window": [3, 5, 10, 20]},
    "ts_max": {"window": [3, 5, 10, 20]},
    "ts_sum": {"window": [3, 5, 10, 20]},
    "ts_product": {"window": [3, 5, 10, 20]},
    "ts_skew": {"window": [5, 10, 20, 30]},
    "ts_kurt": {"window": [5, 10, 20, 30]},
    "ts_count": {"window": [3, 5, 10, 20]},
    "ts_delay": {"delay": [1, 2, 3, 5]},
    "delta": {"delay": [1, 2, 3, 5]},
    "ts_corr": {"window": [5, 10, 20]},
    "decay_linear": {"window": [3, 5, 10, 20]},
    "winsorize": {"pct": [0.005, 0.01, 0.02]},
}

DEFAULT_FEATURES = ["open", "high", "low", "close", "volume"]


# ---------- GP 配置校验（features / op_set / op_config 白名单） ----------


def validate_op_set(op_set) -> list[str] | None:
    """校验算子集合：非空、全部 ∈ OPERATORS（白名单）。返回去重列表；None 表示未传。

    非法值抛 ValueError（由 API 层转 400 / 任务层转 failed），不静默忽略。
    """
    if op_set is None:
        return None
    if not isinstance(op_set, (list, tuple)) or not op_set:
        raise ValueError("op_set 不能为空，请至少选择一个算子")
    out: list[str] = []
    for op in op_set:
        if not isinstance(op, str) or op not in OPERATORS:
            raise ValueError(
                f"op_set 含不支持的算子: {op}（可用: {', '.join(sorted(OPERATORS))}）"
            )
        if op not in out:
            out.append(op)
    return out


def validate_features(features) -> list[str] | None:
    """校验特征字段：非空、全部 ∈ FEATURES（后端支持的终端字段白名单）。返回去重列表。"""
    if features is None:
        return None
    if not isinstance(features, (list, tuple)) or not features:
        raise ValueError("features 不能为空，请至少选择一个特征字段")
    out: list[str] = []
    for f in features:
        if not isinstance(f, str) or f not in FEATURES:
            raise ValueError(
                f"features 含不支持的字段: {f}（可用: {', '.join(FEATURES)}）"
            )
        if f not in out:
            out.append(f)
    return out


def validate_op_config(op_config) -> dict:
    """校验并规范化 op_config：{op: {key: [候选值]}}，返回规范化 dict；未传返回 {}。

    规则（非法均抛 ValueError，不静默忽略）：
    - op 必须 ∈ OPERATORS 且参数键必须 ∈ EDITABLE_OPS[op]（白名单）；
    - 候选数组必须非空、元素为数字（bool 视为非法）；
    - 按参数键做范围验证：window/delay 正整数、power 正数、pct ∈ (0, 0.5)；
    - 结果去重并统一类型（int/float）。
    """
    if not op_config:
        return {}
    if not isinstance(op_config, dict):
        raise ValueError("op_config 必须是对象 {算子: {参数键: [候选值]}}")
    out: dict[str, dict[str, list]] = {}
    for op, params in op_config.items():
        if not isinstance(op, str) or op not in OPERATORS:
            raise ValueError(
                f"op_config 含不支持的算子: {op}（可用: {', '.join(sorted(OPERATORS))}）"
            )
        allowed = EDITABLE_OPS.get(op, {})
        if not isinstance(params, dict) or not params:
            raise ValueError(
                f"op_config[{op}] 必须是 {{参数键: [候选值]}} 对象且不能为空"
            )
        for key, cands in params.items():
            if key not in allowed:
                raise ValueError(
                    f"op_config[{op}] 不支持参数键 {key}（支持: {', '.join(allowed) or '无'}）"
                )
            if not isinstance(cands, (list, tuple)) or not cands:
                raise ValueError(f"op_config[{op}][{key}] 必须是非空候选数组")
            norm: list = []
            for v in cands:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(f"op_config[{op}][{key}] 候选值必须是数字: {v!r}")
                if key in ("window", "delay"):
                    if v < 1 or int(v) != v:
                        raise ValueError(
                            f"op_config[{op}][{key}] 候选值必须为正整数: {v}"
                        )
                    v = int(v)
                elif key == "power":
                    v = float(v)
                    if v <= 0:
                        raise ValueError(
                            f"op_config[{op}][{key}] 候选值必须为正数: {v}"
                        )
                elif key == "pct":
                    v = float(v)
                    if not 0 < v < 0.5:
                        raise ValueError(
                            f"op_config[{op}][{key}] 候选值必须在 (0, 0.5) 区间: {v}"
                        )
                if v not in norm:
                    norm.append(v)
            out.setdefault(op, {})[key] = norm
    return out


def param_candidates(op: str, key: str, op_config: dict | None = None) -> list:
    """参数候选优先级：op_config → EDITABLE_OPS → TS_PARAM_GRID 默认网格。"""
    if op_config and isinstance(op_config.get(op), dict) and key in op_config[op]:
        return op_config[op][key]
    editable = EDITABLE_OPS.get(op, {})
    if key in editable:
        return editable[key]
    return list(TS_PARAM_GRID.get(key, []))


def sample_op_params(op: str, op_config: dict | None = None) -> dict:
    """为算子随机采样参数（random.choice 候选）；无候选（如 add/signed_power 缺省）返回 {}。"""
    key = tunable_param_key(op)
    if key is None:
        return {}
    cands = param_candidates(op, key, op_config)
    if not cands:
        return {}
    return {key: random.choice(cands)}


def compile_rpn(expr) -> list[dict]:
    """表达式（树或 RPN 列表）→ 规范 RPN 指令列表 [{op, params}]。

    expr 输入可以是：
      - list[dict]，已是 RPN（{"op": "ts_mean", "params": {"window": 5}, "args": [...]} 或 {"op":"add"})
      - str 表达式，如 "( ts_mean(close,5) - ts_mean(close,20) )"
    """
    if isinstance(expr, str):
        tokens = _tokenize(expr)
        rpn = _shunting_yard(tokens)
        return rpn
    if isinstance(expr, list) and expr and isinstance(expr[0], dict):
        # 已编译或树形表达
        if "op" in expr[0]:
            return _normalize_rpn(expr)
        if "name" in expr[0]:
            return _tree_to_rpn(expr)
    raise ValueError(f"无法编译表达式: {expr!r}")


def _normalize_rpn(rpn: list[dict]) -> list[dict]:
    out = []
    for ins in rpn:
        op = ins["op"]
        if op not in OPERATORS:
            raise ValueError(f"未知算子: {op}")
        out.append({"op": op, "params": ins.get("params", {})})
    return out


def _tree_to_rpn(tree) -> list[dict]:
    """树形 dict（{name, args, params}）→ RPN。"""
    name = tree.get("name", "")
    if name in FEATURES:
        return [{"op": "__feat__", "params": {"name": name}}]
    if name == "__const__":
        return [
            {
                "op": "__const__",
                "params": {"value": tree.get("params", {}).get("value", 0.0)},
            }
        ]
    if name not in OPERATORS:
        raise ValueError(f"未知算子: {name}")
    out: list[dict] = []
    for a in tree.get("args", []):
        out.extend(_tree_to_rpn(a))
    out.append({"op": name, "params": tree.get("params", {})})
    return out


# ---------- 字符串表达式解析（中缀 → RPN，支持 ( rank( ts_mean(close,5) ) ) 风格） ----------
_TOKEN_RE = _re.compile(r"\s*([(),]|[+\-*/]|[A-Za-z_][A-Za-z0-9_]*|\d+\.?\d*)")


def _tokenize(s: str) -> list[str]:
    toks = []
    pos = 0
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            raise ValueError(f"无法解析: {s[pos:]}")
        tok = m.group(1)
        if tok != "":
            toks.append(tok)
        pos = m.end()
    return _preprocess_unary_minus(toks)


def _preprocess_unary_minus(toks: list[str]) -> list[str]:
    """一元负号转 0 - x（如 -close → 0 - close），避免与二元减号冲突。"""
    out: list[str] = []
    prev = None
    for t in toks:
        if t == "-" and prev in (None, "(", ",", "+", "-", "*", "/"):
            out.extend(["0", "-"])
        else:
            out.append(t)
        prev = t
    return out


def _shunting_yard(tokens: list[str]) -> list[dict]:
    """函数调用 + 中缀算术 → RPN。支持 f(x,y) 与 a+b 混合。"""
    ops = {"+": 1, "-": 1, "*": 2, "/": 2}
    out: list[dict] = []
    stack: list = []
    i = 0
    pending_fn: list[str] = []

    while i < len(tokens):
        t = tokens[i]
        if t == "(":
            if (
                stack
                and isinstance(stack[-1], str)
                and stack[-1] in OPERATORS
                and stack[-1] not in ops
            ):
                pending_fn.append(stack.pop())
                stack.append("(")
            else:
                stack.append("(")
        elif t == ")":
            while stack and stack[-1] != "(":
                _emit(stack.pop(), out)
            if not stack:
                raise ValueError("括号不匹配")
            stack.pop()
            if pending_fn:
                fn = pending_fn.pop()
                if fn in OPERATORS:
                    out.append({"op": fn, "params": _extract_const_params(out, fn)})
        elif t == ",":
            while stack and stack[-1] != "(":
                _emit(stack.pop(), out)
        elif t in ops:
            while stack and stack[-1] in ops and ops[stack[-1]] >= ops[t]:
                _emit(stack.pop(), out)
            stack.append(t)
        elif t in OPERATORS:
            stack.append(t)
        elif _re.fullmatch(r"-?\d+(\.\d+)?", t):
            out.append({"op": "__const__", "params": {"value": float(t)}})
        elif t in FEATURES:
            out.append({"op": "__feat__", "params": {"name": t}})
        else:
            raise ValueError(f"未知符号: {t}")
        i += 1
    while stack:
        if stack[-1] == "(":
            raise ValueError("括号不匹配")
        _emit(stack.pop(), out)
    return out


def _emit(tok, out):
    if tok in ("+", "-", "*", "/"):
        out.append(
            {"op": {"+": "add", "-": "sub", "*": "mul", "/": "div"}[tok], "params": {}}
        )
    else:
        out.append({"op": tok, "params": {}})


# 带标量参数的函数：ts_mean(x,w) / ts_std / ts_rank / ts_delay(x,k) / delta(x,k) / ts_corr(x,y,w)
_SCALAR_PARAM_FUNCS = {
    "ts_mean": "window",
    "ts_std": "window",
    "ts_rank": "window",
    "ts_min": "window",
    "ts_max": "window",
    "ts_sum": "window",
    "ts_product": "window",
    "ts_skew": "window",
    "ts_kurt": "window",
    "ts_count": "window",
    "ts_argmax": "window",
    "ts_argmin": "window",
    "decay_linear": "window",
    "ts_delay": "delay",
    "delta": "delay",
    "ts_corr": "window",
    "signed_power": "power",
}


def _extract_const_params(out: list[dict], fn: str) -> dict:
    """提取函数调用末尾的常量参数（如 ts_mean(close, 5) 中的 5）。

    A9：窗口/延迟/幂等标量参数必须是整数——ts_mean(x, 5.5) 这类非法模板在编译期
    就抛 ValueError（带函数名与参数键上下文），而不是被 int() 静默截断成 5 后
    与用户预期不符地静默运行。
    """
    key = _SCALAR_PARAM_FUNCS.get(fn)
    if key is None:
        return {}
    if out and out[-1]["op"] == "__const__":
        val = out.pop()["params"]["value"]
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{fn} 的参数 {key} 必须为数值，收到 {val!r}")
        if not f.is_integer():
            raise ValueError(
                f"{fn} 的参数 {key} 必须是整数，收到 {val!r}（示例: {fn}(close, 5)）"
            )
        return {key: int(f)}
    return {}


# ---------- 批量求值 ----------
def _is_f32_tensor(v) -> bool:
    """v 是否已是当前后端 float32 张量（mlx: mx.array float32；numpy: ndarray float32）。

    已上载的 float32 张量直接复用，避免 np.asarray 下载 + from_numpy 重上载的
    双重转换（mlx 后端）；numpy 后端下与 np.asarray(v, float32) 视图语义一致。
    """
    if B.backend_name() == "mlx":
        xp = B.xp()
        return xp is not None and isinstance(v, xp.array) and v.dtype == xp.float32
    return isinstance(v, np.ndarray) and v.dtype == np.float32


def evaluate_rpn(rpn: list[dict], data: dict[str, np.ndarray]) -> np.ndarray:
    """在 (S, T) 数据上执行 RPN，返回 (S, T) 结果。

    data: feature -> (S, T) float32 数组。mlx 后端时自动上载 GPU。
    """
    return _evaluate_stack(rpn, data, None)


def evaluate_rpn_memo(
    rpn: list[dict], data: dict[str, np.ndarray], cache: dict | None = None
) -> np.ndarray:
    """带节点级记忆化的 RPN 求值（调优网格共享子表达式，P1-39）。

    cache: 跨多次调用的共享 dict。节点 key = (op, params 序列化, 输入节点 key 元组)；
    同一 (op, params, 输入) 只求值一次——网格调优只改单个节点 params，其余子树
    直接命中缓存。mlx 后端缓存后端张量（避免 numpy 往返）；cache=None 时行为
    与 evaluate_rpn 逐位一致。
    """
    return _evaluate_stack(rpn, data, cache if cache is not None else {})


def _evaluate_stack(
    rpn: list[dict], data: dict[str, np.ndarray], memo: dict | None
) -> np.ndarray:
    """RPN 求值核心。memo 非 None 时按 (op, params, 输入 key) 节点级缓存；
    栈元素为 (张量, 节点 key) 元组，key 用于跨调用缓存命中。"""
    stack: list[tuple] = []
    sample = next(iter(data.values()))
    use_memo = memo is not None
    for ins in rpn:
        op = ins["op"]
        if op == "__feat__":
            name = ins["params"]["name"]
            if name not in data:
                raise ValueError(f"特征 {name} 不在数据集中")
            v = data[name]
            if use_memo:
                key = ("__feat__", name)
                val = memo.get(key)
                if val is None:
                    # 已是后端 float32 张量：直接复用，跳过 numpy 往返
                    val = (
                        v
                        if _is_f32_tensor(v)
                        else B.from_numpy(np.asarray(v, dtype=np.float32))
                    )
                    memo[key] = val
            else:
                key = None
                val = (
                    v
                    if _is_f32_tensor(v)
                    else B.from_numpy(np.asarray(v, dtype=np.float32))
                )
            stack.append((val, key))
            continue
        if op == "__const__":
            value = float(ins["params"]["value"])
            if use_memo:
                key = ("__const__", value)
                val = memo.get(key)
                if val is None:
                    val = B.from_numpy(
                        np.full_like(np.asarray(sample, dtype=np.float32), value)
                    )
                    memo[key] = val
            else:
                key = None
                val = B.from_numpy(
                    np.full_like(np.asarray(sample, dtype=np.float32), value)
                )
            stack.append((val, key))
            continue
        meta = OPERATORS.get(op)
        if meta is None:
            raise ValueError(f"未知算子: {op}")
        arity = meta["arity"]
        if len(stack) < arity:
            raise ValueError(f"RPN 栈下溢 at {op}")
        args = stack[-arity:]
        del stack[-arity:]
        params = ins["params"]
        if use_memo:
            # params 序列化用 repr 兜底非可哈希值（常规 RPN 参数为 int/float/str）
            key = (
                op,
                tuple(sorted((k, repr(v)) for k, v in params.items())),
                tuple(a[1] for a in args),
            )
            val = memo.get(key)
            if val is None:
                val = meta["fn"]([a[0] for a in args], params)
                memo[key] = val
        else:
            key = None
            val = meta["fn"]([a[0] for a in args], params)
        stack.append((val, key))
    if len(stack) != 1:
        raise ValueError(f"RPN 栈残留 {len(stack)} 项")
    return B.to_numpy(stack[0][0])


def evaluate_batch(
    rpn_list: list[list[dict]], data: dict[str, np.ndarray], padded: bool = True
) -> list[np.ndarray]:
    """批量求值多个表达式。默认逐条执行（数组连续，GPU 亲和）。

    padded=True 的「按最长 RPN 填充后锁步执行（同代种群一次 GPU 调用）」
    当前未实现——实现与注释不符，后续轮次再评估（见下方实现段注释）。
    实际行为：单条直接求值；全部同构时复用首条结果；通用路径仍逐条执行
    evaluate_rpn。
    mlx 后端：若 data 仍是 numpy 面板，先整体上载一次（每特征一次
    from_numpy），同批全部表达式共享同一批 GPU 张量，避免逐条重复
    np.asarray 下载 + from_numpy 上载的双重转换；numpy 后端零拷贝直传。
    """
    if (
        B.backend_name() == "mlx"
        and data
        and any(not _is_f32_tensor(v) for v in data.values())
    ):
        data = {
            name: v
            if _is_f32_tensor(v)
            else B.from_numpy(np.asarray(v, dtype=np.float32))
            for name, v in data.items()
        }

    if not padded or len(rpn_list) <= 1:
        return [evaluate_rpn(rpn, data) for rpn in rpn_list]

    # 注：docstring 承诺的「按最长 RPN 填充后锁步执行」未实现（与注释不符，
    # 后续轮次再评估），此处仅处理退化情形：全部同构时复用首条结果，
    # 其余路径逐条求值，语义与直接循环 evaluate_rpn 完全一致。
    lens = {len(r) for r in rpn_list}
    if len(lens) == 1 and all(r == rpn_list[0] for r in rpn_list):
        return [evaluate_rpn(rpn_list[0], data)] * len(rpn_list)

    # 通用路径：逐条（numpy 已向量化，mlx 后端对单条 (S,T) 已上 GPU）
    return [evaluate_rpn(rpn, data) for rpn in rpn_list]


def expr_str(rpn: list[dict]) -> str:
    """RPN → 人类可读表达式。"""
    stack: list[str] = []
    for ins in rpn:
        op = ins["op"]
        if op == "__feat__":
            stack.append(ins["params"]["name"])
            continue
        if op == "__const__":
            stack.append(str(ins["params"]["value"]))
            continue
        meta = OPERATORS[op]
        arity = meta["arity"]
        if len(stack) < arity:
            return "?"
        args = stack[-arity:]
        del stack[-arity:]
        if arity == 1:
            if op in _SCALAR_PARAM_FUNCS:
                key = _SCALAR_PARAM_FUNCS[op]
                val = ins["params"].get(key, 2 if key == "power" else 5)
                d = meta["display"]
                stack.append(
                    d.replace("{x}", args[0])
                    .replace("{w}", str(val))
                    .replace("{k}", str(val))
                )
            else:
                stack.append(meta["display"].replace("{x}", args[0]))
        else:
            stack.append(_binary_display(op, args, ins["params"]))
    return stack[0] if stack else ""


def _binary_display(op: str, args: list[str], params: dict) -> str:
    if op in ("add", "sub", "mul", "div"):
        sym = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[op]
        return f"({args[0]} {sym} {args[1]})"
    d = OPERATORS[op]["display"]
    if op == "ts_corr":
        return (
            d.replace("{x}", args[0])
            .replace("{y}", args[1])
            .replace("{w}", str(params.get("window", 5)))
        )
    return d.replace("{x}", args[0]).replace("{y}", args[1] if len(args) > 1 else "")


# 算子中文说明（计算过程直观展示）
OP_NOTES = {
    "add": "逐元素相加",
    "sub": "逐元素相减",
    "mul": "逐元素相乘",
    "div": "逐元素相除",
    "neg": "取负",
    "abs": "取绝对值",
    "sign": "符号函数",
    "log": "自然对数(仅正值)",
    "rank": "横截面排名(0-1, 股票维度)",
    "scale": "横截面 z-score 标准化",
    "winsorize": "横截面缩尾(按分位截断极端值)",
    "ts_mean": "滚动均值",
    "ts_std": "滚动标准差",
    "ts_rank": "滚动排名分位",
    "ts_min": "滚动最小值",
    "ts_max": "滚动最大值",
    "ts_sum": "滚动求和",
    "ts_product": "滚动连乘",
    "ts_skew": "滚动偏度",
    "ts_kurt": "滚动超额峰度",
    "ts_count": "滚动非空计数",
    "ts_delay": "时间滞后",
    "delta": "差分(当前-滞后)",
    "ts_corr": "滚动相关系数",
    "decay_linear": "线性衰减加权均值",
    "signed_power": "符号幂: sign(x)·|x|^p",
}


# ---------- 可调参数辅助（因子参数调优） ----------
# 每个可调参数键的默认值与类型（is_int=False 视为浮点参数）
_PARAM_DEFAULTS: dict[str, float] = {
    "window": 5.0,
    "delay": 1.0,
    "power": 2.0,
    "pct": 0.01,
}
_INT_PARAM_KEYS = {"window", "delay", "power"}


def tunable_param_key(op: str) -> str | None:
    """算子 → 可调参数键（ts_mean/ts_std/... → window，ts_delay/delta → delay，
    signed_power → power，winsorize → pct；无参数算子返回 None）。"""
    if op in _SCALAR_PARAM_FUNCS:
        return _SCALAR_PARAM_FUNCS[op]
    if op == "winsorize":
        return "pct"
    return None


def collect_tunable_params(rpn: list[dict]) -> list[dict]:
    """收集 RPN 中全部可调参数。

    返回 [{name, op, key, index, default, is_int, count}]：
    - name 格式 '{op}.{key}'；同一 (op, key) 出现多次时按出现顺序追加 '[i]' 区分
      （如 ts_mean.window[0] / ts_mean.window[1]），供精准替换单个位置。
    """
    pairs = [(ins.get("op", ""), tunable_param_key(ins.get("op", ""))) for ins in rpn]
    counts: dict = {}
    for p in pairs:
        if p[1]:
            counts[p] = counts.get(p, 0) + 1
    out: list[dict] = []
    seen: dict = {}
    for ins in rpn:
        key = tunable_param_key(ins.get("op", ""))
        if key is None:
            continue
        pair = (ins["op"], key)
        idx = seen.get(pair, 0)
        seen[pair] = idx + 1
        name = f"{pair[0]}.{pair[1]}"
        if counts[pair] > 1:
            name += f"[{idx}]"
        default = ins.get("params", {}).get(key, _PARAM_DEFAULTS.get(key))
        out.append(
            {
                "name": name,
                "op": pair[0],
                "key": key,
                "index": idx,
                "default": default,
                "is_int": key in _INT_PARAM_KEYS,
                "count": counts[pair],
            }
        )
    return out


def set_tunable_param(rpn: list[dict], name: str, value: float) -> list[dict]:
    """克隆 RPN 并把 name（'{op}.{key}' 或 '{op}.{key}[i]'）对应指令的参数替换为 value。

    返回新 RPN，不改动传入的 rpn。无 index 时替换该 (op, key) 的所有出现。
    """
    m = _re.fullmatch(r"(.+?)\[(\d+)\]$", name)
    if m:
        base, want_idx = m.group(1), int(m.group(2))
    else:
        base, want_idx = name, None
    if "." not in base:
        raise ValueError(f"参数名格式错误: {name}（应为 op.key 或 op.key[i]）")
    op, key = base.rsplit(".", 1)
    if key != tunable_param_key(op):
        raise ValueError(f"参数 {op} 不支持键 {key}")
    out = [dict(ins, params=dict(ins.get("params", {}))) for ins in rpn]
    cur = 0
    replaced = False
    for ins in out:
        if ins["op"] == op:
            if want_idx is None:
                ins["params"][key] = value
                replaced = True
            elif cur == want_idx:
                ins["params"][key] = value
                replaced = True
                break
            cur += 1
    if not replaced:
        raise ValueError(f"参数 {name} 在 RPN 中未找到可替换位置")
    return out


def explain_rpn(rpn: list[dict]) -> list[dict]:
    """计算过程逐步说明（前端可视化）。"""
    steps: list[dict] = []
    stack: list[str] = []
    for i, ins in enumerate(rpn):
        op = ins["op"]
        if op == "__feat__":
            stack.append(ins["params"]["name"])
            steps.append(
                {
                    "step": i + 1,
                    "op": "特征",
                    "display": ins["params"]["name"],
                    "inputs": [],
                    "note": f"输入特征 {ins['params']['name']}",
                }
            )
            continue
        if op == "__const__":
            stack.append(str(ins["params"]["value"]))
            steps.append(
                {
                    "step": i + 1,
                    "op": "常量",
                    "display": str(ins["params"]["value"]),
                    "inputs": [],
                    "note": "常量",
                }
            )
            continue
        meta = OPERATORS.get(op)
        if meta is None:
            continue
        arity = meta["arity"]
        inputs = stack[-arity:] if len(stack) >= arity else []
        del stack[-arity:]
        stack.append(meta["display"])
        steps.append(
            {
                "step": i + 1,
                "op": op,
                "display": meta["display"],
                "inputs": inputs,
                "note": OP_NOTES.get(op, ""),
                "params": ins.get("params", {}),
            }
        )
    return steps
