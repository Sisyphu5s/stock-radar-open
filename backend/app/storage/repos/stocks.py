"""Stock 仓储查询/辅助（api/market、api/signals 名称回填、api/paper 名称查询收敛入口）。

全部函数显式接收 db（api 由 Depends(get_db) 注入）；自建会话的函数
（db=None）走 `..db`（storage 层）——测试 patch app.storage.db.SessionLocal 生效。
"""

from __future__ import annotations

from sqlalchemy import func

from ...lib.codes import in_chunks
from ..models import Stock, WatchlistGroup, WatchlistGroupItem


def get_stock(db, code: str) -> Stock | None:
    """按主键取股票行，不存在返回 None。"""
    return db.get(Stock, code)


def stock_names(db, codes) -> dict[str, str]:
    """股票代码 → 名称映射（分批 IN 防 SQLite 变量上限）。"""
    name_map: dict[str, str] = {}
    for chunk in in_chunks(sorted(codes)):
        name_map.update(
            {
                s.code: s.name
                for s in db.query(Stock).filter(Stock.code.in_(chunk)).all()
            }
        )
    return name_map


def watchlist_codes(db) -> set[str]:
    """全部自选股代码集合（is_watchlist）。"""
    return {s.code for s in db.query(Stock).filter(Stock.is_watchlist.is_(True)).all()}


def watchlist_stocks(db) -> list:
    """全部自选股 ORM 行（is_watchlist）。"""
    return db.query(Stock).filter(Stock.is_watchlist.is_(True)).all()


def add_watchlist(db, code: str) -> None:
    """加入自选：不存在则建行（name=code），置 is_watchlist=True 并提交。"""
    stock = db.get(Stock, code)
    if stock is None:
        stock = Stock(code=code, name=code)
        db.add(stock)
    stock.is_watchlist = True
    db.commit()


def remove_watchlist(db, code: str) -> None:
    """移出自选：存在则置 is_watchlist=False 并提交（不删行）。"""
    stock = db.get(Stock, code)
    if stock is not None:
        stock.is_watchlist = False
        db.commit()


def stock_name(code: str, db=None) -> str:
    """按代码查股票名称；无行/名称为空返回空串（失败静默，不阻塞调用方）。"""
    own = db is None
    if own:
        from ..db import SessionLocal

        db = SessionLocal()
    try:
        row = db.query(Stock).filter(Stock.code == code).first()
        return row.name if row is not None and row.name else ""
    finally:
        if own:
            db.close()


# ===== 自选股分组（T-12）=====
# 约定：名称空/重名 → ValueError；分组 id 不存在 → KeyError（端点映射 400/404）。


def watchlist_groups(db) -> list:
    """全部分组，sort_order 升序（同序按 id）。"""
    return (
        db.query(WatchlistGroup)
        .order_by(WatchlistGroup.sort_order, WatchlistGroup.id)
        .all()
    )


def get_watchlist_group(db, group_id: int) -> WatchlistGroup | None:
    """按 id 取分组，不存在返回 None。"""
    return db.get(WatchlistGroup, group_id)


def create_watchlist_group(db, name: str) -> WatchlistGroup:
    """新建分组：名称去空格后非空且全库唯一；sort_order = 现有最大值 + 1（追加到末尾）。"""
    name = name.strip()
    if not name:
        raise ValueError("分组名称不能为空")
    if db.query(WatchlistGroup).filter(WatchlistGroup.name == name).first() is not None:
        raise ValueError(f"分组「{name}」已存在")
    max_order = db.query(func.max(WatchlistGroup.sort_order)).scalar() or 0
    g = WatchlistGroup(name=name, sort_order=max_order + 1)
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


def rename_watchlist_group(db, group_id: int, name: str) -> WatchlistGroup:
    """重命名分组：名称校验同创建；重名（排除自身）抛 ValueError。"""
    name = name.strip()
    if not name:
        raise ValueError("分组名称不能为空")
    g = db.get(WatchlistGroup, group_id)
    if g is None:
        raise KeyError(f"分组不存在: {group_id}")
    dup = (
        db.query(WatchlistGroup)
        .filter(WatchlistGroup.name == name, WatchlistGroup.id != group_id)
        .first()
    )
    if dup is not None:
        raise ValueError(f"分组「{name}」已存在")
    g.name = name
    db.commit()
    db.refresh(g)
    return g


def delete_watchlist_group(db, group_id: int) -> None:
    """删除分组并连带删除其全部 items（显式删除，不依赖 FK 级联：测试临时库未开 foreign_keys）。"""
    g = db.get(WatchlistGroup, group_id)
    if g is None:
        raise KeyError(f"分组不存在: {group_id}")
    db.query(WatchlistGroupItem).filter(
        WatchlistGroupItem.group_id == group_id
    ).delete()
    db.delete(g)
    db.commit()


def group_item_codes(db, group_id: int) -> set[str]:
    """分组内股票代码集合（空组返回空集）。"""
    return {
        i.code
        for i in db.query(WatchlistGroupItem)
        .filter(WatchlistGroupItem.group_id == group_id)
        .all()
    }


def group_items_stocks(db, group_id: int) -> list:
    """分组内股票 ORM 行（按代码排序；未知代码行缺失时静默跳过）。"""
    codes = group_item_codes(db, group_id)
    rows: list = []
    for chunk in in_chunks(sorted(codes)):
        rows.extend(db.query(Stock).filter(Stock.code.in_(chunk)).all())
    return rows


def add_group_items(db, group_id: int, codes) -> int:
    """批量加入分组（幂等：同组已有跳过），返回新增条数。codes 须已规范化。"""
    if db.get(WatchlistGroup, group_id) is None:
        raise KeyError(f"分组不存在: {group_id}")
    existing = group_item_codes(db, group_id)
    added = 0
    for code in sorted(set(codes)):
        if code in existing:
            continue
        db.add(WatchlistGroupItem(group_id=group_id, code=code))
        existing.add(code)
        added += 1
    db.commit()
    return added


def remove_group_item(db, group_id: int, code: str) -> bool:
    """从分组移出股票；返回是否实际删除（不在组内返回 False，幂等）。"""
    row = (
        db.query(WatchlistGroupItem)
        .filter(
            WatchlistGroupItem.group_id == group_id,
            WatchlistGroupItem.code == code,
        )
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
