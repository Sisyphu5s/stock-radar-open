"""交易时段判定（北京时间）：A 股现语义 + 指数 per-market 扩展（T-73）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")

# 交易时段（含前后缓冲，覆盖集合竞价与收盘撮合）
_SESSION_1 = (9 * 60 + 15, 11 * 60 + 35)  # 09:15 - 11:35
_SESSION_2 = (12 * 60 + 55, 15 * 60 + 10)  # 12:55 - 15:10

# 交易时段快照 TTL（秒）：盘中行情变化快，从基础值 60s 提至 90s（P0-2）；
# 非交易时段仍按 session_spot_ttl 用基础值 ×30 放大（数据不变化）。
_SPOT_TTL_SESSION = 90


def now_cn() -> datetime:
    return datetime.now(_TZ)


def in_trading_session(now: datetime | None = None) -> bool:
    """是否处于交易时段（工作日 09:15-11:35 / 12:55-15:10）。"""
    now = now or now_cn()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return _SESSION_1[0] <= hm <= _SESSION_1[1] or _SESSION_2[0] <= hm <= _SESSION_2[1]


def session_spot_ttl(base_ttl: int, now: datetime | None = None) -> int:
    """快照缓存 TTL：交易时段固定 90s（盘中数据变化频繁，需较新快照）；
    非交易时段用基础值 ×30 放大（数据不变化，无需重复拉取）。"""
    return _SPOT_TTL_SESSION if in_trading_session(now) else base_ttl * 30


# ===== 指数 per-market 交易时段（T-73，指数专用；个股行为零变化，不重构 in_trading_session） =====
# 判定口径统一为北京时间（now 缺省取上海时区；传入其他时区 datetime 亦可，内部按 wall-clock 比较）：
# - CN：沿用现 A 股语义（09:15-11:35 / 12:55-15:10，工作日）；
# - US：北京时间 21:30 - 次日 04:00（美东 09:30-16:00，夏令时 EDT=UTC-4 → 北京 +12h；
#   冬令时 EST=UTC-5 → 北京 22:30-05:00；按任务卡固定 21:30-04:00 夏令时口径，冬令时
#   数据不更新由长 TTL 吸收，不误判）；周末：周六 00:00-04:00 属周五美股夜盘仍算交易时段；
# - HK：09:30-16:00 + 1h 收盘缓冲（上海=香港同时区）= 09:30-17:00，工作日。

_MARKET_WEEKEND_SESSION_MIN = 0
# US 夜盘窗口（北京时间）：21:30-24:00 属当日，00:00-04:00 属前一自然日（美股周五夜盘=北京周六凌晨）
_US_SESSION_EVE = (21 * 60 + 30, 24 * 60)  # 21:30-24:00
_US_SESSION_NEXT_DAY = (0, 4 * 60)  # 00:00-04:00
# HK 交易 + 收盘缓冲（分钟，北京时间）
_HK_SESSION = (9 * 60 + 30, 17 * 60)  # 09:30-17:00


def in_market_session(market: str, now: datetime | None = None) -> bool:
    """指数所属市场交易时段判定（T-73）。

    market: "CN"（现 A 股语义）/ "US"（北京时间 21:30-次日 04:00）/ "HK"
    （09:30-17:00 工作日，含 1h 收盘缓冲）；未知市场回退 CN 语义。
    工作日定义遵循该市场所在交易所历（简化为周一~周五；节假日误判无碍——
    非交易时段长 TTL 仅放大拉取间隔，数据不变化不会错）。
    """
    now = now or now_cn()
    if market == "US":
        hm = now.hour * 60 + now.minute
        if _US_SESSION_EVE[0] <= hm <= _US_SESSION_EVE[1]:
            # 21:30-24:00：周一~周五（周五 23:xx = 美股周五早盘）
            return now.weekday() < 5
        if _US_SESSION_NEXT_DAY[0] <= hm < _US_SESSION_NEXT_DAY[1]:
            # 00:00-04:00：前一自然日须为周一~周五（周六凌晨 = 周五美股尾盘）
            return (now - timedelta(days=1)).weekday() < 5
        return False
    if market == "HK":
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return _HK_SESSION[0] <= hm <= _HK_SESSION[1]
    # CN 与未知市场：沿用现 A 股语义（不改 in_trading_session 本身）
    return in_trading_session(now)


def index_spot_ttl(base_ttl: int, market: str, now: datetime | None = None) -> int:
    """指数快照缓存 TTL（per-market）：该市场交易时段内固定 90s（与 spot 同口径，
    盘中数据变化频繁）；非交易时段基础值 ×30 放大（数据不变化，无需重复拉取）。"""
    return _SPOT_TTL_SESSION if in_market_session(market, now) else base_ttl * 30
