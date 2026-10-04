"""信号引擎：内置信号判定库（单股全信号评估）。"""

import logging
import threading

import pandas as pd

from ..codes import bare_code
from ..indicators.compute import (
    atr,
    bias,
    boll,
    cci,
    dmi,
    ema,
    kdj,
    ma,
    macd,
    mtm,
    obv,
    psy,
    roc,
    rsi,
    trix,
    vr,
    volume_ratio,
    wr,
)
from .catalog import SIGNAL_CATALOG

logger = logging.getLogger("stockradar.signals.engine")

# 内置信号检查器：返回 (命中与否, 证据描述)
_BUILTIN_CHECKS = {}

# 单轮扫描的指标/检查结果 memo（作用域仅一轮扫描，由 scanner 用 indicator_memo 安装）。
# 同一股票 K 线在多个信号间重复计算 rsi/macd/boll 等指标，memo 命中后直接复用，
# 避免跨信号重复计算。用 threading.local 承载：每线程独立 memo，
# 调度扫描线程与手动扫描请求线程并发进入/退出时互不污染（模块级全局 dict 会交错覆盖）。
# memo 未安装（None）时全部直算（signals.py 单信号路径不受影响）。
_indicator_memo = threading.local()


def _get_memo() -> dict | None:
    """当前线程安装的指标 memo；未安装 → None（全部直算）。"""
    return getattr(_indicator_memo, "value", None)


class indicator_memo:
    """指标 memo 作用域：`with engine.indicator_memo(memo): ...` 安装/恢复当前线程的 memo。

    作用域语义：进入时保存本线程旧值、安装新 dict；退出时恢复本线程旧值。
    threading.local 保证并发扫描（调度线程 vs 手动扫描请求线程）各自持有独立 memo，
    不会互相覆盖；键含 id(df) + 强引用 df，保证不同股票的 K 线绝不串 key。
    """

    def __init__(self, memo: dict):
        self._memo = memo
        self._prev: dict | None = None

    def __enter__(self) -> dict:
        self._prev = _get_memo()
        _indicator_memo.value = self._memo
        return self._memo

    def __exit__(self, *exc) -> bool:
        _indicator_memo.value = self._prev
        return False


def _params_key(params: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in (params or {}).items()))


def _call_check(name: str, fn, df: pd.DataFrame, params: dict) -> tuple[bool, str]:
    """检查器调用入口：memo 命中直接复用 (ok, desc)，否则计算并缓存。

    检查器输出是 (name, params, df) 的纯函数，跨信号/跨调用重复执行可安全缓存。
    键含 df 强引用：即使 id 复用也不会串股。
    """
    memo = _get_memo()
    if memo is None:
        return fn(df, params)
    key = (name, id(df), _params_key(params))
    hit = memo.get(key)
    if hit is not None and hit[0] is df:
        return hit[1]
    res = fn(df, params)
    memo[key] = (df, res)
    return res


def _memo_series(name: str, df: pd.DataFrame, fn) -> pd.Series:
    """指标取值函数的 memo 化：同一股票多次取同一指标序列时复用（保留，供取值类调用复用）。"""
    memo = _get_memo()
    if memo is None:
        return fn(df)
    key = ("series", name, id(df))
    hit = memo.get(key)
    if hit is not None and hit[0] is df:
        return hit[1]
    s = fn(df)
    memo[key] = (df, s)
    return s


def _reg(name):
    def deco(fn):
        _BUILTIN_CHECKS[name] = fn
        return fn

    return deco


@_reg("trend_breakout")
def _trend_breakout(df, params) -> tuple[bool, str]:
    win = int(params.get("window", 20))
    min_pct = float(params.get("min_pct", 0.0))
    close = df["close"]
    prev_high = close.shift(1).rolling(win).max()
    broke = close.iloc[-1] > prev_high.iloc[-1] > 0
    pct = (
        (close.iloc[-1] / prev_high.iloc[-1] - 1) * 100 if prev_high.iloc[-1] > 0 else 0
    )
    ok = broke and pct >= min_pct
    return (
        ok,
        f"收盘 {close.iloc[-1]:.2f} 突破{win}日高点 {prev_high.iloc[-1]:.2f} (超出 {pct:.2f}%)",
    )


@_reg("price_up")
def _price_up(df, params) -> tuple[bool, str]:
    min_pct = float(params.get("min_pct", 3.0))
    pct = float(df["pct_change"].iloc[-1]) if "pct_change" in df.columns else 0.0
    return pct >= min_pct, f"当日涨幅 {pct:.2f}% (阈值 {min_pct}%)"


@_reg("volume_surge")
def _volume_surge(df, params) -> tuple[bool, str]:
    ratio = float(params.get("ratio", 2.0))
    vr = volume_ratio(df["volume"], int(params.get("window", 5)))
    cur = vr.iloc[-1]
    return bool(cur >= ratio), f"量比 {cur:.2f} (阈值 {ratio})"


@_reg("rsi_cross_up")
def _rsi_cross_up(df, params) -> tuple[bool, str]:
    thr = float(params.get("threshold", 30.0))
    n = int(params.get("window", 14))
    r = rsi(df["close"], n)
    cross = r.iloc[-2] <= thr < r.iloc[-1] if len(r) >= 2 else False
    return cross, f"RSI({n}) 由 {r.iloc[-2]:.1f} 上穿 {thr}，现 {r.iloc[-1]:.1f}"


@_reg("rsi_above")
def _rsi_above(df, params) -> tuple[bool, str]:
    thr = float(params.get("threshold", 70.0))
    n = int(params.get("window", 14))
    r = rsi(df["close"], n)
    return bool(r.iloc[-1] > thr), f"RSI({n}) = {r.iloc[-1]:.1f} (阈值 {thr})"


@_reg("macd_golden_cross")
def _macd_golden_cross(df, params) -> tuple[bool, str]:
    m = macd(df["close"])
    dif, dea = m["dif"], m["dea"]
    # 标准金叉：前值 DIF ≤ DEA 且 最新 DIF > DEA（链式比较 x<=y<x 会误判）
    cross = (
        len(dif) >= 2
        and bool(dif.iloc[-2] <= dea.iloc[-2])
        and bool(dif.iloc[-1] > dea.iloc[-1])
    )
    return cross, f"DIF {dif.iloc[-1]:.3f} 上穿 DEA {dea.iloc[-1]:.3f}"


@_reg("boll_breakout")
def _boll_breakout(df, params) -> tuple[bool, str]:
    b = boll(df["close"])
    return bool(df["close"].iloc[-1] > b["upper"].iloc[-1]), (
        f"收盘 {df['close'].iloc[-1]:.2f} 突破布林上轨 {b['upper'].iloc[-1]:.2f}"
    )


@_reg("kdj_golden_cross")
def _kdj_golden_cross(df, params) -> tuple[bool, str]:
    k = kdj(df["high"], df["low"], df["close"])
    ks, ds = k["k"], k["d"]
    # 标准金叉：前值 K ≤ D 且 最新 K > D
    cross = (
        len(ks) >= 2
        and bool(ks.iloc[-2] <= ds.iloc[-2])
        and bool(ks.iloc[-1] > ds.iloc[-1])
    )
    return cross, f"K {ks.iloc[-1]:.1f} 上穿 D {ds.iloc[-1]:.1f}"


@_reg("ma_support")
def _ma_support(df, params) -> tuple[bool, str]:
    n = int(params.get("window", 20))
    close = df["close"]
    m = ma(close, n)
    touch = abs(close.iloc[-1] / m.iloc[-1] - 1) <= 0.01 if m.iloc[-1] > 0 else False
    return touch, f"收盘 {close.iloc[-1]:.2f} 贴近 MA{n} {m.iloc[-1]:.2f}"


@_reg("consecutive_up")
def _consecutive_up(df, params) -> tuple[bool, str]:
    days = int(params.get("days", 3))
    min_pct = float(params.get("min_pct", 0.0))
    close = df["close"]
    win = close.iloc[-days:]
    rising = len(win) >= days and bool((win.diff().dropna() > 0).all())
    total = (win.iloc[-1] / win.iloc[0] - 1) * 100 if win.iloc[0] > 0 else 0.0
    ok = rising and total >= min_pct
    return ok, f"连续 {days} 日上涨，累计涨幅 {total:.2f}% (阈值 {min_pct}%)"


@_reg("ma_bullish")
def _ma_bullish(df, params) -> tuple[bool, str]:
    short = int(params.get("short", 5))
    mid = int(params.get("mid", 10))
    long = int(params.get("long", 20))
    close = df["close"]
    s, m, l = ma(close, short), ma(close, mid), ma(close, long)
    vals = (s.iloc[-1], m.iloc[-1], l.iloc[-1])
    ok = vals[0] > vals[1] > vals[2] > 0
    return (
        ok,
        f"MA{short} {vals[0]:.2f} > MA{mid} {vals[1]:.2f} > MA{long} {vals[2]:.2f}",
    )


@_reg("volume_shrink")
def _volume_shrink(df, params) -> tuple[bool, str]:
    max_ratio = float(params.get("max_ratio", 0.6))
    min_drop_pct = float(params.get("min_drop_pct", 1.0))
    vr = volume_ratio(df["volume"], int(params.get("window", 5))).iloc[-1]
    pct = float(df["pct_change"].iloc[-1]) if "pct_change" in df.columns else 0.0
    ok = bool(vr < max_ratio and pct <= -min_drop_pct)
    return ok, f"量比 {vr:.2f} (<{max_ratio})，当日跌幅 {pct:.2f}% (≤-{min_drop_pct}%)"


# ---------- 扩充信号库（25 个新信号） ----------


def _limit_pct(df, params) -> float:
    """涨停/跌停阈值：参数 pct 优先；否则按代码前缀：
    创业板/科创板（300/301/302/688）±19.9%，北交所（4/8）±30%，其余 ±9.9%。
    依赖 df 内 code 列（K 线本身不含，由 scanner 注入）；缺省 9.9。
    """
    pct = float(params.get("pct", 0.0))
    if pct > 0:
        return pct
    if "code" in df.columns:
        bare = bare_code(str(df["code"].iloc[-1]))
        if bare.startswith(("300", "301", "302", "688")):
            return 19.9
        if bare.startswith(("4", "8")):
            return 30.0
    return 9.9


def _last_pct(df) -> float:
    return float(df["pct_change"].iloc[-1]) if "pct_change" in df.columns else 0.0


@_reg("ma_golden_cross")
def _ma_golden_cross(df, params) -> tuple[bool, str]:
    short = int(params.get("short", 5))
    long = int(params.get("long", 10))
    close = df["close"]
    s, l = ma(close, short), ma(close, long)
    # 标准金叉：前值短均线 ≤ 长均线 且 最新短均线 > 长均线
    cross = (
        len(s) >= 2 and bool(s.iloc[-2] <= l.iloc[-2]) and bool(s.iloc[-1] > l.iloc[-1])
    )
    return cross, f"MA{short} {s.iloc[-1]:.2f} 上穿 MA{long} {l.iloc[-1]:.2f}"


@_reg("macd_dead_cross")
def _macd_dead_cross(df, params) -> tuple[bool, str]:
    m = macd(df["close"])
    dif, dea = m["dif"], m["dea"]
    # 标准死叉：前值 DIF ≥ DEA 且 最新 DIF < DEA
    cross = (
        len(dif) >= 2
        and bool(dif.iloc[-2] >= dea.iloc[-2])
        and bool(dif.iloc[-1] < dea.iloc[-1])
    )
    return cross, f"DIF {dif.iloc[-1]:.3f} 下穿 DEA {dea.iloc[-1]:.3f}（死叉）"


@_reg("kdj_oversold")
def _kdj_oversold(df, params) -> tuple[bool, str]:
    thr = float(params.get("threshold", 20.0))
    k = kdj(df["high"], df["low"], df["close"])["k"]
    ok = bool(k.iloc[-1] < thr)
    return ok, f"K值 {k.iloc[-1]:.1f} < {thr}（超卖）"


@_reg("kdj_overbought")
def _kdj_overbought(df, params) -> tuple[bool, str]:
    thr = float(params.get("threshold", 80.0))
    k = kdj(df["high"], df["low"], df["close"])["k"]
    ok = bool(k.iloc[-1] > thr)
    return ok, f"K值 {k.iloc[-1]:.1f} > {thr}（超买）"


@_reg("rsi_oversold")
def _rsi_oversold(df, params) -> tuple[bool, str]:
    thr = float(params.get("threshold", 30.0))
    n = int(params.get("window", 14))
    r = rsi(df["close"], n)
    ok = bool(r.iloc[-1] < thr)
    return ok, f"RSI({n}) = {r.iloc[-1]:.1f} < {thr}（超卖）"


@_reg("breakout_new_high")
def _breakout_new_high(df, params) -> tuple[bool, str]:
    window = int(params.get("window", 60))
    close = df["close"]
    prev = close.shift(1).rolling(window).max()
    ok = prev.iloc[-1] > 0 and close.iloc[-1] > prev.iloc[-1]
    return ok, f"收盘 {close.iloc[-1]:.2f} 创{window}日新高（前高 {prev.iloc[-1]:.2f}）"


@_reg("break_new_low")
def _break_new_low(df, params) -> tuple[bool, str]:
    window = int(params.get("window", 60))
    close = df["close"]
    prev = close.shift(1).rolling(window).min()
    ok = prev.iloc[-1] > 0 and close.iloc[-1] < prev.iloc[-1]
    return ok, f"收盘 {close.iloc[-1]:.2f} 创{window}日新低（前低 {prev.iloc[-1]:.2f}）"


@_reg("limit_up")
def _limit_up(df, params) -> tuple[bool, str]:
    thr = _limit_pct(df, params)
    cur = _last_pct(df)
    return cur >= thr, f"当日涨幅 {cur:.2f}% ≥ {thr}%（涨停）"


@_reg("limit_down")
def _limit_down(df, params) -> tuple[bool, str]:
    thr = _limit_pct(df, params)
    cur = _last_pct(df)
    return cur <= -thr, f"当日跌幅 {cur:.2f}% ≤ -{thr}%（跌停）"


@_reg("big_bullish")
def _big_bullish(df, params) -> tuple[bool, str]:
    min_pct = float(params.get("min_pct", 5.0))
    cur = _last_pct(df)
    positive = float(df["close"].iloc[-1]) > float(df["open"].iloc[-1])
    return cur >= min_pct and positive, f"涨幅 {cur:.2f}% ≥ {min_pct}% 且收阳（大阳线）"


@_reg("big_bearish")
def _big_bearish(df, params) -> tuple[bool, str]:
    min_pct = float(params.get("min_pct", 5.0))
    cur = _last_pct(df)
    return cur <= -min_pct, f"当日跌幅 {cur:.2f}% ≤ -{min_pct}%（大阴线）"


@_reg("long_upper_shadow")
def _long_upper_shadow(df, params) -> tuple[bool, str]:
    high, low, close, open_ = (
        float(df[c].iloc[-1]) for c in ("high", "low", "close", "open")
    )
    body = abs(close - open_)
    upper = high - max(close, open_)
    lower = min(close, open_) - low
    ok = body > 0 and upper > 2 * body and upper > lower
    return ok, f"上影 {upper:.2f} > 2×实体 {body:.2f} 且 > 下影 {lower:.2f}"


@_reg("hammer")
def _hammer(df, params) -> tuple[bool, str]:
    high, low, close, open_ = (
        float(df[c].iloc[-1]) for c in ("high", "low", "close", "open")
    )
    body = abs(close - open_)
    lower = min(close, open_) - low
    upper = high - max(close, open_)
    mid = (high + low) / 2
    ok = body > 0 and lower > 2 * body and close >= mid
    return ok, f"下影 {lower:.2f} > 2×实体 {body:.2f} 且收盘 {close:.2f} 在区间上半部"


@_reg("volume_price_surge")
def _volume_price_surge(df, params) -> tuple[bool, str]:
    ratio = float(params.get("ratio", 2.0))
    min_pct = float(params.get("min_pct", 3.0))
    vr = volume_ratio(df["volume"], int(params.get("window", 5))).iloc[-1]
    cur = _last_pct(df)
    return bool(
        vr >= ratio
    ) and cur >= min_pct, f"量比 {vr:.2f} ≥ {ratio} 且涨幅 {cur:.2f}% ≥ {min_pct}%"


@_reg("ma_squeeze")
def _ma_squeeze(df, params) -> tuple[bool, str]:
    max_pct = float(params.get("max_pct", 1.0))
    close = df["close"]
    vals = [ma(close, n).iloc[-1] for n in (5, 10, 20)]
    if not all(v > 0 for v in vals):
        return False, "MA5/10/20 数据不足"
    spread = (max(vals) - min(vals)) / close.iloc[-1] * 100
    return spread < max_pct, f"MA5/10/20 极差 {spread:.2f}% < {max_pct}%（粘合）"


@_reg("above_ma20")
def _above_ma20(df, params) -> tuple[bool, str]:
    n = int(params.get("window", 20))
    close = df["close"]
    m = ma(close, n)
    return bool(
        close.iloc[-1] > m.iloc[-1]
    ), f"收盘 {close.iloc[-1]:.2f} > MA{n} {m.iloc[-1]:.2f}"


@_reg("below_ma20")
def _below_ma20(df, params) -> tuple[bool, str]:
    n = int(params.get("window", 20))
    close = df["close"]
    m = ma(close, n)
    return bool(
        close.iloc[-1] < m.iloc[-1]
    ), f"收盘 {close.iloc[-1]:.2f} < MA{n} {m.iloc[-1]:.2f}"


@_reg("turnover_spike")
def _turnover_spike(df, params) -> tuple[bool, str]:
    mult = float(params.get("mult", 2.0))
    if "turnover_rate" not in df.columns:
        return False, "无换手数据（缺 turnover_rate 列）"
    tr = pd.to_numeric(df["turnover_rate"], errors="coerce")
    if len(tr) < 6:
        return False, "换手数据不足"
    cur = float(tr.iloc[-1])
    avg = float(tr.iloc[-6:-1].mean())
    ok = cur > mult * avg
    return ok, f"换手 {cur:.2f}% > {mult}× 前5日均值 {avg:.2f}%"


@_reg("volume_divergence")
def _volume_divergence(df, params) -> tuple[bool, str]:
    window = int(params.get("window", 20))
    vol_n = int(params.get("vol_window", 5))
    close, volume = df["close"], df["volume"]
    prev = close.shift(1).rolling(window).max()
    new_high = prev.iloc[-1] > 0 and close.iloc[-1] > prev.iloc[-1]
    avg_vol = float(volume.iloc[-vol_n - 1 : -1].mean())
    ok = new_high and volume.iloc[-1] < avg_vol
    return (
        ok,
        f"创{window}日新高但量 {volume.iloc[-1]:.0f} < 前{vol_n}日均量 {avg_vol:.0f}",
    )


@_reg("consecutive_limit_up")
def _consecutive_limit_up(df, params) -> tuple[bool, str]:
    days = int(params.get("days", 2))
    thr = _limit_pct(df, params)
    if "pct_change" not in df.columns or len(df) < days:
        return False, "数据不足（缺 pct_change）"
    win = df["pct_change"].iloc[-days:]
    ok = bool((win >= thr).all())
    return ok, f"近 {days} 日涨幅 {win.round(2).tolist()} 均 ≥ {thr}%（连续涨停）"


@_reg("doji")
def _doji(df, params) -> tuple[bool, str]:
    max_body_pct = float(params.get("max_body_pct", 0.5))
    high, low, close, open_ = (
        float(df[c].iloc[-1]) for c in ("high", "low", "close", "open")
    )
    if open_ == 0:
        return False, "开盘价为 0"
    body = abs(close - open_) / open_ * 100
    has_shadow = high > max(open_, close) and low < min(open_, close)
    return body < max_body_pct and has_shadow, (
        f"|收盘-开盘|/开盘 = {body:.2f}% < {max_body_pct}% 且带上下影"
    )


@_reg("macd_hist_up")
def _macd_hist_up(df, params) -> tuple[bool, str]:
    days = int(params.get("days", 2))
    hist = macd(df["close"])["hist"]
    win = hist.iloc[-(days + 1) :]
    if len(win) != days + 1:
        return False, "数据不足"
    ok = bool(
        (win.iloc[-days:] > 0).all() and win.diff().dropna().iloc[-days:].gt(0).all()
    )
    vals = win.iloc[-days:].round(3).tolist()
    return ok, f"MACD 红柱连续 {days} 日放大：{vals}"


@_reg("volume_breakout_high")
def _volume_breakout_high(df, params) -> tuple[bool, str]:
    ratio = float(params.get("ratio", 2.0))
    window = int(params.get("window", 20))
    close, volume = df["close"], df["volume"]
    prev = close.shift(1).rolling(window).max()
    vr = volume_ratio(volume, int(params.get("vol_window", 5))).iloc[-1]
    ok = bool(vr >= ratio) and prev.iloc[-1] > 0 and close.iloc[-1] > prev.iloc[-1]
    return ok, f"量比 {vr:.2f} ≥ {ratio} 且收盘突破{window}日高点 {prev.iloc[-1]:.2f}"


@_reg("price_gap_up")
def _price_gap_up(df, params) -> tuple[bool, str]:
    min_pct = float(params.get("min_pct", 1.0))
    close = df["close"]
    if len(df) < 2:
        return False, "数据不足"
    prev_close = float(close.shift(1).iloc[-1])
    if prev_close <= 0:
        return False, "昨收为 0"
    gap = (float(df["open"].iloc[-1]) / prev_close - 1) * 100
    return gap >= min_pct, f"跳空高开 {gap:.2f}% ≥ {min_pct}%"


@_reg("gap_down")
def _gap_down(df, params) -> tuple[bool, str]:
    min_pct = float(params.get("min_pct", 1.0))
    close = df["close"]
    if len(df) < 2:
        return False, "数据不足"
    prev_close = float(close.shift(1).iloc[-1])
    if prev_close <= 0:
        return False, "昨收为 0"
    gap = (float(df["open"].iloc[-1]) / prev_close - 1) * 100
    return gap <= -min_pct, f"跳空低开 {gap:.2f}% ≤ -{min_pct}%"


# ---------- 指标信号扩充（趋势/动量/超买超卖/量能波动/K线形态） ----------


@_reg("price_cross_up_ma")
def _price_cross_up_ma(df, params) -> tuple[bool, str]:
    """收盘上穿均线（昨在下、今在上）。"""
    n = int(params.get("window", 20))
    close = df["close"]
    m = ma(close, n)
    # 标准上穿：昨收 ≤ 昨均线 且 今收 > 今均线
    cross = (
        (close.iloc[-2] <= m.iloc[-2] and close.iloc[-1] > m.iloc[-1])
        if len(close) >= 2
        else False
    )
    return bool(cross), f"收盘 {close.iloc[-1]:.2f} 上穿 MA{n} {m.iloc[-1]:.2f}"


@_reg("price_cross_down_ma")
def _price_cross_down_ma(df, params) -> tuple[bool, str]:
    """收盘跌破均线（昨在上、今在下）。"""
    n = int(params.get("window", 20))
    close = df["close"]
    m = ma(close, n)
    # 标准下穿：昨收 ≥ 昨均线 且 今收 < 今均线
    cross = (
        (close.iloc[-2] >= m.iloc[-2] and close.iloc[-1] < m.iloc[-1])
        if len(close) >= 2
        else False
    )
    return bool(cross), f"收盘 {close.iloc[-1]:.2f} 跌破 MA{n} {m.iloc[-1]:.2f}"


@_reg("ma20_cross_ma60")
def _ma20_cross_ma60(df, params) -> tuple[bool, str]:
    """MA20 上穿 MA60（中期趋势转多）。"""
    short = int(params.get("short", 20))
    long = int(params.get("long", 60))
    s, l = ma(df["close"], short), ma(df["close"], long)
    # 标准上穿：MA{short}昨 ≤ MA{long}昨 且 MA{short}今 > MA{long}今
    cross = (
        (s.iloc[-2] <= l.iloc[-2] and s.iloc[-1] > l.iloc[-1]) if len(s) >= 2 else False
    )
    return bool(cross), f"MA{short} {s.iloc[-1]:.2f} 上穿 MA{long} {l.iloc[-1]:.2f}"


@_reg("ema_golden_cross")
def _ema_golden_cross(df, params) -> tuple[bool, str]:
    """EMA 金叉（EMA12 上穿 EMA26）。"""
    short = int(params.get("short", 12))
    long = int(params.get("long", 26))
    s, l = ema(df["close"], short), ema(df["close"], long)
    # 标准上穿：EMA{short}昨 ≤ EMA{long}昨 且 EMA{short}今 > EMA{long}今
    cross = (
        (s.iloc[-2] <= l.iloc[-2] and s.iloc[-1] > l.iloc[-1]) if len(s) >= 2 else False
    )
    return bool(cross), f"EMA{short} {s.iloc[-1]:.2f} 上穿 EMA{long} {l.iloc[-1]:.2f}"


@_reg("macd_hist_shrink")
def _macd_hist_shrink(df, params) -> tuple[bool, str]:
    """MACD 红柱连续缩小（多头动能衰减）。"""
    days = int(params.get("days", 2))
    h = macd(df["close"])["hist"]
    seg = h.iloc[-(days + 1) :]
    if len(seg) < days + 1 or seg.iloc[-1] <= 0:
        return False, "红柱不足"
    shrinking = all(seg.iloc[i] > seg.iloc[i + 1] for i in range(days))
    return shrinking, f"MACD 红柱连续 {days} 日缩小至 {seg.iloc[-1]:.3f}"


@_reg("kdj_dead_cross")
def _kdj_dead_cross(df, params) -> tuple[bool, str]:
    """KDJ 死叉（K 下穿 D）。一次调用取 k/d 两列（修复前 kdj() 全序列算两遍）。"""
    k = kdj(df["high"], df["low"], df["close"])
    ks, ds = k["k"], k["d"]
    # 标准死叉：前值 K ≥ D 且 最新 K < D（链式 a>=b>c 会把第二项错配为 昨日D > 今日K）
    cross = (
        len(ks) >= 2
        and bool(ks.iloc[-2] >= ds.iloc[-2])
        and bool(ks.iloc[-1] < ds.iloc[-1])
    )
    return cross, f"K {ks.iloc[-1]:.1f} 下穿 D {ds.iloc[-1]:.1f}"


@_reg("rsi_cross_down")
def _rsi_cross_down(df, params) -> tuple[bool, str]:
    """RSI 下穿阈值（超买回落）。"""
    thr = float(params.get("threshold", 70.0))
    n = int(params.get("window", 14))
    r = rsi(df["close"], n)
    cross = r.iloc[-2] >= thr > r.iloc[-1] if len(r) >= 2 else False
    return cross, f"RSI({n}) 由 {r.iloc[-2]:.1f} 下穿 {thr}，现 {r.iloc[-1]:.1f}"


@_reg("mtm_cross_up")
def _mtm_cross_up(df, params) -> tuple[bool, str]:
    """MTM 上穿 0（动量转正）。"""
    n = int(params.get("window", 12))
    mt = mtm(df["close"], n)["mtm"]
    cross = mt.iloc[-2] <= 0 < mt.iloc[-1] if len(mt) >= 2 else False
    return cross, f"MTM({n}) 由 {mt.iloc[-2]:.2f} 上穿 0，现 {mt.iloc[-1]:.2f}"


@_reg("roc_cross_up")
def _roc_cross_up(df, params) -> tuple[bool, str]:
    """ROC 上穿 0（变动率转正）。"""
    n = int(params.get("window", 12))
    r = roc(df["close"], n)
    cross = r.iloc[-2] <= 0 < r.iloc[-1] if len(r) >= 2 else False
    return cross, f"ROC({n}) 由 {r.iloc[-2]:.2f} 上穿 0，现 {r.iloc[-1]:.2f}"


@_reg("cci_cross_up")
def _cci_cross_up(df, params) -> tuple[bool, str]:
    """CCI 上穿 +100（强势启动）。"""
    n = int(params.get("window", 14))
    c = cci(df["high"], df["low"], df["close"], n)
    cross = c.iloc[-2] <= 100 < c.iloc[-1] if len(c) >= 2 else False
    return cross, f"CCI({n}) 由 {c.iloc[-2]:.1f} 上穿 100，现 {c.iloc[-1]:.1f}"


@_reg("cci_cross_down")
def _cci_cross_down(df, params) -> tuple[bool, str]:
    """CCI 下穿 -100（弱势破位）。"""
    n = int(params.get("window", 14))
    c = cci(df["high"], df["low"], df["close"], n)
    cross = c.iloc[-2] >= -100 > c.iloc[-1] if len(c) >= 2 else False
    return cross, f"CCI({n}) 由 {c.iloc[-2]:.1f} 下穿 -100，现 {c.iloc[-1]:.1f}"


@_reg("trix_golden_cross")
def _trix_golden_cross(df, params) -> tuple[bool, str]:
    """TRIX 上穿 MATRIX（长线动能转多）。"""
    tr = trix(df["close"])
    # 标准金叉：前值 TRIX ≤ MATRIX 且 最新 TRIX > MATRIX
    # （链式 x<=y<z 会把第二项错配为 昨日MATRIX < 今日TRIX，缺今日 MATRIX 参与判定）
    cross = (
        len(tr["trix"]) >= 2
        and bool(tr["trix"].iloc[-2] <= tr["matrix"].iloc[-2])
        and bool(tr["trix"].iloc[-1] > tr["matrix"].iloc[-1])
    )
    return (
        cross,
        f"TRIX {tr['trix'].iloc[-1]:.3f} 上穿 MATRIX {tr['matrix'].iloc[-1]:.3f}",
    )


@_reg("dmi_bullish")
def _dmi_bullish(df, params) -> tuple[bool, str]:
    """DMI 多头（PDI>MDI 且 ADX 走强）。"""
    d = dmi(df["high"], df["low"], df["close"])
    ok = (
        bool(
            d["pdi"].iloc[-1] > d["mdi"].iloc[-1]
            and d["adx"].iloc[-1] > d["adx"].iloc[-2]
        )
        if len(d["adx"]) >= 2
        else False
    )
    return (
        ok,
        f"PDI {d['pdi'].iloc[-1]:.1f} > MDI {d['mdi'].iloc[-1]:.1f}，ADX {d['adx'].iloc[-1]:.1f} 走强",
    )


@_reg("wr_oversold")
def _wr_oversold(df, params) -> tuple[bool, str]:
    """WR 超卖（WR10 > 80）。"""
    n = int(params.get("window", 10))
    w = wr(df["high"], df["low"], df["close"], n)
    ok = bool(w.iloc[-1] > 80)
    return ok, f"WR{n} = {w.iloc[-1]:.1f} (>80 超卖)"


@_reg("wr_overbought")
def _wr_overbought(df, params) -> tuple[bool, str]:
    """WR 超买（WR10 < 20）。"""
    n = int(params.get("window", 10))
    w = wr(df["high"], df["low"], df["close"], n)
    ok = bool(w.iloc[-1] < 20)
    return ok, f"WR{n} = {w.iloc[-1]:.1f} (<20 超买)"


@_reg("cci_oversold")
def _cci_oversold(df, params) -> tuple[bool, str]:
    """CCI 超卖（< -100）。"""
    n = int(params.get("window", 14))
    c = cci(df["high"], df["low"], df["close"], n)
    ok = bool(c.iloc[-1] < -100)
    return ok, f"CCI({n}) = {c.iloc[-1]:.1f} (<-100 超卖)"


@_reg("cci_overbought")
def _cci_overbought(df, params) -> tuple[bool, str]:
    """CCI 超买（> +100）。"""
    n = int(params.get("window", 14))
    c = cci(df["high"], df["low"], df["close"], n)
    ok = bool(c.iloc[-1] > 100)
    return ok, f"CCI({n}) = {c.iloc[-1]:.1f} (>100 超买)"


@_reg("bias_overbought")
def _bias_overbought(df, params) -> tuple[bool, str]:
    """BIAS 超买（乖离过大）。"""
    n = int(params.get("window", 24))
    b = bias(df["close"], n).iloc[-1]
    ok = bool(b > 20)
    return ok, f"BIAS{n} = {b:.1f}% (>20 超买)"


@_reg("bias_oversold")
def _bias_oversold(df, params) -> tuple[bool, str]:
    """BIAS 超卖（负乖离过大）。"""
    n = int(params.get("window", 24))
    b = bias(df["close"], n).iloc[-1]
    ok = bool(b < -20)
    return ok, f"BIAS{n} = {b:.1f}% (<-20 超卖)"


@_reg("psy_above")
def _psy_above(df, params) -> tuple[bool, str]:
    """PSY 心理线超买（>75）。"""
    n = int(params.get("window", 12))
    p = psy(df["close"], n).iloc[-1]
    ok = bool(p > 75)
    return ok, f"PSY({n}) = {p:.0f} (>75 超买)"


@_reg("psy_below")
def _psy_below(df, params) -> tuple[bool, str]:
    """PSY 心理线超卖（<25）。"""
    n = int(params.get("window", 12))
    p = psy(df["close"], n).iloc[-1]
    ok = bool(p < 25)
    return ok, f"PSY({n}) = {p:.0f} (<25 超卖)"


@_reg("obv_new_high")
def _obv_new_high(df, params) -> tuple[bool, str]:
    """OBV 创 N 日新高（量能趋势突破）。"""
    win = int(params.get("window", 20))
    o = obv(df["close"], df["volume"])
    if len(o) < win + 1:
        return False, "数据不足"
    prev_max = o.iloc[-(win + 1) : -1].max()
    ok = bool(o.iloc[-1] > prev_max)
    return ok, f"OBV {o.iloc[-1]:.0f} 创 {win} 日新高"


@_reg("vr_high")
def _vr_high(df, params) -> tuple[bool, str]:
    """VR 放量区（>160）。"""
    n = int(params.get("window", 26))
    v = vr(df["close"], df["volume"], n).iloc[-1]
    ok = bool(v > 160)
    return ok, f"VR({n}) = {v:.0f} (>160 放量区)"


@_reg("vr_low")
def _vr_low(df, params) -> tuple[bool, str]:
    """VR 地量区（<40）。"""
    n = int(params.get("window", 26))
    v = vr(df["close"], df["volume"], n).iloc[-1]
    ok = bool(v < 40)
    return ok, f"VR({n}) = {v:.0f} (<40 地量区)"


@_reg("atr_expansion")
def _atr_expansion(df, params) -> tuple[bool, str]:
    """ATR 突然放大（波动加剧）。"""
    ratio = float(params.get("ratio", 1.3))
    a = atr(df["high"], df["low"], df["close"])["atr"]
    if len(a) < 3:
        return False, "数据不足"
    ok = bool(a.iloc[-1] > a.iloc[-2] * ratio and a.iloc[-2] > 0)
    return ok, f"ATR {a.iloc[-1]:.2f} 较前日放大 {ratio} 倍"


@_reg("boll_squeeze")
def _boll_squeeze(df, params) -> tuple[bool, str]:
    """布林收口（带宽处 20 日低位，变盘前兆）。"""
    n = int(params.get("window", 20))
    b = boll(df["close"], n)
    band = (b["upper"] - b["lower"]) / b["mid"].replace(0, pd.NA)
    band = band.fillna(0)
    if len(band) < n + 1:
        return False, "数据不足"
    cur = float(band.iloc[-1])
    hist = band.iloc[-(n + 1) : -1].to_numpy(dtype=float)
    pct = float((hist < cur).mean())
    ok = bool(pct <= 0.2)
    return ok, f"布林带宽 {cur:.3f} 处于 {n} 日 {pct * 100:.0f}% 分位（收口）"


@_reg("bullish_engulfing")
def _bullish_engulfing(df, params) -> tuple[bool, str]:
    """看涨吞没（阳线完全吞没前日阴线）。"""
    if len(df) < 2:
        return False, "数据不足"
    po, pc = float(df["open"].iloc[-2]), float(df["close"].iloc[-2])
    o, c = float(df["open"].iloc[-1]), float(df["close"].iloc[-1])
    ok = bool(pc < po and c > o and o <= pc and c >= po)
    return ok, f"阳线 {o:.2f}→{c:.2f} 吞没前阴线 {po:.2f}→{pc:.2f}"


@_reg("bearish_engulfing")
def _bearish_engulfing(df, params) -> tuple[bool, str]:
    """看跌吞没（阴线完全吞没前日阳线）。"""
    if len(df) < 2:
        return False, "数据不足"
    po, pc = float(df["open"].iloc[-2]), float(df["close"].iloc[-2])
    o, c = float(df["open"].iloc[-1]), float(df["close"].iloc[-1])
    ok = bool(pc > po and c < o and o >= pc and c <= po)
    return ok, f"阴线 {o:.2f}→{c:.2f} 吞没前阳线 {po:.2f}→{pc:.2f}"


@_reg("shooting_star")
def _shooting_star(df, params) -> tuple[bool, str]:
    """射击之星（长上影小实体，顶部警示）。"""
    o, h, l, c = (float(df[x].iloc[-1]) for x in ("open", "high", "low", "close"))
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    ok = bool(body > 0 and upper > body * 2 and upper > lower * 2)
    return (
        ok,
        f"上影 {upper:.2f} 为实体 {body:.2f} 的 {upper / body:.1f} 倍（射击之星）",
    )


SIGNAL_TYPES = tuple(_BUILTIN_CHECKS)


def evaluate_all_signals(df: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    """单股全信号判定：遍历 SIGNAL_TYPES 全部内置信号，逐个调用检查器。

    参数取 catalog.SIGNAL_CATALOG 的默认参数（单一事实源，与 /catalog 接口一致）；
    返回 (命中信号名列表, {信号名: 证据描述})。任一信号计算异常仅跳过该信号并记
    日志（不向调用方抛错，防止单个信号打挂整轮扫描）。调用方负责安装 indicator_memo
    （见 scanner._check_one），本函数不做 memo 安装/清理。
    """
    hits: list[str] = []
    evidence: dict[str, str] = {}
    for name in SIGNAL_TYPES:
        fn = _BUILTIN_CHECKS[name]
        meta = SIGNAL_CATALOG.get(name)
        params = meta[3] if meta else {}
        try:
            ok, desc = _call_check(name, fn, df, params or {})
        except Exception as e:
            ok, desc = False, f"{name}: 计算异常"
            logger.warning("信号 %s 计算异常: %s", name, e)
        evidence[name] = desc
        if ok:
            hits.append(name)
    return hits, evidence
