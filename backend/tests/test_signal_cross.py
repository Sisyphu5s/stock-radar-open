"""均线/价格「上穿/下穿」标准交叉判定测试（回归：昨值均线基准 bug）。

背景：engine.py 的 4 个交叉判定原用链式比较「昨值均线」作基准
（如 close[-2] <= m[-2] < close[-1] 等价「今收 > 昨均线」），
均线快速上行时会产生假信号。修复后统一为标准定义：
  up   ：昨不在上（≤）且 今在上（>）
  down ：昨不在下（≥）且 今在下（<）

覆盖：
1. price_cross_up_ma   假金叉不命中（今收已越过昨均线但未越过今均线）
2. price_cross_up_ma   标准金叉命中
3. price_cross_down_ma 假死叉不命中 / 标准死叉命中
4. ma20_cross_ma60     标准金叉命中（默认参数）
5. ema_golden_cross    标准金叉命中（默认参数）
6. 数据不足（1 行）不崩溃返回 False
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.lib.signals.engine import (
    _ema_golden_cross,
    _ma20_cross_ma60,
    _price_cross_down_ma,
    _price_cross_up_ma,
)
from app.lib.indicators.compute import ema, ma


def _df(closes) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


# ---------- price_cross_up_ma（window=3） ----------


def test_up_ma_fake_golden_cross_not_hit():
    """均线快速上行场景：今收越过昨均线但未越过今均线 → 不命中。

    构造验证：close[-2]=11 ≤ m[-2]=11；今收 11.8 > 昨均线 11（旧链式会命中），
    但 11.8 ≤ 今均线 ≈11.933 → 标准定义下未上穿。
    """
    closes = [9.0, 13.0, 11.0, 11.8]
    df = _df(closes)
    m = ma(df["close"], 3)
    # 场景前提自检：确认数据确实覆盖 bug（旧逻辑命中、新逻辑不命中）
    assert closes[-2] <= m.iloc[-2]  # 昨在均线下（含持平）
    assert closes[-1] > m.iloc[-2]  # 今收已越过昨均线
    assert closes[-1] <= m.iloc[-1]  # 但未越过今均线

    ok, desc = _price_cross_up_ma(df, {"window": 3})
    assert ok is False
    assert "上穿" in desc


def test_up_ma_standard_golden_cross_hit():
    """标准金叉：昨收 ≤ 昨均线 且 今收 > 今均线 → 命中。"""
    closes = [10.0, 10.0, 10.0, 13.0]
    df = _df(closes)
    m = ma(df["close"], 3)
    assert closes[-2] <= m.iloc[-2]
    assert closes[-1] > m.iloc[-1]

    ok, _ = _price_cross_up_ma(df, {"window": 3})
    assert ok is True


# ---------- price_cross_down_ma（window=3） ----------


def test_down_ma_fake_dead_cross_not_hit():
    """快速下行场景：今收跌破昨均线但未跌破今均线 → 不命中。"""
    closes = [13.0, 9.0, 11.0, 10.2]
    df = _df(closes)
    m = ma(df["close"], 3)
    # 场景前提自检
    assert closes[-2] >= m.iloc[-2]  # 昨在均线上（含持平）
    assert closes[-1] < m.iloc[-2]  # 今收已跌破昨均线（旧链式会命中）
    assert closes[-1] >= m.iloc[-1]  # 但未跌破今均线

    ok, desc = _price_cross_down_ma(df, {"window": 3})
    assert ok is False
    assert "跌破" in desc


def test_down_ma_standard_dead_cross_hit():
    """标准死叉：昨收 ≥ 昨均线 且 今收 < 今均线 → 命中。"""
    closes = [10.0, 10.0, 10.0, 7.0]
    df = _df(closes)
    m = ma(df["close"], 3)
    assert closes[-2] >= m.iloc[-2]
    assert closes[-1] < m.iloc[-1]

    ok, _ = _price_cross_down_ma(df, {"window": 3})
    assert ok is True


# ---------- ma20_cross_ma60 / ema_golden_cross（默认参数） ----------


def test_ma20_cross_ma60_standard_golden_cross_hit():
    """MA20 上穿 MA60（默认 short=20/long=60）：平坦段 100 根 10 后尾两根 [10, 20]。"""
    closes = [10.0] * 100 + [10.0, 20.0]
    df = _df(closes)
    s, l = ma(df["close"], 20), ma(df["close"], 60)
    # 场景前提自检
    assert s.iloc[-2] <= l.iloc[-2]  # 昨 MA20 ≤ MA60（持平）
    assert s.iloc[-1] > l.iloc[-1]  # 今 MA20 > MA60

    ok, desc = _ma20_cross_ma60(df, {})
    assert ok is True
    assert "上穿" in desc


def test_ema_golden_cross_standard_golden_cross_hit():
    """EMA12 上穿 EMA26（默认 short=12/long=26）：平坦段 60 根 10 后尾两根 [10, 20]。"""
    closes = [10.0] * 60 + [10.0, 20.0]
    df = _df(closes)
    s, l = ema(df["close"], 12), ema(df["close"], 26)
    # 场景前提自检
    assert s.iloc[-2] <= l.iloc[-2]
    assert s.iloc[-1] > l.iloc[-1]

    ok, desc = _ema_golden_cross(df, {})
    assert ok is True
    assert "上穿" in desc


# ---------- 数据不足 ----------


def test_cross_signals_single_row_no_crash():
    """单行数据（含均线 window 大于行数）不崩溃且返回 False。"""
    df = _df([10.0])
    assert _price_cross_up_ma(df, {"window": 3})[0] is False
    assert _price_cross_down_ma(df, {"window": 3})[0] is False
    assert _ma20_cross_ma60(df, {})[0] is False
    assert _ema_golden_cross(df, {})[0] is False


def test_cross_signals_short_series_nan_safe():
    """两行数据但均线 window 更大（指标为 NaN）→ 不崩溃且不命中。

    注：ema 为递归计算无 NaN 窗口概念，短序列可算出值，此处仅测 ma 系。
    """
    df = _df([10.0, 12.0])
    assert _price_cross_up_ma(df, {"window": 20})[0] is False
    assert _price_cross_down_ma(df, {"window": 20})[0] is False
    assert _ma20_cross_ma60(df, {})[0] is False


def test_up_ma_default_window_smoke():
    """默认 window=20 下标准金叉命中（回归：默认参数路径）。"""
    closes = [10.0] * 20 + [10.0, 15.0]
    df = _df(closes)
    m = ma(df["close"], 20)
    assert closes[-2] <= m.iloc[-2]
    assert closes[-1] > m.iloc[-1]
    ok, _ = _price_cross_up_ma(df, {})
    assert ok is True
