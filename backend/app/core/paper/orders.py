"""模拟盘委托撮合编排服务（P1-53b：撮合编排自 api 层抽离）。

撮合编排（读账户/持仓 → execute_order 纯计算 → 条件写回 → 落 order/position/trade
→ commit）由本服务承担，api 层只做参数校验 + 调服务 + 序列化。

下单入口「取 K 线 → prepare → resolve_bar_price → place_order_service」也归属
本模块（place_order_bar），api 层不再直接触碰 K 线缓存与 bar 解析。

并发防护（P0-29，自 api/paper.py place_order 完整搬移，语义不变）：
同账户并发下单时资金写回走条件更新（WHERE cash=旧值），冲突则重读重算
（最多 MAX_PLACE_RETRIES 次），极端并发下整单拒绝，杜绝资金/持仓双花。
SQLite 单写者保证「条件更新 → 持仓落库 → commit」整个事务原子，无需进程内锁。

撮合语义与 core/paper/account.py 一致：市价按 bar 价全额成交；限价按委托价
触发条件成交，否则挂起；触发但资金/持仓不足 → 整单拒绝（status=rejected）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...storage.klines import cached_kline
from ...storage.models import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperTrade,
    utcnow,
)
from .account import execute_order, prepare_kline, resolve_bar_price

# 下单并发防护：cash 条件写回（WHERE cash=旧值）失败时的重读重算次数上限
MAX_PLACE_RETRIES = 3


def place_order_bar(
    code: str,
    period: str,
    bar_date: str | None = None,
    max_rows: int = 800,
) -> tuple[float, str] | None:
    """下单入口：取 K 线 → prepare → 解析撮合 bar，返回 (bar 收盘价, bar 日期)。

    行情不可用返回 None（api 层转 404）；指定 bar 时点无可用行情抛 ValueError
    （api 层转 422）。撮合判定与写库由 place_order_service 承担。
    """
    k = cached_kline(code, period, max_rows=max_rows)
    if k is None or k.empty:
        return None
    k = prepare_kline(k)
    return resolve_bar_price(k, bar_date)


def place_order_service(
    db: Session,
    aid: int,
    order: dict,
    bar_price: float,
    bar_date: str,
) -> PaperOrder:
    """撮合编排：读账户/持仓 → execute_order 纯计算 → 条件写回 → 落库，返回已提交的委托。

    order: {code, side, order_type, price, quantity}（api 层已完成参数校验与 bar 解析）；
    bar_price/bar_date 由 api 层经 resolve_bar_price 解析传入。
    账户不存在抛 ValueError（api 层转 404）；撮合结果经委托状态表达
    （filled/pending/rejected），本服务不抛业务异常。
    """
    acc = db.get(PaperAccount, aid)
    if acc is None:
        raise ValueError(f"账户 {aid} 不存在")
    code = order["code"]

    # 并发防护临界区：读 cash/持仓 → execute_order 纯计算 → 条件写回 cash。
    # 同账户两路并发下单时，后写者的条件更新（WHERE cash=旧值）rowcount=0，重读最新
    # 资金/持仓重算——资金不足自然转 rejected，杜绝资金/持仓双花。
    positions = {
        r.code: r
        for r in db.query(PaperPosition).filter(PaperPosition.account_id == aid).all()
    }
    o = PaperOrder(
        account_id=aid,
        code=code,
        side=order["side"],
        order_type=order["order_type"],
        price=order["price"] if order["order_type"] == "limit" else None,
        quantity=order["quantity"],
        bar_date=bar_date,
        status="pending",
    )
    for _ in range(MAX_PLACE_RETRIES):
        pos_state = {
            c: {"quantity": r.quantity, "avg_cost": r.avg_cost}
            for c, r in positions.items()
        }
        result, new_cash, new_pos, _realized = execute_order(
            acc.cash, pos_state, order, bar_price
        )
        if result["status"] != "filled":
            break  # 挂起/拒绝不写资金，无需条件更新
        updated = (
            db.query(PaperAccount)
            .filter(PaperAccount.id == aid, PaperAccount.cash == acc.cash)
            .update({"cash": new_cash})
        )
        if updated:
            break  # 条件写回成功；持仓落库随 commit 一并生效（事务内原子）
        # 资金已被并发修改：放弃本次计算，重读最新账户与持仓再算
        db.rollback()
        acc = db.get(PaperAccount, aid)
        positions = {
            r.code: r
            for r in db.query(PaperPosition)
            .filter(PaperPosition.account_id == aid)
            .all()
        }
    else:
        # 重试耗尽仍冲突（极端并发）：拒绝本单，资金/持仓保持 DB 最新值不写入
        result = {
            "status": "rejected",
            "price": None,
            "fee": 0.0,
            "reason": "账户资金并发变动，请重试",
        }

    db.add(o)
    if result["status"] == "filled":
        db.flush()  # 先取 order.id 再写成交
        o.status = "filled"
        o.filled_qty = order["quantity"]
        o.filled_avg_price = result["price"]
        for c, ps in new_pos.items():
            if c in positions:
                r = positions[c]
                r.quantity = ps["quantity"]
                r.avg_cost = ps["avg_cost"]
            else:
                db.add(
                    PaperPosition(
                        account_id=aid,
                        code=c,
                        quantity=ps["quantity"],
                        avg_cost=ps["avg_cost"],
                    )
                )
        for c, r in positions.items():
            if c not in new_pos:
                db.delete(r)
        db.add(
            PaperTrade(
                account_id=aid,
                order_id=o.id,
                code=code,
                side=order["side"],
                price=result["price"],
                quantity=order["quantity"],
                amount=round(result["price"] * order["quantity"], 2),
                fee=result["fee"],
                bar_date=bar_date,
            )
        )
    elif result["status"] == "rejected":
        o.status = "rejected"
        o.reject_reason = result["reason"]
    else:
        o.status = "pending"  # 限价未触发，挂起待撤
    acc.updated_at = utcnow()
    db.commit()
    db.refresh(o)
    return o
