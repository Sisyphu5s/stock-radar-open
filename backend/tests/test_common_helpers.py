"""公共工具 in_chunks / normalize_code / sector_of 聚焦测试：分批正确性 / 边界（空、恰好整除、不足一批）与板块口径。

不触碰数据库，纯函数单测。
"""

from __future__ import annotations

import pytest

from app.lib.codes import in_chunks, normalize_code, sector_of


def test_in_chunks_basic_batching():
    values = list(range(1000))
    chunks = list(in_chunks(values))
    # 1000 = 400 + 400 + 200
    assert [len(c) for c in chunks] == [400, 400, 200]
    # 分批不丢不重，顺序保持
    assert [x for c in chunks for x in c] == values


def test_in_chunks_empty():
    assert list(in_chunks([])) == []


def test_in_chunks_exact_divisible():
    chunks = list(in_chunks(list(range(800))))
    assert [len(c) for c in chunks] == [400, 400]


def test_in_chunks_less_than_batch():
    chunks = list(in_chunks(list(range(300))))
    assert [len(c) for c in chunks] == [300]


def test_in_chunks_custom_batch():
    values = list(range(10))
    assert [len(c) for c in in_chunks(values, batch=3)] == [3, 3, 3, 1]


def test_in_chunks_is_generator_of_lists():
    values = ["600519.SH", "000001.SZ", "300750.SZ"]
    gen = in_chunks(values, batch=2)
    assert iter(gen) is gen  # 生成器
    first = next(gen)
    assert isinstance(first, list)
    assert first == ["600519.SH", "000001.SZ"]
    assert next(gen) == ["300750.SZ"]


def test_in_chunks_preserves_input_order_without_sorting():
    # 传入顺序保持原样（排序语义由调用方决定，与 market.py 既有 _in_chunks 内部排序行为无关）
    values = ["b", "a", "d", "c"]
    assert list(in_chunks(values, batch=2)) == [["b", "a"], ["d", "c"]]


def test_in_chunks_non_positive_batch_yields_all():
    values = list(range(5))
    assert list(in_chunks(values, batch=0)) == [values]


# ---------------------------------------------------------------------------
# normalize_code：920 北交所号段不得误判为 .SH
# ---------------------------------------------------------------------------


def test_normalize_code_bj_920_prefix():
    assert normalize_code("920819") == "920819.BJ"


def test_normalize_code_bj_920_with_suffix_unchanged():
    assert normalize_code("920819.BJ") == "920819.BJ"


def test_normalize_code_sh_no_regression():
    assert normalize_code("600519") == "600519.SH"


def test_normalize_code_bj_8_prefix_no_regression():
    assert normalize_code("830799") == "830799.BJ"


# ---------------------------------------------------------------------------
# sector_of：920 北交所号段板块判定
# ---------------------------------------------------------------------------


def test_sector_of_bj_920():
    assert sector_of("920819") == "北交所"


def test_sector_of_bj_8_prefix_no_regression():
    assert sector_of("830799") == "北交所"


def test_sector_of_sh_main_board_no_regression():
    assert sector_of("600519") == "沪主板"
