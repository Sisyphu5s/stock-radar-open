"""Alpha 特征纯计算（bt-fac，lib 层）：数据集日期范围 / 特征面板对齐构造。

由原 dataset.py 模块提取的纯计算部分（无 SessionLocal / 网络 /
文件读写 / DB 查询，仅 stdlib + numpy/pandas 运算）；数据通道（DB 读写、
网络拉取、面板 npz 落盘）留在 app.core.datasets。

lib 依赖规则：本模块禁止 import storage / core(services 层已并入 core)。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import numpy as np
import pandas as pd

# 数据集 universe 配置（语义契约）：
# - all    : 全市场（较慢），limit 控制上限，0 = 全量
# - hs300  : 中证指数官方沪深300成分（降级：新浪/成交额Top300），limit 控制上限，0 = 全量
# - top500 : 成交额 Top500，limit 控制 Top N（0 = 默认 500）
# - custom : 自定义池（custom_codes 必填；缺省时用自选股，仍为空则报错）
UNIVERSES = {
    "all": "全市场（limit 上限，0 = 全量）",
    "hs300": "沪深300成分股",
    "top500": "成交额Top500（limit=Top N，0 = 默认500）",
    "custom": "自定义池",
}

# 各 universe 在 limit=0（不传上限）时的默认规模/行为
UNIVERSE_DEFAULT_LIMITS = {"all": 0, "hs300": 0, "top500": 500, "custom": 0}

# T-07 特征终端扩展：估值特征（来自每日快照 get_spot 链路，面板构建时按交易日注入，
# 非快照日缺失为 NaN）与行业分类特征（整数编码，见 industry_codes）。
ESTIMATE_FEATURES = ("pe", "pb", "ps", "market_cap", "float_cap", "turnover")
INDUSTRY_FEATURE = "industry"


def industry_codes(names, table: dict[str, int]) -> np.ndarray:
    """行业名列表 → 整数编码数组（纯计算）。

    table 为行业名 → 编码的映射表（由调用方从候选集合构建，保证同一数据集内
    编码一致：同行业同名同码）；空串 / None / 非字符串 / 表外名 → 0（未知）。
    编码值参与 RPN 时按数值语义处理。
    """
    return np.array(
        [
            table.get(str(n), 0) if isinstance(n, str) and n.strip() else 0
            for n in names
        ],
        dtype=np.float32,
    )


def list_universes() -> list[dict]:
    return [{"key": k, "name": v} for k, v in UNIVERSES.items()]


def default_range(days_back: int = 365 * 5) -> tuple[str, str]:
    """近 N 天（默认近 5 年）：(start_date, end_date)。"""
    end = date.today()
    start = end - timedelta(days=days_back)
    return start.isoformat(), end.isoformat()


def align_panel(
    candidates: list[tuple[str, pd.DataFrame]], want: list[str]
) -> dict | None:
    """候选 K 线按日期对齐并构造 (S, T) 特征面板（纯计算）。

    参考日期 = 所有候选中出现次数最多的日期序列（新上市/停牌股自然被剔除）；
    一致性过严（对齐后 <2 只）时回退以首只股票日期为参考——仍校验日期相等，
    仅比较长度会让停牌股（长度相同但日期错位）混入面板，造成横截面按位置
    错位对齐。只构造请求的特征子集（want），返回 float32 矩阵。

    返回 {"panel": {f: (S,T) ndarray}, "dates": list[str], "good_codes": list[str],
    "stock_count": S}；无候选 / 无合格股票返回 None。
    """
    if not candidates:
        return None
    ref_dates = Counter(tuple(c[1]["date"].tolist()) for c in candidates).most_common(
        1
    )[0][0]
    aligned = [
        (code, k) for code, k in candidates if tuple(k["date"].tolist()) == ref_dates
    ]
    if len(aligned) < 2:
        ref_dates = tuple(candidates[0][1]["date"].tolist())
        aligned = [
            (code, k)
            for code, k in candidates
            if tuple(k["date"].tolist()) == ref_dates
        ]
    dates = list(ref_dates)
    panel: dict[str, list[np.ndarray]] = {f: [] for f in want}
    good_codes: list[str] = []
    for code, k in aligned:
        if len(k) != len(dates):
            continue
        for f in want:
            if f in k.columns:
                panel[f].append(np.asarray(k[f], dtype=np.float32))
            else:
                # 特征列缺失（如快照不可用未注入估值列）→ 整列 NaN，不抛 KeyError；
                # 缺失列在 RPN 中经 ts_mean/rank 等算子自然传播 NaN。
                panel[f].append(np.full(len(dates), np.nan, dtype=np.float32))
        good_codes.append(code)
    if not good_codes:
        return None
    out_panel = {f: np.stack(v) for f, v in panel.items()}
    return {
        "panel": out_panel,
        "dates": dates,
        "good_codes": good_codes,
        "stock_count": out_panel[want[0]].shape[0],
    }
