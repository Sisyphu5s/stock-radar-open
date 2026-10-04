"""K 线缓存独立 SQLite 引擎 + 主库→新库一次性迁移（T-103）。

klines/cache_meta 两张表从业务主库分离到独立文件 data/kline_cache.db：
- Kline/CacheMeta 模型绑定 klines_base（与主库 Base 共享同一 MetaData 实例，
  保证测试/兼容路径中 `Base.metadata.create_all` 仍能建出这两张表；生产主库
  init_db 用 `tables=` 参数显式排除——见 storage/db.py）。
- migrate_klines()：老库升级时把主库 klines/cache_meta 数据分块复制到新库，
  行数校验通过后 DROP 主库两表；任何失败保持主库原表，不阻塞服务启动。
"""

import logging
import threading
from typing import Any

from sqlalchemy import (
    Float,
    Index,
    Integer,
    String,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from ..config import settings
from .db import Base, _sqlite_pragma

logger = logging.getLogger("stockradar.klines_db")

engine = create_engine(
    settings.klines_db_url,
    connect_args={"check_same_thread": False},
)

# WAL + 长 busy timeout + 外键约束：与主库同口径（并发读写不互锁），
# 复用 storage/db.py 的 _sqlite_pragma（单一事实源，不再逐行复制）
event.listens_for(engine, "connect")(_sqlite_pragma)


klines_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class klines_base(DeclarativeBase):
    """K 线库 ORM 基类：与主库 Base 共享 MetaData（见模块 docstring）。"""

    metadata = Base.metadata


class Kline(klines_base):
    """历史 K 线（qfq 前复权），支持多周期。"""

    __tablename__ = "klines"
    __table_args__ = (
        Index("ix_kline_code_period_date", "code", "period", "date", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # code 不加单列索引：ix_kline_code_period_date 最左前缀（code）已覆盖
    # 按 code 查询/排序路径，单列索引冗余（写放大 + 双份索引维护）。
    code: Mapped[str] = mapped_column(String(12))
    period: Mapped[str] = mapped_column(
        String(8), default="daily"
    )  # 1/5/15/30/60/daily/weekly/monthly
    source: Mapped[str] = mapped_column(
        String(12), default=""
    )  # 数据来源(sina/akshare/mock)
    date: Mapped[str] = mapped_column(String(20))  # YYYY-MM-DD 或 YYYY-MM-DD HH:MM
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)  # 手
    amount: Mapped[float] = mapped_column(Float, default=0.0)


class CacheMeta(klines_base):
    """cache_meta 键值表（版本号 ver:*、partial 标记、meta:last_scan 等）。

    结构与 kcache._ensure_meta 的建表 SQL 一字不差：
    `CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)`。
    """

    __tablename__ = "cache_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


# 仅属于 K 线库的表（其余业务表不落入独立库）
_KLINES_TABLES = ("klines", "cache_meta")
_MIGRATE_CHUNK = 400  # 每批行数（同 kcache.upsert_many 的 CHUNK，规避 SQLite 变量上限）
_PROGRESS_LOG_EVERY = 50000  # 每复制 5 万行打一条进度日志


# ensure_schema 结果缓存:同一 bind 首次建表后跳过重复 DDL 检查
# (scan_status 每请求 9 次 meta_get 都会触发幂等检查,DDL 开销非零)。
# 以 bind 对象身份入集:模块级 engine 为进程单例、测试临时引擎各自独立互不误伤;
# 表结构生命周期内只需建一次。删表重建场景(测试隔离)调用 ensure_schema_cache_clear()。
_schema_ensured: set = set()
_schema_lock = threading.Lock()


def ensure_schema(bind=None) -> None:
    """K 线库建表（仅 klines/cache_meta，业务表不落入独立库）。幂等。

    启动/迁移时调用；kcache._ensure_meta 的运行时代建 SQL 仅作幂等兜底。
    结果按 bind 模块级缓存：命中直接返回，不做重复 DDL 检查。
    """
    target = bind or engine
    with _schema_lock:
        if target in _schema_ensured:
            return
        klines_base.metadata.create_all(
            bind=target,
            tables=[
                t for n, t in klines_base.metadata.tables.items() if n in _KLINES_TABLES
            ],
        )
        _schema_ensured.add(target)


def ensure_schema_cache_clear() -> None:
    """清空 ensure_schema 结果缓存（测试隔离：删表重建后需重新建表时调用）。"""
    with _schema_lock:
        _schema_ensured.clear()


def _drop_main_tables(main_engine) -> None:
    """DROP 主库 klines/cache_meta（IF EXISTS 幂等，重复执行自动跳过）。"""
    with main_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS klines"))
        conn.execute(text("DROP TABLE IF EXISTS cache_meta"))


def _copy_klines(src, kconn, main_total: int) -> int:
    """keyset 分块复制 klines（id 升序，每批 ≤400 行），带进度日志；返回复制行数。"""
    ins = text(
        "INSERT INTO klines (id, code, period, source, date, open, high, low, close, "
        "volume, amount) VALUES (:id, :code, :period, :source, :date, :open, :high, "
        ":low, :close, :volume, :amount)"
    )
    sel = text(
        "SELECT id, code, period, source, date, open, high, low, close, volume, amount "
        "FROM klines WHERE id > :last ORDER BY id LIMIT :n"
    )
    last_id = 0
    copied = 0
    last_logged = 0
    while True:
        rows = src.execute(sel, {"last": last_id, "n": _MIGRATE_CHUNK}).mappings().all()
        if not rows:
            break
        kconn.execute(ins, [dict(r) for r in rows])
        copied += len(rows)
        last_id = rows[-1]["id"]
        if copied - last_logged >= _PROGRESS_LOG_EVERY:
            logger.info("K 线迁移中: %d/%d 行", copied, main_total)
            last_logged = copied
    if copied != main_total:
        raise RuntimeError(
            f"K 线迁移复制行数不一致: 复制 {copied} != 主库 {main_total}"
        )
    return copied


def migrate_klines(main_engine=None, klines_engine=None) -> bool:
    """主库 → 独立 K 线库一次性数据迁移（幂等；任何失败保持主库原表，不阻塞启动）。

    - 新安装：主库无 klines 表或无数据 → 跳过（不建表不复制）；
    - 老库升级：分块复制 klines + cache_meta → 行数校验 → DROP 主库两表；
    - 中断重跑：新库残留部分数据 → 事务内清空后全量重来（不重复不丢数据）；
      复制/校验失败 → 事务回滚，主库不动。

    main_engine / klines_engine 可注入（测试用临时库），默认取存储层模块级引擎
    （运行时读取 .db.engine 当前值——与 app/database.py 兼容层的 engine 同步语义一致，
    测试 monkeypatch 主库引擎后迁移自动作用于临时主库）。
    """
    if main_engine is None:
        from .db import engine as main_db_engine

        main_engine = main_db_engine  # 别名避免遮蔽本模块全局 engine（新库引擎）
    klines_engine = klines_engine or engine
    try:
        ensure_schema(klines_engine)
        main_insp = inspect(main_engine)
        if "klines" not in main_insp.get_table_names():
            logger.info("K 线库迁移跳过: 主库无 klines 表（新安装）")
            return True
        main_has_meta = "cache_meta" in main_insp.get_table_names()
        with main_engine.connect() as src:
            main_total = src.execute(text("SELECT COUNT(*) FROM klines")).scalar() or 0
            main_meta = (
                src.execute(text("SELECT COUNT(*) FROM cache_meta")).scalar() or 0
                if main_has_meta
                else 0
            )
        if main_total == 0:
            logger.info("K 线库迁移跳过: 主库 klines 无数据（新安装）")
            return True
        with klines_engine.connect() as conn:
            new_total = conn.execute(text("SELECT COUNT(*) FROM klines")).scalar() or 0
            new_meta = (
                conn.execute(text("SELECT COUNT(*) FROM cache_meta")).scalar() or 0
            )
        if new_total == main_total and new_meta == main_meta:
            # 前次迁移已完整复制（仅 DROP 未完成）→ 补 DROP，不重复复制
            logger.info(
                "K 线库已迁移完整（%d 行），补 DROP 主库 klines/cache_meta", main_total
            )
            _drop_main_tables(main_engine)
            return True
        # 全量重来（单事务）：清空新库残留，保证中断重跑幂等
        with main_engine.connect() as src, klines_engine.begin() as kconn:
            kconn.execute(text("DELETE FROM klines"))
            kconn.execute(text("DELETE FROM cache_meta"))
            _copy_klines(src, kconn, main_total)
            if main_has_meta:
                meta_rows = (
                    src.execute(text("SELECT key, value FROM cache_meta"))
                    .mappings()
                    .all()
                )
                if meta_rows:
                    kconn.execute(
                        text(
                            "INSERT INTO cache_meta (key, value) VALUES (:key, :value)"
                        ),
                        [dict(r) for r in meta_rows],
                    )
            # 行数校验（事务内，失败即回滚，主库不动）
            new_total = kconn.execute(text("SELECT COUNT(*) FROM klines")).scalar() or 0
            new_meta = (
                kconn.execute(text("SELECT COUNT(*) FROM cache_meta")).scalar() or 0
            )
            if new_total != main_total or new_meta != main_meta:
                raise RuntimeError(
                    f"K 线迁移行数校验失败: klines {new_total} != {main_total}, "
                    f"cache_meta {new_meta} != {main_meta}; 已放弃，主库保持原状"
                )
        _drop_main_tables(main_engine)
        logger.info(
            "K 线库迁移完成: klines %d 行 + cache_meta %d 行 → %s，主库两表已 DROP",
            main_total,
            main_meta,
            klines_engine.url,
        )
        return True
    except Exception as e:
        logger.error("K 线库迁移失败（主库保持原状）: %s", str(e)[:300])
        return False
