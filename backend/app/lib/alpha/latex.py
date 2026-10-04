"""因子表达式 → LaTeX 渲染（配合前端 KaTeX）。"""

from __future__ import annotations

from .operators import OPERATORS, _SCALAR_PARAM_FUNCS, compile_rpn

# 特征名 → LaTeX 显示名
_FEATURE_TEX = {
    "open": r"\mathrm{open}",
    "high": r"\mathrm{high}",
    "low": r"\mathrm{low}",
    "close": r"\mathrm{close}",
    "volume": r"\mathrm{volume}",
    "amount": r"\mathrm{amount}",
    "pct_change": r"\mathrm{pct\_change}",
}

# 一元算子 → 模板（{x} 占位）
_UNARY_TEX = {
    "abs": r"\left| {x} \right|",
    "sign": r"\operatorname{sign}({x})",
    "log": r"\log({x})",
    "neg": r"-{x}",
    "rank": r"\operatorname{rank}({x})",
    "signed_power": r"\operatorname{sgn}({x}) \cdot {x}^{p}",
    "winsorize": r"\operatorname{winsorize}({x})",
    "scale": r"\operatorname{scale}({x})",
}

# 带窗口/延迟参数的时间序列算子：{x}/{y}/{w}/{k} 占位
_TS_TEX = {
    "ts_mean": r"\operatorname{ts\_mean}_{w}({x})",
    "ts_std": r"\operatorname{ts\_std}_{w}({x})",
    "ts_rank": r"\operatorname{ts\_rank}_{w}({x})",
    "ts_min": r"\min_{w}({x})",
    "ts_max": r"\max_{w}({x})",
    "ts_sum": r"\operatorname{ts\_sum}_{w}({x})",
    "ts_product": r"\prod_{w}({x})",
    "ts_skew": r"\operatorname{ts\_skew}_{w}({x})",
    "ts_kurt": r"\operatorname{ts\_kurt}_{w}({x})",
    "ts_count": r"\operatorname{ts\_count}_{w}({x})",
    "decay_linear": r"\operatorname{decay\_linear}_{w}({x})",
    "ts_delay": r"\operatorname{ts\_delay}_{k}({x})",
    "delta": r"\Delta_{k}({x})",
    "ts_corr": r"\operatorname{ts\_corr}_{w}({x}, {y})",
}


def rpn_to_latex(rpn: list[dict]) -> str:
    """RPN 指令 → LaTeX 字符串。"""
    stack: list[str] = []
    for ins in rpn:
        op = ins["op"]
        if op == "__feat__":
            stack.append(
                _FEATURE_TEX.get(
                    ins["params"]["name"], rf"\mathrm{{{ins['params']['name']}}}"
                )
            )
            continue
        if op == "__const__":
            val = ins["params"]["value"]
            stack.append(str(val) if float(val) != 0 else "0")
            continue
        meta = OPERATORS.get(op)
        if meta is None:
            stack.append(r"\mathrm{?}")
            continue
        arity = meta["arity"]
        if len(stack) < arity:
            return r"\mathrm{?}"
        args = stack[-arity:]
        del stack[-arity:]

        if op in _UNARY_TEX:
            stack.append(_UNARY_TEX[op].replace("{x}", args[0]))
        elif op in _TS_TEX:
            params = ins["params"]
            if op == "ts_corr":
                w = params.get("window", 5)
                stack.append(
                    _TS_TEX[op]
                    .replace("{x}", args[0])
                    .replace("{y}", args[1])
                    .replace("{w}", str(w))
                )
            else:
                key = _SCALAR_PARAM_FUNCS.get(op, "window")
                val = params.get(key, 5)
                stack.append(
                    _TS_TEX[op]
                    .replace("{x}", args[0])
                    .replace("{w}", str(val))
                    .replace("{k}", str(val))
                )
        elif op in ("add", "sub", "mul", "div"):
            if op == "add":
                stack.append(f"({args[0]} + {args[1]})")
            elif op == "sub":
                # 一元负号预处理产生的 0 - x → -x
                if args[0] == "0":
                    stack.append(f"-{args[1]}")
                else:
                    stack.append(f"({args[0]} - {args[1]})")
            elif op == "mul":
                stack.append(rf"{args[0]} \cdot {args[1]}")
            else:
                stack.append(rf"\frac{{{args[0]}}}{{{args[1]}}}")
        elif op == "max2":
            stack.append(rf"\max({args[0]}, {args[1]})")
        elif op == "min2":
            stack.append(rf"\min({args[0]}, {args[1]})")
        else:
            stack.append(r"\mathrm{?}")
    return stack[0] if stack else ""


def expression_to_latex(expr: str) -> str:
    """字符串表达式 → LaTeX（解析失败时返回原样的转义版本）。"""
    try:
        return rpn_to_latex(compile_rpn(expr))
    except Exception:
        return expr.replace("_", r"\_").replace(" ", r"\ ")


_LATEX_CACHE: dict[str, str] = {}
# 无界缓存上限：超过后清空最旧一半（表达式集远小于此规模，FIFO 近似足够）
_LATEX_CACHE_MAX = 500


def cached_latex(expr: str) -> str:
    if expr not in _LATEX_CACHE:
        if len(_LATEX_CACHE) >= _LATEX_CACHE_MAX:
            _LATEX_CACHE.clear()
        _LATEX_CACHE[expr] = expression_to_latex(expr)
    return _LATEX_CACHE[expr]
