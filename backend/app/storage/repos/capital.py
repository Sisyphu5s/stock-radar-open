"""资金类历史仓储(T-08):四类数据按类型 upsert/查询,数据驱动注册表。

全部函数显式接收 db(api 由 Depends(get_db) 注入);自建会话的函数
(db=None)走 `..db`(storage 层)——测试 patch app.storage.db.SessionLocal 生效。
批量写统一分块(in_chunks 默认 400 行/批),防 SQLite `too many SQL variables`。
唯一键:moneyflow/margin/northbound 为 (code, date);lhb 为 (code, date, reason)
(同一股票同一交易日可能多次上榜,以 reason 区分)。
"""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert

from ...lib.codes import in_chunks
from ..models import (
    CapitalLhb,
    CapitalMargin,
    CapitalMoneyFlow,
    CapitalNorthbound,
    utcnow,
)

# 类型 → ORM 模型(数据驱动:端点 type 参数与 API 层序列化共用)
CAPITAL_MODELS: dict[str, type] = {
    "moneyflow": CapitalMoneyFlow,
    "lhb": CapitalLhb,
    "margin": CapitalMargin,
    "northbound": CapitalNorthbound,
}

# 非业务公共列:upsert 排除(id 自增 / code 主键维度 / 更新时间自填)
_COMMON = ("id", "code", "updated_at")
# 各类型日期列(唯一键组成,不入 set_ 覆盖)
_DATE_COLS = {
    "moneyflow": "date",
    "lhb": "date",
    "margin": "date",
    "northbound": "date",
}
# 各类型唯一键(除 (code) 外的列)
_UNIQUE_COLS = {
    "moneyflow": ("date",),
    "lhb": ("date", "reason"),
    "margin": ("date",),
    "northbound": ("date",),
}


def _session(db):
    """db 为 None 时自建会话;返回 (session, 是否自建)。调用方在 finally 关闭自建会话。"""
    if db is not None:
        return db, False
    from ..db import SessionLocal

    return SessionLocal(), True


def _fields(model: type, date_col: str) -> list[str]:
    """模型业务字段(排除 id/code/日期列/updated_at)。"""
    return [
        c.name
        for c in model.__table__.columns
        if c.name not in _COMMON and c.name != date_col
    ]


def upsert_capital(db, code: str, ctype: str, rows: list[dict]) -> int:
    """批量 upsert 资金类行(rows 每项含 date + 业务字段,date 为 YYYY-MM-DD);
    幂等,返回写入行数。lhb 行额外含 reason(唯一键三列)。"""
    model = CAPITAL_MODELS[ctype]
    date_col = _DATE_COLS[ctype]
    unique_cols = _UNIQUE_COLS[ctype]
    table = model.__table__
    fields = _fields(model, date_col)
    s, own = _session(db)
    try:
        written = 0
        for chunk in in_chunks(rows):
            payload = []
            for r in chunk:
                payload.append(
                    {
                        "code": code,
                        date_col: r[date_col],
                        **{f: r.get(f) for f in fields},
                    }
                )
            stmt = insert(table).values(payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", *unique_cols],
                set_={f: stmt.excluded[f] for f in fields} | {"updated_at": utcnow()},
            )
            s.execute(stmt)
            written += len(payload)
        s.commit()
        return written
    finally:
        if own:
            s.close()


def list_capital(db, code: str, ctype: str, limit: int = 60) -> list:
    """资金类历史(日期倒序,最多 limit 条;lhb 同日多次上榜按 reason 排序稳定)。"""
    model = CAPITAL_MODELS[ctype]
    s, own = _session(db)
    try:
        q = s.query(model).filter(model.code == code)
        if ctype == "lhb":
            q = q.order_by(model.date.desc(), model.reason.asc())  # type: ignore[attr-defined]
        else:
            q = q.order_by(model.date.desc())
        return q.limit(max(1, limit)).all()
    finally:
        if own:
            s.close()


def latest_capital_date(db, code: str, ctype: str) -> str | None:
    """最近一个交易日(YYYY-MM-DD);无数据返回 None。"""
    model = CAPITAL_MODELS[ctype]
    s, own = _session(db)
    try:
        row = (
            s.query(model)
            .filter(model.code == code)
            .order_by(model.date.desc())
            .first()
        )
        return row.date if row is not None else None
    finally:
        if own:
            s.close()


def latest_capital(db, code: str, ctype: str):
    """最近一行;无数据返回 None。"""
    model = CAPITAL_MODELS[ctype]
    s, own = _session(db)
    try:
        return (
            s.query(model)
            .filter(model.code == code)
            .order_by(model.date.desc())
            .first()
        )
    finally:
        if own:
            s.close()
