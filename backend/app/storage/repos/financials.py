"""财务历史仓储（T-06）：三表按报告期 upsert/查询 + 每日估值 upsert/查询。

全部函数显式接收 db（api 由 Depends(get_db) 注入）；自建会话的函数
（db=None）走 `..db`（storage 层）——测试 patch app.storage.db.SessionLocal 生效。
批量写统一分块（in_chunks 默认 400 行/批），防 SQLite `too many SQL variables`。
"""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert

from ...lib.codes import in_chunks
from ..models import (
    FinancialsBalance,
    FinancialsCash,
    FinancialsIncome,
    ValuationHistory,
    utcnow,
)

# 报表类型 → ORM 模型（数据驱动：端点 type 参数与 API 层序列化共用）
REPORT_MODELS: dict[str, type] = {
    "balance": FinancialsBalance,
    "income": FinancialsIncome,
    "cash": FinancialsCash,
}

# 三表非业务公共列：upsert 排除（id 自增 / code 主键维度 / 更新时间自填）
_COMMON = ("id", "code", "report_date", "updated_at")


def _session(db):
    """db 为 None 时自建会话；返回 (session, 是否自建)。调用方在 finally 关闭自建会话。"""
    if db is not None:
        return db, False
    from ..db import SessionLocal

    return SessionLocal(), True


def _fields(model: type) -> list[str]:
    """模型业务字段（排除 id/code/report_date/updated_at）。"""
    return [c.name for c in model.__table__.columns if c.name not in _COMMON]


def upsert_financials(db, code: str, report_type: str, rows: list[dict]) -> int:
    """批量 upsert 三表行（rows 每项含 report_date + 业务字段）；幂等，返回写入行数。

    唯一键 (code, report_date)：同报告期已存在 → 全字段覆盖（数据修正后刷新）。
    """
    model = REPORT_MODELS[report_type]
    table = model.__table__
    fields = _fields(model)
    s, own = _session(db)
    try:
        written = 0
        for chunk in in_chunks(rows):
            payload = []
            for r in chunk:
                payload.append(
                    {
                        "code": code,
                        "report_date": r["report_date"],
                        **{f: r.get(f) for f in fields},
                    }
                )
            stmt = insert(table).values(payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "report_date"],
                set_={f: stmt.excluded[f] for f in fields} | {"updated_at": utcnow()},
            )
            s.execute(stmt)
            written += len(payload)
        s.commit()
        return written
    finally:
        if own:
            s.close()


def list_financials(db, code: str, report_type: str, limit: int = 40) -> list:
    """三表历史（报告期倒序，最多 limit 条）。"""
    model = REPORT_MODELS[report_type]
    s, own = _session(db)
    try:
        return (
            s.query(model)
            .filter(model.code == code)
            .order_by(model.report_date.desc())
            .limit(max(1, limit))
            .all()
        )
    finally:
        if own:
            s.close()


def latest_financials(db, code: str, report_type: str):
    """最近一期财务行；无数据返回 None。"""
    model = REPORT_MODELS[report_type]
    s, own = _session(db)
    try:
        return (
            s.query(model)
            .filter(model.code == code)
            .order_by(model.report_date.desc())
            .first()
        )
    finally:
        if own:
            s.close()


def latest_financial_date(db, code: str, report_type: str) -> str | None:
    """最近一期报告期（YYYY-MM-DD）；无数据返回 None。"""
    row = latest_financials(db, code, report_type)
    return row.report_date if row is not None else None


def upsert_valuation(db, code: str, rows: list[dict]) -> int:
    """批量 upsert 每日估值（rows 每项含 date + pe/pb/ps/total_mv/float_mv）；唯一键 (code, date)。"""
    table = ValuationHistory.__table__
    fields = [
        c.name for c in table.columns if c.name not in _COMMON and c.name != "date"
    ]
    s, own = _session(db)
    try:
        written = 0
        for chunk in in_chunks(rows):
            payload = []
            for r in chunk:
                payload.append(
                    {"code": code, "date": r["date"], **{f: r.get(f) for f in fields}}
                )
            stmt = insert(table).values(payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "date"],
                set_={f: stmt.excluded[f] for f in fields} | {"updated_at": utcnow()},
            )
            s.execute(stmt)
            written += len(payload)
        s.commit()
        return written
    finally:
        if own:
            s.close()


def list_valuation(db, code: str, limit: int = 250) -> list:
    """估值历史（日期倒序，最多 limit 条）。"""
    s, own = _session(db)
    try:
        return (
            s.query(ValuationHistory)
            .filter(ValuationHistory.code == code)
            .order_by(ValuationHistory.date.desc())
            .limit(max(1, limit))
            .all()
        )
    finally:
        if own:
            s.close()


def latest_valuation_date(db, code: str) -> str | None:
    """最近一个估值交易日（YYYY-MM-DD）；无数据返回 None。"""
    s, own = _session(db)
    try:
        row = (
            s.query(ValuationHistory)
            .filter(ValuationHistory.code == code)
            .order_by(ValuationHistory.date.desc())
            .first()
        )
        return row.date if row is not None else None
    finally:
        if own:
            s.close()
