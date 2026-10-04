"""模拟盘：指标信号实验与实时观察（纯计算，无持仓/资金模拟）。

逐 bar 信号判定规则与 signals/engine 内置检查器同口径：
- 金叉/死叉按「前值 ≤/> 后值」跨 bar 判定（与 engine 的 _macd_golden_cross 等一致）；
- 指标由 compute.compute_all 一次性算全量（macd/kdj/rsi/ma/boll/volume_ratio）；
- warmup 通过 NaN 自然跳过：指标未成形的 bar 比较结果为 False，不判定。

仅支持信号白名单（SIGNAL_TYPES）中本模块实现了逐 bar 规则的子集；
未覆盖的信号在 validate_signals 抛 ValueError（API 层转 400「暂不支持该信号做历史实验」）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ...lib.indicators.compute import compute_all
from ...lib.signals.catalog import SIGNAL_CATALOG
from ...lib.signals.engine import SIGNAL_TYPES

# 每个信号最多返回的触发事件条数（最近 N 条）
MAX_EVENTS_PER_SIGNAL = 50

# 模拟盘支持周期与最小 bar 数（单一事实源）：api/paper 与 core/tasks/runner
# （PAPER_* 前缀别名）均引用本处，不再各持一份。
# 分钟线 + 日线；周/月由日线重采样，与逐 bar 收益口径不一致，暂不支持
PAPER_VALID_PERIODS = ("1", "5", "15", "30", "60", "daily")
PAPER_MIN_BARS = 20  # macd/boll 成形 ~35 bar，留余量；不足视为数据不可用

# 历史实验收益基准说明（单一事实源，随 experiment 结果返回，供前端标注展示）：
# r5/r10 以触发日收盘价为基准——信号于当日收盘后才可确认，实际不可成交，
# 属理论收益（触发日收盘买入），仅供统计参考，不代表可成交收益。
RETURN_BASIS_NOTE = (
    "理论收益（触发日收盘买入）：r5/r10 以触发日收盘价为基准买入、第 5/10 根 bar "
    "收盘卖出；信号于当日收盘后才可确认，实际不可成交，仅供统计参考"
)

# 信号中文标签/说明的单一事实源 = lib/signals/catalog.SIGNAL_CATALOG
# （code → (中文名, 分类, 一句话说明, 默认参数)），此处不再重复维护文案，
# 派生见 _RULES 定义后（SIGNAL_LABELS/SIGNAL_DESCRIPTIONS）。

# compute_all 所需指标集（rsi 同时产出 rsi6/12/24 与 rsi(14)）
_INDICATOR_FIELDS = [
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "boll",
    "macd",
    "kdj",
    "rsi",
    "volume_ratio",
]

# watch.indicators 固定键（末尾 close：原始收盘价非指标，供前端价格曲线/快照落库）
_WATCH_KEYS = (
    "rsi6",
    "rsi12",
    "rsi24",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "volume_ratio",
    "close",
)


# ---------- 逐 bar 判定原语（numpy 向量化，NaN 比较自动为 False） ----------


def _cross_up(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a 上穿 b（前值 ≤ 后值 >）。"""
    hit = np.zeros(len(a), dtype=bool)
    if len(a) >= 2:
        hit[1:] = (a[:-1] <= b[:-1]) & (a[1:] > b[1:])
    return hit


def _cross_down(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a 下穿 b（前值 ≥ 后值 <）。"""
    hit = np.zeros(len(a), dtype=bool)
    if len(a) >= 2:
        hit[1:] = (a[:-1] >= b[:-1]) & (a[1:] < b[1:])
    return hit


def _cross_threshold_up(a: np.ndarray, thr: float) -> np.ndarray:
    hit = np.zeros(len(a), dtype=bool)
    if len(a) >= 2:
        hit[1:] = (a[:-1] <= thr) & (a[1:] > thr)
    return hit


def _cross_threshold_down(a: np.ndarray, thr: float) -> np.ndarray:
    hit = np.zeros(len(a), dtype=bool)
    if len(a) >= 2:
        hit[1:] = (a[:-1] >= thr) & (a[1:] < thr)
    return hit


# 信号 → 逐 bar 触发向量（对齐 bar 下标；True = 该 bar 触发）
_RULES: dict[str, object] = {
    "macd_golden_cross": lambda b: _cross_up(b["macd_dif"], b["macd_dea"]),
    "macd_dead_cross": lambda b: _cross_down(b["macd_dif"], b["macd_dea"]),
    "kdj_golden_cross": lambda b: _cross_up(b["kdj_k"], b["kdj_d"]),
    "kdj_dead_cross": lambda b: _cross_down(b["kdj_k"], b["kdj_d"]),
    "rsi_cross_up": lambda b: _cross_threshold_up(b["rsi"], 30.0),
    "rsi_cross_down": lambda b: _cross_threshold_down(b["rsi"], 70.0),
    "ma_golden_cross": lambda b: _cross_up(b["ma5"], b["ma20"]),
    "boll_breakout": lambda b: b["close"] > b["boll_upper"],
    "volume_surge": lambda b: (b["volume_ratio"] > 2.0) & b["close_up"],
    "trend_breakout": lambda b: (
        (b["close"] > b["prev_high20"]) & (b["prev_high20"] > 0)
    ),
}

# 支持做实验的信号白名单（engine.SIGNAL_TYPES 的子集）
SUPPORTED_SIGNALS = tuple(_RULES)

# 信号 → 中文标签 / 一句话说明（GET /paper/meta、watch.triggered 用）：
# 从 lib/signals/catalog.SIGNAL_CATALOG 派生（单一事实源，catalog 缺条目即缺失，
# 不在此重复维护文案）。
SIGNAL_LABELS: dict[str, str] = {k: SIGNAL_CATALOG[k][0] for k in _RULES}
SIGNAL_DESCRIPTIONS: dict[str, str] = {k: SIGNAL_CATALOG[k][2] for k in _RULES}


# ---------- 指标数组构建 ----------


def _bar_arrays(df: pd.DataFrame) -> dict:
    """一次性算全量指标 → 对齐 bar 下标的 numpy dict。"""
    ind = compute_all(df, _INDICATOR_FIELDS)
    b: dict = {k: np.asarray(v, dtype=float) for k, v in ind.items()}
    close = df["close"].to_numpy(dtype=float)
    b["close"] = close
    b["close_up"] = np.concatenate(([False], close[1:] > close[:-1]))
    # 20 日高点（不含当日）：与 engine trend_breakout 的 close.shift(1).rolling(20).max() 同口径
    b["prev_high20"] = pd.Series(close).shift(1).rolling(20).max().to_numpy(dtype=float)
    return b


# ---------- 信号校验 ----------


def validate_signals(signals) -> list[str]:
    """校验信号列表：SIGNAL_TYPES 白名单 + 支持集；去重保序。

    违规抛 ValueError（人类可读中文，API 层转 400）。
    """
    if not isinstance(signals, (list, tuple)) or not signals:
        raise ValueError("signals 必须为非空列表")
    if len(signals) > 10:
        raise ValueError("signals 最多 10 个")
    seen: set[str] = set()
    out: list[str] = []
    for s in signals:
        if not isinstance(s, str) or not s.strip():
            raise ValueError(f"非法信号项: {s!r}")
        s = s.strip()
        if s in seen:
            continue
        if s not in SIGNAL_TYPES:
            raise ValueError(f"未知信号 {s!r}，可选: {', '.join(SIGNAL_TYPES)}")
        if s not in _RULES:
            raise ValueError(f"暂不支持信号 {s!r} 做历史实验")
        seen.add(s)
        out.append(s)
    return out


# ---------- 触发事件检测 ----------


def detect_events(df: pd.DataFrame, signals: list[str]) -> dict[str, list[dict]]:
    """逐 bar 判定信号触发，返回 signal → [{date, close, r5, r10}]（时间升序）。

    r5/r10：触发后第 5/10 根 bar 收盘收益（小数，如 0.0088 = 0.88%，与 win_rate 同口径，
    前端 fmtPct 会再 ×100 展示）；末尾不足则 None。
    收益基准 = 触发日收盘价：信号于当日收盘后才可确认，实际不可成交，属理论收益
    （触发日收盘买入），仅供统计参考，口径说明见 RETURN_BASIS_NOTE。
    """
    signals = validate_signals(signals)
    b = _bar_arrays(df)
    n = len(df)
    close = b["close"]
    dates = df["date"].astype(str).tolist()
    out: dict[str, list[dict]] = {}
    for sig in signals:
        hit = _RULES[sig](b)
        events: list[dict] = []
        for i in np.flatnonzero(hit):
            # 理论收益：以触发日收盘价为基准（信号收盘后才确认，不可成交），仅统计口径
            r5 = (close[i + 5] / close[i] - 1) if i + 5 < n else None
            r10 = (close[i + 10] / close[i] - 1) if i + 10 < n else None
            events.append(
                {
                    "date": dates[i],
                    "close": round(float(close[i]), 4),
                    "r5": round(float(r5), 4) if r5 is not None else None,
                    "r10": round(float(r10), 4) if r10 is not None else None,
                }
            )
        out[sig] = events
    return out


def _summary(sig: str, events: list[dict]) -> dict:
    """单信号汇总统计 + 最近 MAX_EVENTS_PER_SIGNAL 条事件（新在前）。

    键名与前端 PaperSignalResult 契约对齐：triggers/last_trigger/details
    （历史漂移的 count/last_triggered_at/events 已弃用）；avg_r5/avg_r10 为小数
    （与 win_rate=0.4 同口径，前端 fmtPct 展示时 ×100）。best_r5/worst_r5 为附加字段。
    """
    r5s = [e["r5"] for e in events if e["r5"] is not None]
    r10s = [e["r10"] for e in events if e["r10"] is not None]
    win_rate = sum(1 for r in r5s if r > 0) / len(r5s) if r5s else None
    return {
        "signal": sig,
        "triggers": len(events),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_r5": round(float(np.mean(r5s)), 4) if r5s else None,
        "avg_r10": round(float(np.mean(r10s)), 4) if r10s else None,
        "best_r5": round(float(max(r5s)), 4) if r5s else None,
        "worst_r5": round(float(min(r5s)), 4) if r5s else None,
        "last_trigger": events[-1]["date"] if events else None,
        "details": list(reversed(events[-MAX_EVENTS_PER_SIGNAL:])),
    }


def experiment(df: pd.DataFrame, signals: list[str]) -> dict:
    """指标信号历史实验：{results: [汇总...], computed_bars: bar 数, return_note: 收益基准说明}。

    全部收益（r5/r10 及派生的 avg/best/worst/win_rate）均为理论收益：以触发日收盘价为
    基准（信号收盘后才确认，不可成交），口径见 RETURN_BASIS_NOTE。
    """
    signals = validate_signals(signals)
    ev = detect_events(df, signals)
    return {
        "results": [_summary(sig, ev[sig]) for sig in signals],
        "computed_bars": len(df),
        "return_note": RETURN_BASIS_NOTE,
    }


# ---------- 实时观察 ----------


def _latest_value(b: dict, key: str, i: int):
    v = b[key][i]
    return None if np.isnan(v) else round(float(v), 4)


def _detail(sig: str, b: dict, i: int) -> str:
    """最新 bar 命中时的证据描述（与 engine 证据同风格）。"""
    if sig == "macd_golden_cross":
        return f"DIF {b['macd_dif'][i]:.3f} 上穿 DEA {b['macd_dea'][i]:.3f}"
    if sig == "macd_dead_cross":
        return f"DIF {b['macd_dif'][i]:.3f} 下穿 DEA {b['macd_dea'][i]:.3f}"
    if sig == "kdj_golden_cross":
        return f"K {b['kdj_k'][i]:.1f} 上穿 D {b['kdj_d'][i]:.1f}"
    if sig == "kdj_dead_cross":
        return f"K {b['kdj_k'][i]:.1f} 下穿 D {b['kdj_d'][i]:.1f}"
    if sig == "rsi_cross_up":
        return f"RSI(14) {b['rsi'][i]:.1f} 上穿 30"
    if sig == "rsi_cross_down":
        return f"RSI(14) {b['rsi'][i]:.1f} 下穿 70"
    if sig == "ma_golden_cross":
        return f"MA5 {b['ma5'][i]:.2f} 上穿 MA20 {b['ma20'][i]:.2f}"
    if sig == "boll_breakout":
        return f"收盘 {b['close'][i]:.2f} 突破布林上轨 {b['boll_upper'][i]:.2f}"
    if sig == "volume_surge":
        return f"量比 {b['volume_ratio'][i]:.2f} > 2 且收涨"
    if sig == "trend_breakout":
        return f"收盘 {b['close'][i]:.2f} 突破 20 日高点"
    return ""


def watch(df: pd.DataFrame, signals: list[str]) -> dict:
    """实时指标观察：{indicators, triggered, recent}。

    - indicators：最新 bar 各指标值（NaN → None）；
    - triggered：最新 bar 命中的信号（与实验同一套逐 bar 规则）；
    - recent：全区间最近 5 个触发点（新在前）。
    """
    signals = validate_signals(signals)
    b = _bar_arrays(df)
    n = len(df)
    last = n - 1
    dates = df["date"].astype(str).tolist()
    indicators = {k: _latest_value(b, k, last) for k in _WATCH_KEYS}
    triggered: list[dict] = []
    points: list[tuple[int, str]] = []
    for sig in signals:
        hit = _RULES[sig](b)
        if bool(hit[last]):
            triggered.append(
                {
                    "signal": sig,
                    "label": SIGNAL_LABELS.get(sig, sig),
                    "detail": _detail(sig, b, last),
                }
            )
        points.extend((int(i), sig) for i in np.flatnonzero(hit))
    points.sort(key=lambda t: t[0])
    recent = [{"date": dates[i], "signal": sig} for i, sig in points[-5:]]
    recent.reverse()
    return {"indicators": indicators, "triggered": triggered, "recent": recent}
