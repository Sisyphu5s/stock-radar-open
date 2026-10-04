"""条件选股 API 聚焦测试（T-17）：mock 快照下的字段白名单 / 逻辑与或 / universe 限定 / 输出契约。

全部走 mock 数据源（monkeypatch screener 模块引用的 get_spot / watchlist / hs300），
不触碰生产库、不发起真实网络请求。模式与 test_stocks_api 一致（直接调用路由函数）。
"""

from __future__ import annotations

import types

import pandas as pd
import pytest
from fastapi import HTTPException

from app.api import screener as SC


@pytest.fixture()
def spot() -> pd.DataFrame:
    """mock 全市场快照（内部列契约，与 providers 输出一致，无 volume_ratio 列）。"""
    return pd.DataFrame(
        [
            {  # 高涨幅 + 大市值 + 低换手 + 高 PE
                "code": "600519.SH",
                "name": "贵州茅台",
                "price": 1700.0,
                "pct_change": 3.2,
                "volume": 30000.0,
                "amount": 5.1e8,
                "turnover_rate": 0.3,
                "pe": 30.0,
                "pb": 9.0,
                "market_cap": 21350.0,
                "float_cap": 21350.0,
                "industry": "白酒",
                "source": "sina",
                "timestamp": "2026-08-14T15:00:00",
            },
            {  # 负涨幅 + 小市值 + 高换手 + 低 PE
                "code": "000858.SZ",
                "name": "五粮液",
                "price": 150.0,
                "pct_change": -0.5,
                "volume": 20000.0,
                "amount": 3.0e8,
                "turnover_rate": 5.2,
                "pe": 12.0,
                "pb": 5.0,
                "market_cap": 5800.0,
                "float_cap": 5800.0,
                "industry": "白酒",
                "source": "sina",
                "timestamp": "2026-08-14T15:00:00",
            },
            {  # 小涨 + 中等市值 + 中等换手 + 低 PB
                "code": "601318.SH",
                "name": "中国平安",
                "price": 45.0,
                "pct_change": 1.0,
                "volume": 50000.0,
                "amount": 2.2e9,
                "turnover_rate": 1.8,
                "pe": 9.0,
                "pb": 1.1,
                "market_cap": 8200.0,
                "float_cap": 8200.0,
                "industry": "保险",
                "source": "sina",
                "timestamp": "2026-08-14T15:00:00",
            },
            {  # 平盘 + 微市值 + 微换手
                "code": "300750.SZ",
                "name": "宁德时代",
                "price": 200.0,
                "pct_change": 0.0,
                "volume": 10000.0,
                "amount": 2.0e8,
                "turnover_rate": 0.5,
                "pe": 40.0,
                "pb": 7.0,
                "market_cap": 8800.0,
                "float_cap": 8800.0,
                "industry": "电池",
                "source": "sina",
                "timestamp": "2026-08-14T15:00:00",
            },
        ]
    )


def _run(monkeypatch, spot_df: pd.DataFrame, payload: dict):
    monkeypatch.setattr(SC, "get_spot", lambda: spot_df)
    monkeypatch.setattr(SC, "get_provider", lambda: types.SimpleNamespace(name="sina"))
    return SC.run(payload, db=None)


# ---------------------------------------------------------------------------
# 参数校验（白名单 / 类型）
# ---------------------------------------------------------------------------


def test_invalid_universe(monkeypatch, spot):
    with pytest.raises(HTTPException) as e:
        _run(
            monkeypatch, spot, {"universe": "nasdaq", "conditions": [], "logic": "and"}
        )
    assert e.value.status_code == 400


def test_invalid_logic(monkeypatch, spot):
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, spot, {"universe": "all", "conditions": [], "logic": "xor"})
    assert e.value.status_code == 400


def test_invalid_field_whitelist(monkeypatch, spot):
    with pytest.raises(HTTPException) as e:
        _run(
            monkeypatch,
            spot,
            {
                "universe": "all",
                "conditions": [{"field": "turnover", "op": "gt", "value": 1}],
                "logic": "and",
            },
        )
    assert e.value.status_code == 400
    assert "turnover" in e.value.detail


def test_invalid_op_whitelist(monkeypatch, spot):
    with pytest.raises(HTTPException) as e:
        _run(
            monkeypatch,
            spot,
            {
                "universe": "all",
                "conditions": [{"field": "pe", "op": "eq", "value": 1}],
                "logic": "and",
            },
        )
    assert e.value.status_code == 400
    assert "eq" in e.value.detail


def test_invalid_value_type(monkeypatch, spot):
    # 非数值（字符串/布尔）→ 400
    for bad in ("3", True, None, float("nan")):
        with pytest.raises(HTTPException) as e:
            _run(
                monkeypatch,
                spot,
                {
                    "universe": "all",
                    "conditions": [{"field": "pe", "op": "gt", "value": bad}],
                    "logic": "and",
                },
            )
        assert e.value.status_code == 400


def test_conditions_must_be_list(monkeypatch, spot):
    with pytest.raises(HTTPException) as e:
        _run(
            monkeypatch,
            spot,
            {"universe": "all", "conditions": {"a": 1}, "logic": "and"},
        )
    assert e.value.status_code == 400


def test_condition_must_be_object(monkeypatch, spot):
    with pytest.raises(HTTPException) as e:
        _run(
            monkeypatch, spot, {"universe": "all", "conditions": ["pe"], "logic": "and"}
        )
    assert e.value.status_code == 400


def test_source_unavailable_503(monkeypatch):
    monkeypatch.setattr(
        SC, "get_spot", lambda: (_ for _ in ()).throw(RuntimeError("挂"))
    )
    with pytest.raises(HTTPException) as e:
        SC.run({"universe": "all", "conditions": [], "logic": "and"}, db=None)
    assert e.value.status_code == 503


# ---------------------------------------------------------------------------
# 过滤语义
# ---------------------------------------------------------------------------


def test_empty_conditions_returns_all_sorted_by_pct_desc(monkeypatch, spot):
    out = _run(monkeypatch, spot, {"universe": "all", "conditions": [], "logic": "and"})
    assert out["count"] == 4
    codes = [r["code"] for r in out["data"]]
    assert codes == [
        "600519.SH",
        "601318.SH",
        "300750.SZ",
        "000858.SZ",
    ]  # 3.2 > 1.0 > 0 > -0.5
    # 输出字段子集 = 契约字段（固定顺序），不泄露快照内部列
    assert list(out["data"][0].keys()) == [
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
    assert out["source"] == "sina"
    assert out["as_of"] == "2026-08-14T15:00:00"


def test_and_logic_all_conditions(monkeypatch, spot):
    # 涨跌幅 > 0 且 总市值 ≥ 10000 亿 → 仅茅台
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": [
                {"field": "pct_change", "op": "gt", "value": 0},
                {"field": "market_cap", "op": "gte", "value": 10000},
            ],
            "logic": "and",
        },
    )
    assert [r["code"] for r in out["data"]] == ["600519.SH"]


def test_and_logic_lte_lt(monkeypatch, spot):
    # 市盈率 ≤ 12 且 市净率 < 1.5 → 仅中国平安
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": [
                {"field": "pe", "op": "lte", "value": 12},
                {"field": "pb", "op": "lt", "value": 1.5},
            ],
            "logic": "and",
        },
    )
    assert [r["code"] for r in out["data"]] == ["601318.SH"]


def test_or_logic_union(monkeypatch, spot):
    # 换手率 > 5 或 市净率 < 2 → 五粮液 ∪ 中国平安
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": [
                {"field": "turnover_rate", "op": "gt", "value": 5},
                {"field": "pb", "op": "lt", "value": 2},
            ],
            "logic": "or",
        },
    )
    assert sorted(r["code"] for r in out["data"]) == ["000858.SZ", "601318.SH"]


def test_no_match_returns_empty(monkeypatch, spot):
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": [{"field": "price", "op": "gt", "value": 99999}],
            "logic": "and",
        },
    )
    assert out["data"] == []
    assert out["count"] == 0


# ---------------------------------------------------------------------------
# universe 限定
# ---------------------------------------------------------------------------


def test_universe_watchlist(monkeypatch, spot):
    monkeypatch.setattr(
        SC._stock_repo, "watchlist_codes", lambda db: ["000858.SZ", "300750.SZ"]
    )
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "watchlist",
            "conditions": [{"field": "pct_change", "op": "gt", "value": -10}],
            "logic": "and",
        },
    )
    assert sorted(r["code"] for r in out["data"]) == ["000858.SZ", "300750.SZ"]


def test_universe_hs300(monkeypatch, spot):
    monkeypatch.setattr(SC, "_hs300", lambda: ["600519.SH", "601318.SH"])
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "hs300",
            "conditions": [{"field": "pct_change", "op": "gt", "value": -10}],
            "logic": "and",
        },
    )
    assert sorted(r["code"] for r in out["data"]) == ["600519.SH", "601318.SH"]


def test_universe_hs300_unavailable_503(monkeypatch, spot):
    def boom():
        raise RuntimeError("成分股拉取失败")

    monkeypatch.setattr(SC, "_hs300", boom)
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, spot, {"universe": "hs300", "conditions": [], "logic": "and"})
    assert e.value.status_code == 503


# ---------------------------------------------------------------------------
# volume_ratio（可选列）防御：列缺失不误判
# ---------------------------------------------------------------------------


def test_volume_ratio_missing_column_and_logic_no_match(monkeypatch, spot):
    # 快照无 volume_ratio 列：and 语义下该条件不命中 → 空结果（不误判、不抛错）
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": [{"field": "volume_ratio", "op": "gt", "value": 2}],
            "logic": "and",
        },
    )
    assert out["count"] == 0


def test_volume_ratio_missing_column_or_logic_ignored(monkeypatch, spot):
    # or 语义下该条件恒 False → 只按另一条件过滤
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": [
                {"field": "volume_ratio", "op": "gt", "value": 2},
                {"field": "pct_change", "op": "gt", "value": 0},
            ],
            "logic": "or",
        },
    )
    assert sorted(r["code"] for r in out["data"]) == ["600519.SH", "601318.SH"]


def test_volume_ratio_present_column(monkeypatch):
    # 快照含 volume_ratio 列：正常过滤并输出
    df = pd.DataFrame(
        [
            {
                "code": "600001.SH",
                "name": "A",
                "price": 10.0,
                "pct_change": 2.0,
                "volume": 100.0,
                "amount": 1e6,
                "turnover_rate": 1.0,
                "pe": 20.0,
                "pb": 3.0,
                "market_cap": 100.0,
                "volume_ratio": 3.5,
                "industry": "",
                "source": "mock",
                "timestamp": "2026-08-14T15:00:00",
            },
            {
                "code": "600002.SH",
                "name": "B",
                "price": 10.0,
                "pct_change": -1.0,
                "volume": 100.0,
                "amount": 1e6,
                "turnover_rate": 1.0,
                "pe": 20.0,
                "pb": 3.0,
                "market_cap": 100.0,
                "volume_ratio": 1.2,
                "industry": "",
                "source": "mock",
                "timestamp": "2026-08-14T15:00:00",
            },
        ]
    )
    out = _run(
        monkeypatch,
        df,
        {
            "universe": "all",
            "conditions": [{"field": "volume_ratio", "op": "gte", "value": 2}],
            "logic": "and",
        },
    )
    assert [r["code"] for r in out["data"]] == ["600001.SH"]
    assert out["data"][0]["volume_ratio"] == 3.5


def test_missing_sort_column_still_returns_rows(monkeypatch):
    # 降级快照缺 pct_change 列：不排序直接输出（防御），行不丢
    df = pd.DataFrame(
        [
            {
                "code": "600001.SH",
                "name": "A",
                "price": 10.0,
                "volume": 100.0,
                "amount": 1e6,
                "turnover_rate": 1.0,
                "pe": 20.0,
                "pb": 3.0,
                "market_cap": 100.0,
                "industry": "",
                "source": "mock",
                "timestamp": "2026-08-14T15:00:00",
            },
        ]
    )
    out = _run(monkeypatch, df, {"universe": "all", "conditions": [], "logic": "and"})
    assert out["count"] == 1
    assert out["data"][0]["pct_change"] is None
    assert out["as_of"] == "2026-08-14T15:00:00"


# ---------------------------------------------------------------------------
# T-75 条件树：嵌套分组（and/or 混用 + 括号）+ 递归求值 + 空组语义 + 兼容
# ---------------------------------------------------------------------------
# 快照语义速查（fixture spot）：
#   600519 茅台: pct 3.2   cap 21350  pe 30.0  pb 9.0
#   000858 五粮液: pct -0.5  cap 5800   pe 12.0  pb 5.0
#   601318 平安:  pct 1.0   cap 8200   pe 9.0   pb 1.1
#   300750 宁德:  pct 0.0   cap 8800   pe 40.0  pb 7.0


def test_tree_and_with_or_group(monkeypatch, spot):
    # pct > 0 AND (pe < 10 OR pb < 1.5) → 仅平安（茅台 pe/pb 均不满足；其余 pct 不满足）
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": {
                "logic": "and",
                "children": [
                    {"field": "pct_change", "op": "gt", "value": 0},
                    {
                        "logic": "or",
                        "children": [
                            {"field": "pe", "op": "lt", "value": 10},
                            {"field": "pb", "op": "lt", "value": 1.5},
                        ],
                    },
                ],
            },
        },
    )
    assert [r["code"] for r in out["data"]] == ["601318.SH"]


def test_tree_or_with_and_group(monkeypatch, spot):
    # pct > 0 OR (pe < 10 AND pb < 2) → 茅台 ∪ 平安
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": {
                "logic": "or",
                "children": [
                    {"field": "pct_change", "op": "gt", "value": 0},
                    {
                        "logic": "and",
                        "children": [
                            {"field": "pe", "op": "lt", "value": 10},
                            {"field": "pb", "op": "lt", "value": 2},
                        ],
                    },
                ],
            },
        },
    )
    assert sorted(r["code"] for r in out["data"]) == ["600519.SH", "601318.SH"]


def test_tree_three_level_nested(monkeypatch, spot):
    # (pct > 0 AND cap >= 10000) OR ((pe < 15 AND pb < 6) OR (cap >= 20000))
    # → 茅台（左真）；五粮液（pe 12<15 且 pb 5<6）；平安（pe 9<15 且 pb 1.1<6）
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": {
                "logic": "or",
                "children": [
                    {
                        "logic": "and",
                        "children": [
                            {"field": "pct_change", "op": "gt", "value": 0},
                            {"field": "market_cap", "op": "gte", "value": 10000},
                        ],
                    },
                    {
                        "logic": "or",
                        "children": [
                            {
                                "logic": "and",
                                "children": [
                                    {"field": "pe", "op": "lt", "value": 15},
                                    {"field": "pb", "op": "lt", "value": 6},
                                ],
                            },
                            {"field": "market_cap", "op": "gte", "value": 20000},
                        ],
                    },
                ],
            },
        },
    )
    assert sorted(r["code"] for r in out["data"]) == [
        "000858.SZ",
        "600519.SH",
        "601318.SH",
    ]


def test_tree_empty_or_group_ignored(monkeypatch, spot):
    # 嵌套 or 空组 = 忽略（恒 True）→ 只剩 pct > 0 过滤 → 茅台 ∪ 平安
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": {
                "logic": "and",
                "children": [
                    {"field": "pct_change", "op": "gt", "value": 0},
                    {"logic": "or", "children": []},
                ],
            },
        },
    )
    assert sorted(r["code"] for r in out["data"]) == ["600519.SH", "601318.SH"]


def test_tree_empty_and_group_no_match(monkeypatch, spot):
    # 嵌套 and 空组 = 全不命中（恒 False）→ 整个 and 组无结果
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "conditions": {
                "logic": "and",
                "children": [
                    {"field": "pct_change", "op": "gt", "value": 0},
                    {"logic": "and", "children": []},
                ],
            },
        },
    )
    assert out["count"] == 0


def test_tree_root_empty_group_semantics(monkeypatch, spot):
    # 新树格式根空组：and → 全不命中；or → 忽略（全量）
    out_and = _run(
        monkeypatch,
        spot,
        {"universe": "all", "conditions": {"logic": "and", "children": []}},
    )
    assert out_and["count"] == 0
    out_or = _run(
        monkeypatch,
        spot,
        {"universe": "all", "conditions": {"logic": "or", "children": []}},
    )
    assert out_or["count"] == 4


def test_tree_nested_invalid_leaf(monkeypatch, spot):
    # 白名单校验对树内所有叶子递归执行（嵌套层中的非法字段/操作符/阈值 → 400）
    cases = [
        {
            "logic": "and",
            "children": [
                {"field": "pct_change", "op": "gt", "value": 0},
                {
                    "logic": "or",
                    "children": [{"field": "turnover", "op": "gt", "value": 1}],
                },
            ],
        },
        {
            "logic": "and",
            "children": [
                {"field": "pct_change", "op": "gt", "value": 0},
                {"logic": "or", "children": [{"field": "pe", "op": "eq", "value": 1}]},
            ],
        },
        {
            "logic": "and",
            "children": [
                {"field": "pct_change", "op": "gt", "value": 0},
                {
                    "logic": "or",
                    "children": [{"field": "pe", "op": "gt", "value": "3"}],
                },
            ],
        },
    ]
    for cond in cases:
        with pytest.raises(HTTPException) as e:
            _run(
                monkeypatch,
                spot,
                {"universe": "all", "conditions": cond},
            )
        assert e.value.status_code == 400


def test_tree_invalid_group_shape(monkeypatch, spot):
    # 分组缺 children / children 非数组 / 非法 logic → 400
    for cond in [
        {"logic": "and"},
        {"logic": "and", "children": {"a": 1}},
        {"logic": "xor", "children": []},
    ]:
        with pytest.raises(HTTPException) as e:
            _run(
                monkeypatch,
                spot,
                {"universe": "all", "conditions": cond},
            )
        assert e.value.status_code == 400


def test_old_flat_format_compat(monkeypatch, spot):
    # 旧扁平格式 → 单组树：结果与 and 语义一致（回归保护）
    out = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "logic": "and",
            "conditions": [
                {"field": "pct_change", "op": "gt", "value": 0},
                {"field": "market_cap", "op": "gte", "value": 10000},
            ],
        },
    )
    assert [r["code"] for r in out["data"]] == ["600519.SH"]
    # 旧扁平 or → 并集
    out_or = _run(
        monkeypatch,
        spot,
        {
            "universe": "all",
            "logic": "or",
            "conditions": [
                {"field": "turnover_rate", "op": "gt", "value": 5},
                {"field": "pb", "op": "lt", "value": 2},
            ],
        },
    )
    assert sorted(r["code"] for r in out_or["data"]) == ["000858.SZ", "601318.SH"]


def test_old_flat_empty_returns_all(monkeypatch, spot):
    # 旧扁平空 conditions（任意逻辑）→ 不限条件，返回 universe 全量（零破坏）
    for logic in ("and", "or"):
        out = _run(
            monkeypatch, spot, {"universe": "all", "logic": logic, "conditions": []}
        )
        assert out["count"] == 4


# ---------------------------------------------------------------------------
# T-104 P2-44：条件树嵌套深度上限（校验 + 求值双重受限）
# ---------------------------------------------------------------------------


def _nested_tree(depth: int) -> dict:
    """构造 depth 层嵌套分组（最内层为叶子 pe > 1）。"""
    node: dict = {"field": "pe", "op": "gt", "value": 1}
    for _ in range(depth):
        node = {"logic": "and", "children": [node]}
    return node


def test_tree_nesting_at_limit_ok(monkeypatch, spot):
    # 恰好 50 层（默认上限）→ 正常求值（pe>1 全量命中）
    out = _run(monkeypatch, spot, {"universe": "all", "conditions": _nested_tree(50)})
    assert out["count"] == 4


def test_tree_nesting_over_limit_400(monkeypatch, spot):
    # 51 层 → 校验阶段 400（不再 RecursionError 500）
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, spot, {"universe": "all", "conditions": _nested_tree(51)})
    assert e.value.status_code == 400
    assert "嵌套" in e.value.detail
