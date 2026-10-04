"""模拟盘账户核心：撮合 / 执行 / 绩效（T-01 账户模型）。

撮合语义（与信号时点契约对齐，docs/DESIGN-CONTRACT.md §1.7）：
- 成交以 bar 收盘价为准：订单提交时解析 bar 时点（bar_date），撮合价 = 该 bar 的
  close；bar_date 缺省取最新 bar。bar 由 API 层从 K 线缓存解析后传入（本模块纯计算）。
- 市价单：按 bar 价全额成交；
- 限价买单：bar 价 ≤ 委托价 → 成交；否则挂起（pending）；
- 限价卖单：bar 价 ≥ 委托价 → 成交；否则挂起（pending）；
- 触发但资金/持仓不足 → 整单拒绝（rejected + 原因），不支持部分成交。
- 涨跌停 / 停牌 / 滑点等约束属 T-02，本模块不处理。

费用（可变数字集中显式化，单一事实源；T-02 成本拆分在此扩展）：
- 佣金：max(成交额 × COMMISSION_RATE, MIN_COMMISSION)，买卖双向；
- 印花税：成交额 × STAMP_DUTY_RATE，仅卖出。
"""

from __future__ import annotations

import logging

import numpy as np

from ...lib.metrics import annualized_return, max_drawdown, sharpe_ratio

# 一手股数：委托数量必须是其整数倍（A 股最小交易单位，领域事实）
LOT_SIZE = 100
# 佣金费率（万 2.5）与最低佣金（元）
COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
# 印花税（仅卖出，2023-08 起万 5）
STAMP_DUTY_RATE = 0.0005
# 净值曲线最多点数：超出按末尾密集保留采样，防超长历史拖垮响应
MAX_EQUITY_POINTS = 500

VALID_SIDES = ("buy", "sell")
VALID_ORDER_TYPES = ("market", "limit")
# 撮合状态（订单终态机：pending → filled / canceled / rejected）
ORDER_STATUS = ("pending", "filled", "canceled", "rejected")

logger = logging.getLogger("stockradar.core.paper.account")


def fee_for(side: str, amount: float) -> float:
    """单笔成交费用：佣金（双向，max(比例, 最低)）+ 卖出印花税。"""
    commission = max(amount * COMMISSION_RATE, MIN_COMMISSION)
    stamp = amount * STAMP_DUTY_RATE if side == "sell" else 0.0
    return round(commission + stamp, 2)


def validate_order(
    side: str, order_type: str, price: float | None, quantity: float
) -> None:
    """委托参数校验：违规抛 ValueError（人类可读中文，API 层转 400）。"""
    if side not in VALID_SIDES:
        raise ValueError(f"非法方向 {side!r}，可选: {'/'.join(VALID_SIDES)}")
    if order_type not in VALID_ORDER_TYPES:
        raise ValueError(
            f"非法委托类型 {order_type!r}，可选: {'/'.join(VALID_ORDER_TYPES)}"
        )
    if not quantity or quantity <= 0 or quantity % LOT_SIZE != 0:
        raise ValueError(f"数量必须为正的 {LOT_SIZE} 股整数倍")
    if order_type == "limit" and (price is None or price <= 0):
        raise ValueError("限价单必须提供正的委托价格")


def prepare_kline(df) -> "object":
    """K 线按可解析日期升序排序去重（同日期保留最后一行），date 统一为字符串。

    与撮合/实验/观察的 bar 解析共用（api 层与下单编排不再各自维护清洗实现）。
    """
    df = df.reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    return (
        df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    )


def resolve_bar_price(df, bar_date: str | None = None) -> tuple[float, str]:
    """从已整理的 K 线 DataFrame（date 字符串升序）解析撮合价与 bar 标签。

    返回 (bar 收盘价, bar 日期)。bar_date 缺省取最新 bar；指定但不在 df 中 → ValueError。
    """
    if df is None or len(df) == 0:
        raise ValueError("行情不可用")
    dates = list(df["date"].astype(str))
    if bar_date is not None:
        if bar_date not in dates:
            raise ValueError(f"bar 时点 {bar_date} 无可用行情")
        i = dates.index(bar_date)
    else:
        i = len(dates) - 1
    return float(df["close"].iloc[i]), dates[i]


def execute_order(
    cash: float,
    positions: dict[str, dict],
    order: dict,
    bar_price: float,
) -> tuple[dict, float, dict, float | None]:
    """执行撮合（纯计算，不改库）：返回 (结果, 新现金, 新持仓, 已实现盈亏)。

    positions: {code: {"quantity": qty, "avg_cost": cost}}；order: {code, side,
    order_type, price, quantity}。
    结果: {"status": filled|pending|rejected, "price": 成交价|None, "fee": 费用,
    "reason": 拒绝原因|None}。卖出成交返回已实现盈亏（含费用），其余 None。
    """
    side = order["side"]
    order_type = order["order_type"]
    qty = order["quantity"]
    if order_type == "market":
        triggered = True
    elif side == "buy":
        triggered = bar_price <= order["price"]
    else:
        triggered = bar_price >= order["price"]
    if not triggered:
        return (
            {"status": "pending", "price": None, "fee": 0.0, "reason": None},
            cash,
            positions,
            None,
        )

    amount = bar_price * qty
    p = {k: dict(v) for k, v in positions.items()}
    pos = p.get(order["code"], {"quantity": 0.0, "avg_cost": 0.0})
    realized: float | None = None
    if side == "buy":
        fee = fee_for(side, amount)
        if amount + fee > cash:
            return (
                {
                    "status": "rejected",
                    "price": None,
                    "fee": fee,
                    "reason": f"资金不足（需 {amount + fee:.2f}，可用 {cash:.2f}）",
                },
                cash,
                positions,
                None,
            )
        total_cost = pos["quantity"] * pos["avg_cost"] + amount + fee
        pos["quantity"] = pos["quantity"] + qty
        pos["avg_cost"] = total_cost / pos["quantity"]
        p[order["code"]] = pos
        cash = cash - amount - fee
    else:
        fee = fee_for(side, amount)
        if qty > pos["quantity"]:
            return (
                {
                    "status": "rejected",
                    "price": None,
                    "fee": fee,
                    "reason": f"持仓不足（持有 {pos['quantity']:.0f}，卖出 {qty:.0f}）",
                },
                cash,
                positions,
                None,
            )
        realized = (bar_price - pos["avg_cost"]) * qty - fee
        pos["quantity"] = pos["quantity"] - qty
        cash = cash + amount - fee
        if pos["quantity"] > 1e-9:
            p[order["code"]] = pos
        else:
            p.pop(order["code"], None)
    return (
        {"status": "filled", "price": bar_price, "fee": fee, "reason": None},
        cash,
        p,
        realized,
    )


def _metrics_from_equity(
    initial_cash: float, curve: list[dict], wins: int, closed_trades: int
) -> dict:
    """净值序列 → 绩效指标（收益/年化/回撤/夏普/胜率；纯函数）。

    收益基于每日净值 pct_change（252 年化口径，复用 lib.metrics）；胜率 = 已平仓
    卖出成交中实现盈亏为正的占比（交易域语义）。无序列字段返回 None，前端统一「—」。
    """
    empty = {
        "total_return": None,
        "annualized_return": None,
        "max_drawdown": None,
        "sharpe": None,
        "win_rate": None,
        "trade_count": 0,
        "winning_trades": 0,
        "initial_cash": round(float(initial_cash), 2),
        "final_equity": round(float(initial_cash), 4),
        "days": 0,
    }
    if not curve:
        return empty
    equities = np.array([p["equity"] for p in curve], dtype=float)
    base = float(initial_cash)
    final = float(equities[-1])
    returns = (
        np.diff(equities) / equities[:-1]
        if len(equities) > 1
        else np.array([], dtype=float)
    )
    out = {
        "total_return": round(final / base - 1, 6) if base > 0 else None,
        "annualized_return": round(annualized_return(returns, 252), 6)
        if len(returns)
        else None,
        "max_drawdown": round(max_drawdown(returns), 6),
        "sharpe": round(sharpe_ratio(returns, 252), 6) if len(returns) else None,
        "win_rate": round(wins / closed_trades, 4) if closed_trades else None,
        "trade_count": closed_trades,
        "winning_trades": wins,
        "initial_cash": round(base, 2),
        "final_equity": round(final, 4),
        "days": len(curve),
    }
    return out


def equity_curve(
    initial_cash: float,
    trades: list[dict],
    price_lookup,
) -> dict:
    """重放成交流水 → 净值曲线 + 绩效指标（纯函数）。

    trades: 按 bar_date 升序的成交记录，每项 {code, side, price, quantity, fee, bar_date}；
    price_lookup: code → {date: close}（日线 close 映射，date 为 'YYYY-MM-DD' 字符串）。
    时间轴 = 全部涉股价格日期并集 ∩ [首笔 bar_date, 末笔 bar_date]；持仓按当日收盘
    估值，当日无 bar（停牌）向前沿用最近收盘。净值序列超 MAX_EQUITY_POINTS 时按末尾
    密集保留采样。
    """
    if not trades:
        return {"curve": [], "metrics": _metrics_from_equity(initial_cash, [], 0, 0)}
    closes = {t["code"]: price_lookup(t["code"]) for t in trades}
    first = trades[0]["bar_date"]
    last = trades[-1]["bar_date"]
    dates = sorted({d for m in closes.values() for d in m if first <= d <= last})
    if not dates:
        dates = sorted({t["bar_date"] for t in trades})
    if len(dates) > MAX_EQUITY_POINTS:
        dates = dates[-MAX_EQUITY_POINTS:]
    # 停牌/非交易日向前沿用最近收盘：沿全局日期轴预构建每股 as-of 收盘映射（P2-39），
    # 估值循环 O(1) 查表，消除每（日期×持仓）对 close_map 的全键线性扫描（O(dates²×positions)）
    asof: dict[str, dict[str, float | None]] = {}
    for c, m in closes.items():
        row: dict[str, float | None] = {}
        carry = None
        for d in dates:
            px = m.get(d)
            if px is not None:
                carry = px
            row[d] = carry
        asof[c] = row

    cash = float(initial_cash)
    qty: dict[str, float] = {}
    avg: dict[str, float] = {}
    wins = 0
    closed = 0
    ti = 0
    curve: list[dict] = []
    for d in dates:
        while ti < len(trades) and trades[ti]["bar_date"] <= d:
            t = trades[ti]
            n = t["quantity"]
            if t["side"] == "buy":
                old = qty.get(t["code"], 0.0)
                old_avg = avg.get(t["code"], 0.0)
                new_qty = old + n
                qty[t["code"]] = new_qty
                avg[t["code"]] = (
                    (old * old_avg + n * t["price"] + t["fee"]) / new_qty
                    if new_qty
                    else 0.0
                )
                cash = cash - n * t["price"] - t["fee"]
            else:
                held = qty.get(t["code"], 0.0)
                cost = avg.get(t["code"])
                if cost is None or held < n:
                    # 数据完整性防御（P2-53）：无持仓/成本缺失的卖出为脏数据，
                    # 跳过该笔重放（realized 不虚高、qty 不为负），并告警
                    logger.warning(
                        "成交流水完整性校验失败：%s %s 卖出 %s，但持仓 %s，跳过该笔重放",
                        t["code"],
                        t["bar_date"],
                        n,
                        held,
                    )
                else:
                    realized = (t["price"] - cost) * n - t["fee"]
                    qty[t["code"]] = held - n
                    cash = cash + n * t["price"] - t["fee"]
                    closed += 1
                    if realized > 0:
                        wins += 1
            ti += 1
        mv = 0.0
        for c, n in qty.items():
            if n <= 0:
                continue
            px = asof[c].get(d)
            if px is not None:
                mv += n * px
        curve.append({"date": d, "equity": round(cash + mv, 4)})
    metrics = _metrics_from_equity(initial_cash, curve, wins, closed)
    return {"curve": curve, "metrics": metrics}
