"""信号目录（services/signals/catalog.py）单一事实源一致性测试。

覆盖：
1. 目录非空且条数 ≥ 60（当前 66 条），结构与分类字段合法。
2. 目录每个 key 都在引擎注册信号集（engine.SIGNAL_TYPES）内，防 catalog 与注册表漂移。
3. paper 层 _RULES 键集合 ⊆ 目录键集合，防 future 漂移（只断言，不统一行为）。
4. 常用度排序 SIGNAL_RANK 与目录键集对齐（无孤儿、无缺失）。
"""

from __future__ import annotations

from app.lib.signals.catalog import SIGNAL_CATALOG, SIGNAL_RANK
from app.lib.signals.engine import SIGNAL_TYPES

VALID_CATEGORIES = {"趋势", "动量", "反转", "波动", "量能", "形态", "位置"}


def test_catalog_nonempty_and_sufficient():
    assert len(SIGNAL_CATALOG) >= 60
    for code, (name, category, desc, params) in SIGNAL_CATALOG.items():
        assert isinstance(code, str) and code
        assert isinstance(name, str) and name
        assert isinstance(category, str) and category in VALID_CATEGORIES
        assert isinstance(desc, str) and desc
        assert isinstance(params, dict)


def test_catalog_keys_within_engine_registry():
    registry = set(SIGNAL_TYPES)
    assert registry, "engine 注册表不应为空"
    missing = set(SIGNAL_CATALOG) - registry
    assert not missing, f"catalog 含引擎未注册信号: {sorted(missing)}"


def test_paper_rules_keys_subset_of_catalog():
    from app.core.paper import _RULES

    assert _RULES, "paper _RULES 不应为空"
    extra = set(_RULES) - set(SIGNAL_CATALOG)
    assert not extra, f"paper _RULES 含 catalog 未收录信号: {sorted(extra)}"


def test_rank_aligned_with_catalog():
    assert set(SIGNAL_RANK) == set(SIGNAL_CATALOG), (
        f"SIGNAL_RANK 与 SIGNAL_CATALOG 键集不一致: "
        f"rank 缺失 {sorted(set(SIGNAL_CATALOG) - set(SIGNAL_RANK))}, "
        f"rank 多余 {sorted(set(SIGNAL_RANK) - set(SIGNAL_CATALOG))}"
    )
    assert sorted(SIGNAL_RANK.values()) == list(range(len(SIGNAL_RANK))), (
        "rank 值应为 0..n-1 无重复无空洞"
    )
