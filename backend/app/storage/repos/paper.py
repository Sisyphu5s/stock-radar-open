"""paper 域仓储（api/paper 收敛入口）：项目/观察快照/账户/持仓/委托/成交流水读写。

全部函数显式接收 db（api 由 Depends(get_db) 注入）；级联删除（账户/项目）在
同一 session 内完成（单事务），不拆多次会话。名称快照查询复用 repos/stocks。
"""

from __future__ import annotations

from ..models import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperProject,
    PaperProjectStock,
    PaperTrade,
    PaperWatchSnapshot,
    utcnow,
)
from . import stocks as _stock_repo


# ================= 项目（paper_projects / paper_project_stocks）=================


def list_projects(db) -> list[PaperProject]:
    """全部实验项目，updated_at 倒序。"""
    return db.query(PaperProject).order_by(PaperProject.updated_at.desc()).all()


def get_project(db, pid: int) -> PaperProject | None:
    """按 id 取项目，不存在返回 None。"""
    return db.get(PaperProject, pid)


def project_stock_rows(db, pid: int) -> list[PaperProjectStock]:
    """项目股票关联行（按 seq 排序）；旧单股项目无关联行 → 空列表。"""
    return (
        db.query(PaperProjectStock)
        .filter(PaperProjectStock.project_id == pid)
        .order_by(PaperProjectStock.seq)
        .all()
    )


def project_codes(db, p: PaperProject) -> list[str]:
    """项目股票代码序列：关联表优先，旧项目回退单 code 列（迁移兼容）。"""
    rows = project_stock_rows(db, p.id)
    return [r.code for r in rows] or [p.code]


def replace_project_stocks(db, pid: int, codes: list[str]) -> None:
    """整体替换项目股票关联行（名称取 DB 快照，查询失败空串）。"""
    db.query(PaperProjectStock).filter(PaperProjectStock.project_id == pid).delete()
    for i, c in enumerate(codes):
        db.add(PaperProjectStock(project_id=pid, code=c, name=_snapshot_name(c), seq=i))


def create_project(
    db,
    *,
    name: str,
    kind: str,
    code: str,
    period: str,
    signals: list[str],
    days: int,
    stock_codes: list[str],
) -> PaperProject:
    """创建项目 + 写股票关联行（同一事务）；stock_codes 已校验去重、以首股为 code。"""
    p = PaperProject(
        name=name,
        kind=kind,
        code=code,
        period=period,
        signals=signals,
        days=days,
    )
    db.add(p)
    db.flush()  # 先取 id 再写关联行
    replace_project_stocks(db, p.id, stock_codes)
    db.commit()
    db.refresh(p)
    return p


def save_project(db, p: PaperProject) -> PaperProject:
    """提交项目变更并刷新（属性由调用方赋值）。"""
    db.commit()
    db.refresh(p)
    return p


def delete_project(db, pid: int) -> None:
    """删除项目：观察快照 + 股票关联行 + 项目本身，同一事务级联。"""
    db.query(PaperWatchSnapshot).filter(PaperWatchSnapshot.project_id == pid).delete()
    db.query(PaperProjectStock).filter(PaperProjectStock.project_id == pid).delete()
    p = db.get(PaperProject, pid)
    if p is not None:
        db.delete(p)
    db.commit()


# ================= 观察快照（paper_watch_snapshots）=================


def latest_watch_snapshot(db, pid: int) -> PaperWatchSnapshot | None:
    """最近一条观察快照（ts 倒序），无则 None。"""
    return (
        db.query(PaperWatchSnapshot)
        .filter(PaperWatchSnapshot.project_id == pid)
        .order_by(PaperWatchSnapshot.ts.desc())
        .first()
    )


def add_watch_snapshot(
    db,
    *,
    project_id: int,
    ts,
    bar_date: str,
    prices: dict,
    hit_signals: list,
) -> None:
    """落一条观察快照并提交。"""
    db.add(
        PaperWatchSnapshot(
            project_id=project_id,
            ts=ts,
            bar_date=bar_date,
            prices=prices,
            hit_signals=hit_signals,
        )
    )
    db.commit()


def list_watch_snapshots(db, pid: int, limit: int) -> list[PaperWatchSnapshot]:
    """观察历史快照（时间升序，最近 limit 条）。"""
    rows = (
        db.query(PaperWatchSnapshot)
        .filter(PaperWatchSnapshot.project_id == pid)
        .order_by(PaperWatchSnapshot.ts.desc())
        .limit(max(1, limit))
        .all()
    )
    rows.reverse()  # 时间升序输出，便于直接画曲线
    return rows


# ================= 账户（paper_accounts / positions / orders / trades）=================


def list_accounts(db) -> list[PaperAccount]:
    """全部模拟盘账户（updated_at 倒序）。"""
    return db.query(PaperAccount).order_by(PaperAccount.updated_at.desc()).all()


def get_account(db, aid: int) -> PaperAccount | None:
    """按 id 取账户，不存在返回 None。"""
    return db.get(PaperAccount, aid)


def create_account(db, *, name: str, initial_cash: float) -> PaperAccount:
    """创建模拟盘账户（初始现金即可用现金）。"""
    acc = PaperAccount(name=name, initial_cash=initial_cash, cash=initial_cash)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def delete_account(db, aid: int) -> None:
    """删除账户：持仓 → 委托 → 成交 → 账户，同一事务级联清理。"""
    db.query(PaperPosition).filter(PaperPosition.account_id == aid).delete()
    db.query(PaperOrder).filter(PaperOrder.account_id == aid).delete()
    db.query(PaperTrade).filter(PaperTrade.account_id == aid).delete()
    db.query(PaperAccount).filter(PaperAccount.id == aid).delete()
    db.commit()


def list_orders(db, aid: int) -> list[PaperOrder]:
    """账户委托列表（created_at 倒序，新在前）。"""
    return (
        db.query(PaperOrder)
        .filter(PaperOrder.account_id == aid)
        .order_by(PaperOrder.created_at.desc(), PaperOrder.id.desc())
        .all()
    )


def get_order(db, aid: int, oid: int) -> PaperOrder | None:
    """按 (账户, 委托 id) 取委托，不存在返回 None。"""
    return (
        db.query(PaperOrder)
        .filter(PaperOrder.id == oid, PaperOrder.account_id == aid)
        .first()
    )


def cancel_order(db, aid: int, oid: int) -> PaperOrder | None:
    """撤单：仅 pending 委托可撤；委托不存在返回 None，非 pending 抛 ValueError。"""
    o = get_order(db, aid, oid)
    if o is None:
        return None
    if o.status != "pending":
        raise ValueError(f"仅挂起委托可撤单（当前状态 {o.status}）")
    o.status = "canceled"
    o.updated_at = utcnow()
    db.commit()
    db.refresh(o)
    return o


def list_positions(db, aid: int) -> list[PaperPosition]:
    """账户持仓（code 升序）。"""
    return (
        db.query(PaperPosition)
        .filter(PaperPosition.account_id == aid)
        .order_by(PaperPosition.code)
        .all()
    )


def list_trades(db, aid: int, limit: int) -> list[PaperTrade]:
    """账户成交流水（bar_date 倒序，新在前，最多 limit 条）。"""
    return (
        db.query(PaperTrade)
        .filter(PaperTrade.account_id == aid)
        .order_by(PaperTrade.bar_date.desc(), PaperTrade.id.desc())
        .limit(max(1, limit))
        .all()
    )


def list_trades_asc(db, aid: int) -> list[PaperTrade]:
    """账户成交流水（bar_date 升序，绩效重放顺序）。"""
    return (
        db.query(PaperTrade)
        .filter(PaperTrade.account_id == aid)
        .order_by(PaperTrade.bar_date, PaperTrade.id)
        .all()
    )


# ================= 内部辅助 =================


def _snapshot_name(code: str) -> str:
    """股票名称 DB 快照（查询失败空串，不阻塞写入）；先裸码后规范化码尝试。"""
    from ...lib.codes import normalize_code

    for c in (code, normalize_code(code)):
        try:
            name = _stock_repo.stock_name(c)
        except Exception:
            name = ""
        if name:
            return name
    return ""
