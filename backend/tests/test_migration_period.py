"""signal_events.period 列迁移契约测试。

覆盖（任务卡）：
1. 新库 create_all + _migrate 后：period 列存在、NOT NULL、默认值 daily。
2. 旧库（无 period 列）执行 _migrate：ALTER 补列，默认值 daily，旧数据保留且回填 daily。
3. 幂等：重复执行 _migrate 不报错，period 列与索引保持唯一。
4. 迁移末尾统一建 ix_signal_events_period 索引。
临时 SQLite，不触碰生产库（monkeypatch database.engine 指向临时库）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, inspect, text

import app.storage.db as DB
from app.storage.db import Base

# 确保所有模型注册进 Base.metadata（create_all 建全量表）；仅副作用导入
import app.storage.models as _m  # noqa: F401


@pytest.fixture()
def temp_engine(tmp_path, monkeypatch):
    """独立临时 DB：database.engine 替换为临时引擎（_migrate 作用于临时库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mig.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    monkeypatch.setattr(DB, "engine", engine)
    yield engine
    engine.dispose()


def _signal_event_cols(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("signal_events")}


def test_new_db_period_column_with_daily_default(temp_engine):
    """新库 create_all + _migrate 后 signal_events 含 period 列，默认值 daily。"""
    Base.metadata.create_all(bind=temp_engine)
    DB._migrate()
    cols = _signal_event_cols(temp_engine)
    assert "period" in cols
    assert cols["period"]["nullable"] is False
    # SQLite PRAGMA dflt_value 保留 SQL 字面量引号
    assert cols["period"]["default"] == "'daily'"


def test_migrate_adds_period_to_legacy_db(temp_engine):
    """旧库（无 period 列）执行 _migrate：ALTER 补列，旧数据保留且回填 daily。

    注：CREATE TABLE 保留 template_id/template_name/resonance_score 旧列
    （模拟迁移前真实旧库结构）——_migrate 的去模板化迁移块会 DROP 这三列，
    迁移后断言其已不存在（列名字符串仅作为旧库结构模拟保留）。
    """
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE signal_events (
                    id INTEGER PRIMARY KEY,
                    stock_code VARCHAR(12),
                    template_id INTEGER,
                    template_name VARCHAR(64),
                    signals JSON,
                    resonance_score FLOAT,
                    status VARCHAR(16),
                    evidence JSON,
                    triggered_at DATETIME,
                    as_of DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO signal_events (stock_code, template_id, template_name, signals) "
                "VALUES ('600519.SH', 1, '涨跌', '[\"price_up\"]')"
            )
        )
    DB._migrate()
    cols = _signal_event_cols(temp_engine)
    assert "period" in cols
    assert cols["period"]["nullable"] is False
    assert cols["period"]["default"] == "'daily'"
    # 去模板化迁移：旧库模板三列被删除
    assert "template_id" not in cols
    assert "template_name" not in cols
    assert "resonance_score" not in cols
    with temp_engine.connect() as conn:
        row = conn.execute(text("SELECT stock_code, period FROM signal_events")).one()
        assert row.stock_code == "600519.SH"
        assert row.period == "daily"  # 旧行回填默认值


def test_migrate_idempotent(temp_engine):
    """重复执行 _migrate 不报错，period 列与索引保持唯一。"""
    Base.metadata.create_all(bind=temp_engine)
    DB._migrate()
    DB._migrate()  # 第二遍：列检查跳过 + IF NOT EXISTS 索引跳过
    cols = _signal_event_cols(temp_engine)
    assert "period" in cols
    names = [i["name"] for i in inspect(temp_engine).get_indexes("signal_events")]
    assert names.count("ix_signal_events_period") == 1


def test_period_index_created(temp_engine):
    """迁移末尾统一建 ix_signal_events_period 索引。"""
    Base.metadata.create_all(bind=temp_engine)
    DB._migrate()
    names = {i["name"] for i in inspect(temp_engine).get_indexes("signal_events")}
    assert "ix_signal_events_period" in names
