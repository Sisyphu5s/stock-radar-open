"""数据库：SQLite + SQLAlchemy 2.0（存储层）。"""

import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ..config import settings

logger = logging.getLogger("stockradar.db")

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _record):
    """WAL + 长 busy timeout + 外键约束：并发扫描/预热读写不互锁（WAL 下读不阻塞写）；
    foreign_keys=ON 让 models 中 ForeignKey（如 factors→factor_versions）真正生效
    （SQLite 默认关闭 FK 检查，仅声明不启用等于没有约束）。

    K 线独立库（storage/klines_db.py）复用本函数（单一事实源，不再逐行复制）。"""
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception as e:
        logger.warning("SQLite PRAGMA 初始化失败: %s", str(e)[:200])


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# klines/cache_meta 已归独立 K 线库（storage/klines_db.py，T-103）：
# 主库 create_all 显式排除这两张表（模型仍在共享 MetaData 中，tables= 过滤即不建）；
# 数据由 init_db 末尾的 migrate_klines() 从老库复制到 data/kline_cache.db 后 DROP。
_MAIN_DB_EXCLUDED = ("klines", "cache_meta")


def init_db():
    _tables = [t for n, t in Base.metadata.tables.items() if n not in _MAIN_DB_EXCLUDED]
    Base.metadata.create_all(bind=engine, tables=_tables)
    _migrate()
    from .klines_db import migrate_klines

    migrate_klines()


def _migrate():
    """轻量迁移：旧表缺列时重建（原型阶段，数据可重建）。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    # ALTER ADD COLUMN 仅追加列不重建表，任务历史完整保留。
    if "experiment_jobs" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("experiment_jobs")}
        if "phase" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE experiment_jobs ADD COLUMN phase VARCHAR(64) DEFAULT ''"
                    )
                )
            import logging

            logging.getLogger("stockradar").warning(
                "experiment_jobs 已迁移：新增 phase 列（数据保留）"
            )

    # signal_events 新增 as_of 列（数据实际截止时刻，旧事件保持 NULL）：
    # ALTER ADD COLUMN 仅追加列不重建表，历史事件数据完整保留。
    if "signal_events" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("signal_events")}
        if "as_of" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE signal_events ADD COLUMN as_of DATETIME")
                )
            import logging

            logging.getLogger("stockradar").warning(
                "signal_events 已迁移：新增 as_of 列（数据保留）"
            )

    # signal_events 新增 period 列（信号周期，旧事件保持 daily）：
    # ALTER ADD COLUMN 仅追加列不重建表，历史事件数据完整保留。
    if "signal_events" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("signal_events")}
        if "period" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE signal_events ADD COLUMN period VARCHAR(16) NOT NULL DEFAULT 'daily'"
                    )
                )
            import logging

            logging.getLogger("stockradar").warning(
                "signal_events 已迁移：新增 period 列（数据保留）"
            )

    # factors 新增 kind 列（expr/nn，nn 因子 = 神经网络模型引用）；历史因子保持 expr。
    if "factors" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("factors")}
        if "kind" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE factors ADD COLUMN kind VARCHAR(16) DEFAULT 'expr'"
                    )
                )
            import logging

            logging.getLogger("stockradar").warning(
                "factors 已迁移：新增 kind 列（数据保留）"
            )

    # factor_versions 新增 model_ref 列（nn 因子关联 nn_models.id）；历史版本保持 NULL。
    if "factor_versions" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("factor_versions")}
        if "model_ref" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE factor_versions ADD COLUMN model_ref INTEGER")
                )
            import logging

            logging.getLogger("stockradar").warning(
                "factor_versions 已迁移：新增 model_ref 列（数据保留）"
            )

    # signal_events 新增 scan_discovered_at 列（首次发现时刻，旧事件保持 NULL）：
    # ALTER ADD COLUMN 仅追加列不重建表，历史事件数据完整保留。
    if "signal_events" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("signal_events")}
        if "scan_discovered_at" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE signal_events ADD COLUMN scan_discovered_at DATETIME"
                    )
                )
            import logging

            logging.getLogger("stockradar").warning(
                "signal_events 已迁移：新增 scan_discovered_at 列（数据保留）"
            )

    # signal_templates/resonance_weights 整表删除 + signal_events 去模板化迁移（2026-08）：
    # 模板/共振权重概念移除，事件改为「股票×bar 聚合事件，携带命中信号数组」。
    # - signal_templates/resonance_weights 表存在即 DROP（模板数据全量弃用，无需保留）；
    # - signal_events 删除 template_id/template_name/resonance_score 三列。SQLite DROP COLUMN
    #   限制：被索引/参与唯一约束的列无法直接删除——必须先 DROP 旧唯一索引
    #   uq_signal_event_stock_tpl_time（含 template_id）与 template_id 单列索引
    #   ix_signal_events_template_id，全部 DROP INDEX IF EXISTS 幂等；
    # - 旧库同一 bar 时点可能有多条事件（同 stock+bar 不同模板各一条）→ 新唯一键
    #   (stock_code, triggered_at, period) 下重复，建索引前先按组保留最新一条（id 最大）；
    # - 新唯一索引 uq_signal_event_stock_time 幂等创建（新库由 create_all 已建，IF NOT EXISTS 跳过）。
    # 全部带存在性检查，重复执行自动跳过。本块置于全部 ADD COLUMN 迁移之后：
    # 去重/建索引均引用 period 列，极旧库需先由上方迁移补列。
    if "signal_templates" in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE signal_templates"))
        import logging

        logging.getLogger("stockradar").warning(
            "signal_templates 已迁移：整表删除（模板概念移除）"
        )
    if "resonance_weights" in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE resonance_weights"))
        import logging

        logging.getLogger("stockradar").warning(
            "resonance_weights 已迁移：整表删除（共振权重概念移除）"
        )
    if "signal_events" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("signal_events")}
        if "template_id" in cols:
            with engine.begin() as conn:
                conn.execute(
                    text("DROP INDEX IF EXISTS uq_signal_event_stock_tpl_time")
                )
                conn.execute(text("DROP INDEX IF EXISTS ix_signal_events_template_id"))
                # 去重（仅旧库存在 template_id 列时才可能发生）：同 (stock_code,
                # triggered_at, period) 保留最新一条，其余删除——否则新唯一索引建不成。
                conn.execute(
                    text(
                        "DELETE FROM signal_events WHERE id NOT IN "
                        "(SELECT MAX(id) FROM signal_events "
                        "GROUP BY stock_code, triggered_at, period)"
                    )
                )
                conn.execute(text("ALTER TABLE signal_events DROP COLUMN template_id"))
                conn.execute(
                    text("ALTER TABLE signal_events DROP COLUMN template_name")
                )
                conn.execute(
                    text("ALTER TABLE signal_events DROP COLUMN resonance_score")
                )
            # 新唯一索引（旧库 template_id 列存在 → 旧唯一索引必被上方删除，这里重建）；
            # 新库无 template_id 列 → 本分支不进入，索引由 create_all 创建。
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_event_stock_time "
                        "ON signal_events(stock_code, triggered_at, period)"
                    )
                )
            import logging

            logging.getLogger("stockradar").warning(
                "signal_events 已迁移：删除模板三列，唯一索引收敛为 (stock_code, triggered_at, period)"
            )

    # signal_events.period 查询索引：迁移末尾统一执行（IF NOT EXISTS 幂等，
    # 新旧库均保证存在，重复执行自动跳过）。
    if "signal_events" in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_signal_events_period ON signal_events(period)"
                )
            )
