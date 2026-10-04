"""list_alpha101 模块级 enrich 缓存测试：连续调用幂等且返回独立对象（行为不变）。"""

from app.lib.alpha.alpha101 import ALPHA101, list_alpha101


def test_list_alpha101_twice_consistent():
    """连续两次调用返回全量 101 条且逐条相等（缓存的 compile_rpn 结果一致）。"""
    first = list_alpha101()
    second = list_alpha101()
    assert len(first) == len(ALPHA101) == 101
    assert first == second


def test_list_alpha101_fields_present():
    """每条含完整 enrich 字段与 id（组装补回，非缓存共享）。"""
    items = list_alpha101()
    for i, a in enumerate(items, start=1):
        assert a["id"] == i
        assert a["name"] and a["formula"] and a["category"]
        assert "formula_expr" in a and "params_desc" in a and "usage" in a
        assert "desc" in a and "evaluable" in a and "latex" in a


def test_list_alpha101_result_isolation():
    """调用方修改返回 dict 不影响缓存，下次调用仍是原始值。"""
    first = list_alpha101()
    first[0]["formula_expr"] = "mutated"
    first[0]["desc"] = "mutated"
    second = list_alpha101()
    assert second[0]["formula_expr"] != "mutated"
    assert second[0]["desc"] != "mutated"
    assert second[0]["desc"] == (ALPHA101[0].get("desc") or "").strip()


def test_get_alpha_matches_list_alpha101():
    """get_alpha 与 list_alpha101 同一条目 enrich 结果一致（共享缓存路径）。"""
    from app.lib.alpha.alpha101 import get_alpha

    items = {a["id"]: a for a in list_alpha101()}
    for alpha_id in (1, 50, 101):
        assert get_alpha(alpha_id) == items[alpha_id]
