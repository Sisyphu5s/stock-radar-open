"""在线指标计算：numpy 向量化，输入为 K 线 DataFrame，输出指标 dict。"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("stockradar.indicators.compute")


# 具名参数 → 计算函数位置参数顺序（v2 自定义对象 / 调优规格共用；键须与 tune.SPECS 一致）
CUSTOM_PARAM_ORDER: dict[str, tuple[str, ...]] = {
    "ma": ("window",),
    "ema": ("window",),
    "rsi": ("window",),
    "kdj": ("window",),
    "boll": ("window", "multiplier"),
    "macd": ("fast", "slow", "signal"),
    "wr": ("window",),
    "cci": ("window",),
    "roc": ("window",),
    "mtm": ("window",),
    "bias": ("window",),
    "psy": ("window",),
    "trix": ("window",),
    "cmo": ("window",),
    "volume_ratio": ("window",),
}


def normalize_custom(indicator: str, val) -> dict | None:
    """自定义参数归一为具名 dict：接受 v2 对象（{"fast":12,...}）与旧数字/数组。

    非法（缺键、非数值、非正数、长度不匹配）返回 None。
    """
    keys = CUSTOM_PARAM_ORDER.get(indicator)
    if keys is None:
        return None
    if isinstance(val, dict) and not isinstance(val, bool):
        if len(val) != len(keys) or any(k not in val for k in keys):
            return None
        raw = [val[k] for k in keys]
    elif isinstance(val, (list, tuple)):
        if len(val) != len(keys):
            return None
        raw = list(val)
    elif isinstance(val, (int, float)) and not isinstance(val, bool):
        if len(keys) != 1:
            return None
        raw = [val]
    else:
        return None
    out: dict = {}
    for k, v in zip(keys, raw):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(f) or f <= 0:
            return None
        out[k] = f
    if len(out) != len(keys):
        return None
    return out


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def ma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    return _ema(close, n)


def boll(close: pd.Series, n: int = 20, k: float = 2.0) -> dict:
    mid = ma(close, n)
    std = close.rolling(n).std()
    return {"upper": mid + k * std, "lower": mid - k * std, "mid": mid}


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    hist = (dif - dea) * 2
    return {"dif": dif, "dea": dea, "hist": hist}


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    gain = diff.clip(lower=0)
    loss = -diff.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # avg_loss==0（连续上涨无回撤）→ RSI=100，保证 rsi_above(>70) 在连涨时能触发
    out = out.where(avg_loss.ne(0), 100.0)
    return out.fillna(50.0)  # 首段 NaN（数据不足）回填 50


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9) -> dict:
    llv = low.rolling(n).min()
    hhv = high.rolling(n).max()
    rsv = (close - llv) / (hhv - llv).replace(0, np.nan) * 100
    rsv = rsv.fillna(50.0)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    j = 3 * k - 2 * d
    return {"k": k, "d": d, "j": j}


def volume_ratio(volume: pd.Series, n: int = 5) -> pd.Series:
    """量比：当前成交量 / 前 n 日均量。"""
    return volume / volume.shift(1).rolling(n).mean().replace(0, np.nan)


def compute_all(
    df: pd.DataFrame, fields: list[str] | None = None, custom: dict | None = None
) -> dict:
    """一次性计算请求的指标集（含东财全量传统指标）。df 需含 open/high/low/close/volume，按日期升序。

    支持字段：
      主图: ma5/10/20/30/60/120/250, ema12/26, expma12/50, boll(upper/mid/lower), sar
      副图: macd(dif/dea/hist), kdj(k/d/j), rsi(6/12/24/14), wr(10/6), dmi(pdi/mdi/adx/adxr),
            cci, roc, mtm/mtmma, obv, bias6/12/24, psy, trix/matrix, vr, atr/tr, cmo, dpo,
            emv/emvma, volume_ratio
    custom: 可选自定义参数 dict，键为指标名，输出覆盖到固定字段名。值接受两种格式：
      旧格式：单个数字参数（如 {"rsi": 9}）；boll/macd 用位置数组
             （{"boll": [20, 2.5]}、{"macd": [8, 17, 9]}）；ma 用单个数字或数字列表。
      v2 具名对象：{"macd": {"fast": 8, "slow": 17, "signal": 9}}、
             {"boll": {"window": 20, "multiplier": 2.5}}、
             {"rsi": {"window": 9}} 等（键顺序见 CUSTOM_PARAM_ORDER）。
      ema 自定义输出 ema{n} 并同步覆盖主图 ema12/ema26；
      rsi/wr/bias 自定义同步输出多线（rsi6/12/24、wr10/6、bias6/12/24）使用同一自定义窗口；
      mtm/trix 自定义同步输出信号线 mtmma/matrix（信号线一致性）。
    其他键静默忽略；非法参数（非正数、长度不匹配）静默忽略该键。
    返回 dict: {ma5: [...], macd_dif: [...], ...}，长度与 df 对齐。
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    out: dict = {}
    wanted = set(
        fields or ["ma5", "ma10", "ma20", "boll", "macd", "rsi", "kdj", "volume_ratio"]
    )

    # 均线全周期
    for n in (5, 10, 20, 30, 60, 120, 250):
        if f"ma{n}" in wanted:
            out[f"ma{n}"] = ma(close, n).tolist()
    # EMA / EXPMA
    if "ema12" in wanted:
        out["ema12"] = _ema(close, 12).tolist()
    if "ema26" in wanted:
        out["ema26"] = _ema(close, 26).tolist()
    if "expma12" in wanted or "expma50" in wanted:
        e = expma(close)
        if "expma12" in wanted:
            out["expma12"] = e["expma_12"].tolist()
        if "expma50" in wanted:
            out["expma50"] = e["expma_50"].tolist()
    if "boll" in wanted:
        b = boll(close)
        out["boll_upper"], out["boll_mid"], out["boll_lower"] = (
            b["upper"].tolist(),
            b["mid"].tolist(),
            b["lower"].tolist(),
        )
    if "sar" in wanted:
        out["sar"] = sar(high, low).tolist()
    if "macd" in wanted:
        m = macd(close)
        out["macd_dif"], out["macd_dea"], out["macd_hist"] = (
            m["dif"].tolist(),
            m["dea"].tolist(),
            m["hist"].tolist(),
        )
    if "kdj" in wanted:
        k = kdj(high, low, close)
        out["kdj_k"], out["kdj_d"], out["kdj_j"] = (
            k["k"].tolist(),
            k["d"].tolist(),
            k["j"].tolist(),
        )
    # RSI 多周期（东财 6/12/24）
    rsi_on = "rsi" in wanted or any(f"rsi{n}" in wanted for n in (6, 12, 24, 14))
    for n in (6, 12, 24):
        if rsi_on:
            out[f"rsi{n}"] = rsi(close, n).tolist()
    if "rsi" in wanted:
        out["rsi"] = rsi(close, 14).tolist()
    # 威廉 WR（10/6）组名或字段名
    wr_on = "wr" in wanted or any(f"wr{n}" in wanted for n in (10, 6))
    for n in (10, 6):
        if wr_on:
            out[f"wr{n}"] = wr(high, low, close, n).tolist()
    if "wr" in wanted and wr_on:
        # 组名键与 custom 分支口径一致（wr = 默认窗口 10；旧实现仅 custom 分支输出）
        out["wr"] = wr(high, low, close, 10).tolist()
    if "dmi" in wanted:
        d = dmi(high, low, close)
        out["dmi_pdi"], out["dmi_mdi"], out["dmi_adx"], out["dmi_adxr"] = (
            d["pdi"].tolist(),
            d["mdi"].tolist(),
            d["adx"].tolist(),
            d["adxr"].tolist(),
        )
    if "cci" in wanted:
        out["cci"] = cci(high, low, close).tolist()
    if "roc" in wanted:
        out["roc"] = roc(close).tolist()
    if "mtm" in wanted or "mtmma" in wanted:
        m = mtm(close)
        if "mtm" in wanted:
            out["mtm"] = m["mtm"].tolist()
            out["mtmma"] = m[
                "mtmma"
            ].tolist()  # 默认分支补齐信号线（副图成对字段；旧实现仅 custom 分支输出）
        if "mtmma" in wanted:
            out["mtmma"] = m["mtmma"].tolist()
    if "obv" in wanted:
        out["obv"] = obv(close, volume).tolist()
    bias_on = "bias" in wanted or any(f"bias{n}" in wanted for n in (6, 12, 24))
    for n in (6, 12, 24):
        if bias_on:
            out[f"bias{n}"] = bias(close, n).tolist()
    if "bias" in wanted and bias_on:
        # 组名键与 custom 分支口径一致（bias = 默认窗口 6；旧实现仅 custom 分支输出）
        out["bias"] = bias(close, 6).tolist()
    if "psy" in wanted:
        out["psy"] = psy(close).tolist()
    if "trix" in wanted or "matrix" in wanted:
        t = trix(close)
        if "trix" in wanted:
            out["trix"] = t["trix"].tolist()
            out["matrix"] = t[
                "matrix"
            ].tolist()  # 默认分支补齐信号线（副图成对字段；旧实现仅 custom 分支输出）
        if "matrix" in wanted:
            out["matrix"] = t["matrix"].tolist()
    if "vr" in wanted:
        out["vr"] = vr(close, volume).tolist()
    if "atr" in wanted or "tr" in wanted:
        a = atr(high, low, close)
        if "atr" in wanted:
            out["atr"] = a["atr"].tolist()
            out["tr"] = a[
                "tr"
            ].tolist()  # 默认分支补齐真实波幅（副图成对字段；旧实现仅 custom 分支输出）
        if "tr" in wanted:
            out["tr"] = a["tr"].tolist()
    if "cmo" in wanted:
        out["cmo"] = cmo(close).tolist()
    if "dpo" in wanted:
        out["dpo"] = dpo(close).tolist()
    if "emv" in wanted or "emvma" in wanted:
        e = emv(high, low, volume)
        if "emv" in wanted:
            out["emv"] = e["emv"].tolist()
            out["emvma"] = e[
                "emvma"
            ].tolist()  # 默认分支补齐均线（副图成对字段；旧实现仅 custom 分支输出）
        if "emvma" in wanted:
            out["emvma"] = e["emvma"].tolist()
    if "volume_ratio" in wanted:
        out["volume_ratio"] = volume_ratio(volume).tolist()
    if custom:
        for key, val in custom.items():
            # ma 特殊：旧格式支持数字列表（多均线），v2 只取 window
            if key == "ma":
                if isinstance(val, dict) and not isinstance(val, bool):
                    ns = [val.get("window")]
                elif isinstance(val, (list, tuple)):
                    ns = list(val)
                else:
                    ns = [val]
                for n in ns:
                    try:
                        n = int(n)
                    except (TypeError, ValueError):
                        continue
                    if n > 0:
                        out[f"ma{n}"] = ma(close, n).tolist()
                continue
            params = normalize_custom(key, val)
            if params is None:
                continue
            try:
                if key == "ema":
                    w = int(params["window"])
                    out[f"ema{w}"] = ema(close, w).tolist()
                    # 同步覆盖主图默认 EMA 线，使调优真实影响图表输出
                    if "ema12" in out:
                        out["ema12"] = ema(close, w).tolist()
                    if "ema26" in out:
                        out["ema26"] = ema(close, w).tolist()
                elif key == "rsi":
                    w = int(params["window"])
                    for n in (6, 12, 24):
                        out[f"rsi{n}"] = rsi(close, w).tolist()
                    out["rsi"] = rsi(close, w).tolist()
                elif key == "kdj":
                    k = kdj(high, low, close, int(params["window"]))
                    out["kdj_k"], out["kdj_d"], out["kdj_j"] = (
                        k["k"].tolist(),
                        k["d"].tolist(),
                        k["j"].tolist(),
                    )
                elif key == "boll":
                    b = boll(close, int(params["window"]), float(params["multiplier"]))
                    out["boll_upper"], out["boll_mid"], out["boll_lower"] = (
                        b["upper"].tolist(),
                        b["mid"].tolist(),
                        b["lower"].tolist(),
                    )
                elif key == "macd":
                    fast, slow, signal = (
                        int(params["fast"]),
                        int(params["slow"]),
                        int(params["signal"]),
                    )
                    if fast >= slow:
                        # 反向 MACD（fast>=slow）无意义：跳过该键并记录，不产出错误曲线
                        logger.warning(
                            "自定义 macd 参数非法: fast=%s >= slow=%s，跳过", fast, slow
                        )
                        continue
                    m = macd(close, fast, slow, signal)
                    out["macd_dif"], out["macd_dea"], out["macd_hist"] = (
                        m["dif"].tolist(),
                        m["dea"].tolist(),
                        m["hist"].tolist(),
                    )
                elif key == "wr":
                    w = int(params["window"])
                    for n in (10, 6):
                        out[f"wr{n}"] = wr(high, low, close, w).tolist()
                    out["wr"] = wr(high, low, close, w).tolist()
                elif key == "cci":
                    out["cci"] = cci(high, low, close, int(params["window"])).tolist()
                elif key == "roc":
                    out["roc"] = roc(close, int(params["window"])).tolist()
                elif key == "mtm":
                    m = mtm(close, int(params["window"]))
                    out["mtm"] = m["mtm"].tolist()
                    out["mtmma"] = m["mtmma"].tolist()  # 信号线一致性
                elif key == "bias":
                    w = int(params["window"])
                    for n in (6, 12, 24):
                        out[f"bias{n}"] = bias(close, w).tolist()
                    out["bias"] = bias(close, w).tolist()
                elif key == "psy":
                    out["psy"] = psy(close, int(params["window"])).tolist()
                elif key == "trix":
                    t = trix(close, int(params["window"]))
                    out["trix"] = t["trix"].tolist()
                    out["matrix"] = t["matrix"].tolist()  # 信号线一致性
                elif key == "cmo":
                    out["cmo"] = cmo(close, int(params["window"])).tolist()
                elif key == "volume_ratio":
                    out["volume_ratio"] = volume_ratio(
                        volume, int(params["window"])
                    ).tolist()
            except Exception as e:
                # 单指标失败只跳过该键并留痕，不吞掉整轮计算（旧实现静默 continue）
                logger.warning("自定义指标 %s 计算失败: %s", key, str(e)[:120])
                continue
    return out


def last_values(df: pd.DataFrame, fields: list[str] | None = None) -> dict:
    """返回各指标最新值（用于信号证据）。"""
    full = compute_all(df, fields)
    return {k: (v[-1] if v else None) for k, v in full.items()}


# ================= 全量传统指标（参考东方财富默认参数） =================


def expma(close: pd.Series, n1: int = 12, n2: int = 50) -> dict:
    """EXPMA：双均线（东财 12/50）。"""
    return {"expma_12": _ema(close, n1), "expma_50": _ema(close, n2)}


def wr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 10) -> pd.Series:
    """威廉指标 WR（东财 10/6）。"""
    hh = high.rolling(n).max()
    ll = low.rolling(n).min()
    out = (hh - close) / (hh - ll).replace(0, np.nan) * 100
    return out.fillna(50.0)


def dmi(
    high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14, m: int = 6
) -> dict:
    """DMI 趋向指标：PDI/MDI/ADX/ADXR（东财 14/6）。"""
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=close.index)
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=close.index
    )
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan) * 100
    mdi = minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan) * 100
    dx = (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100
    adx = dx.ewm(alpha=1 / m, adjust=False).mean()
    adxr = (adx + adx.shift(m)) / 2
    return {
        "pdi": pdi.fillna(0),
        "mdi": mdi.fillna(0),
        "adx": adx.fillna(0),
        "adxr": adxr.fillna(0),
    }


def cci(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """CCI 顺势指标（东财 14）。"""
    tp = (high + low + close) / 3
    ma_tp = tp.rolling(n).mean()
    md = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    out = (tp - ma_tp) / (0.015 * md.replace(0, np.nan))
    return out.fillna(0.0)


def roc(close: pd.Series, n: int = 12) -> pd.Series:
    """ROC 变动率（东财 12）。"""
    out = (close - close.shift(n)) / close.shift(n).replace(0, np.nan) * 100
    return out.fillna(0.0)


def mtm(close: pd.Series, n: int = 12, m: int = 6) -> dict:
    """MTM 动量线（东财 12/6）。"""
    mtm_v = close - close.shift(n)
    mtmma = mtm_v.rolling(m).mean()
    return {"mtm": mtm_v.fillna(0), "mtmma": mtmma.fillna(0)}


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """OBV 能量潮。"""
    direction = np.sign(close.diff().fillna(0))
    out = (direction * volume).cumsum()
    return out


def bias(close: pd.Series, n: int = 6) -> pd.Series:
    """BIAS 乖离率（东财 6/12/24）。"""
    out = (close - ma(close, n)) / ma(close, n).replace(0, np.nan) * 100
    return out.fillna(0.0)


def psy(close: pd.Series, n: int = 12) -> pd.Series:
    """PSY 心理线（东财 12）。"""
    up = (close.diff() > 0).astype(float)
    out = up.rolling(n).sum() / n * 100
    return out.fillna(50.0)


def trix(close: pd.Series, n: int = 12, m: int = 9) -> dict:
    """TRIX 三重指数平滑（东财 12/9）。"""
    e1 = _ema(close, n)
    e2 = _ema(e1, n)
    e3 = _ema(e2, n)
    trix_v = (e3 - e3.shift(1)) / e3.shift(1).replace(0, np.nan) * 100
    matrix = trix_v.rolling(m).mean()
    return {"trix": trix_v.fillna(0), "matrix": matrix.fillna(0)}


def vr(close: pd.Series, volume: pd.Series, n: int = 26) -> pd.Series:
    """VR 成交量比率（东财 26）。"""
    up = close.diff() > 0
    down = close.diff() < 0
    av = volume.where(up, 0.0).rolling(n).sum()
    bv = volume.where(down, 0.0).rolling(n).sum()
    cv = volume.where(~up & ~down, 0.0).rolling(n).sum()
    out = (av + cv / 2) / (bv + cv / 2).replace(0, np.nan) * 100
    return out.fillna(100.0)


def sar(
    high: pd.Series, low: pd.Series, step: float = 0.02, max_step: float = 0.2
) -> pd.Series:
    """SAR 抛物线转向（东财 2%/20%）。"""
    n = len(high)
    sar_v = np.full(n, np.nan)
    if n < 3:
        return pd.Series(sar_v, index=high.index)
    uptrend = True
    af = step
    ep = high.iloc[0]
    sar_v[0] = low.iloc[0]
    for i in range(1, n):
        prev_sar = sar_v[i - 1]
        if uptrend:
            sar_v[i] = prev_sar + af * (ep - prev_sar)
            if low.iloc[i] < sar_v[i]:
                uptrend = False
                sar_v[i] = ep
                ep = low.iloc[i]
                af = step
            else:
                if high.iloc[i] > ep:
                    ep = high.iloc[i]
                    af = min(af + step, max_step)
        else:
            sar_v[i] = prev_sar + af * (ep - prev_sar)
            if high.iloc[i] > sar_v[i]:
                uptrend = True
                sar_v[i] = ep
                ep = high.iloc[i]
                af = step
            else:
                if low.iloc[i] < ep:
                    ep = low.iloc[i]
                    af = min(af + step, max_step)
    return pd.Series(sar_v, index=high.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> dict:
    """ATR 平均真实波幅（东财 14）。"""
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr_v = tr.ewm(alpha=1 / n, adjust=False).mean()
    return {"atr": atr_v.fillna(0), "tr": tr.fillna(0)}


def cmo(close: pd.Series, n: int = 14) -> pd.Series:
    """CMO 钱德动量摆动（东财 14）。"""
    diff = close.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    out = (up - dn) / (up + dn).replace(0, np.nan) * 100
    return out.fillna(0.0)


def dpo(close: pd.Series, n: int = 20) -> pd.Series:
    """DPO 区间震荡线（东财 20）。"""
    ma_shift = close.rolling(n).mean().shift(int(n / 2) + 1)
    out = close - ma_shift
    return out.fillna(0.0)


def emv(
    high: pd.Series, low: pd.Series, volume: pd.Series, n: int = 14, m: int = 9
) -> dict:
    """EMV 简易波动指标（东财 14/9）。"""
    mid = (high + low) / 2
    mid_prev = mid.shift(1)
    box = (volume / 100000000).replace(0, np.nan)
    emv_v = (mid - mid_prev) / box * (high - low).replace(0, np.nan)
    emvma = emv_v.rolling(m).mean()
    return {"emv": emv_v.fillna(0), "emvma": emvma.fillna(0)}
