"""自选股分组/标签 + CSV 导入导出（T-12）聚焦测试。

覆盖：
1. 模型：分组建表、成员唯一约束 (group_id, code) 幂等语义（repo 层跳过 + 模型层约束）。
2. 分组 CRUD 端点：create/rename/delete（连带 items 清理）；重名 400、不存在 404。
3. 成员端点：批量移入幂等（重复 added=0）、移出、明细。
4. 导入幂等：POST /watchlist/import 已关注跳过；无效代码忽略计数。
5. 导出内容：CSV header/行字段/sector/分组列/UTF-8 BOM。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

import csv
from io import StringIO

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import Stock, WatchlistGroup, WatchlistGroupItem, utcnow
from app.storage.repos import stocks as repo
from app.api.market import (
    add_group_items,
    create_watchlist_group,
    delete_watchlist_group,
    export_watchlist_csv,
    get_group_items,
    get_watchlist_groups,
    import_watchlist,
    remove_group_item,
    rename_watchlist_group,
)


@pytest.fixture()
def temp_db(tmp_path):
    """独立临时 DB（直接传 db 给路由函数，不触碰生产库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield Maker
    engine.dispose()


def _seed(db):
    """两只关注股票 + 一只非关注。"""
    for code, name, wl in [
        ("600519.SH", "贵州茅台", True),
        ("000001.SZ", "平安银行", True),
        ("300750.SZ", "宁德时代", False),
    ]:
        db.add(
            Stock(
                code=code,
                name=name,
                is_watchlist=wl,
                industry="白酒"
                if code == "600519.SH"
                else "银行"
                if code == "000001.SZ"
                else "电池",
                updated_at=utcnow(),
            )
        )
    db.commit()


def _mk_group(db, name: str) -> WatchlistGroup:
    return repo.create_watchlist_group(db, name)


# ---------------------------------------------------------------------------
# 模型：建表 + 唯一约束
# ---------------------------------------------------------------------------


def test_model_unique_constraint_group_item(temp_db):
    db = temp_db()
    try:
        g = _mk_group(db, "核心仓")
        db.add(WatchlistGroupItem(group_id=g.id, code="600519.SH"))
        db.commit()
        # 同组同股二次插入 → 唯一约束拒绝
        db.add(WatchlistGroupItem(group_id=g.id, code="600519.SH"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 不同组同股允许
        g2 = _mk_group(db, "观察仓")
        db.add(WatchlistGroupItem(group_id=g2.id, code="600519.SH"))
        db.commit()
        assert repo.group_item_codes(db, g2.id) == {"600519.SH"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 分组 CRUD：创建 / 重命名 / 删除（连带 items）
# ---------------------------------------------------------------------------


def test_group_create_rename_delete(temp_db):
    db = temp_db()
    try:
        _seed(db)
        g = create_watchlist_group({"name": "  核心仓  "}, db=db)["data"]
        assert g["name"] == "核心仓"
        assert g["sort_order"] == 1 and g["count"] == 0

        add_group_items(g["id"], {"codes": ["600519.SH", "000001.SZ"]}, db=db)
        # 重命名
        renamed = rename_watchlist_group(g["id"], {"name": "长期仓"}, db=db)["data"]
        assert renamed["name"] == "长期仓"
        assert renamed["count"] == 2

        # 删除分组 → items 连带清理
        delete_watchlist_group(g["id"], db=db)
        assert db.query(WatchlistGroupItem).count() == 0
        assert get_watchlist_groups(db=db)["data"] == []
    finally:
        db.close()


def test_group_name_conflict_and_validation(temp_db):
    db = temp_db()
    try:
        _mk_group(db, "核心仓")
        with pytest.raises(Exception) as e:
            create_watchlist_group({"name": "核心仓"}, db=db)
        assert e.value.status_code == 400
        with pytest.raises(Exception) as e:
            create_watchlist_group({"name": "  "}, db=db)
        assert e.value.status_code == 400
        # 重命名撞名
        g2 = _mk_group(db, "观察仓")
        with pytest.raises(Exception) as e:
            rename_watchlist_group(g2.id, {"name": "核心仓"}, db=db)
        assert e.value.status_code == 400
        # 不存在分组 → 404
        with pytest.raises(Exception) as e:
            delete_watchlist_group(999, db=db)
        assert e.value.status_code == 404
        with pytest.raises(Exception) as e:
            rename_watchlist_group(999, {"name": "x"}, db=db)
        assert e.value.status_code == 404
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 成员：批量移入幂等 / 移出 / 明细
# ---------------------------------------------------------------------------


def test_group_items_idempotent_and_remove(temp_db):
    db = temp_db()
    try:
        _seed(db)
        g = _mk_group(db, "核心仓")
        r1 = add_group_items(
            g.id, {"codes": ["600519", "000001.SZ", "600519.SH"]}, db=db
        )
        assert r1["added"] == 2  # 600519 与 600519.SH 规范化后同码，去重
        assert r1["codes"] == ["000001.SZ", "600519.SH"]
        # 重复移入 → added 0（幂等）
        r2 = add_group_items(g.id, {"codes": ["600519.SH"]}, db=db)
        assert r2["added"] == 0

        # 明细：仅返回关注列表结构字段（分组内股票 600519/000001 均关注）
        items = get_group_items(g.id, db=db)["data"]
        assert [x["code"] for x in items] == ["000001.SZ", "600519.SH"]
        assert items[1]["name"] == "贵州茅台" and items[1]["industry"] == "白酒"

        # 移出（幂等：不在组内返回 ok）
        assert remove_group_item(g.id, "600519.SH", db=db)["ok"] is True
        assert remove_group_item(g.id, "600519.SH", db=db)["ok"] is True
        assert repo.group_item_codes(db, g.id) == {"000001.SZ"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 导入：幂等 + 无效代码忽略
# ---------------------------------------------------------------------------


def test_import_watchlist_idempotent(temp_db):
    db = temp_db()
    try:
        _seed(db)  # 600519.SH / 000001.SZ 已关注
        r1 = import_watchlist(
            {
                "codes": [
                    "600519.SH",
                    "300750.SZ",
                    "601318",
                    "not-a-code",
                    "",
                    "688981.SH",
                ]
            },
            db=db,
        )
        assert r1["total"] == 4  # 有效：600519/300750/601318/688981
        assert r1["invalid"] == 2  # not-a-code + 空串
        assert r1["added"] == 3  # 600519 已关注不算新增
        assert repo.watchlist_codes(db) == {
            "600519.SH",
            "000001.SZ",
            "300750.SZ",
            "601318.SH",
            "688981.SH",
        }
        # 重复导入 → added 0
        r2 = import_watchlist({"codes": ["300750.SZ", "601318.SH"]}, db=db)
        assert r2["added"] == 0 and r2["total"] == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CSV 导出：header / 行字段 / 分组列 / BOM
# ---------------------------------------------------------------------------


def test_export_csv_content(temp_db):
    db = temp_db()
    try:
        _seed(db)
        g1 = _mk_group(db, "核心仓")
        g2 = _mk_group(db, "白酒")
        add_group_items(g1.id, {"codes": ["600519.SH"]}, db=db)
        add_group_items(g2.id, {"codes": ["600519.SH", "000001.SZ"]}, db=db)

        resp = export_watchlist_csv(db=db)
        raw = resp.body.decode("utf-8")
        assert resp.headers["content-type"].startswith("text/csv")
        assert raw.startswith("\ufeff")  # UTF-8 BOM

        rows = list(csv.reader(StringIO(raw.lstrip("\ufeff"))))
        assert rows[0] == ["code", "name", "sector", "分组"]
        by_code = {r[0]: r for r in rows[1:]}
        assert set(by_code) == {"600519.SH", "000001.SZ"}  # 仅关注股票，不含 300750
        assert by_code["600519.SH"][1] == "贵州茅台"
        assert by_code["600519.SH"][2] == "白酒"  # sector = industry
        assert set(by_code["600519.SH"][3].split(" | ")) == {
            "核心仓",
            "白酒",
        }  # 多组以「 | 」连接
        assert by_code["000001.SZ"][3] == "白酒"
    finally:
        db.close()
