"""T-103 klines 独立库迁移专项测试。

覆盖迁移全场景（数据风险最高卡的专项验证）：
① 主库无 klines（新安装）→ 跳过，新库只建表不复制数据
② 主库有数据 → 分块复制 + 行数校验 + DROP 主库两表
③ 中断重跑（新库残留部分数据）→ 清空后全量重来，不重复不丢数据
④ 失败场景（新库不可写）→ 返回 False，主库原表与数据分毫不动

全部使用 tmp_path 双库（主库/新库均为临时文件）：monkeypatch
settings.klines_db_url + 重建 storage.klines_db.engine/klines_session，
不触碰生产库。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.config import settings
from app.storage import klines_db

_MAIN_META = {"ver:global": 7, "partial:600001:daily": 20260810}


def _make_main_engine(tmp_path, n_rows: int, meta: dict | None = None):
    """建 tmp 主库（结构同真实老库）：klines + cache_meta 表，插入 n_rows 行 K 线。

    返回 (engine, 期望 klines 行数, 期望 cache_meta 行数)。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE klines (id INTEGER PRIMARY KEY, code VARCHAR(12), "
                "period VARCHAR(8), source VARCHAR(12), date VARCHAR(20), "
                "open FLOAT, high FLOAT, low FLOAT, close FLOAT, "
                "volume FLOAT, amount FLOAT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, "
                "value INTEGER NOT NULL DEFAULT 0)"
            )
        )
        if n_rows:
            conn.execute(
                text(
                    "INSERT INTO klines (id, code, period, source, date, open, high, "
                    "low, close, volume, amount) VALUES (:id, :code, 'daily', 'test', "
                    ":date, :o, :h, :l, :c, :v, :a)"
                ),
                [
                    {
                        "id": i + 1,
                        "code": f"600{i:05d}",  # 每行唯一 code → (code, period, date) 不冲突
                        "date": f"2026-01-{i % 28 + 1:02d}",
                        "o": 10.0,
                        "h": 10.5,
                        "l": 9.8,
                        "c": 10.2,
                        "v": 100.0,
                        "a": 1e6,
                    }
                    for i in range(n_rows)
                ],
            )
    meta = dict(_MAIN_META if meta is None else meta)
    if meta:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO cache_meta (key, value) VALUES (:k, :v)"),
                [{"k": k, "v": v} for k, v in meta.items()],
            )
    return engine, n_rows, len(meta)


def _new_klines_engine(monkeypatch, tmp_path, name: str = "kline_cache.db"):
    """重建 klines_db 引擎指向 tmp 新库（monkeypatch settings + 模块级 engine/session）。"""
    url = f"sqlite:///{tmp_path / name}"
    monkeypatch.setattr(settings, "klines_db_url", url)
    new_engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(klines_db, "engine", new_engine)
    monkeypatch.setattr(
        klines_db,
        "klines_session",
        klines_db.sessionmaker(bind=new_engine, autoflush=False, autocommit=False),
    )
    return new_engine


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0


def test_skip_when_main_has_no_klines(monkeypatch, tmp_path):
    """① 新安装：主库无 klines 表 → 跳过，不复制不报错，新库只建表。"""
    main = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    with main.begin() as conn:  # 主库只有业务表，无 klines/cache_meta
        conn.execute(text("CREATE TABLE stocks (code VARCHAR(12) PRIMARY KEY)"))
    new = _new_klines_engine(monkeypatch, tmp_path)
    try:
        assert klines_db.migrate_klines(main_engine=main, klines_engine=new) is True
        # 新库表结构已建（ensure_schema）但无数据；主库从未有 klines 表
        assert _count(new, "klines") == 0
        assert _count(new, "cache_meta") == 0
        assert "klines" not in inspect(main).get_table_names()
    finally:
        main.dispose()
        new.dispose()


def test_skip_when_main_klines_empty(monkeypatch, tmp_path):
    """①' 新安装变体：主库有 klines 表但无数据 → 跳过，不 DROP 不报错。"""
    main, main_total, main_meta = _make_main_engine(tmp_path, 0)
    new = _new_klines_engine(monkeypatch, tmp_path)
    try:
        assert klines_db.migrate_klines(main_engine=main, klines_engine=new) is True
        # 主库 klines 空表原样保留（迁移不动它），新库无数据
        assert "klines" in inspect(main).get_table_names()
        assert _count(main, "klines") == 0
        assert _count(new, "klines") == 0
    finally:
        main.dispose()
        new.dispose()


def test_migrate_copy_verify_drop(monkeypatch, tmp_path):
    """② 老库升级：1500 行（>400 分块）→ 复制 + 行数校验通过 → DROP 主库两表。"""
    main, main_total, main_meta = _make_main_engine(tmp_path, 1500)
    new = _new_klines_engine(monkeypatch, tmp_path)
    try:
        assert klines_db.migrate_klines(main_engine=main, klines_engine=new) is True
        # 主库两表已 DROP（IF EXISTS 幂等）
        tables = inspect(main).get_table_names()
        assert "klines" not in tables
        assert "cache_meta" not in tables
        # 新库数据完整：行数与 cache_meta 键值逐一对应
        assert _count(new, "klines") == main_total
        assert _count(new, "cache_meta") == main_meta
        with new.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, code, period, source, date, close FROM klines WHERE id = :i"
                ),
                {"i": main_total},
            ).first()
            assert row is not None
            assert row[1].startswith("600") and row[2] == "daily" and row[3] == "test"
            keys = {
                r[0] for r in conn.execute(text("SELECT key FROM cache_meta")).all()
            }
            assert keys == set(_MAIN_META)
    finally:
        main.dispose()
        new.dispose()


def test_resume_after_interrupt_idempotent(monkeypatch, tmp_path):
    """③ 中断重跑：新库残留 100 行 → 清空重来，不重复不丢数据；再跑幂等跳过。"""
    main, main_total, main_meta = _make_main_engine(tmp_path, 1200)
    new = _new_klines_engine(monkeypatch, tmp_path)
    try:
        # 模拟上次迁移中断：新库已残留 100 行 klines
        klines_db.ensure_schema(new)
        with new.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO klines (id, code, period, source, date, open, high, "
                    "low, close, volume, amount) VALUES (:id, :code, 'daily', "
                    "'test', '2026-01-01', 10, 10.5, 9.8, 10.2, 100, 1e6)"
                ),
                [{"id": i + 1, "code": f"600{i:05d}"} for i in range(100)],
            )
        assert _count(new, "klines") == 100  # 残留确认
        assert klines_db.migrate_klines(main_engine=main, klines_engine=new) is True
        # 不重复不丢：新库恰好全量 1200 行（残留被清空重建）
        assert _count(new, "klines") == main_total
        assert _count(new, "cache_meta") == main_meta
        assert "klines" not in inspect(main).get_table_names()
        # 主库已无表后重跑 → 幂等跳过，返回 True 不报错
        assert klines_db.migrate_klines(main_engine=main, klines_engine=new) is True
    finally:
        main.dispose()
        new.dispose()


def test_failure_keeps_main_intact(monkeypatch, tmp_path):
    """④ 失败场景：新库路径不可写（父目录不存在）→ 返回 False，主库原表与数据不动。"""
    main, main_total, main_meta = _make_main_engine(tmp_path, 500)
    url = f"sqlite:///{tmp_path / 'no_such_dir' / 'kline_cache.db'}"
    monkeypatch.setattr(settings, "klines_db_url", url)
    bad = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(klines_db, "engine", bad)
    monkeypatch.setattr(
        klines_db,
        "klines_session",
        klines_db.sessionmaker(bind=bad, autoflush=False, autocommit=False),
    )
    try:
        assert klines_db.migrate_klines(main_engine=main, klines_engine=bad) is False
        # 主库 klines/cache_meta 原表与数据分毫未动
        assert "klines" in inspect(main).get_table_names()
        assert "cache_meta" in inspect(main).get_table_names()
        assert _count(main, "klines") == main_total
        assert _count(main, "cache_meta") == main_meta
    finally:
        main.dispose()
        bad.dispose()


def test_drop_completion_after_previous_success(monkeypatch, tmp_path):
    """⑤ 前次迁移已完整复制（仅 DROP 未完成）→ 重跑补 DROP，不重复复制。"""
    main, main_total, main_meta = _make_main_engine(tmp_path, 800)
    new = _new_klines_engine(monkeypatch, tmp_path)
    try:
        assert klines_db.migrate_klines(main_engine=main, klines_engine=new) is True
        # 手动恢复主库表（模拟 DROP 前崩溃 + 从备份还原主库，新库数据仍在）：
        main.dispose()
        main, main_total, main_meta = _make_main_engine(tmp_path, 800)
        # 重跑 → 新库行数 == 主库行数 → 判定已迁移完整 → 只补 DROP，不重复复制
        assert klines_db.migrate_klines(main_engine=main, klines_engine=new) is True
        assert "klines" not in inspect(main).get_table_names()
        assert _count(new, "klines") == main_total  # 数据未被二次复制（仍 800）
    finally:
        main.dispose()
        new.dispose()
