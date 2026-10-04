"""因子回测：横截面多空组合回测，输出净值曲线与绩效指标。"""

from __future__ import annotations

import numpy as np

# lib 层依赖例外:metrics 为纯计算模块(仅 numpy),归属 lib 待后续卡处理,
# lib 层依赖例外已消除:metrics 已迁 app.lib.metrics(S3 卡)
from app.lib.metrics import (
    annualized_return,
    max_drawdown,
    rank_array,
    sharpe_ratio,
    spearman_corr,
    win_rate,
)
from ..indicators.risk import risk_metrics as _risk_metrics
from .operators import evaluate_rpn

# 前端方向枚举 → 后端内部语义：positive=long（正向使用因子）、negative=short（取负）
_DIRECTION_ALIASES = {
    "auto": "auto",
    "positive": "long",
    "long": "long",
    "negative": "short",
    "short": "short",
}


def _normalize_direction(direction: str | None) -> str:
    return _DIRECTION_ALIASES.get((direction or "auto").lower(), "auto")


def downsample_indices(n: int, max_series: int = 600) -> np.ndarray:
    """to_series 相同的抽稀规则：返回均匀抽稀后的原始索引（供日期轴与净值对齐）。"""
    idx = np.arange(n)
    if n > max_series:
        step = n // max_series
        idx = idx[::step][:max_series]
    return idx


def _to_series(ret: np.ndarray, max_series: int = 600) -> list[float]:
    """日收益 → 净值序列（抽稀规则见 downsample_indices）。"""
    valid = np.isfinite(ret)
    cum = np.cumprod(1.0 + np.where(valid, ret, 0.0))
    cum[~valid] = np.nan
    if len(cum) > max_series:
        cum = cum[downsample_indices(len(cum), max_series)]
    return [round(float(v), 6) if np.isfinite(v) else None for v in cum]


def _metrics(ret: np.ndarray) -> dict:
    """绩效指标：年化/波动/夏普/最大回撤/胜率。"""
    v = ret[np.isfinite(ret)]
    if len(v) < 10:
        return {
            "annual_return": None,
            "volatility": None,
            "sharpe": None,
            "max_drawdown": None,
            "win_rate": None,
            "trading_days": len(v),
        }
    ann = annualized_return(v)
    vol = float(np.std(v)) * np.sqrt(252)
    sharpe = sharpe_ratio(v)
    mdd = max_drawdown(v)
    win = win_rate(v)
    return {
        "annual_return": round(ann, 4),
        "volatility": round(vol, 4),
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(mdd, 4),
        "win_rate": round(win, 3),
        "trading_days": int(len(v)),
    }


def _daily_and_bench(close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """等权全市场日收益与基准：一次计算，多 mode/多次调用共享（P1-38）。"""
    n_t = close.shape[1]
    daily_ret = np.full_like(close, np.nan)
    daily_ret[:, 1:] = close[:, 1:] / close[:, :-1] - 1.0
    bench_ret = np.full(n_t, np.nan)
    bench_ret[1:] = np.nanmean(daily_ret[:, 1:], axis=0)
    return daily_ret, bench_ret


def _probe_direction(
    f: np.ndarray, fwd_ret: np.ndarray, n_t: int, horizon: int | None = None
) -> int:
    """样本内 IC 定向探测：返回 1(正向) / -1(反向)。

    P1-38：收益取 fwd_ret 的 h 日前向收益（此前硬编码 5 日、fwd_ret 为死参数）；
    horizon 缺省时从 fwd_ret 尾部 NaN 结构推断（_infer_horizon，clamp [1,60]）。

    P1-33（前视声明）：方向由全样本前向收益 IC 决定——探测遍历整个样本区间
    （含回测时段之后的收益）逐期计算 IC 并取均值定方向，属研究性前视
    （look-ahead）：方向选择步骤使用了未来数据，等价于回测开始时即已知全程
    收益结构。本声明不改变计算逻辑，仅如实标注该前视事实；auto 方向的回测
    结果仅作研究参考，不代表样本外可实现收益。
    """
    if horizon is None:
        from .evaluate import _infer_horizon

        horizon = _infer_horizon(fwd_ret)
    ics = []
    for t in range(30, min(n_t - horizon, 260), 5):
        x, y = f[:, t], fwd_ret[:, t]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() > 10:
            ics.append(spearman_corr(x[m], y[m]))
    if ics and np.mean(ics) < 0:
        return -1
    return 1


# ---------------------------------------------------------------------------
# T-02 交易约束纯函数:涨跌停(按板块)/一字板/停牌 + 滑点/佣金/印花税成本。
# 全部为可测纯函数(仅 numpy),不依赖面板外的状态;组合调仓在 run_backtest 内
# 复用本组函数(vectorized 掩码 + 权重重建),三路径(等权/分位/多空)共用。
# ---------------------------------------------------------------------------


def limit_pct_by_code(code: str) -> float:
    """板块涨跌停阈值(百分比):创业板/科创板(300/301/302/688) 19.9、
    北交所(4/8) 30.0、其余(主板) 9.9。

    与 lib/signals/engine._limit_pct 同规则、同数值(0.1% 缓冲天然吸收浮点误差),
    单一事实源由 tests/test_backtest_constraints.py 锁定一致性。
    """
    bare = str(code).split(".")[0]
    if bare.startswith(("300", "301", "302", "688")):
        return 19.9
    if bare.startswith(("4", "8")):
        return 30.0
    return 9.9


def _is_one_price(high_p: float, low_p: float) -> bool:
    """一字板:开=高=低=收 的充分条件 high≈low(OHLC 满足 high≥open/close≥low)。"""
    return bool(
        np.isfinite(high_p)
        and np.isfinite(low_p)
        and abs(high_p - low_p) <= 1e-6 * max(abs(high_p), 1e-9)
    )


def can_buy(
    prev_close: float,
    high_p: float,
    low_p: float,
    close_p: float,
    volume: float,
    limit_pct: float,
) -> bool:
    """涨停不可买入(含一字板/停牌)。prev_close 缺失(新股首日)时只判一字板/停牌。

    涨停 = 当日涨幅 ≥ 板块阈值 且 收盘触板(close≈high);跌停可以买(卖出受限不影响买入)。
    """
    if volume is not None and (not np.isfinite(volume) or volume <= 0):
        return False
    if _is_one_price(high_p, low_p):
        return False
    if not (
        np.isfinite(prev_close)
        and prev_close > 0
        and np.isfinite(close_p)
        and np.isfinite(high_p)
    ):
        return True  # 前收缺失:无法判定涨停,不拦截
    if close_p / prev_close - 1.0 >= limit_pct / 100.0 - 1e-9:
        if close_p >= high_p - 1e-6 * max(abs(high_p), 1e-9):
            return False  # 涨停触板 → 买不到
    return True


def can_sell(
    prev_close: float,
    high_p: float,
    low_p: float,
    close_p: float,
    volume: float,
    limit_pct: float,
) -> bool:
    """跌停不可卖出(含一字板/停牌)。prev_close 缺失时只判一字板/停牌。

    跌停 = 当日跌幅 ≥ 板块阈值 且 收盘触板(close≈low);涨停可以卖(买入受限不影响卖出)。
    """
    if volume is not None and (not np.isfinite(volume) or volume <= 0):
        return False
    if _is_one_price(high_p, low_p):
        return False
    if not (
        np.isfinite(prev_close)
        and prev_close > 0
        and np.isfinite(close_p)
        and np.isfinite(low_p)
    ):
        return True
    if close_p / prev_close - 1.0 <= -(limit_pct / 100.0) + 1e-9:
        if close_p <= low_p + 1e-6 * max(abs(low_p), 1e-9):
            return False  # 跌停触板 → 卖不出
    return True


def effective_price(side: str, price: float, slippage_bps: float) -> float:
    """滑点作用后的成交价:买入抬价、卖出压价(基点,1bp=1e-4)。"""
    rate = float(slippage_bps or 0.0) / 10000.0
    return price * (1.0 + rate) if side == "buy" else price * (1.0 - rate)


def resolve_cost_rate(
    cost_rate: float,
    slippage_bps: float | None = None,
    commission_bps: float | None = None,
    stamp_tax_bps: float | None = None,
) -> tuple[float, bool]:
    """统一单边成本率:新成本模型折算值与是否启用拆分模型。

    组合级成本 = |Δw| × (滑点 + 佣金 + 印花税),其中印花税仅卖出(卖出量≈|Δw|/2):
      总成本 = |Δw|·s + |Δw|·c + (|Δw|/2)·t = 单边换手(|Δw|/2) × (2s + 2c + t)
    旧式单边成本率 = cost_rate(保持 |Δw|·cost_rate/2 同构)。三个新参任一显式 → 拆分模型,
    未传的按 0(前端恒传默认 0/2.5/5);全部 None → 旧 cost_rate(兼容历史任务)。
    默认值(0/2.5/5)折算 2×(0+0.00025)+0.0005 = 0.001,与旧默认 cost_rate=0.001 一致。
    """
    if slippage_bps is None and commission_bps is None and stamp_tax_bps is None:
        return float(cost_rate), False
    s = float(slippage_bps or 0.0) / 10000.0
    c = float(commission_bps or 0.0) / 10000.0
    t = float(stamp_tax_bps or 0.0) / 10000.0
    return 2.0 * (s + c) + t, True


def _suspended_day(ohlc: dict | None, t: int) -> bool:
    """整日无成交(volume 列存在且当日全 0/NaN)→ 跳过调仓(停牌/数据缺失)。

    volume 特征缺失(旧面板)时无信息,不跳过——保持旧行为。
    """
    vol = (ohlc or {}).get("volume")
    if vol is None:
        return False
    v = np.asarray(vol[:, t], dtype=np.float64)
    if len(v) == 0:
        return False
    return bool(np.all(~np.isfinite(v) | (v <= 0)))


def _tradability_masks(
    close: np.ndarray, ohlc: dict, t: int, limit_pct: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """调仓日 t 的逐股可买/可卖掩码(S,)(向量化)。

    - 涨停(涨幅≥板块阈值 且 close≈high)/一字板(high≈low)/停牌(volume≤0) → 不可买
    - 跌停(跌幅≥阈值 且 close≈low)/一字板/停牌 → 不可卖
    - t=0 无前收:仅判一字板/停牌,不做涨跌停
    """
    S = close.shape[0]
    buy_ok = np.ones(S, dtype=bool)
    sell_ok = np.ones(S, dtype=bool)
    vol = ohlc.get("volume")
    if vol is not None:
        v = np.asarray(vol[:, t], dtype=np.float64)
        bad = ~np.isfinite(v) | (v <= 0)
        buy_ok &= ~bad
        sell_ok &= ~bad
    high = ohlc.get("high")
    low = ohlc.get("low")
    if high is None or low is None:
        return buy_ok, sell_ok
    h = np.asarray(high[:, t], dtype=np.float64)
    l = np.asarray(low[:, t], dtype=np.float64)
    c = np.asarray(close[:, t], dtype=np.float64)
    valid = np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    one_price = np.zeros(S, dtype=bool)
    if valid.any():
        one_price[valid] = (h[valid] - l[valid]) <= 1e-6 * np.maximum(
            np.abs(h[valid]), 1e-9
        )
    buy_ok &= ~one_price
    sell_ok &= ~one_price
    if t == 0:
        return buy_ok, sell_ok
    pc = np.asarray(close[:, t - 1], dtype=np.float64)
    lp = np.asarray(limit_pct, dtype=np.float64)
    m = valid & np.isfinite(pc) & (pc > 0)
    if not m.any():
        return buy_ok, sell_ok
    chg = np.full(S, np.nan)
    chg[m] = c[m] / pc[m] - 1.0
    up = np.zeros(S, dtype=bool)
    down = np.zeros(S, dtype=bool)
    h_tol = 1e-6 * np.maximum(np.abs(h), 1e-9)
    l_tol = 1e-6 * np.maximum(np.abs(l), 1e-9)
    up[m] = (chg[m] >= lp[m] / 100.0 - 1e-9) & (c[m] >= h[m] - h_tol[m])
    down[m] = (chg[m] <= -lp[m] / 100.0 + 1e-9) & (c[m] <= l[m] + l_tol[m])
    buy_ok &= ~up
    sell_ok &= ~down
    return buy_ok, sell_ok


def _rebuild_weights(
    prev: np.ndarray | None,
    target_idx: np.ndarray,
    can_enter: np.ndarray | None,
    can_exit: np.ndarray | None,
    sign: float,
    n_s: int,
) -> np.ndarray:
    """调仓权重重建:目标成员等权(可建仓过滤),prev 中平仓受阻的持仓保留原权重。

    - prev: 前次权重(多头 >0 / 空头 <0);None = 首期
    - target_idx: 目标成员(全局下标)
    - can_enter/can_exit: 可建仓/可平仓掩码(S,);None = 无约束(旧行为)
    - sign: +1(多头)/ -1(空头)
    保留语义:prev 持仓今日不可平(跌停卖不出/涨停买不回/停牌/一字板)→ 继续持有,
    剩余权重在可建仓目标上等权分配(总权重 ≤ 1,受阻部分不强制平仓)。
    """
    w = np.zeros(n_s)
    if prev is not None and can_exit is not None:
        hold = ((sign * prev) > 0) & ~can_exit
        if hold.any():
            held = float((sign * prev[hold]).sum())
            w[hold] = prev[hold]
            sel = (
                target_idx[can_enter[target_idx]]
                if can_enter is not None
                else target_idx
            )
            sel = sel[~hold[sel]]
            if len(sel) and held < 1.0:
                w[sel] = sign * (1.0 - held) / len(sel)
            return w
    sel = target_idx[can_enter[target_idx]] if can_enter is not None else target_idx
    if len(sel):
        w[sel] = sign * 1.0 / len(sel)
    return w


def run_backtest(
    factor: np.ndarray,
    close: np.ndarray,
    fwd_ret: np.ndarray,
    top_pct: float = 0.2,
    bottom_pct: float = 0.2,
    cost_rate: float = 0.001,
    trade_interval: int = 5,
    max_series: int = 600,
    mode: str = "long_short",
    direction: str = "auto",
    _daily_ret: np.ndarray | None = None,
    _bench_ret: np.ndarray | None = None,
    slippage_bps: float | None = None,
    commission_bps: float | None = None,
    stamp_tax_bps: float | None = None,
    codes: list[str] | np.ndarray | None = None,
    ohlc: dict | None = None,
    _debug_weights: bool = False,
) -> dict:
    """横截面回测（每 trade_interval 日调仓，等权）。

    mode:      long_short(多空) / long(仅多) / short(仅空)
    direction: auto(按样本内IC自动定向) / long(正向) / short(反向)
    fwd_ret:   供 auto 方向探测的 h 日前向收益（horizon 参数化，P1-38）
    _daily_ret/_bench_ret: 内部共享参数——由 run_backtest_multi 预计算一次传入，
               None 时自行计算（对外行为不变）。
    T-02 交易约束（均为可选，缺省保持旧行为）:
      slippage_bps/commission_bps/stamp_tax_bps: 新成本模型(基点)；任一显式 → 拆分，
               未传的按 0；全 None → 旧 cost_rate。
      codes/ohlc: 股票代码(板块阈值)与 open/high/low/volume 面板——非 None 时启用
               涨跌停/一字板/停牌约束；None 时纯成本回测(旧行为)。
    返回：净值序列（组合/基准）、绩效指标。
    """
    f = np.asarray(factor, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    direction = _normalize_direction(direction)
    n_s, n_t = c.shape
    cost_rate_eff, split_cost = resolve_cost_rate(
        cost_rate, slippage_bps, commission_bps, stamp_tax_bps
    )
    if codes is not None and ohlc is not None:
        codes_arr = np.asarray(codes, dtype=object)
        if len(codes_arr) != n_s:
            # 防御:面板行数 ≠ codes(旧缓存/重建时序错位)→ 降级为全部主板阈值(10%)，
            # 不抛错打断线上任务
            limit_pct = np.full(n_s, 9.9, dtype=np.float64)
        else:
            limit_pct = np.asarray(
                [limit_pct_by_code(str(x)) for x in codes_arr], dtype=np.float64
            )
    else:
        limit_pct = None
    # P1-33（前视声明）：auto 方向由 _probe_direction 用全样本前向收益 IC 决定——
    # 方向选择步骤存在 look-ahead（见 _probe_direction docstring），结果仅作研究参考；
    # 手工 long/short 不经探测，无此前视。
    if direction == "short":
        f = -f
    elif direction == "auto" and _probe_direction(f, fwd_ret, n_t) < 0:
        f = -f
    use_bottom = mode in ("long_short", "short")
    use_top = mode in ("long_short", "long")

    # 基准：等权全市场日收益（用 close 计算当日收益）；共享预计算结果时跳过重算
    if _daily_ret is None or _bench_ret is None:
        daily_ret, bench_ret = _daily_and_bench(c)
    else:
        daily_ret, bench_ret = _daily_ret, _bench_ret

    # 多空组合：按因子排名构建持仓权重（静态持有 trade_interval 日）
    long_w = np.zeros((n_s, n_t))
    short_w = np.zeros((n_s, n_t))
    turnover_cost = np.zeros(n_t)
    prev_long: np.ndarray | None = None
    prev_short: np.ndarray | None = None

    for t in range(0, n_t):
        x = f[:, t]
        mask = np.isfinite(x)
        if mask.sum() < 20:
            continue
        if t % trade_interval != 0:
            # 保持前次持仓
            if prev_long is not None:
                long_w[:, t] = prev_long
            if prev_short is not None:
                short_w[:, t] = prev_short
            continue
        # T-02:整日无成交(volume 全 0/缺失)→ 跳过调仓,保持前次持仓
        if limit_pct is not None and _suspended_day(ohlc, t):
            if prev_long is not None:
                long_w[:, t] = prev_long
            if prev_short is not None:
                short_w[:, t] = prev_short
            continue
        order = rank_array(x[mask])
        n = len(order)
        n_long = max(1, int(n * top_pct))
        n_short = max(1, int(n * bottom_pct))
        idx = np.where(mask)[0]
        top_idx = idx[order >= n - n_long]
        bot_idx = idx[order < n_short]

        # T-02 涨跌停/停牌约束:可买/可卖掩码(无约束时 None,行为与旧完全一致)
        if limit_pct is not None:
            buy_ok, sell_ok = _tradability_masks(c, ohlc, t, limit_pct)
        else:
            buy_ok = sell_ok = None

        new_long = np.zeros(n_s)
        new_short = np.zeros(n_s)
        if use_top:
            new_long = _rebuild_weights(prev_long, top_idx, buy_ok, sell_ok, 1.0, n_s)
        if use_bottom:
            new_short = _rebuild_weights(
                prev_short, bot_idx, sell_ok, buy_ok, -1.0, n_s
            )

        if prev_long is not None:
            turnover_cost[t] = (
                (
                    np.abs(new_long - prev_long).sum()
                    + np.abs(new_short - prev_short).sum()
                )
                * cost_rate_eff
                / 2
            )
        prev_long = new_long
        prev_short = new_short
        long_w[:, t] = new_long
        short_w[:, t] = new_short

    # 组合日收益 = 昨日持仓权重 × 今日收益。
    # 权重在 t 日收盘由 t 日因子决定，t+1 日起才生效；调仓成本 t 日算出、t+1 日扣除，
    # 整体后移一天避免用当日权重赚当日收益的前视偏差。
    combo_ret = np.full(n_t, np.nan)
    long_ret = np.zeros(n_t)
    short_ret = np.zeros(n_t)
    long_ret[1:] = np.nansum(long_w[:, :-1] * daily_ret[:, 1:], axis=0)
    short_ret[1:] = np.nansum(short_w[:, :-1] * daily_ret[:, 1:], axis=0)
    combo_ret[1:] = long_ret[1:] - short_ret[1:] - turnover_cost[1:]
    combo_ret[0] = 0.0

    # 风控指标（FRM）：组合收益的 VaR/CVaR/波动/回撤
    # T-111b：risk_metrics 已迁 lib/indicators/risk.py（纯计算），直接调用；
    # 序列过短（<30 交易日）时 risk_metrics 返回空 dict（合法空结果，非错误）。
    risk = _risk_metrics(np.cumprod(1 + np.nan_to_num(combo_ret)) * 100)
    out = {
        "combo_nav": _to_series(combo_ret, max_series),
        "bench_nav": _to_series(bench_ret, max_series),
        "metrics": _metrics(combo_ret),
        "bench_metrics": _metrics(bench_ret),
        "risk": risk,
        "params": {
            "top_pct": top_pct,
            "bottom_pct": bottom_pct,
            "cost_rate": cost_rate,
            "trade_interval": trade_interval,
            "mode": mode,
            "direction": direction,
            # P1-33：True = 方向由全样本前向收益 IC 探测决定（研究性前视），
            # 结果含 look-ahead，仅作研究参考；False = 手工方向，无此前视。
            "direction_probe": direction == "auto",
            "slippage_bps": slippage_bps,
            "commission_bps": commission_bps,
            "stamp_tax_bps": stamp_tax_bps,
        },
    }
    if _debug_weights:
        out["long_w"] = long_w
        out["short_w"] = short_w
    return out


def run_backtest_quantile(
    factor: np.ndarray,
    close: np.ndarray,
    fwd_ret: np.ndarray,
    n_q: int = 5,
    top_pct: float = 0.2,
    bottom_pct: float = 0.2,
    cost_rate: float = 0.001,
    trade_interval: int = 5,
    max_series: int = 600,
    direction: str = "auto",
    slippage_bps: float | None = None,
    commission_bps: float | None = None,
    stamp_tax_bps: float | None = None,
    codes: list[str] | np.ndarray | None = None,
    ohlc: dict | None = None,
) -> dict:
    """分层（分位数组合）回测：每调仓日按因子值分 n_q 等分位，各分位独立等权组合。

    q1 = 因子值最低分位，q{n_q} = 因子值最高分位；各分位 long-only、净值相互独立。
    调仓/成本/持仓后移语义与 run_backtest 完全一致（权重 t 日决定、t+1 日生效，
    成本 t 日算出、t+1 日扣除）。top_pct/bottom_pct 仅用于与 run_backtest 签名对齐，不使用。
    T-02 约束参数语义与 run_backtest 一致(codes/ohlc 非 None → 涨跌停/停牌约束)。
    返回：quantiles（分位净值与指标）+ 基准 + params。
    """
    f = np.asarray(factor, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    n_s, n_t = c.shape
    cost_rate_eff, _ = resolve_cost_rate(
        cost_rate, slippage_bps, commission_bps, stamp_tax_bps
    )
    if codes is not None and ohlc is not None:
        codes_arr = np.asarray(codes, dtype=object)
        if len(codes_arr) != n_s:
            limit_pct = np.full(n_s, 9.9, dtype=np.float64)
        else:
            limit_pct = np.asarray(
                [limit_pct_by_code(str(x)) for x in codes_arr], dtype=np.float64
            )
    else:
        limit_pct = None
    direction = _normalize_direction(direction)
    # P1-33（前视声明）：auto 方向由 _probe_direction 用全样本前向收益 IC 决定——
    # 方向选择步骤存在 look-ahead（见 _probe_direction docstring），结果仅作研究参考。
    if direction == "auto":
        resolved = "short" if _probe_direction(f, fwd_ret, n_t) < 0 else "long"
    else:
        resolved = "long" if direction == "long" else "short"
    if resolved == "short":
        f = -f
    n_q = max(2, int(n_q))
    if n_q > 50:
        # 每分位分配独立的 (n_s, n_t) 权重矩阵，n_q 无上限时大值直接 OOM
        # （如 n_q=10^5 级 × 300 股 × 2000 日 ≈ 数十 GB）
        raise ValueError(f"n_q={n_q} 超过上限 50，请降低分位层数")

    # 基准：等权全市场日收益（与 run_backtest 一致）
    daily_ret, bench_ret = _daily_and_bench(c)

    quantiles: list[dict] = []
    for qi in range(n_q):
        w = np.zeros((n_s, n_t))
        turnover_cost = np.zeros(n_t)
        prev: np.ndarray | None = None
        for t in range(0, n_t):
            x = f[:, t]
            mask = np.isfinite(x)
            if mask.sum() < 20:
                continue
            if t % trade_interval != 0:
                # 保持前次持仓
                if prev is not None:
                    w[:, t] = prev
                continue
            # T-02:整日无成交 → 跳过调仓
            if limit_pct is not None and _suspended_day(ohlc, t):
                if prev is not None:
                    w[:, t] = prev
                continue
            order = rank_array(x[mask])
            n = len(order)
            lo = int(n * qi / n_q)
            hi = int(n * (qi + 1) / n_q)
            idx = np.where(mask)[0]
            members = idx[(order >= lo) & (order < hi)]
            if limit_pct is not None:
                buy_ok, sell_ok = _tradability_masks(c, ohlc, t, limit_pct)
            else:
                buy_ok = sell_ok = None
            new_w = _rebuild_weights(prev, members, buy_ok, sell_ok, 1.0, n_s)

            if prev is not None:
                turnover_cost[t] = np.abs(new_w - prev).sum() * cost_rate_eff / 2
            prev = new_w
            w[:, t] = new_w

        # 组合日收益 = 昨日持仓权重 × 今日收益（后移一天，同 run_backtest）
        ret = np.full(n_t, np.nan)
        ret[1:] = np.nansum(w[:, :-1] * daily_ret[:, 1:], axis=0) - turnover_cost[1:]
        ret[0] = 0.0
        quantiles.append(
            {
                "q": qi + 1,
                "combo_nav": _to_series(ret, max_series),
                "metrics": _metrics(ret),
            }
        )

    return {
        "quantiles": quantiles,
        "bench_nav": _to_series(bench_ret, max_series),
        "bench_metrics": _metrics(bench_ret),
        "params": {
            "n_q": n_q,
            "cost_rate": cost_rate,
            "trade_interval": trade_interval,
            "direction": resolved,
            # P1-33：True = 方向由全样本前向收益 IC 探测决定（研究性前视），结果含 look-ahead
            "direction_probe": direction == "auto",
            "slippage_bps": slippage_bps,
            "commission_bps": commission_bps,
            "stamp_tax_bps": stamp_tax_bps,
        },
    }


def backtest_expression(
    rpn: list[dict],
    panel: dict[str, np.ndarray],
    top_pct: float = 0.2,
    bottom_pct: float = 0.2,
    cost_rate: float = 0.001,
    trade_interval: int = 5,
    horizon: int = 5,
    mode: str = "long_short",
    direction: str = "auto",
    slippage_bps: float | None = None,
    commission_bps: float | None = None,
    stamp_tax_bps: float | None = None,
    codes: list[str] | np.ndarray | None = None,
) -> dict:
    """从表达式与面板数据运行回测（含使用方式/方向）。

    T-02:codes 非 None 且面板含 open/high/low/volume 时启用涨跌停/停牌约束。
    """
    factor = evaluate_rpn(rpn, panel)
    close = panel["close"]
    from .evaluate import forward_returns

    fwd = forward_returns(close, horizon)
    ohlc = {k: panel[k] for k in ("open", "high", "low", "volume") if k in panel}
    return run_backtest(
        factor,
        close,
        fwd,
        top_pct,
        bottom_pct,
        cost_rate,
        trade_interval,
        mode=mode,
        direction=direction,
        slippage_bps=slippage_bps,
        commission_bps=commission_bps,
        stamp_tax_bps=stamp_tax_bps,
        codes=codes,
        ohlc=ohlc or None,
    )


def run_backtest_multi(
    factor: np.ndarray,
    close: np.ndarray,
    fwd_ret: np.ndarray,
    top_pct: float = 0.2,
    bottom_pct: float = 0.2,
    cost_rate: float = 0.001,
    trade_interval: int = 5,
    modes: list[str] | None = None,
    direction: str = "auto",
    slippage_bps: float | None = None,
    commission_bps: float | None = None,
    stamp_tax_bps: float | None = None,
    codes: list[str] | np.ndarray | None = None,
    ohlc: dict | None = None,
) -> dict:
    """多使用方式一次回测：long_short(多空对冲) / long(仅多) / short(仅空)。

    方向只在第一个 mode 前探测一次，全部 mode 共用，保证各曲线方向一致。
    返回：
      modes:       [{"mode", "combo_nav", "metrics", "risk"}, ...]（顺序同传入 modes）
      bench_nav / bench_metrics: 等权基准（与因子无关，各 mode 相同）
      params:      回测参数 + modes + 解析后的 direction
    """
    if not modes:
        modes = ["long_short", "long", "short"]
    direction = _normalize_direction(direction)
    c = np.asarray(close, dtype=np.float64)
    # P1-33（前视声明）：auto 方向由 _probe_direction 用全样本前向收益 IC 决定——
    # 方向选择步骤存在 look-ahead（见 _probe_direction docstring），结果仅作研究参考；
    # 全部 mode 共用同一探测结果，保证各曲线方向一致。
    if direction == "auto":
        resolved = (
            "short"
            if _probe_direction(
                np.asarray(factor, dtype=np.float64), fwd_ret, c.shape[1]
            )
            < 0
            else "long"
        )
    else:
        resolved = "long" if direction == "long" else "short"

    # P1-38：基准/日收益只算一次，全部 mode 共享（此前 run_backtest 每 mode 重算 3 遍）
    daily_ret, bench_ret = _daily_and_bench(c)
    out_modes: list[dict] = []
    bench_nav: list[float] | None = None
    bench_metrics: dict | None = None
    for mode in modes:
        r = run_backtest(
            factor,
            close,
            fwd_ret,
            top_pct=top_pct,
            bottom_pct=bottom_pct,
            cost_rate=cost_rate,
            trade_interval=trade_interval,
            mode=mode,
            direction=resolved,
            _daily_ret=daily_ret,
            _bench_ret=bench_ret,
            slippage_bps=slippage_bps,
            commission_bps=commission_bps,
            stamp_tax_bps=stamp_tax_bps,
            codes=codes,
            ohlc=ohlc,
        )
        out_modes.append(
            {
                "mode": mode,
                "combo_nav": r["combo_nav"],
                "metrics": r["metrics"],
                "risk": r["risk"],
            }
        )
        if bench_nav is None:
            bench_nav = r["bench_nav"]
            bench_metrics = r["bench_metrics"]

    return {
        "modes": out_modes,
        "bench_nav": bench_nav,
        "bench_metrics": bench_metrics,
        "params": {
            "top_pct": top_pct,
            "bottom_pct": bottom_pct,
            "cost_rate": cost_rate,
            "trade_interval": trade_interval,
            "modes": modes,
            "direction": resolved,
            # P1-33：True = 方向由全样本前向收益 IC 探测决定（研究性前视），结果含 look-ahead
            "direction_probe": direction == "auto",
            "slippage_bps": slippage_bps,
            "commission_bps": commission_bps,
            "stamp_tax_bps": stamp_tax_bps,
        },
    }
