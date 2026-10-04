"""paper_experiment 任务全链路：worker 计算 → job done → paper_projects.result 回写 +
summary 抽取；run_project 提交契约与项目级并发防重 409；取消后项目 result 保持旧值；
重启恢复 recover_stale_jobs；任务系统接入（白名单/labels）。

临时 SQLite + mock cached_kline，不触碰生产库、不发真实网络请求。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, PaperProject, utcnow


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
    """独立临时 DB，替换 queue.SessionLocal（worker/API 走同一临时库）。"""
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


def _seed_project(maker, **kw):
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
    db.close()
    return pid


# ---------------------------------------------------------------------------
# 全链路：submit → worker 计算 → done → 项目 result 回写 + summary 抽取
# ---------------------------------------------------------------------------


def test_paper_experiment_full_chain(temp_db, monkeypatch):
    from app.core.tasks.runner import list_jobs, submit

    _patch_kline(monkeypatch)
    pid = _seed_project(temp_db)

    jid = submit("paper_experiment", {"project_id": pid})
    row = _wait_status(temp_db, jid, "done")

    # 任务 result 结构契约
    result = row.result
    assert result["code"] == "600519.SH"
    assert result["period"] == "daily"
    assert result["days"] == 80
    assert result["computed_bars"] == 80
    assert len(result["results"]) == 1
    assert result["results"][0]["signal"] == "macd_golden_cross"
    assert result["results"][0]["triggers"] >= 1

    # 项目 result 同步回写 + updated_at 更新
    db = temp_db()
    p2 = db.get(PaperProject, pid)
    db.close()
    assert p2.result == result
    assert p2.updated_at is not None

    # summary 抽取：信号数 / 总触发次数 / 平均胜率（list 视图）
    items = [it for it in list_jobs(20) if it["id"] == jid]
    assert len(items) == 1
    s = items[0]["summary"]
    assert items[0]["label"] == "模拟盘实验"
    assert s["signal_count"] == 1
    assert s["total_triggers"] == result["results"][0]["triggers"]
    assert s["avg_win_rate"] is None or 0.0 <= s["avg_win_rate"] <= 1.0


def test_paper_experiment_failed_when_kline_unavailable(temp_db, monkeypatch):
    """K 线不可用 → 任务 failed + error（HTTPException 语义转字符串），项目 result 不写入。"""
    from app.core.tasks.runner import submit

    import pandas as pd

    _patch_kline(monkeypatch, df=pd.DataFrame())
    pid = _seed_project(temp_db)

    jid = submit("paper_experiment", {"project_id": pid})
    row = _wait_status(temp_db, jid, "failed")

    assert "K 线不可用" in row.error
    assert row.result == {}


# ---------------------------------------------------------------------------
# run_project：提交契约（{job_id, status, project} 外壳）与并发防重
# ---------------------------------------------------------------------------


def test_run_project_experiment_submits_job_and_returns_envelope(temp_db, monkeypatch):
    """experiment 项目 run：返回 {job_id, status='pending', project(旧 result)}，
    后台任务已创建（job_type=paper_experiment, params={project_id}）。"""
    from app.api.paper import run_project
    import app.core.tasks.runner as Q

    # 假执行器（no-op）：仅验证提交路径，避免真实计算与回写竞态
    monkeypatch.setattr(Q, "_run_paper", lambda jid, params: None)

    old_result = {"code": "600519.SH", "computed_bars": 80}
    pid = _seed_project(temp_db, result=old_result)

    res = run_project(pid, temp_db())
    assert isinstance(res["job_id"], int)
    assert res["status"] == "pending"
    assert res["project"]["id"] == pid
    assert res["project"]["result"] == old_result  # 旧值保持不变

    db = temp_db()
    job = db.get(ExperimentJob, res["job_id"])
    db.close()
    assert job is not None
    assert job.job_type == "paper_experiment"
    assert job.params == {"project_id": pid}


def test_run_project_409_when_active_job_exists(temp_db):
    """项目级并发防重：DB 中已有该项目的 pending/running/paused 任务 → 409。"""
    from app.api.paper import run_project

    pid = _seed_project(temp_db)
    db = temp_db()
    job = ExperimentJob(
        job_type="paper_experiment", status="pending", params={"project_id": pid}
    )
    db.add(job)
    db.commit()
    db.close()

    with pytest.raises(HTTPException) as e:
        run_project(pid, temp_db())
    assert e.value.status_code == 409
    assert "运行中" in str(e.value.detail)


def test_run_project_409_only_for_same_project(temp_db, monkeypatch):
    """防重按 project_id 隔离：其他项目的活动任务不影响本项目提交。"""
    from app.api.paper import run_project
    import app.core.tasks.runner as Q

    # 假执行器 no-op：仅验证提交路径
    monkeypatch.setattr(Q, "_run_paper", lambda jid, params: None)

    other_pid = _seed_project(temp_db, name="其他项目")
    pid = _seed_project(temp_db, name="本项目")
    db = temp_db()
    job = ExperimentJob(
        job_type="paper_experiment", status="running", params={"project_id": other_pid}
    )
    db.add(job)
    db.commit()
    db.close()

    # 其他项目有活动任务 → 409
    with pytest.raises(HTTPException) as e:
        run_project(other_pid, temp_db())
    assert e.value.status_code == 409

    # 本项目无活动任务 → 正常提交
    res = run_project(pid, temp_db())
    assert res["status"] == "pending"
    assert res["job_id"] is not None


def test_run_project_watch_returns_done_envelope(temp_db, monkeypatch):
    """watch 项目 run：同步计算落库，返回 {job_id=None, status='done', project(最新 result)}。"""
    from app.api.paper import run_project
    import app.api.paper as P

    monkeypatch.setattr(
        P,
        "_run_watch",
        lambda code, period, signals: (
            {"indicators": {}, "triggered": [], "recent": []},
            "2026-01-01",
        ),
    )
    pid = _seed_project(temp_db, kind="watch")

    res = run_project(pid, temp_db())
    assert res["job_id"] is None
    assert res["status"] == "done"
    assert res["project"]["result"] is not None
    assert res["project"]["result"]["snapshot"] == {}
    assert res["project"]["kind"] == "watch"


def test_project_lock_reclaimed_after_run(temp_db, monkeypatch):
    """per-project 并发锁用完即回收（P2-47）：run 结束后 _project_locks 不残留一次性项目 id。"""
    import app.api.paper as P
    import app.core.tasks.runner as Q
    from app.api.paper import run_project

    monkeypatch.setattr(Q, "_run_paper", lambda jid, params: None)
    pid = _seed_project(temp_db)
    with P._project_locks_guard:
        P._project_locks.clear()

    run_project(pid, temp_db())
    with P._project_locks_guard:
        assert pid not in P._project_locks

    # 409 路径（已有活动任务）同样不残留
    db = temp_db()
    db.add(
        ExperimentJob(
            job_type="paper_experiment", status="pending", params={"project_id": pid}
        )
    )
    db.commit()
    db.close()
    with pytest.raises(HTTPException) as e:
        run_project(pid, temp_db())
    assert e.value.status_code == 409
    with P._project_locks_guard:
        assert pid not in P._project_locks


# ---------------------------------------------------------------------------
# 取消语义：任务 cancelled，项目 result 保持旧值
# ---------------------------------------------------------------------------


def test_cancel_paper_job_keeps_old_project_result(temp_db):
    from app.core.tasks.runner import delete_job

    old_result = {
        "code": "600519.SH",
        "period": "daily",
        "days": 80,
        "computed_bars": 80,
    }
    pid = _seed_project(temp_db, result=old_result)

    db = temp_db()
    job = ExperimentJob(
        job_type="paper_experiment",
        status="running",
        progress=30.0,
        params={"project_id": pid},
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert delete_job(jid) == {"ok": True, "deleted": False}
    db = temp_db()
    row = db.get(ExperimentJob, jid)
    p2 = db.get(PaperProject, pid)
    db.close()
    assert row.status == "cancelled"
    assert row.error == "已被用户取消"
    assert p2.result == old_result  # 项目 result 保持旧值


# ---------------------------------------------------------------------------
# 重启恢复：遗留的 pending/running/paused → failed + 服务重启中断
# ---------------------------------------------------------------------------


def test_recover_stale_jobs_flags_leftover(temp_db):
    """T-122 恢复语义：pending/running → pending 重排队（不再无条件置 failed），
    paused 保持暂停；终态任务不受影响。"""
    from app.core.tasks.runner import recover_stale_jobs

    db = temp_db()
    for st in ("pending", "running", "paused"):
        db.add(ExperimentJob(job_type="gp_run", params={}, status=st))
    db.add(ExperimentJob(job_type="gp_run", params={}, status="done"))
    db.add(ExperimentJob(job_type="gp_run", params={}, status="failed"))
    db.commit()

    n = recover_stale_jobs(db)
    assert n == 2  # pending+running 重排队；paused 保持
    rows = db.query(ExperimentJob).all()
    assert len(rows) == 5
    requeued = [r for r in rows if r.status == "pending"]
    assert len(requeued) == 2  # 原 pending/running
    assert all(r.params.get("_restart_count") == 1 for r in requeued)
    assert any(r.status == "paused" for r in rows)  # 暂停语义不丢
    # 终态任务不受影响
    assert any(r.status == "done" for r in rows)
    assert any(
        r.status == "failed" and "服务重启中断" not in (r.error or "") for r in rows
    )
    db.close()


def test_recover_stale_jobs_own_session(temp_db):
    """不传 db 时自建会话（main.py lifespan 调用路径）。"""
    from app.core.tasks.runner import recover_stale_jobs

    db = temp_db()
    db.add(ExperimentJob(job_type="evaluate", params={}, status="pending"))
    db.commit()
    db.close()

    n = recover_stale_jobs()  # 使用 monkeypatch 后的临时库
    assert n == 1

    db = temp_db()
    row = db.query(ExperimentJob).one()
    db.close()
    assert row.status == "pending" and row.params.get("_restart_count") == 1


# ---------------------------------------------------------------------------
# 任务系统接入：白名单 / labels / 防重查询
# ---------------------------------------------------------------------------


def test_paper_experiment_registered_in_system(temp_db, monkeypatch):
    """paper_experiment 在提交白名单、JOB_LABELS 与防重查询中均已注册。"""
    from app.core.tasks.runner import _JOB_LABELS, has_active_paper_job

    assert _JOB_LABELS["paper_experiment"] == "模拟盘实验"

    pid = _seed_project(temp_db)
    assert has_active_paper_job(pid) is False

    db = temp_db()
    db.add(
        ExperimentJob(
            job_type="paper_experiment", status="running", params={"project_id": pid}
        )
    )
    db.commit()
    db.close()
    assert has_active_paper_job(pid) is True

    # 仅匹配 paper_experiment 类型
    other = _seed_project(temp_db, name="other")
    db = temp_db()
    db.add(
        ExperimentJob(job_type="gp_run", status="running", params={"project_id": other})
    )
    db.commit()
    db.close()
    assert has_active_paper_job(other) is False

    # 终态任务不计入防重
    db = temp_db()
    db.add(
        ExperimentJob(
            job_type="paper_experiment", status="done", params={"project_id": pid}
        )
    )
    db.commit()
    db.close()
    assert has_active_paper_job(pid) is True  # 仍有 running 的那条
