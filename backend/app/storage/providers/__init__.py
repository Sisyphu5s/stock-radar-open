"""存储适配层：行情数据源（网络 I/O 直连，bt-scan）。

QuoteProvider 抽象基类 + Sina/Tencent/Akshare 三个网络适配器 + 共享网络工具
（SESSION/_ak_call/_ak_retry/...）。供 core/sources.py 门面消费；本包不承载
多源竞速/冷却/回退策略（见 app/core/sources.py）。
"""

from __future__ import annotations

from .akshare import AkshareProvider
from .base import (
    PERIOD_SCALE,
    SESSION,
    QuoteProvider,
    _MINUTE_WALL_FACTOR,
    _MOCK_STOCKS,
    _ak_call,
    _ak_error,
    _ak_retry,
    _minute_window_minutes,
    _normalize_volume,
    _recent_quarters,
    _resample_period,
    _sina_symbol,
    _slice_kline,
)
from .sina import SinaFinancialsMixin, SinaProvider
from .tencent import TencentProvider

__all__ = [
    "PERIOD_SCALE",
    "SESSION",
    "QuoteProvider",
    "AkshareProvider",
    "SinaFinancialsMixin",
    "SinaProvider",
    "TencentProvider",
    "_MINUTE_WALL_FACTOR",
    "_MOCK_STOCKS",
    "_ak_call",
    "_ak_error",
    "_ak_retry",
    "_minute_window_minutes",
    "_normalize_volume",
    "_recent_quarters",
    "_resample_period",
    "_sina_symbol",
    "_slice_kline",
]
