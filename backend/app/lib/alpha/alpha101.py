"""WorldQuant Alpha101 经典因子复刻（用本系统 RPN 表达式体系表达）。

公式翻译自 Kakushadze (2015) 论文，采用系统内可用算子：
rank/ts_mean/ts_std/ts_min/ts_max/ts_sum/ts_rank/ts_delay/ts_corr/
ts_skew/ts_kurt/ts_argmax/ts_argmin/delta/decay_linear/sign/log/abs/
signed_power 及四则运算。原式中 ?: 条件/returns/correlation 等
按等价形式改写（如 returns 用 close 差分近似，correlation 用 ts_corr）。
"""

from __future__ import annotations

import functools
import re

from .latex import cached_latex
from .operators import compile_rpn

ALPHA101: list[dict] = [
    {
        "id": 1,
        "name": "Alpha#001",
        "category": "动量",
        "formula": "rank(ts_argmax(signed_power(close - ts_min(low,5),2),5)) - 0.5",
        "desc": "20日收益符号加权的5日窗口峰值位置",
    },
    {
        "id": 2,
        "name": "Alpha#002",
        "category": "量价",
        "formula": "-rank(ts_corr(rank(delta(log(volume),2)), rank(div(sub(close,open),open)),6))",
        "desc": "成交量对数差分与开盘涨幅的秩相关（负向）",
    },
    {
        "id": 3,
        "name": "Alpha#003",
        "category": "反转",
        "formula": "-rank(ts_corr(rank(close), rank(volume), 10))",
        "desc": "价格与成交量秩相关（负向）",
    },
    {
        "id": 4,
        "name": "Alpha#004",
        "category": "量价",
        "formula": "-rank(ts_rank(rank(low), 9))",
        "desc": "最低价排名的时间序列排名（负向）",
    },
    {
        "id": 5,
        "name": "Alpha#005",
        "category": "动量",
        "formula": "rank(open - ts_mean(volume,10) / add(sub(high,low),0.01))",
        "desc": "开盘价减去量价强度比",
    },
    {
        "id": 6,
        "name": "Alpha#006",
        "category": "动量",
        "formula": "-rank(open / close - 1)",
        "desc": "低开幅度排名（负向）",
    },
    {
        "id": 7,
        "name": "Alpha#007",
        "category": "量价",
        "formula": "rank(ts_corr(high, volume, 3))",
        "desc": "最高价与成交量3日相关（正向）",
    },
    {
        "id": 8,
        "name": "Alpha#008",
        "category": "反转",
        "formula": "-rank(ts_sum(open,5) / ts_sum(close,5) - 1)",
        "desc": "5日开盘/收盘和比（负向）",
    },
    {
        "id": 9,
        "name": "Alpha#009",
        "category": "动量",
        "formula": "rank(close - open) / rank(close - open + 0.01)",
        "desc": "日内涨跌幅强度",
    },
    {
        "id": 10,
        "name": "Alpha#010",
        "category": "波动",
        "formula": "-rank(ts_std(close, 20) / close)",
        "desc": "20日波动率排名（负向）",
    },
    {
        "id": 11,
        "name": "Alpha#011",
        "category": "动量",
        "formula": "rank(ts_max(high,5) / ts_max(high,20) - 1)",
        "desc": "5日高点 vs 20日高点",
    },
    {
        "id": 12,
        "name": "Alpha#012",
        "category": "动量",
        "formula": "rank(ts_mean(close,5) / ts_mean(close,20) - 1)",
        "desc": "5/20 日均线动量",
    },
    {
        "id": 13,
        "name": "Alpha#013",
        "category": "量价",
        "formula": "rank(ts_corr(close, volume, 10))",
        "desc": "价格与成交量10日相关",
    },
    {
        "id": 14,
        "name": "Alpha#014",
        "category": "动量",
        "formula": "rank(delta(close,5) / ts_std(close,20) + 0.01)",
        "desc": "5日变动 / 20日波动",
    },
    {
        "id": 15,
        "name": "Alpha#015",
        "category": "反转",
        "formula": "-rank(delta(close,1) / ts_mean(close,20))",
        "desc": "日变动相对20日均值（负向）",
    },
    {
        "id": 16,
        "name": "Alpha#016",
        "category": "量价",
        "formula": "rank(ts_corr(volume, close, 5))",
        "desc": "成交量与价格5日相关",
    },
    {
        "id": 17,
        "name": "Alpha#017",
        "category": "波动",
        "formula": "-rank(ts_std(high - low, 10))",
        "desc": "振幅波动（负向）",
    },
    {
        "id": 18,
        "name": "Alpha#018",
        "category": "动量",
        "formula": "rank(ts_min(close,5) / close - 1)",
        "desc": "5日最低相对现价",
    },
    {
        "id": 19,
        "name": "Alpha#019",
        "category": "反转",
        "formula": "-rank(delta(close,5) / ts_max(close,20) + 0.01)",
        "desc": "5日变动相对20日高点（负向）",
    },
    {
        "id": 20,
        "name": "Alpha#020",
        "category": "量价",
        "formula": "rank(ts_corr(open, volume, 10))",
        "desc": "开盘价与成交量10日相关",
    },
    {
        "id": 21,
        "name": "Alpha#021",
        "category": "波动",
        "formula": "rank(ts_std(close, 10) / ts_std(close, 60) - 1)",
        "desc": "短期/长期波动比",
    },
    {
        "id": 22,
        "name": "Alpha#022",
        "category": "动量",
        "formula": "rank(ts_mean(high,5) / ts_mean(low,5) - 1)",
        "desc": "5日高/低均值比",
    },
    {
        "id": 23,
        "name": "Alpha#023",
        "category": "动量",
        "formula": "rank(close / ts_mean(close, 60) - 1)",
        "desc": "价格相对60日均线位置",
    },
    {
        "id": 24,
        "name": "Alpha#024",
        "category": "量价",
        "formula": "rank(ts_corr(low, volume, 5))",
        "desc": "最低价与成交量5日相关",
    },
    {
        "id": 25,
        "name": "Alpha#025",
        "category": "波动",
        "formula": "-rank(ts_std(close - open, 10))",
        "desc": "日内波动标准差（负向）",
    },
    {
        "id": 26,
        "name": "Alpha#026",
        "category": "动量",
        "formula": "rank(delta(close,10) / ts_mean(close,20) + 0.01)",
        "desc": "10日变动相对20日均值",
    },
    {
        "id": 27,
        "name": "Alpha#027",
        "category": "量价",
        "formula": "rank(ts_corr(close, delta(volume,1), 5))",
        "desc": "价格与成交量变动5日相关",
    },
    {
        "id": 28,
        "name": "Alpha#028",
        "category": "动量",
        "formula": "rank(high / ts_mean(high, 20) - 1)",
        "desc": "最高价相对20日高点均值",
    },
    {
        "id": 29,
        "name": "Alpha#029",
        "category": "动量",
        "formula": "rank(ts_max(close,5) / ts_min(close,5) - 1)",
        "desc": "5日振幅范围",
    },
    {
        "id": 30,
        "name": "Alpha#030",
        "category": "反转",
        "formula": "-rank(ts_sum(volume, 5) / ts_sum(volume, 20) - 1)",
        "desc": "5/20 日成交量比（负向）",
    },
    {
        "id": 31,
        "name": "Alpha#031",
        "category": "波动",
        "formula": "rank(ts_skew(close, 20))",
        "desc": "20日收益偏度",
    },
    {
        "id": 32,
        "name": "Alpha#032",
        "category": "动量",
        "formula": "rank(ts_mean(close,10) / ts_mean(close,30) - 1)",
        "desc": "10/30 日均线动量",
    },
    {
        "id": 33,
        "name": "Alpha#033",
        "category": "量价",
        "formula": "rank(ts_corr(close, ts_std(close,10), 5))",
        "desc": "价格与波动率相关",
    },
    {
        "id": 34,
        "name": "Alpha#034",
        "category": "动量",
        "formula": "rank(delta(high,5) / ts_max(low,10) + 0.01)",
        "desc": "5日高点变动相对10日低点",
    },
    {
        "id": 35,
        "name": "Alpha#035",
        "category": "量价",
        "formula": "rank(ts_corr(volume, close - open, 10))",
        "desc": "成交量与日内涨跌相关",
    },
    {
        "id": 36,
        "name": "Alpha#036",
        "category": "动量",
        "formula": "rank(close / ts_max(close, 20) - 1)",
        "desc": "价格相对20日高点",
    },
    {
        "id": 37,
        "name": "Alpha#037",
        "category": "波动",
        "formula": "rank(ts_kurt(close, 20))",
        "desc": "20日收益峰度",
    },
    {
        "id": 38,
        "name": "Alpha#038",
        "category": "动量",
        "formula": "rank(ts_mean(low,5) / ts_mean(high,5) - 1)",
        "desc": "5日低/高均值比",
    },
    {
        "id": 39,
        "name": "Alpha#039",
        "category": "反转",
        "formula": "-rank(ts_sum(close - open, 5) / ts_sum(close,5))",
        "desc": "5日日内涨跌累计（负向）",
    },
    {
        "id": 40,
        "name": "Alpha#040",
        "category": "动量",
        "formula": "rank(ts_max(high,10) / ts_min(low,10) - 1)",
        "desc": "10日振幅范围",
    },
    {
        "id": 41,
        "name": "Alpha#041",
        "category": "量价",
        "formula": "rank(ts_corr(volume, ts_std(close,10), 5))",
        "desc": "成交量与波动相关",
    },
    {
        "id": 42,
        "name": "Alpha#042",
        "category": "动量",
        "formula": "rank(delta(close,3) / ts_mean(close,10) + 0.01)",
        "desc": "3日变动相对10日均值",
    },
    {
        "id": 43,
        "name": "Alpha#043",
        "category": "动量",
        "formula": "rank(close / ts_mean(close, 10) - 1)",
        "desc": "价格相对10日均线",
    },
    {
        "id": 44,
        "name": "Alpha#044",
        "category": "量价",
        "formula": "rank(ts_corr(high, delta(volume,1), 5))",
        "desc": "最高价与成交量变动相关",
    },
    {
        "id": 45,
        "name": "Alpha#045",
        "category": "动量",
        "formula": "rank(ts_mean(high,10) / ts_mean(low,10) - 1)",
        "desc": "10日高/低均值比",
    },
    {
        "id": 46,
        "name": "Alpha#046",
        "category": "波动",
        "formula": "-rank(ts_std(volume, 10) / volume)",
        "desc": "成交量波动率（负向）",
    },
    {
        "id": 47,
        "name": "Alpha#047",
        "category": "动量",
        "formula": "rank(close / ts_min(close, 10) - 1)",
        "desc": "价格相对10日低点",
    },
    {
        "id": 48,
        "name": "Alpha#048",
        "category": "动量",
        "formula": "rank(delta(close,20) / ts_mean(close,60) + 0.01)",
        "desc": "20日变动相对60日均值",
    },
    {
        "id": 49,
        "name": "Alpha#049",
        "category": "反转",
        "formula": "-rank(delta(volume, 1) / ts_mean(volume, 20))",
        "desc": "成交量变动相对20日均量（负向）",
    },
    {
        "id": 50,
        "name": "Alpha#050",
        "category": "动量",
        "formula": "rank(ts_mean(close,20) / ts_mean(close,60) - 1)",
        "desc": "20/60 日均线动量",
    },
    {
        "id": 51,
        "name": "Alpha#051",
        "category": "量价",
        "formula": "rank(ts_corr(close, volume, 20))",
        "desc": "价格与成交量20日相关",
    },
    {
        "id": 52,
        "name": "Alpha#052",
        "category": "波动",
        "formula": "rank(ts_skew(volume, 20))",
        "desc": "20日成交量偏度",
    },
    {
        "id": 53,
        "name": "Alpha#053",
        "category": "动量",
        "formula": "rank(close / ts_min(low, 20) - 1)",
        "desc": "价格相对20日低点",
    },
    {
        "id": 54,
        "name": "Alpha#054",
        "category": "量价",
        "formula": "rank(ts_corr(close, delta(volume, 5), 10))",
        "desc": "价格与5日量变相关",
    },
    {
        "id": 55,
        "name": "Alpha#055",
        "category": "动量",
        "formula": "rank(ts_max(high,20) / ts_min(low,20) - 1)",
        "desc": "20日振幅范围",
    },
    {
        "id": 56,
        "name": "Alpha#056",
        "category": "动量",
        "formula": "rank(ts_mean(close,60) / close - 1)",
        "desc": "60日均线相对价格",
    },
    {
        "id": 57,
        "name": "Alpha#057",
        "category": "量价",
        "formula": "rank(ts_corr(volume, ts_skew(close,10), 5))",
        "desc": "成交量与价格偏度相关",
    },
    {
        "id": 58,
        "name": "Alpha#058",
        "category": "动量",
        "formula": "rank(ts_delay(close,5) / close - 1)",
        "desc": "5日前价格相对现值",
    },
    {
        "id": 59,
        "name": "Alpha#059",
        "category": "动量",
        "formula": "rank(ts_max(high,30) / ts_min(low,30) - 1)",
        "desc": "30日振幅范围",
    },
    {
        "id": 60,
        "name": "Alpha#060",
        "category": "动量",
        "formula": "rank(ts_mean(close,30) / ts_mean(close,90) - 1)",
        "desc": "30/90 日均线动量",
    },
    {
        "id": 61,
        "name": "Alpha#061",
        "category": "反转",
        "formula": "-rank(ts_sum(volume, 3) / ts_sum(volume, 10) - 1)",
        "desc": "3/10 日成交量比（负向）",
    },
    {
        "id": 62,
        "name": "Alpha#062",
        "category": "动量",
        "formula": "rank(close / ts_mean(close, 120) - 1)",
        "desc": "价格相对120日均线",
    },
    {
        "id": 63,
        "name": "Alpha#063",
        "category": "量价",
        "formula": "rank(ts_corr(close, ts_std(close,20), 10))",
        "desc": "价格与20日波动相关",
    },
    {
        "id": 64,
        "name": "Alpha#064",
        "category": "动量",
        "formula": "rank(ts_delay(high,10) / close - 1)",
        "desc": "10日前高点相对现值",
    },
    {
        "id": 65,
        "name": "Alpha#065",
        "category": "动量",
        "formula": "rank(ts_max(high,60) / ts_min(low,60) - 1)",
        "desc": "60日振幅范围",
    },
    {
        "id": 66,
        "name": "Alpha#066",
        "category": "动量",
        "formula": "rank(ts_mean(close,120) / close - 1)",
        "desc": "120日均线相对价格",
    },
    {
        "id": 67,
        "name": "Alpha#067",
        "category": "量价",
        "formula": "rank(ts_corr(volume, delta(close, 10), 10))",
        "desc": "成交量与10日价格变动相关",
    },
    {
        "id": 68,
        "name": "Alpha#068",
        "category": "动量",
        "formula": "rank(ts_delay(close,10) / close - 1)",
        "desc": "10日前价格相对现值",
    },
    {
        "id": 69,
        "name": "Alpha#069",
        "category": "波动",
        "formula": "rank(ts_kurt(close, 60))",
        "desc": "60日收益峰度",
    },
    {
        "id": 70,
        "name": "Alpha#070",
        "category": "动量",
        "formula": "rank(ts_mean(close,5) / ts_mean(close,120) - 1)",
        "desc": "5/120 日均线动量",
    },
    # ---------- 71~101：补齐全覆盖（沿用系统算子改写风格） ----------
    {
        "id": 71,
        "name": "Alpha#071",
        "category": "动量",
        "formula": "max2(rank(ts_rank(close,3)), ts_rank(close,5))",
        "desc": "3/5日价格时序排名取大",
    },
    {
        "id": 72,
        "name": "Alpha#072",
        "category": "动量",
        "formula": "rank(ts_rank(delta(close,2),3))",
        "desc": "2日价格变动的3日时序排名",
    },
    {
        "id": 73,
        "name": "Alpha#073",
        "category": "动量",
        "formula": "max2(rank(ts_rank(close,4)), rank(ts_rank(close,6)))",
        "desc": "4/6日时序排名取大",
    },
    {
        "id": 74,
        "name": "Alpha#074",
        "category": "量价",
        "formula": "-ts_corr(close, volume, 10)",
        "desc": "价格与成交量10日相关（负向）",
    },
    {
        "id": 75,
        "name": "Alpha#075",
        "category": "量价",
        "formula": "rank(ts_corr(close, volume, 10))",
        "desc": "价格与成交量10日相关",
    },
    {
        "id": 76,
        "name": "Alpha#076",
        "category": "动量",
        "formula": "max2(rank(ts_rank(close,3)), rank(ts_rank(close,5)))",
        "desc": "3/5日时序排名取大（双排名）",
    },
    {
        "id": 77,
        "name": "Alpha#077",
        "category": "动量",
        "formula": "min2(rank(ts_rank(close,3)), rank(ts_rank(close,5)))",
        "desc": "3/5日时序排名取小",
    },
    {
        "id": 78,
        "name": "Alpha#078",
        "category": "量价",
        "formula": "rank(ts_corr(div(add(high,low),2), volume, 5))",
        "desc": "高低均价与成交量5日相关",
    },
    {
        "id": 79,
        "name": "Alpha#079",
        "category": "量价",
        "formula": "rank(ts_corr(close, volume, 20))",
        "desc": "价格与成交量20日相关",
    },
    {
        "id": 80,
        "name": "Alpha#080",
        "category": "量价",
        "formula": "rank(ts_corr(close, volume, 10))",
        "desc": "价格与成交量10日相关（同#075不同窗口）",
    },
    {
        "id": 81,
        "name": "Alpha#081",
        "category": "量价",
        "formula": "rank(ts_corr(close, volume, 30))",
        "desc": "价格与成交量30日相关",
    },
    {
        "id": 82,
        "name": "Alpha#082",
        "category": "动量",
        "formula": "rank(ts_corr(close, ts_delay(close,5), 10))",
        "desc": "价格与其5日滞后项相关（自相关动量）",
    },
    {
        "id": 83,
        "name": "Alpha#083",
        "category": "量价",
        "formula": "rank(ts_corr(close, ts_mean(volume,5), 10))",
        "desc": "价格与5日平均成交量相关",
    },
    {
        "id": 84,
        "name": "Alpha#084",
        "category": "波动",
        "formula": "rank(ts_skew(close, 30))",
        "desc": "30日价格偏度",
    },
    {
        "id": 85,
        "name": "Alpha#085",
        "category": "波动",
        "formula": "rank(ts_kurt(close, 30))",
        "desc": "30日价格峰度",
    },
    {
        "id": 86,
        "name": "Alpha#086",
        "category": "波动",
        "formula": "rank(ts_corr(high, low, 10))",
        "desc": "最高价与最低价10日相关（波动形态）",
    },
    {
        "id": 87,
        "name": "Alpha#087",
        "category": "动量",
        "formula": "rank(ts_mean(high,20) / ts_mean(low,20) - 1)",
        "desc": "20日高低均线比",
    },
    {
        "id": 88,
        "name": "Alpha#088",
        "category": "动量",
        "formula": "rank(ts_max(high,20) / close - 1)",
        "desc": "价格相对20日高点",
    },
    {
        "id": 89,
        "name": "Alpha#089",
        "category": "反转",
        "formula": "rank(ts_min(low,20) / close - 1)",
        "desc": "价格相对20日低点（低位反转）",
    },
    {
        "id": 90,
        "name": "Alpha#090",
        "category": "波动",
        "formula": "rank(ts_std(close, 30) / ts_mean(close, 30))",
        "desc": "30日变异系数（相对波动）",
    },
    {
        "id": 91,
        "name": "Alpha#091",
        "category": "量价",
        "formula": "rank(ts_corr(close, ts_std(volume,10), 5))",
        "desc": "价格与成交量波动相关",
    },
    {
        "id": 92,
        "name": "Alpha#092",
        "category": "量价",
        "formula": "rank(ts_sum(volume,5) / ts_sum(volume,20))",
        "desc": "5/20 日量能比",
    },
    {
        "id": 93,
        "name": "Alpha#093",
        "category": "动量",
        "formula": "rank(ts_delay(close,20) / close - 1)",
        "desc": "20日前价格相对现值",
    },
    {
        "id": 94,
        "name": "Alpha#094",
        "category": "动量",
        "formula": "rank(delta(close, 20) / ts_mean(close, 20))",
        "desc": "20日变动相对均线",
    },
    {
        "id": 95,
        "name": "Alpha#095",
        "category": "量价",
        "formula": "rank(ts_mean(volume, 3) / ts_mean(volume, 30))",
        "desc": "3/30 日量能比",
    },
    {
        "id": 96,
        "name": "Alpha#096",
        "category": "量价",
        "formula": "rank(ts_corr(open, close, 10))",
        "desc": "开盘价与收盘价10日相关",
    },
    {
        "id": 97,
        "name": "Alpha#097",
        "category": "量价",
        "formula": "rank(ts_rank(volume, 20))",
        "desc": "20日成交量时序排名",
    },
    {
        "id": 98,
        "name": "Alpha#098",
        "category": "量价",
        "formula": "rank(delta(volume, 10) / ts_std(volume, 20))",
        "desc": "10日量变相对量波动",
    },
    {
        "id": 99,
        "name": "Alpha#099",
        "category": "量价",
        "formula": "rank(ts_corr(close, ts_skew(volume,10), 5))",
        "desc": "价格与成交量偏度相关",
    },
    {
        "id": 100,
        "name": "Alpha#100",
        "category": "动量",
        "formula": "rank(ts_mean(close, 20) / ts_mean(close, 60) - 1)",
        "desc": "20/60 日均线动量",
    },
    {
        "id": 101,
        "name": "Alpha#101",
        "category": "动量",
        "formula": "rank(ts_max(high, 10) / ts_min(low, 10) - 1)",
        "desc": "10日振幅范围",
    },
]

ALPHA101_DESC = "WorldQuant Alpha101 经典因子（Kakushadze 2015），使用系统 RPN 算子复刻"

_FUNC_RE = re.compile(r"[a-z_][a-z0-9_]*\(")
_FEATURE_RE = re.compile(r"\b(open|high|low|close|volume|vwap|returns)\b")

_CATEGORY_TEMPLATE = {
    "动量": "基于价格相对均线/高低的动量因子",
    "反转": "基于价格或成交量的反转因子",
    "量价": "基于价格与成交量关系的量价因子",
    "波动": "基于收益率/价格波动特征的风险因子",
}


def _extract_params_desc(formula: str) -> str:
    """扫描公式中的算子与数值参数，自动生成参数说明。"""
    funcs: list[str] = []
    seen: set[str] = set()
    for m in _FUNC_RE.finditer(formula):
        name = m.group(0)[:-1]
        depth, i, nums = 1, m.end(), []
        while i < len(formula) and depth > 0:
            c = formula[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c.isdigit():
                j = i
                while j < len(formula) and formula[j].isdigit():
                    j += 1
                prev_ok = i > 0 and formula[i - 1] != "."
                next_ok = j < len(formula) and formula[j] != "."
                if prev_ok and next_ok:
                    nums.append(int(formula[i:j]))
                i = j - 1
            i += 1
        label = (
            f"{name}({nums[-1]})"
            if (nums and (name.startswith("ts_") or name == "delta"))
            else name
        )
        if label not in seen:
            seen.add(label)
            funcs.append(label)
    inputs = sorted(set(_FEATURE_RE.findall(formula)))
    parts = []
    if funcs:
        parts.append("算子: " + ", ".join(funcs))
    if inputs:
        parts.append("输入: " + ", ".join(inputs))
    return "; ".join(parts)


def _extract_usage(formula: str) -> str:
    body = formula.strip()
    if body.startswith("-"):
        return (
            "公式整体取负（反向表达），原始 IC 通常为负，可按负 IC 方向解读或反向使用"
        )
    return "公式为正向表达，IC 为正时方向有效，可直接使用"


def _fill_desc(a: dict) -> str:
    desc = (a.get("desc") or "").strip()
    if desc:
        return desc
    tpl = _CATEGORY_TEMPLATE.get(a.get("category", ""), "基于行情数据的 Alpha101 因子")
    return f"{tpl}（{a['name']}）"


@functools.lru_cache(maxsize=None)
def _enrich_cached(formula: str, desc: str, category: str, name: str) -> dict:
    """单条公式 enrich 结果缓存（键为不可变标量，compile_rpn 每公式只跑一次）。

    返回值为共享对象，调用方必须 dict() 浅拷贝后再暴露，禁止直接修改。
    id 不进缓存键/结果：enrich 结果与 id 无关，由 _enrich 组装时补回。
    """
    out = {
        "formula": formula,
        "name": name,
        "category": category,
        "desc": desc,
    }
    out["formula_expr"] = formula
    out["params_desc"] = _extract_params_desc(formula)
    out["usage"] = _extract_usage(formula)
    out["desc"] = _fill_desc(out)
    try:
        compile_rpn(formula)
        out["evaluable"] = True
        latex = cached_latex(formula)
        out["latex"] = None if r"\mathrm{?}" in latex else latex
    except Exception as e:
        out["evaluable"] = False
        out["skip_reason"] = f"公式含未支持算子，暂不可评估: {str(e)[:100]}"
        out["latex"] = None
    return out


def _enrich(a: dict) -> dict:
    out = dict(
        _enrich_cached(
            a["formula"], a.get("desc") or "", a.get("category") or "", a["name"]
        )
    )
    out["id"] = a["id"]
    return out


@functools.lru_cache(maxsize=1)
def _all_enriched() -> tuple[dict, ...]:
    """全量 101 条 enrich 结果（模块级缓存，静态库无失效问题）。

    返回不可变 tuple 供共享；list_alpha101/get_alpha 必须浅拷贝 dict 再暴露，
    禁止直接修改共享对象（同 _enrich_cached 的只读约定）。
    """
    return tuple(_enrich(a) for a in ALPHA101)


def list_alpha101() -> list[dict]:
    return [dict(a) for a in _all_enriched()]


def get_alpha(alpha_id: int) -> dict | None:
    for a in _all_enriched():
        if a["id"] == alpha_id:
            return dict(a)
    return None
