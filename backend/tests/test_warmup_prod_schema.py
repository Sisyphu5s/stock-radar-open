"""warmup 生产 schema 校验分支测试（P0-30）。

回归背景：K 线已迁独立 K 线库（T-103），主库 init_db 用 _MAIN_DB_EXCLUDED
排除 klines/cache_meta 两表并 DROP。旧 warmup 用主库 SessionLocal 查 Kline
计数 → 迁移后主库无 klines 表，抛 no such table 被外层 except 吞成 failed。
修复：计数查询改用 klines_db.klines_session（K 线库会话），klines 库无表时
graceful 按无缓存处理。

测试用 tmp 双库模拟生产 schema：主库按 Base.metadata.create_all 排除
klines/cache_meta（与 storage/db.init_db 同口径），K 线库独立建表，
不触碰生产库。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import warmup
from app.lib.session import now_cn
from app.storage import klines_db
from app.storage.db import Base, _MAIN_DB_EXCLUDED


def _reset_state():
    warmup._state.update(
        {"status": "idle", "progress": 0.0, "message": "", "dataset_id": None}
    )


def _make_main_engine(tmp_path):
    """建 tmp 主库（生产 schema：排除 klines/cache_meta），返回 engine + Maker。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'main.db'}", connect_args={"check_same_thread": False}
    )
    tables = [t for n, t in Base.metadata.tables.items() if n not in _MAIN_DB_EXCLUDED]
    Base.metadata.create_all(bind=engine, tables=tables)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Maker


def _make_klines_engine(monkeypatch, tmp_path):
    """重建 klines_db 引擎指向 tmp 新库（同 test_klines_db_migration 方式）。"""
    url = f"sqlite:///{tmp_path / 'kline_cache.db'}"
    monkeypatch.setattr(klines_db.settings, "klines_db_url", url)
    new_engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(klines_db, "engine", new_engine)
    monkeypatch.setattr(
        klines_db,
        "klines_session",
        klines_db.sessionmaker(bind=new_engine, autoflush=False, autocommit=False),
    )
    return new_engine


def _insert_dataset(Maker, *, end_date: str, row_count: int = 5000) -> None:
    """主库插入一条 universe='hs300' 的数据集（合格判定依据）。"""
    from app.storage.models import Dataset

    db = Maker()
    try:
        ds = Dataset(
            name="沪深300 · 近5年日线",
            universe="hs300",
            start_date="2021-01-01",
            end_date=end_date,
            stock_count=300,
            row_count=row_count,
            status="ready",
        )
        db.add(ds)
        db.commit()
    finally:
        db.close()


def _insert_klines(engine, codes: list[str]) -> None:
    """K 线库插入成分股 daily K 线（code 为裸代码，与 warmup split('.')[0] 对应）。"""
    kdb = klines_db.klines_session()
    try:
        for code in codes:
            kdb.add(
                klines_db.Kline(
                    code=code,
                    period="daily",
                    source="test",
                    date="2026-08-10",
                    open=10.0,
                    high=10.5,
                    low=9.8,
                    close=10.2,
                    volume=100.0,
                    amount=1e6,
                )
            )
        kdb.commit()
    finally:
        kdb.close()


@pytest.fixture(autouse=True)
def _cleanup():
    """每个用例结束后复位 warmup 全局状态，避免污染其他测试。"""
    yield
    _reset_state()


def test_warmup_validation_ok_with_prod_schema(monkeypatch, tmp_path):
    """生产 schema（主库无 klines 表）+ K 线库有成分缓存 + 合格数据集
    → 校验分支走 done，不抛异常（修复前主库查 Kline 抛 no such table → failed）。"""
    main_engine, Maker = _make_main_engine(tmp_path)
    k_engine = _make_klines_engine(monkeypatch, tmp_path)
    klines_db.ensure_schema(k_engine)

    yesterday = (now_cn().date() - timedelta(days=1)).isoformat()
    _insert_dataset(Maker, end_date=yesterday)
    _insert_klines(k_engine, ["600000", "600001"])

    monkeypatch.setattr("app.storage.db.SessionLocal", Maker)
    monkeypatch.setattr(
        "app.storage.hs300.get_hs300_codes",
        lambda refresh=False: ["600000.SH", "600001.SZ"],
    )

    warmup._run_warmup()  # 同步执行，不抛异常

    state = warmup.get_warmup_status()
    assert state["status"] == "done"
    assert "跳过预热" in state["message"]
    assert state["dataset_id"] is not None
    main_engine.dispose()
    k_engine.dispose()


def test_warmup_validation_graceful_when_klines_empty(monkeypatch, tmp_path):
    """K 线库有表但无成分缓存（cnt=0）→ 不误判合格，走重建（不抛异常）。"""
    main_engine, Maker = _make_main_engine(tmp_path)
    k_engine = _make_klines_engine(monkeypatch, tmp_path)
    klines_db.ensure_schema(k_engine)

    today = now_cn().date().isoformat()
    _insert_dataset(Maker, end_date=today, row_count=5000)  # 数据集合格但 K 线为空

    monkeypatch.setattr("app.storage.db.SessionLocal", Maker)
    monkeypatch.setattr(
        "app.storage.hs300.get_hs300_codes",
        lambda refresh=False: ["600000.SH", "600001.SZ"],
    )
    # 阻断网络：重建路径的 fetch_many / build_dataset 打桩
    monkeypatch.setattr("app.storage.klines.fetch_many", lambda *a, **kw: {})
    monkeypatch.setattr(
        "app.core.datasets.build_dataset",
        lambda *a, **kw: {"id": 1, "stock_count": 2, "row_count": 2},
    )

    warmup._run_warmup()  # 全程不抛异常

    state = warmup.get_warmup_status()
    assert state["status"] == "done"  # 无成分缓存 → 重建完成
    main_engine.dispose()
    k_engine.dispose()


def test_warmup_validation_graceful_when_klines_no_table(monkeypatch, tmp_path):
    """K 线库无 klines 表（迁移未执行/被清空）→ graceful 按无缓存重建，不抛异常。"""
    main_engine, Maker = _make_main_engine(tmp_path)
    _make_klines_engine(monkeypatch, tmp_path)  # 只建空库，不 ensure_schema

    today = now_cn().date().isoformat()
    _insert_dataset(Maker, end_date=today)

    monkeypatch.setattr("app.storage.db.SessionLocal", Maker)
    monkeypatch.setattr(
        "app.storage.hs300.get_hs300_codes",
        lambda refresh=False: ["600000.SH"],
    )
    monkeypatch.setattr("app.storage.klines.fetch_many", lambda *a, **kw: {})
    monkeypatch.setattr(
        "app.core.datasets.build_dataset",
        lambda *a, **kw: {"id": 2, "stock_count": 1, "row_count": 1},
    )

    warmup._run_warmup()  # no such table 被内层 except 吞掉 → 继续重建，不外抛

    state = warmup.get_warmup_status()
    assert state["status"] == "done"
    main_engine.dispose()
