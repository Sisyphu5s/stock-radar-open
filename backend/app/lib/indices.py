"""全球核心指数清单常量（T-73）：单一事实源。

8 只核心指数（A 股 4 + 美股 3 + 港股 1），每只含：
- code：内部规范代码（API 契约 code 字段，稳定标识）；
- name：展示名称；
- market：市场标识（CN=中国 A 股 / US=美股 / HK=港股）；
- currency：货币（CNY / USD / HKD）；
- tz：IETF 时区名（per-market 交易时段判定与展示口径）；
- is_index：是否指数（当前恒 True，为将来清单可能混入股票类基准保留字段位）；
- 各数据源符号：东财全球指数（index_global_spot_em）/ 新浪 A 股指数
  （stock_zh_index_spot_sina）/ 新浪港股指数（stock_hk_index_spot_sina）/
  新浪美股实时（hq.sinajs.cn gb_* 通道）。

源符号依据（akshare 1.18.83 实测 + 源码确认）：
- 东财全球指数 index_global_spot_em：fs 参数含 i:1.000001 / i:0.399001 /
  i:0.399006 / i:1.000300 / i:100.HSI / i:100.DJIA / i:100.NDX / i:100.SPX，
  code 列即上述 6 位数字 / HSI / DJIA / NDX / SPX；
- 新浪 A 股 stock_zh_index_spot_sina：sh/sz 前缀 + 6 位（sh000001 / sz399001 ...）；
- 新浪港股 stock_hk_index_spot_sina：HSI；
- 新浪美股实时 hq.sinajs.cn：gb_dji / gb_ixic / gb_inx（实测 200 可用）。

代码匹配策略（provider 内实现）：先按各源符号精确匹配，匹配不到再按名称
匹配（东财字段/代码格式有变时名称兜底，不因接口微调而全链路失败）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 市场标识（单一事实源：session.py in_market_session / provider 分市场缓存共用）
MARKET_CN = "CN"
MARKET_US = "US"
MARKET_HK = "HK"
MARKET_ORDER = (MARKET_CN, MARKET_US, MARKET_HK)


@dataclass(frozen=True)
class IndexSpec:
    """单只核心指数规格。"""

    code: str  # 内部规范代码（API 契约 code）
    name: str  # 展示名称
    market: str  # CN / US / HK
    currency: str  # CNY / USD / HKD
    tz: str  # IETF 时区名（Asia/Shanghai / America/New_York / Asia/Hong_Kong）
    is_index: bool = True
    # 数据源符号（空 = 该源无此指数）
    em_global: str = ""  # 东财全球指数 index_global_spot_em code
    sina_cn: str = ""  # 新浪 A 股指数 code
    sina_hk: str = ""  # 新浪港股指数 code
    sina_us_hq: str = ""  # 新浪美股实时 hq 符号（gb_*）


# 8 只核心指数（顺序即展示顺序：A 股 → 美股 → 港股）
INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec(
        "000001.SH",
        "上证指数",
        MARKET_CN,
        "CNY",
        "Asia/Shanghai",
        em_global="000001",
        sina_cn="sh000001",
    ),
    IndexSpec(
        "399001.SZ",
        "深证成指",
        MARKET_CN,
        "CNY",
        "Asia/Shanghai",
        em_global="399001",
        sina_cn="sz399001",
    ),
    IndexSpec(
        "399006.SZ",
        "创业板指",
        MARKET_CN,
        "CNY",
        "Asia/Shanghai",
        em_global="399006",
        sina_cn="sz399006",
    ),
    IndexSpec(
        "000300.SH",
        "沪深300",
        MARKET_CN,
        "CNY",
        "Asia/Shanghai",
        em_global="000300",
        sina_cn="sh000300",
    ),
    IndexSpec(
        ".DJI",
        "道琼斯",
        MARKET_US,
        "USD",
        "America/New_York",
        em_global="DJIA",
        sina_us_hq="gb_dji",
    ),
    IndexSpec(
        ".IXIC",
        "纳斯达克",
        MARKET_US,
        "USD",
        "America/New_York",
        em_global="NDX",
        sina_us_hq="gb_ixic",
    ),
    IndexSpec(
        ".INX",
        "标普500",
        MARKET_US,
        "USD",
        "America/New_York",
        em_global="SPX",
        sina_us_hq="gb_inx",
    ),
    IndexSpec(
        "HSI",
        "恒生指数",
        MARKET_HK,
        "HKD",
        "Asia/Hong_Kong",
        em_global="HSI",
        sina_hk="HSI",
    ),
)

# 按市场分组（provider per-market 缓存/TTL 用）
INDEXES_BY_MARKET: dict[str, tuple[IndexSpec, ...]] = {
    m: tuple(spec for spec in INDEXES if spec.market == m) for m in MARKET_ORDER
}
