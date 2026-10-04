"""T-84 模拟盘后端增强测试：watch 快照限频落库 + 历史序列、多股批量、
白名单 meta、result_invalidated 语义、删除项目级联取消任务。

临时 SQLite + 直接调端点函数（与 test_paper_task.py / test_t48 同模式），
monkeypatch kline/_run_watch，不触碰生产库、不发真实网络请求。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import (
    ExperimentJob,
    PaperProject,
    PaperProjectStock,
    PaperWatchSnapshot,
    utcnow,
)


def _vshape_kline(n: int = 90) -> pd.DataFrame:
    """V 形收盘价（先跌后涨）→ 保证 macd 出现金叉。"""
    dates = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    half = n // 2
    close = np.concatenate(
        [np.linspace(100.0, 60.0, half), np.linspace(60.0, 120.0, n - half)]
    )
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
            "amount": 1e6,
        }
    )
    df["pct_change"] = df["close"].pct_change().fillna(0) * 100
    return df


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（worker/端点直调走同一临时库）。"""
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

    import app.core.tasks.runner as Q

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    monkeypatch.setattr(Q, "init_backend", lambda: None)

    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()

    yield Maker
    engine.dispose()


def _seed_project(maker, **kw):
    """种子项目：默认单股 experiment；stocks 提供时写关联行（多股）。"""
    db = maker()
    p = PaperProject(
        name=kw.get("name", "实验项目"),
        kind=kw.get("kind", "experiment"),
        code=kw.get("code", "600519.SH"),
        period=kw.get("period", "daily"),
        signals=kw.get("signals", ["macd_golden_cross"]),
        days=kw.get("days", 80),
        result=kw.get("result"),
    )
    db.add(p)
    db.commit()
    pid = p.id
    stocks = kw.get("stocks")
    if stocks:
        for i, c in enumerate(stocks):
            db.add(PaperProjectStock(project_id=pid, code=c, name=f"股{c}", seq=i))
        db.commit()
    db.close()
    return pid


def _wait_status(maker, jid: int, status: str, timeout: float = 20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = maker()
        row = db.get(ExperimentJob, jid)
        db.close()
        if row is not None and row.status == status:
            return row
        time.sleep(0.01)
    raise AssertionError(f"等待任务 {jid} 状态 {status} 超时")


def _patch_kline(monkeypatch, df=None):
    import app.core.tasks.runner as Q

    monkeypatch.setattr(
        Q,
        "cached_kline",
        lambda code, period, max_rows=250: df if df is not None else _vshape_kline(80),
    )


def _patch_watch(monkeypatch, close: float = 100.0):
    """mock watch 计算：固定指标快照（含 close）+ 命中 volume_surge。"""
    import app.api.paper as P

    def fake(code, period, signals):
        return (
            {
                "indicators": {"close": close},
                "triggered": [{"signal": "volume_surge"}],
                "recent": [],
            },
            "2026-01-05",
        )

    monkeypatch.setattr(P, "_run_watch", fake)
    monkeypatch.setattr(P, "_stock_name", lambda code: f"股{code}")


# ---------------------------------------------------------------------------
# 1. watch 快照限频落库 + 多股价格汇总
# ---------------------------------------------------------------------------


def test_watch_snapshot_ratelimited(temp_db, monkeypatch):
    from app.api.paper import SNAPSHOT_MIN_INTERVAL_SEC, watch_project

    _patch_watch(monkeypatch)
    pid = _seed_project(temp_db, kind="watch", code="600519.SH")

    # 第一次轮询 → 落一条快照
    watch_project(pid, temp_db())
    db = temp_db()
    snaps = db.query(PaperWatchSnapshot).filter_by(project_id=pid).all()
    db.close()
    assert len(snaps) == 1
    assert snaps[0].prices == {"600519.SH": 100.0}
    assert snaps[0].hit_signals == ["volume_surge"]
    assert snaps[0].bar_date == "2026-01-05"

    # 限频窗口内再次轮询 → 不落新快照
    watch_project(pid, temp_db())
    db = temp_db()
    n = db.query(PaperWatchSnapshot).filter_by(project_id=pid).count()
    db.close()
    assert n == 1

    # 把最近快照时间推前一个间隔 → 再轮询应落第二条
    db = temp_db()
    snap = (
        db.query(PaperWatchSnapshot)
        .filter_by(project_id=pid)
        .order_by(PaperWatchSnapshot.ts.desc())
        .first()
    )
    snap.ts = utcnow() - __import__("datetime").timedelta(
        seconds=SNAPSHOT_MIN_INTERVAL_SEC + 5
    )
    db.commit()
    db.close()
    watch_project(pid, temp_db())
    db = temp_db()
    n = db.query(PaperWatchSnapshot).filter_by(project_id=pid).count()
    db.close()
    assert n == 2


def test_watch_project_multi_stock_payload_and_snapshot(temp_db, monkeypatch):
    from app.api.paper import watch_project

    _patch_watch(monkeypatch)
    pid = _seed_project(
        temp_db, kind="watch", code="000001.SZ", stocks=["000001.SZ", "000002.SZ"]
    )

    resp = watch_project(pid, temp_db())
    assert resp["multi"] is True
    assert [s["code"] for s in resp["stocks"]] == ["000001.SZ", "000002.SZ"]
    assert resp["stocks"][0]["snapshot"]["close"] == 100.0
    assert resp["stocks"][0]["hit_signals"] == ["volume_surge"]

    db = temp_db()
    snap = db.query(PaperWatchSnapshot).filter_by(project_id=pid).one()
    db.close()
    # 多股：prices 每股一键；hit_signals 跨股合并去重
    assert snap.prices == {"000001.SZ": 100.0, "000002.SZ": 100.0}
    assert snap.hit_signals == ["volume_surge"]


# ---------------------------------------------------------------------------
# 2. watch 历史序列返回
# ---------------------------------------------------------------------------


def test_watch_history_returns_ascending_sequence(temp_db):
    from app.api.paper import watch_history

    pid = _seed_project(temp_db, kind="watch")
    db = temp_db()
    base = utcnow()
    for i in range(3):
        db.add(
            PaperWatchSnapshot(
                project_id=pid,
                ts=base - __import__("datetime").timedelta(seconds=60 * (2 - i)),
                bar_date=f"2026-01-0{i + 1}",
                prices={"600519.SH": 100.0 + i},
                hit_signals=["macd_golden_cross"],
            )
        )
    db.commit()
    db.close()

    resp = watch_history(pid, limit=10, db=temp_db())
    data = resp["data"]
    assert len(data) == 3
    # 时间升序
    assert [d["bar_date"] for d in data] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert data[0]["prices"] == {"600519.SH": 100.0}
    assert data[-1]["prices"] == {"600519.SH": 102.0}
    assert data[0]["hit_signals"] == ["macd_golden_cross"]
    assert data[0]["ts"] is not None

    # limit 截断：只返回最近 N 条（升序）
    resp = watch_history(pid, limit=2, db=temp_db())
    assert [d["bar_date"] for d in resp["data"]] == ["2026-01-02", "2026-01-03"]

    # 无快照项目 → 空 data
    empty_pid = _seed_project(temp_db, kind="watch", name="空项目")
    assert watch_history(empty_pid, db=temp_db())["data"] == []


# ---------------------------------------------------------------------------
# 3. 多股批量运行（任务层）+ 单股兼容
# ---------------------------------------------------------------------------


def test_multi_stock_run_paper_loops_and_summarizes(temp_db, monkeypatch):
    from app.core.tasks.runner import submit

    _patch_kline(monkeypatch)
    pid = _seed_project(
        temp_db,
        kind="experiment",
        code="000001.SZ",
        stocks=["000001.SZ", "000002.SZ"],
    )

    jid = submit("paper_experiment", {"project_id": pid})
    row = _wait_status(temp_db, jid, "done")

    result = row.result
    assert result["multi"] is True
    assert result["period"] == "daily"
    assert result["days"] == 80
    stocks = result["stocks"]
    assert [s["code"] for s in stocks] == ["000001.SZ", "000002.SZ"]
    assert stocks[0]["name"] == "股000001.SZ"
    assert stocks[0]["computed_bars"] == 80
    assert stocks[0]["results"][0]["signal"] == "macd_golden_cross"
    # 汇总：跨股触发器数 = 各股之和
    expected = sum(int(s["results"][0]["triggers"]) for s in stocks)
    assert result["summary"]["stock_count"] == 2
    assert result["summary"]["total_triggers"] == expected
    assert (
        result["summary"]["avg_win_rate"] is None
        or 0.0 <= result["summary"]["avg_win_rate"] <= 1.0
    )

    # 项目 result 同步回写（同样 multi 结构）
    db = temp_db()
    p2 = db.get(PaperProject, pid)
    db.close()
    assert p2.result == result

    # 任务 summary 抽取（list 视图）兼容 multi
    from app.core.tasks.runner import list_jobs

    item = [it for it in list_jobs(20) if it["id"] == jid][0]
    assert item["summary"]["total_triggers"] == expected
    assert item["summary"]["signal_count"] == 2  # 2 股 × 1 信号


def test_single_stock_run_paper_keeps_legacy_result_shape(temp_db, monkeypatch):
    """旧单股项目（无关联行）result 结构与原一致：顶层 code/results/computed_bars。"""
    from app.core.tasks.runner import submit

    _patch_kline(monkeypatch)
    pid = _seed_project(temp_db, kind="experiment", code="600519.SH")

    jid = submit("paper_experiment", {"project_id": pid})
    row = _wait_status(temp_db, jid, "done")

    result = row.result
    assert "multi" not in result
    assert result["code"] == "600519.SH"
    assert result["period"] == "daily"
    assert result["days"] == 80
    assert result["computed_bars"] == 80
    assert result["results"][0]["signal"] == "macd_golden_cross"


def test_create_project_with_stocks_sets_code_to_first(temp_db):
    from app.api.paper import ProjectCreate, create_project

    body = ProjectCreate(
        code="000001.SZ",
        stocks=["000001.SZ", "000002.SZ"],
        signals=["macd_golden_cross"],
        days=80,
    )
    resp = create_project(body, temp_db())
    assert resp["code"] == "000001.SZ"
    assert [s["code"] for s in resp["stocks"]] == ["000001.SZ", "000002.SZ"]

    db = temp_db()
    rows = (
        db.query(PaperProjectStock)
        .filter_by(project_id=resp["id"])
        .order_by(PaperProjectStock.seq)
        .all()
    )
    db.close()
    assert [r.code for r in rows] == ["000001.SZ", "000002.SZ"]
    assert [r.seq for r in rows] == [0, 1]


# ---------------------------------------------------------------------------
# 4. 白名单 meta 端点（单一事实源派生）
# ---------------------------------------------------------------------------


def test_paper_meta_endpoint(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.paper import router
    from app.core.paper import SUPPORTED_SIGNALS, SIGNAL_DESCRIPTIONS, SIGNAL_LABELS

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    resp = TestClient(app).get("/api/v1/paper/meta")
    assert resp.status_code == 200
    body = resp.json()
    keys = [s["key"] for s in body["signals"]]
    assert keys == list(SUPPORTED_SIGNALS)  # 白名单与 _RULES 键完全一致（单一事实源）
    for s in body["signals"]:
        assert s["label"] == SIGNAL_LABELS[s["key"]]
        assert s["description"] == SIGNAL_DESCRIPTIONS[s["key"]]
    assert "daily" in body["periods"]
    assert body["max_signals"] == 10
    assert body["max_stocks"] == 20


# ---------------------------------------------------------------------------
# 5. result_invalidated 语义（PATCH 变更使结果失效）
# ---------------------------------------------------------------------------


def _patch(maker, pid, body):
    from app.api.paper import ProjectPatch, update_project

    res = update_project(pid, body, maker())
    db = maker()
    p = db.get(PaperProject, pid)
    db.close()
    return res, p.result


def test_patch_code_invalidates_result(temp_db):
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result={"code": "600519.SH", "computed_bars": 80})
    res, db_result = _patch(temp_db, pid, ProjectPatch(code="000001.SZ"))
    assert res["result_invalidated"] is True
    assert res["result"] is None
    assert db_result is None


def test_patch_stocks_invalidates_result_and_syncs_code(temp_db):
    from app.api.paper import ProjectPatch

    pid = _seed_project(temp_db, result={"code": "600519.SH", "computed_bars": 80})
    res, db_result = _patch(
        temp_db, pid, ProjectPatch(stocks=["000001.SZ", "000002.SZ"])
    )
    assert res["result_invalidated"] is True
    assert res["result"] is None
    assert res["code"] == "000001.SZ"  # code 与首股同步
    assert [s["code"] for s in res["stocks"]] == ["000001.SZ", "000002.SZ"]
    assert db_result is None


def test_patch_name_only_not_invalidated(temp_db):
    from app.api.paper import ProjectPatch

    old = {"code": "600519.SH", "computed_bars": 80}
    pid = _seed_project(temp_db, result=dict(old))
    res, db_result = _patch(temp_db, pid, ProjectPatch(name="改名"))
    assert res["result_invalidated"] is False
    assert res["result"] == old
    assert db_result == old


# ---------------------------------------------------------------------------
# 6. 删除项目级联取消任务 + 清理快照/关联行
# ---------------------------------------------------------------------------


def test_delete_project_cancels_active_jobs_and_cleans_up(temp_db):
    from app.api.paper import delete_project

    pid = _seed_project(temp_db, stocks=["000001.SZ", "000002.SZ"])
    db = temp_db()
    job = ExperimentJob(
        job_type="paper_experiment", status="running", params={"project_id": pid}
    )
    db.add(job)
    db.add(PaperWatchSnapshot(project_id=pid, prices={"000001.SZ": 1.0}))
    db.commit()
    jid = job.id
    db.close()

    delete_project(pid, temp_db())  # 204 语义由装饰器承担，函数返回 None

    db = temp_db()
    assert db.get(PaperProject, pid) is None
    assert db.query(PaperProjectStock).filter_by(project_id=pid).count() == 0
    assert db.query(PaperWatchSnapshot).filter_by(project_id=pid).count() == 0
    canceled = db.get(ExperimentJob, jid)
    db.close()
    assert canceled.status == "cancelled"  # 级联取消：不再留给任务中心


def test_delete_project_keeps_terminal_jobs(temp_db):
    """终态任务（done）不随项目删除被取消/删除（任务中心独立管理）。"""
    from app.api.paper import delete_project

    pid = _seed_project(temp_db)
    db = temp_db()
    job = ExperimentJob(
        job_type="paper_experiment", status="done", params={"project_id": pid}
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    delete_project(pid, temp_db())

    db = temp_db()
    done = db.get(ExperimentJob, jid)
    db.close()
    assert done is not None and done.status == "done"


def test_cancel_paper_jobs_only_targets_project(temp_db):
    """cancel_paper_jobs 按 project_id 精确匹配，不动其他项目任务。"""
    from app.core.tasks.runner import cancel_paper_jobs

    pid_a = _seed_project(temp_db, name="A")
    pid_b = _seed_project(temp_db, name="B")
    db = temp_db()
    ja = ExperimentJob(
        job_type="paper_experiment", status="running", params={"project_id": pid_a}
    )
    jb = ExperimentJob(
        job_type="paper_experiment", status="running", params={"project_id": pid_b}
    )
    db.add_all([ja, jb])
    db.commit()
    ja_id, jb_id = (
        ja.id,
        jb.id,
    )  # commit 后未关闭前读主键（expire_on_commit 后访问触发 refresh）
    db.close()

    assert cancel_paper_jobs(pid_a) == 1
    db = temp_db()
    assert db.get(ExperimentJob, ja_id).status == "cancelled"
    assert db.get(ExperimentJob, jb_id).status == "running"
    db.close()
