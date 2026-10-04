"""T-19 后端契约聚焦测试：信号分页 stats 聚合 + 三任务 phase 文案 + 因子入库指标透传。

覆盖（docs/TODO.md T-19）：
- GET /signals/events/page 响应 stats{today_new, stock_count}：SQL 与大集合两条路径
  全量聚合（不受分页截断）、按筛选口径、空结果 0 值；
- factor_tune/alpha101_score/dataset_build 三个后台任务上报 phase 阶段文案；
- alpha101 to-library 透传最近 alpha101_score 的 ic_mean/stability → oos_ic/stability；
- kind=nn 创建透传 val_ic → oos_ic；创建停在 draft（不 auto_advance）。
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, NNModel, SignalEvent


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite：runner / factors / db 三处 SessionLocal 统一替换。"""
    import app.core.tasks.runner as Q
    import app.core.factors as FS
    import app.storage.db as DB

    engine = create_engine(
        f"sqlite:///{tmp_path / 't19.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    monkeypatch.setattr(Q, "init_backend", lambda: None)
    monkeypatch.setattr(FS, "SessionLocal", Maker)
    monkeypatch.setattr(DB, "SessionLocal", Maker)

    # 清理跨测试残留的模块级状态
    from app.api import signals as S

    S._sector_codes_cache.clear()
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


def _cn_now_naive() -> datetime:
    """Asia/Shanghai 墙钟（naive），用于造自然日边界稳定的种子事件。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def _seed_events_at(maker, rows):
    """rows: list of dict(stock_code, signals, at: naive 时间, period?)。"""
    db = maker()
    try:
        for r in rows:
            db.add(
                SignalEvent(
                    stock_code=r["stock_code"],
                    signals=r["signals"],
                    status=r.get("status", "观察"),
                    evidence=r.get("evidence", {"price": 1.0}),
                    triggered_at=r["at"],
                    as_of=r["at"],
                    scan_discovered_at=r["at"],
                    period=r.get("period", "daily"),
                )
            )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. GET /signals/events/page stats{today_new, stock_count}
# ---------------------------------------------------------------------------


def test_events_page_stats_today_new_and_stock_count(temp_db):
    from app.api.signals import list_events_page

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {"stock_code": "600519.SH", "signals": ["price_up"], "at": now},
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now - timedelta(minutes=1),
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=1),
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=2),
            },
        ],
    )
    db = temp_db()
    try:
        r = list_events_page(limit=50, offset=0, db=db)
        assert r["total"] == 4
        # today_new=triggered_at 落今日零点后的事件数（同股票两条都算），
        # stock_count=去重股票数
        assert r["stats"] == {"today_new": 2, "stock_count": 3}
    finally:
        db.close()


def test_events_page_stats_respect_filters(temp_db):
    """stats 与 total 同口径：筛选（code/time_range/exclude_today）先于聚合。"""
    from app.api.signals import list_events_page

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {"stock_code": "600519.SH", "signals": ["price_up"], "at": now},
            {
                "stock_code": "600519.SH",
                "signals": ["volume_surge"],
                "at": now - timedelta(days=1),
            },
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now,
            },
            {
                "stock_code": "300750.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=3),
            },
        ],
    )
    db = temp_db()
    try:
        # code 筛选：单股票 2 条
        r = list_events_page(limit=50, offset=0, code="600519.SH", db=db)
        assert r["total"] == 2 and r["stats"] == {"today_new": 1, "stock_count": 1}
        # signal_types 筛选：volume_surge 仅 1 条（昨天）
        r2 = list_events_page(limit=50, offset=0, signal_types="volume_surge", db=db)
        assert r2["total"] == 1 and r2["stats"] == {"today_new": 0, "stock_count": 1}
        # time_range=today：只含今日事件，today_new=total
        r3 = list_events_page(limit=50, offset=0, time_range="today", db=db)
        assert r3["total"] == 2 and r3["stats"] == {"today_new": 2, "stock_count": 2}
        # exclude_today：今日被排除，today_new 恒 0（筛选口径一致）
        r4 = list_events_page(
            limit=50, offset=0, time_range="all", exclude_today=True, db=db
        )
        assert r4["total"] == 2 and r4["stats"]["today_new"] == 0
        assert r4["stats"]["stock_count"] == 2
    finally:
        db.close()


def test_events_page_stats_not_truncated_by_pagination(temp_db):
    """stats 全量聚合：limit/offset 只截断 items，stats 覆盖筛选后全集。"""
    from app.api.signals import list_events_page

    now = _cn_now_naive()
    rows = []
    for i in range(5):
        rows.append(
            {
                "stock_code": "600519.SH",
                "signals": ["price_up"],
                "at": now - timedelta(days=i),
            }
        )
    for i in range(3):
        rows.append(
            {
                "stock_code": "000001.SZ",
                "signals": ["price_up"],
                "at": now - timedelta(days=i),
            }
        )
    _seed_events_at(temp_db, rows)
    db = temp_db()
    try:
        # time_range=all：8 条全在窗口内；limit/offset 只截断 items
        r = list_events_page(limit=2, offset=0, time_range="all", db=db)
        assert len(r["items"]) == 2 and r["total"] == 8
        assert r["stats"] == {"today_new": 2, "stock_count": 2}
        r2 = list_events_page(limit=2, offset=6, time_range="all", db=db)
        assert len(r2["items"]) == 2 and r2["total"] == 8
        assert r2["stats"] == {"today_new": 2, "stock_count": 2}
    finally:
        db.close()


def test_events_page_stats_empty_zeros(temp_db):
    from app.api.signals import list_events_page

    db = temp_db()
    try:
        r = list_events_page(limit=50, offset=0, db=db)
        assert r["total"] == 0 and r["items"] == []
        assert r["stats"] == {"today_new": 0, "stock_count": 0}
        # 筛选后为空集同样返回 0 值 stats（不报错）
        r2 = list_events_page(limit=50, offset=0, signal_types="nope", db=db)
        assert r2["total"] == 0 and r2["stats"] == {"today_new": 0, "stock_count": 0}
    finally:
        db.close()


def test_events_page_stats_big_sets_path(temp_db):
    """大集合内存路径（>IN_BATCH 分批 IN）：stats 由内存行全量聚合。"""
    from app.api.signals import list_events_page
    from app.storage.repos import signals as _sig_repo
    from app.lib.timex import market_time_range

    now = _cn_now_naive()
    _seed_events_at(
        temp_db,
        [
            {"stock_code": "000001.SH", "signals": ["price_up"], "at": now},
            {
                "stock_code": "000002.SH",
                "signals": ["price_up"],
                "at": now - timedelta(days=1),
            },
            {
                "stock_code": "000003.SH",
                "signals": ["price_up"],
                "at": now - timedelta(days=2),
            },
        ],
    )
    # 大集合：>IN_BATCH 只走分批内存路径（只有 3 只有事件，其余为过滤代码）
    codes = {f"{i:06d}.SH" for i in range(1, 402)}
    db = temp_db()
    try:
        today_start = market_time_range("today")[0]
        events, total, stats = _sig_repo.page_events(
            db,
            period="daily",
            start=None,
            end=now + timedelta(days=1),
            big_sets=[codes],
            limit=50,
            offset=0,
            today_start=today_start,
        )
        assert total == 3 and len(events) == 3
        assert stats == {"today_new": 1, "stock_count": 3}
        # API 层同口径：watchlist_only 大集合路径
        db2 = temp_db()
        try:
            from app.storage.repos import stocks as _stock_repo

            _stock_repo.add_watchlist(db2, "000001.SH")
            _stock_repo.add_watchlist(db2, "000002.SH")
            wl = _stock_repo.watchlist_codes(db2)
            assert len(wl) <= 2  # 小集合：走 SQL 路径（防误触发大集合）
            r = list_events_page(limit=50, offset=0, watchlist_only=True, db=db2)
            assert r["stats"] == {"today_new": 1, "stock_count": 2}
        finally:
            db2.close()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. 三任务 phase 阶段文案
# ---------------------------------------------------------------------------


@pytest.fixture()
def phase_capture(temp_db, monkeypatch):
    """包装 Q._update：记录 phase 文案，其余透传真实 _update（phase 列已存在，直写）。"""
    import app.core.tasks.runner as Q

    phases: list[str] = []
    real_update = Q._update

    def patched(job_id, **fields):
        if fields.get("phase"):
            phases.append(fields["phase"])
        return real_update(job_id, **fields)

    monkeypatch.setattr(Q, "_update", patched)
    return phases


def _install_tune_fakes(monkeypatch):
    """factor_tune 打桩（模式同 test_t58_grid_arith_cap）。"""
    import app.core.datasets as AD
    import app.lib.alpha.evaluate as AE
    import app.lib.alpha.operators as AO
    import app.core.tasks.runner as Q

    def fake_load_panel(ds_id, features=None):
        return {"panel": {"close": np.zeros((5, 40))}, "dates": ["2026-01-01"] * 40}

    def fake_compile_rpn(expr):
        return {"expr": expr}

    def fake_collect_tunable_params(rpn):
        return [{"name": "ts_mean.window"}]

    def fake_set_tunable_param(rpn, name, value):
        return {"expr": f"{rpn['expr']}-{value}"}

    def fake_evaluate_rpn_memo(rpn_i, panel, memo=None):
        return np.zeros((5, 32))

    def fake_evaluate_factor(rpn_i, panel, fwd, horizon=5, _memo=None):
        v = float(str(rpn_i["expr"]).rsplit("-", 1)[1])
        return {
            "ic": 0.01 * v,
            "rank_ic": 0.02 * v,
            "ic_series": [0.01] * 10,
            "long_short_annual": 0.1,
            "stability": 0.5,
            "turnover": 0.3,
        }

    def fake_forward_returns(close, horizon=5):
        return close

    def fake_expr_str(rpn_i):
        return str(rpn_i["expr"])

    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AO, "collect_tunable_params", fake_collect_tunable_params)
    monkeypatch.setattr(AO, "set_tunable_param", fake_set_tunable_param)
    monkeypatch.setattr(AO, "evaluate_rpn_memo", fake_evaluate_rpn_memo)
    monkeypatch.setattr(AO, "expr_str", fake_expr_str)
    monkeypatch.setattr(AE, "evaluate_factor", fake_evaluate_factor)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "load_panel", fake_load_panel)


def _spawn_job(maker, job_type, params) -> int:
    db = maker()
    job = ExperimentJob(job_type=job_type, params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


def test_factor_tune_phases(temp_db, phase_capture, monkeypatch):
    import app.core.tasks.runner as Q

    _install_tune_fakes(monkeypatch)
    params = {
        "dataset_id": 1,
        "expression": "ts_mean(close,5)",
        "target": "ic",
        "horizon": 5,
        "param": {
            "name": "ts_mean.window",
            "min": 1,
            "max": 5,
            "step": 1,
            "is_int": True,
        },
    }
    jid = _spawn_job(temp_db, "factor_tune", params)
    t = threading.Thread(target=Q._run_factor_tune, args=(jid, params))
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "任务线程未及时退出"

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    assert phase_capture[0] == "数据准备"
    assert "网格搜索" in phase_capture
    assert phase_capture[-1] == "结果整理"
    assert row.phase == "结果整理"


def test_alpha101_score_phases(temp_db, phase_capture, monkeypatch):
    import app.core.tasks.runner as Q
    from app.lib.alpha import alpha101 as A101
    from app.lib.alpha import evaluate as AE
    from app.lib.alpha import operators as AO
    from app.core import datasets as AD

    def fake_list_alpha101():
        return [{"id": 1, "name": "A1", "formula": "close"}]

    def fake_compile_rpn(f):
        return [{"op": "__feat__", "params": {"name": "close"}}]

    def fake_load_panel(ds_id, features=None):
        return {"panel": {"close": np.zeros((10, 40))}, "dates": ["2026-01-01"] * 40}

    def fake_forward_returns(close, horizon=5):
        return close

    def fake_evaluate_factor(rpn, panel, fwd, horizon=None, **kw):
        return {"ic": 0.1, "stability": 0.5}

    monkeypatch.setattr(A101, "list_alpha101", fake_list_alpha101)
    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "load_panel", fake_load_panel)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(AE, "evaluate_factor", fake_evaluate_factor)

    params = {"dataset_id": 1, "horizon": 5}
    jid = _spawn_job(temp_db, "alpha101_score", params)
    Q._run_alpha101_score(jid, params)

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    assert phase_capture[0] == "数据准备"
    assert "因子评估" in phase_capture
    assert phase_capture[-1] == "结果整理"
    assert row.phase == "结果整理"


def test_dataset_build_phases(temp_db, phase_capture, monkeypatch):
    import app.core.tasks.runner as Q
    import app.core.datasets as AD

    def fake_build_dataset(**kw):
        cb = kw["progress_cb"]
        cb(1, 10)
        cb(10, 10)
        return {"id": 1, "name": kw["name"], "stock_count": 10, "row_count": 100}

    monkeypatch.setattr(AD, "build_dataset", fake_build_dataset)

    params = {
        "name": "ds",
        "universe": "hs300",
        "start_date": "2021-01-01",
        "end_date": "2026-01-01",
    }
    jid = _spawn_job(temp_db, "dataset_build", params)
    Q._run_dataset_build(jid, params)

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    assert phase_capture == ["数据准备", "拉取行情", "结果整理"]
    assert row.phase == "结果整理"


# ---------------------------------------------------------------------------
# 3. 因子入库指标透传（to-library 与 nn 分支）+ 创建停 draft
# ---------------------------------------------------------------------------


def _seed_score_job(maker, results):
    db = maker()
    job = ExperimentJob(
        job_type="alpha101_score", status="done", result={"results": results}
    )
    db.add(job)
    db.commit()
    db.close()


def test_alpha101_to_library_passes_score_metrics(temp_db):
    from app.api.alpha import alpha101_to_library

    _seed_score_job(
        temp_db,
        [
            {
                "id": 1,
                "name": "Alpha#1",
                "ic_mean": 0.12,
                "stability": 0.66,
                "score": 12.0,
            },
            {
                "id": 2,
                "name": "Alpha#2",
                "ic_mean": 0.03,
                "stability": 0.4,
                "score": 3.0,
            },
        ],
    )
    db = temp_db()
    try:
        r = alpha101_to_library(1, {"dataset_id": 0}, db=db)
        assert r["ok"] is True and r["duplicate"] is False
        # ic_mean/stability → oos_ic/stability 映射落库
        assert r["version"]["oos_ic"] == 0.12
        assert r["version"]["stability"] == 0.66
        # 创建停 draft，不推进发布状态机
        assert r["version"]["status"] == "draft"
        assert r["factor"]["status"] == "draft"
        assert r["version"]["oos_verified"] is False
    finally:
        db.close()


def test_alpha101_to_library_without_score_no_metrics(temp_db):
    """从未评分/无评分记录：不透传指标，版本指标走列默认 0。"""
    from app.api.alpha import alpha101_to_library

    db = temp_db()
    try:
        r = alpha101_to_library(1, {"dataset_id": 0}, db=db)
        assert r["ok"] is True
        assert r["version"]["oos_ic"] == 0.0
        assert r["version"]["stability"] == 0.0
        assert r["version"]["status"] == "draft"
    finally:
        db.close()


def test_alpha101_to_library_takes_latest_score_job(temp_db):
    """取最近一次 done 的 alpha101_score：旧任务结果被新任务覆盖。"""
    from app.api.alpha import alpha101_to_library

    _seed_score_job(
        temp_db, [{"id": 1, "name": "A", "ic_mean": 0.01, "stability": 0.1}]
    )
    _seed_score_job(temp_db, [{"id": 1, "name": "A", "ic_mean": 0.2, "stability": 0.8}])
    db = temp_db()
    try:
        r = alpha101_to_library(1, {"dataset_id": 0}, db=db)
        assert r["version"]["oos_ic"] == 0.2
        assert r["version"]["stability"] == 0.8
    finally:
        db.close()


def test_create_nn_factor_passes_val_ic_metric(temp_db):
    from app.api.factors import _new_nn_factor

    db = temp_db()
    m = NNModel(
        checkpoint="/tmp/fake.npz",
        architecture={"layers": [32, 16], "activation": "relu"},
        dataset_id=1,
        epochs=10,
        train_ic=0.3,
        val_ic=0.21,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    mid = m.id
    db.close()

    r = _new_nn_factor("NN 因子", {"name": "NN 因子", "model_id": mid, "dataset_id": 3})
    assert r["factor"]["kind"] == "nn"
    # 模型 val_ic → oos_ic 透传；stability 无源不写（默认 0）
    assert r["version"]["oos_ic"] == 0.21
    assert r["version"]["stability"] == 0.0
    assert r["version"]["status"] == "draft"
    assert r["factor"]["status"] == "draft"
