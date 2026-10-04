"""重跑端点测试：终态（done/failed/cancelled）可重跑、非终态 409、不存在 404；
cancelled 状态兼容归一（旧数据 failed+已被用户取消 → 输出 cancelled，DB 原样不动）。

临时 SQLite，不触碰生产库；重跑产生的新任务用假执行器立即终态。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, utcnow


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（计算线程/删除走同一临时库）。"""
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


def _wait_status(maker, jid: int, status: str, timeout: float = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = maker()
        row = db.get(ExperimentJob, jid)
        db.close()
        if row is not None and row.status == status:
            return row
        time.sleep(0.01)
    raise AssertionError(f"等待任务 {jid} 状态 {status} 超时")


def _install_fake_executors(monkeypatch, job_type: str):
    """把对应 job_type 的执行器替换为立即 done 的假实现（避免真实计算）。"""
    import app.core.tasks.runner as Q

    def fake(jid, params):
        return Q._terminal(
            jid,
            status="done",
            progress=100.0,
            result={"ok": 1},
            finished_at=utcnow(),
        )

    name = {
        "gp_run": "_run_gp",
        "evaluate": "_run_evaluate",
        "backtest": "_run_backtest",
        "alpha101_score": "_run_alpha101_score",
        "factor_tune": "_run_factor_tune",
        "dataset_build": "_run_dataset_build",
        "market_scan": "_run_market_scan",
        "paper_experiment": "_run_paper",
    }[job_type]
    monkeypatch.setattr(Q, name, fake)


def test_rerun_terminal_job_creates_new_job_with_same_params(temp_db, monkeypatch):
    """done 任务重跑：新任务复用原 params，job_type 相同，状态流转 pending → done。"""
    from app.api.experiments import experiment_rerun

    _install_fake_executors(monkeypatch, "evaluate")

    db = temp_db()
    job = ExperimentJob(
        job_type="evaluate",
        status="done",
        progress=100.0,
        params={"expression": "close * 2", "dataset_id": 3},
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    res = experiment_rerun(jid)
    assert res["status"] == "pending"
    new_id = res["job_id"]
    assert isinstance(new_id, int) and new_id != jid

    db = temp_db()
    new = db.get(ExperimentJob, new_id)
    db.close()
    assert new.job_type == "evaluate"
    assert new.params == {"expression": "close * 2", "dataset_id": 3}
    _wait_status(temp_db, new_id, "done")


def test_rerun_failed_and_cancelled_allowed(temp_db, monkeypatch):
    """failed 与 cancelled 同为终态，均可重跑。"""
    from app.api.experiments import experiment_rerun

    _install_fake_executors(monkeypatch, "gp_run")

    db = temp_db()
    db.add_all(
        [
            ExperimentJob(job_type="gp_run", status="failed", params={"a": 1}),
            ExperimentJob(job_type="gp_run", status="cancelled", params={"a": 2}),
        ]
    )
    db.commit()
    ids = [r.id for r in db.query(ExperimentJob).order_by(ExperimentJob.id).all()]
    db.close()

    for jid in ids:
        res = experiment_rerun(jid)
        assert res["status"] == "pending"
        db = temp_db()
        new = db.get(ExperimentJob, res["job_id"])
        db.close()
        assert new.job_type == "gp_run"
        _wait_status(temp_db, res["job_id"], "done")


def test_rerun_non_terminal_409(temp_db):
    """pending/running/paused 非终态 → 409。"""
    from app.api.experiments import experiment_rerun

    db = temp_db()
    db.add_all(
        [
            ExperimentJob(job_type="gp_run", status="pending", params={}),
            ExperimentJob(job_type="gp_run", status="running", params={}),
            ExperimentJob(job_type="gp_run", status="paused", params={}),
        ]
    )
    db.commit()
    ids = [r.id for r in db.query(ExperimentJob).all()]
    db.close()

    for jid in ids:
        with pytest.raises(HTTPException) as e:
            experiment_rerun(jid)
        assert e.value.status_code == 409
        assert "重跑" in str(e.value.detail)


def test_rerun_missing_404(temp_db):
    from app.api.experiments import experiment_rerun

    with pytest.raises(HTTPException) as e:
        experiment_rerun(99999)
    assert e.value.status_code == 404


def test_rerun_paper_experiment_409_when_project_active(temp_db):
    """paper_experiment rerun 补项目级防重：项目已有 pending/running/paused 任务 → 409
    （rerun 直连 submit 绕过 paper.run_project 的查重，此处复用 has_active_paper_job）。"""
    from app.api.experiments import experiment_rerun

    db = temp_db()
    done = ExperimentJob(
        job_type="paper_experiment", status="done", params={"project_id": 7}
    )
    db.add(done)
    db.commit()
    jid = done.id
    active = ExperimentJob(
        job_type="paper_experiment", status="running", params={"project_id": 7}
    )
    db.add(active)
    db.commit()
    db.close()

    with pytest.raises(HTTPException) as e:
        experiment_rerun(jid)
    assert e.value.status_code == 409
    assert "运行中" in str(e.value.detail)


def test_rerun_paper_experiment_allowed_when_no_active(temp_db, monkeypatch):
    """项目无活动任务时 paper_experiment 可正常重跑（防重按 project_id 隔离）。"""
    from app.api.experiments import experiment_rerun
    import app.core.tasks.runner as Q

    def fake(jid, params):
        return Q._terminal(
            jid, status="done", progress=100.0, result={"ok": 1}, finished_at=utcnow()
        )

    monkeypatch.setattr(Q, "_run_paper", fake)

    db = temp_db()
    done = ExperimentJob(
        job_type="paper_experiment", status="failed", params={"project_id": 8}
    )
    db.add(done)
    db.commit()
    jid = done.id
    # 其他项目有活动任务，不影响本项目重跑
    active = ExperimentJob(
        job_type="paper_experiment", status="running", params={"project_id": 9}
    )
    db.add(active)
    db.commit()
    db.close()

    res = experiment_rerun(jid)
    assert res["status"] == "pending"
    _wait_status(temp_db, res["job_id"], "done")


def test_rerun_route_registered():
    from app.api.experiments import router as er

    paths = [r.path for r in er.routes]
    assert "/experiments/{job_id}/rerun" in paths


# ---------------------------------------------------------------------------
# cancelled 状态兼容归一（旧数据 failed + 已被用户取消 → 输出 cancelled）
# ---------------------------------------------------------------------------


def test_legacy_cancelled_normalized_in_list_and_detail(temp_db):
    """旧格式（failed + 已被用户取消）在 list/detail 输出为 cancelled，DB 原样不动。"""
    from app.api.experiments import experiment_status
    from app.core.tasks.runner import get_job, list_jobs

    db = temp_db()
    job = ExperimentJob(
        job_type="gp_run", status="failed", error="已被用户取消", params={}
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert get_job(jid)["status"] == "cancelled"
    assert list_jobs(10)[0]["status"] == "cancelled"
    assert experiment_status(jid)["status"] == "cancelled"

    # DB 原样不动（归一仅影响输出）
    db = temp_db()
    assert db.get(ExperimentJob, jid).status == "failed"
    db.close()


def test_legacy_cancelled_can_rerun(temp_db, monkeypatch):
    """旧格式取消任务（failed+已被用户取消）按归一后的 cancelled 判定，可重跑。"""
    from app.api.experiments import experiment_rerun

    _install_fake_executors(monkeypatch, "gp_run")

    db = temp_db()
    job = ExperimentJob(
        job_type="gp_run", status="failed", error="已被用户取消", params={"b": 7}
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    res = experiment_rerun(jid)
    assert res["status"] == "pending"
    _wait_status(temp_db, res["job_id"], "done")
