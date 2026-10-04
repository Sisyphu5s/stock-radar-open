"""时间契约：K 线 bar 股市时间 / 市场时区转换 / 信号事件时间范围（纯函数，无 I/O）。

由原 common.py 迁移而来（common-split），常量与函数体逐字搬移，行为不改。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PERIODS = ("1", "5", "15", "30", "60", "daily", "weekly", "monthly")

# 日/周/月（按自然日 bar 收盘）与分钟周期分组
_CALENDAR_PERIODS = ("daily", "weekly", "monthly")

# 信号事件时间范围（市场日历口径，Asia/Shanghai 自然日）
# 任意 Nd 通用机制（market_time_range 按天数解析）；P2-54 扩展细粒度档位
# （14d/60d/90d = 2周/2月/3月），支撑 copilot 时间映射语义正确。
MARKET_SPANS = ("today", "3d", "7d", "14d", "30d", "60d", "90d", "all")


def bar_market_time(value, period: str = "daily") -> datetime:
    """K 线 bar 的股市时间（Asia/Shanghai 无时区墙钟，保持 SQLite naive 契约）。

    契约（信号时点 = 触发 bar 在股市数据时间轴上的精确标签）：
    - daily/weekly/monthly：时区折算后无条件归一到该上海日期的 15:00:00.000000
      收盘时点（bar 携带的任何其他时间都只表示该真实交易日，统一收盘语义；
      pandas 归一化的 00:00:00 同样归一到 15:00）。周/月 bar 的日期必须是组内
      最后一个真实交易日（见 quote._resample_period），此处不做日期推断。
    - 分钟周期（1/5/15/30/60）：返回该 bar 的精确时分标签，**去除秒与微秒**
      （市场分钟 bar 标签粒度到分钟），不因 pandas 索引/输入精度产生 :30:xx 偏移。
    - 未知 period：抛 ValueError，绝不默默按分钟处理。
    - 支持 datetime/date/pandas Timestamp/ISO `T` 分隔/小数秒/时区 offset；带时区
      输入先折算到 Asia/Shanghai 墙钟再去时区。
    - 非法 / NaT / 解析失败：抛 ValueError，绝不回退到当前扫描时间。
    """
    if period not in PERIODS:
        raise ValueError(f"未知周期 {period!r}，可选: {PERIODS}")
    try:
        import pandas as pd

        t = pd.Timestamp(value)
    except Exception as e:
        raise ValueError(f"无法解析 K 线时间: {value!r}") from e
    if pd.isna(t):
        raise ValueError(f"K 线时间为空(NaT): {value!r}")
    if t.tzinfo is not None:
        t = t.tz_convert(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if period in _CALENDAR_PERIODS:
        return t.replace(hour=15, minute=0, second=0, microsecond=0)
    # 分钟周期：精确时分标签，去除秒与微秒（粒度到分钟）
    return t.replace(second=0, microsecond=0)


def to_market_naive(dt: datetime) -> datetime:
    """UTC naive 存储值 → API 输出 Asia/Shanghai naive（市场时区契约）。

    存储统一为 UTC naive（见 models.utcnow），而 API 契约（docs/DESIGN-CONTRACT.md §1.7）要求
    返回的 naive ISO 一律是 Asia/Shanghai 市场时区，故仅在序列化输出前转换，不改存储。
    仅用于 created_at / updated_at / finished_at / approved_at 等 UTC naive 存储字段；
    信号路径时间（triggered_at / as_of / bar_market_time）已是上海 naive，禁止使用本函数。
    """
    return (
        dt.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .replace(tzinfo=None)
    )


def market_time_range(
    span: str = "3d", exclude_today: bool = False
) -> tuple[datetime | None, datetime]:
    """信号事件时间范围（市场日历口径，按 Asia/Shanghai 自然日边界，naive，end 开区间）。

    span: today / 3d / 7d / 30d / all（all 无下界）。`Nd` = 含 today 在内最近 N 个自然日，
    即 start = today 零点前推 (N-1) 天、end = 明日零点（开区间）。
    exclude_today=True：end 收在今日零点，排除今天（对 all 同样生效，today+exclude_today 为空区间）。
    非法 span 抛 ValueError。start 为 None 表示无下界（all）。
    """
    if span not in MARKET_SPANS:
        raise ValueError(f"不支持时间范围 {span}，可选: {MARKET_SPANS}")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    tomorrow = today + timedelta(days=1)
    start: datetime | None = None
    if span == "today":
        start = today
    elif span != "all":
        days = int(span[:-1])
        start = today - timedelta(days=days - 1)
    end = today if exclude_today else tomorrow
    return start, end
