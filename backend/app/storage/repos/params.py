"""AppParam 仓储查询（api/indicators 全局参数读取收敛入口）。

自建会话（db=None）走 `..db`（storage 层）——测试 patch app.storage.db.SessionLocal 生效。
"""

from __future__ import annotations

from ..models import AppParam


def app_params_like(prefix: str, db=None) -> list:
    """AppParam 行按 key LIKE 'prefix%' 查询（调用方自取列）。"""
    own = db is None
    if own:
        from ..db import SessionLocal

        db = SessionLocal()
    try:
        return db.query(AppParam).filter(AppParam.key.like(f"{prefix}%")).all()
    finally:
        if own:
            db.close()


def app_param_exists(key: str, db=None) -> bool:
    """AppParam 单键存在性（用于全局参数已保存标记）。"""
    own = db is None
    if own:
        from ..db import SessionLocal

        db = SessionLocal()
    try:
        return db.get(AppParam, key) is not None
    finally:
        if own:
            db.close()


def latest_indicator_params(db=None) -> dict[str, str]:
    """AppParam 中 ind:{indicator}:{target} 行 → 每指标取最新（updated_at 最大）一个参数值。

    updated_at 相同按 key 升序取更小者（与 api 现实现一致）。
    """
    best: dict[str, list] = {}
    for r in app_params_like("ind:%", db):
        parts = r.key.split(":")
        if len(parts) < 2 or parts[0] != "ind":
            continue
        ind = parts[1]
        cur = best.get(ind)
        if (
            cur is None
            or r.updated_at > cur[1]
            or (r.updated_at == cur[1] and r.key < cur[2])
        ):
            best[ind] = [r.value, r.updated_at, r.key]
    return {k: v[0] for k, v in best.items()}
