"""/api/v1/screener/* : 条件选股器（快照条件组合查询）。

T-17：自定义条件组合选股。请求 {universe, conditions, logic} → 拉全市场快照
（get_spot，含源级缓存）→ universe 限定 → 条件过滤 → 排序输出。

T-75：conditions 升级为**条件树**（嵌套分组，括号语义）：
- 新格式 conditions = {logic: 'and'|'or', children: [叶子|分组]...}，分组同构递归；
- 旧扁平格式（conditions: [叶子...] + 顶层 logic）自动归一为单组树，零破坏；
- 白名单校验（FIELDS/OPS）对树内所有叶子递归执行。

条件字段白名单来自快照可得列（FIELDS 单一事实源）；volume_ratio 为**可选列**
（akshare 东财原始快照含量比但内部转换未提取，sina/tencent 亦无）——列缺失时
该条件不命中任何行（and 语义下无结果、or 语义下条件被忽略），输出为 null，
不误判、不抛错（机制防御：边界长在机制里）。
"""

from __future__ import annotations

import logging
import math
import operator
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.sources import get_provider, get_spot
from ..config import settings
from ..storage.db import get_db
from ..storage.hs300 import get_hs300_codes as _hs300
from ..storage.repos import stocks as _stock_repo

router = APIRouter(prefix="/screener", tags=["screener"])

logger = logging.getLogger("stockradar.screener")


# ===== 响应模型（P2-29 契约硬化：逐路径 response_model） =====


class ScreenerRow(BaseModel):
    """选股结果行：字段固定为 OUT_FIELDS 白名单（缺列/NaN → None）。"""

    code: str
    name: str
    price: Optional[float] = None
    pct_change: Optional[float] = None
    turnover_rate: Optional[float] = None
    volume_ratio: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None
    market_cap: Optional[float] = None


class ScreenerRunResponse(BaseModel):
    data: list[ScreenerRow]
    count: int
    source: str
    as_of: Optional[str] = None

# ===== 条件契约（白名单，单一事实源）=====
# 字段名 → 中文名（错误提示/前端标签展示用）；顺序 = 前端字段选择排序
FIELDS: dict[str, str] = {
    "price": "最新价",
    "pct_change": "涨跌幅(%)",
    "turnover_rate": "换手率(%)",
    "volume_ratio": "量比",
    "pe": "市盈率",
    "pb": "市净率",
    "market_cap": "总市值(亿)",
}

# 操作符 → 数学符号（提示用）；_OP_FN 为实际比较函数
OPS: dict[str, str] = {
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
}
_OP_FN = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}

LOGICS = ("and", "or")
UNIVERSES = ("watchlist", "hs300", "all")

# 输出列（结果契约，固定顺序）；排序键 = pct_change 降序
OUT_FIELDS = [
    "code",
    "name",
    "price",
    "pct_change",
    "turnover_rate",
    "volume_ratio",
    "pe",
    "pb",
    "market_cap",
]
SORT_FIELD = "pct_change"
_ROUND = 4  # 浮点输出统一精度，避免 pandas 尾数噪音


def _round(v: float) -> float:
    return round(float(v), _ROUND)


def _cell(v) -> float | str | None:
    """输出单元格序列化：数值 round 到统一精度；字符串原样；NaN/None → None。"""
    if v is None or pd.isna(v):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _round(v)
    return v


def _cond_mask(spot: pd.DataFrame, cond: dict) -> pd.Series:
    """单条件行过滤掩码：字段缺失 → 全 False（不命中，机制防御，见模块 docstring）。"""
    field = cond["field"]
    if field not in spot.columns:
        return pd.Series(False, index=spot.index)
    fn = _OP_FN[cond["op"]]
    value = cond["value"]
    return pd.to_numeric(spot[field], errors="coerce").map(
        lambda x: False if pd.isna(x) else fn(float(x), value)
    )


# ===== 条件树：递归校验 + 递归求值（T-75）=====
# 节点二态：叶子 {field, op, value} / 分组 {logic, children}；
# 判别依据 = 是否含 children/logic 键。children 允许为空，语义见 _group_mask。


def _validate_leaf(node: dict, path: str) -> dict:
    """校验并归一化叶子条件：field/op 白名单 + value 必须为有限数值（与旧格式一致）。"""
    field, op, value = node.get("field"), node.get("op"), node.get("value")
    if field not in FIELDS:
        raise HTTPException(
            400, f"{path} 字段 {field!r} 不在白名单: {', '.join(FIELDS)}"
        )
    if op not in OPS:
        raise HTTPException(400, f"{path} 操作符 {op!r} 不在白名单: {', '.join(OPS)}")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise HTTPException(400, f"{path} 阈值 value 必须为有限数值")
    return {"field": field, "op": op, "value": float(value)}


def _validate_node(node, path: str, depth: int = 0) -> dict:
    """递归校验条件树节点（叶子/分组同构），返回归一化节点；错误路径带前缀定位。

    depth 为嵌套层数（根分组 0），超 settings.screener_tree_max_depth → 400
    （防恶意构造超深树触发 RecursionError，P2-44）。
    """
    if depth > settings.screener_tree_max_depth:
        raise HTTPException(
            400, f"{path} 条件树嵌套超过 {settings.screener_tree_max_depth} 层上限"
        )
    if not isinstance(node, dict):
        raise HTTPException(400, f"{path} 必须为对象")
    if "children" in node or "logic" in node:
        logic = node.get("logic")
        children = node.get("children")
        if logic not in LOGICS:
            raise HTTPException(
                400, f"{path} 分组逻辑 {logic!r} 不支持, 可选: {', '.join(LOGICS)}"
            )
        if not isinstance(children, list):
            raise HTTPException(400, f"{path}.children 必须为数组")
        return {
            "logic": logic,
            "children": [
                _validate_node(c, f"{path}.children[{i}]", depth + 1)
                for i, c in enumerate(children)
            ],
        }
    return _validate_leaf(node, path)


def _normalize_flat(conditions: list, logic: str) -> dict | None:
    """旧扁平格式兼容：conditions 数组 + 顶层 logic → 单组树。

    空数组 → None（= 不限条件，保持「空条件 = 返回 universe 全量」既有契约）；
    非空 → {logic, children: 叶子列表}，校验错误路径沿用 conditions[i]。
    """
    if not conditions:
        return None
    children = []
    for i, c in enumerate(conditions):
        if not isinstance(c, dict):
            raise HTTPException(400, f"conditions[{i}] 必须为对象")
        children.append(_validate_leaf(c, f"conditions[{i}]"))
    return {"logic": logic, "children": children}


def _group_mask(spot: pd.DataFrame, group: dict, _depth: int = 0) -> pd.Series:
    """分组掩码：按 logic 递归归并子节点掩码（and=全真 / or=任一真）。

    空组语义（children 为空时，注释即契约）：
    - and 空组 → 全不命中（恒 False，保守：空括号组内没有任何条件成立）；
    - or 空组 → 忽略（恒 True，该组不改变结果）。
    根分组为空时不走本函数：旧扁平空 conditions 在 _normalize_flat 归为 None
    （= 不限条件）；新树格式根空组按上述语义执行（and→无结果 / or→全量）。

    _depth 为嵌套层数（与 _validate_node 同口径），超限 400（防御性：合法树
    已在校验期限制深度，此处兜底保护未来直接调用求值路径的代码，P2-44）。
    """
    logic = group["logic"]
    children = group["children"]
    if not children:
        if logic == "and":
            return pd.Series(False, index=spot.index)
        return pd.Series(True, index=spot.index)
    if logic == "and":
        keep = pd.Series(True, index=spot.index)
        for c in children:
            keep &= _node_mask(spot, c, _depth + 1)
    else:
        keep = pd.Series(False, index=spot.index)
        for c in children:
            keep |= _node_mask(spot, c, _depth + 1)
    return keep


def _node_mask(spot: pd.DataFrame, node: dict, _depth: int = 0) -> pd.Series:
    """节点掩码：叶子走 _cond_mask；分组递归 _group_mask。

    _depth 为嵌套层数（根分组 0，子节点 +1），超 settings.screener_tree_max_depth → 400。
    """
    if _depth > settings.screener_tree_max_depth:
        raise HTTPException(
            400, f"条件树嵌套超过 {settings.screener_tree_max_depth} 层上限"
        )
    if "children" in node:
        return _group_mask(spot, node, _depth)
    return _cond_mask(spot, node)


def _as_of(spot: pd.DataFrame) -> str | None:
    """快照数据时刻：timestamp 列（ISO 字符串）取最后一行；列缺失返回 None。"""
    if "timestamp" not in spot.columns or spot["timestamp"].empty:
        return None
    v = spot["timestamp"].iloc[-1]
    return str(v) if pd.notna(v) else None


@router.post("/run", response_model=ScreenerRunResponse)
def run(payload: dict, db: Session = Depends(get_db)):
    """运行选股：快照 → universe 限定 → 条件树过滤 → pct_change 降序输出。

    请求体（T-75 条件树；旧扁平格式自动兼容）：
    - universe: 'watchlist'（自选股）| 'hs300'（沪深300 成分）| 'all'（全市场），缺省 all
    - conditions:
        新格式 = 条件树 {logic: 'and'|'or', children: [叶子|分组]...}，分组同构递归
        （叶子 {field, op, value}，field/op 白名单见 FIELDS/OPS，value 必须为有限数值）；
        旧格式 = 扁平数组 [{field, op, value}] + 顶层 logic，自动归一为单组树；
        空数组 = 不限条件（返回 universe 全量，兼容契约保持）。
        - 嵌套深度上限 settings.screener_tree_max_depth（50 层），超限 400（P2-44）。
    - 空组语义（嵌套分组 children 为空）：and 空组 = 全不命中；or 空组 = 忽略。

    响应：{data: [{code,name,price,pct_change,turnover_rate,volume_ratio,pe,pb,market_cap}],
    count, source, as_of}，data 按 pct_change 降序。
    """
    universe = payload.get("universe") or "all"
    logic = payload.get("logic") or "and"

    if universe not in UNIVERSES:
        raise HTTPException(
            400, f"不支持 universe {universe!r}，可选: {', '.join(UNIVERSES)}"
        )
    if logic not in LOGICS:
        raise HTTPException(400, f"不支持 logic {logic!r}，可选: {', '.join(LOGICS)}")

    # ---- 条件解析：新树格式 / 旧扁平格式（兼容归一）/ None = 不限条件 ----
    raw = payload.get("conditions")
    tree: dict | None = None
    if raw is None:
        tree = None
    elif isinstance(raw, list):
        tree = _normalize_flat(raw, logic)
    elif isinstance(raw, dict):
        tree = _validate_node(raw, "conditions")
    else:
        raise HTTPException(400, "conditions 必须为条件树对象或旧格式条件数组")

    try:
        spot = get_spot()
    except Exception as e:
        logger.warning("选股拉取快照失败: %s", str(e)[:120])
        raise HTTPException(503, f"行情源暂不可用: {str(e)[:120]}")

    if spot is None or spot.empty:
        return {"data": [], "count": 0, "source": get_provider().name, "as_of": None}

    # ---- universe 限定：按代码集合过滤（带后缀代码，与快照列一致）----
    if universe == "watchlist":
        codes = set(_stock_repo.watchlist_codes(db))
        spot = spot[spot["code"].isin(codes)]
    elif universe == "hs300":
        try:
            codes = set(_hs300())
        except Exception as e:
            raise HTTPException(503, f"沪深300 成分股不可用: {str(e)[:120]}")
        spot = spot[spot["code"].isin(codes)]

    # ---- 条件树过滤（递归求值；tree=None = 不限条件，全保留）----
    if tree is not None:
        spot = spot[_node_mask(spot, tree)]

    # ---- 排序 + 输出（缺列字段输出 None）----
    if SORT_FIELD in spot.columns:
        spot = spot.sort_values(SORT_FIELD, ascending=False)
    data = [
        {f: (_cell(r[f]) if f in r.index else None) for f in OUT_FIELDS}
        for _, r in spot.iterrows()
    ]
    return {
        "data": data,
        "count": len(data),
        "source": get_provider().name,
        "as_of": _as_of(spot),
    }
