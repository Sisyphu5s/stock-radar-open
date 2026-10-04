"""协作式暂停/恢复聚焦测试：paused 状态、pending/running 暂停、paused 取消、
完成竞态、超时不记暂停、非法状态。临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

import threading
import time

import pytest
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

    # 清理跨测试残留的模块级状态
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


def _install_alpha101_fakes(monkeypatch, evaluate=None, n: int = 20):
    """把 alpha101 全库评分替换为可观测的假实现（与取消竞态测试同款打桩）。"""
    import app.lib.alpha.alpha101 as A101
    import app.core.datasets as AD
    import app.lib.alpha.evaluate as AE
    import app.lib.alpha.operators as AO

    def fake_list_alpha101():
        return [{"id": i, "name": f"f{i}", "formula": f"close{i}"} for i in range(n)]

    def fake_compile_rpn(f):
        return f

    def fake_load_panel(ds_id, features=None):
        import numpy as np

        return {"panel": {"close": np.zeros((5, 40))}, "dates": ["2026-01-01"] * 40}

    def fake_forward_returns(close, horizon=5):
        return close

    monkeypatch.setattr(A101, "list_alpha101", fake_list_alpha101)
    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    if evaluate is None:
        evaluate = lambda rpn, panel, fwd, horizon=5: {"ic": 0.1, "stability": 0.5}  # noqa: E731
    monkeypatch.setattr(AE, "evaluate_factor", evaluate)


# ---------------------------------------------------------------------------
# running 暂停 → 恢复
# ---------------------------------------------------------------------------


def test_running_pause_blocks_then_resume_completes(temp_db, monkeypatch):
    """运行中暂停：worker 在下一检查点阻塞（进度冻结、求值停摆），
    恢复后继续并正常完成。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import submit

    calls = []
    N = 30

    def fake_evaluate(rpn, panel, fwd, horizon=5):
        calls.append(rpn)
        time.sleep(0.02)
        return {"ic": 0.1, "stability": 0.5}

    _install_alpha101_fakes(monkeypatch, evaluate=fake_evaluate, n=N)

    jid = submit("alpha101_score", {"dataset_id": 1, "limit": N})
    _wait_status(temp_db, jid, "running")

    r = Q.pause_job(jid)
    assert r["ok"] is True and r["status"] == "paused" and r["cooperative"] is True
    assert "message" in r

    time.sleep(0.4)  # 等 worker 在下一检查点停下

    def snapshot():
        db = temp_db()
        row = db.get(ExperimentJob, jid)
        db.close()
        return row.status, row.progress, len(calls)

    s1 = snapshot()
    time.sleep(0.5)
    s2 = snapshot()
    assert s1[0] == "paused" and s2[0] == "paused"
    assert s2[1] == s1[1]  # 进度冻结
    assert s2[2] - s1[2] <= 2  # 至多在途 1 个因子求值

    r2 = Q.resume_job(jid)
    assert r2["ok"] is True and r2["status"] == "running" and r2["cooperative"] is True

    _wait_status(temp_db, jid, "done")
    assert len(calls) == N
    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done" and row.progress == 100.0
    assert len(row.result.get("results", [])) == N


# ---------------------------------------------------------------------------
# pending 暂停 → 恢复
# ---------------------------------------------------------------------------


def test_pending_pause_blocks_worker_start_until_resume(temp_db, monkeypatch):
    """pending 暂停：worker 不得启动计算；恢复后继续启动并完成。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import _run_alpha101_score, resume_job

    calls = []
    N = 8

    def fake_evaluate(rpn, panel, fwd, horizon=5):
        calls.append(rpn)
        return {"ic": 0.1, "stability": 0.5}

    _install_alpha101_fakes(monkeypatch, evaluate=fake_evaluate, n=N)

    db = temp_db()
    job = ExperimentJob(job_type="alpha101_score", params={}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert Q.pause_job(jid)["ok"] is True
    db = temp_db()
    assert db.get(ExperimentJob, jid).status == "paused"
    db.close()

    t = threading.Thread(
        target=lambda: _run_alpha101_score(jid, {"dataset_id": 1, "limit": N})
    )
    t.start()
    time.sleep(0.3)
    assert t.is_alive()  # 阻塞在启动控制点
    assert calls == []  # 未启动任何底层计算

    r = resume_job(jid)
    assert r["ok"] is True and r["status"] == "pending"  # 来源 pending → 回 pending
    t.join(timeout=10)
    assert not t.is_alive()

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done" and len(calls) == N
    assert row.result.get("meta", {}).get("scored") == N


# ---------------------------------------------------------------------------
# paused 取消
# ---------------------------------------------------------------------------


def test_pause_cancel_wakes_worker_and_terminates(temp_db, monkeypatch):
    """取消能唤醒 paused 线程并终止：worker 退出、终态保持取消。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import delete_job, submit

    calls = []
    N = 40

    def fake_evaluate(rpn, panel, fwd, horizon=5):
        calls.append(rpn)
        time.sleep(0.02)
        return {"ic": 0.1, "stability": 0.5}

    _install_alpha101_fakes(monkeypatch, evaluate=fake_evaluate, n=N)

    jid = submit("alpha101_score", {"dataset_id": 1, "limit": N})
    _wait_status(temp_db, jid, "running")

    assert Q.pause_job(jid)["ok"] is True
    time.sleep(0.4)
    c1 = len(calls)
    time.sleep(0.3)
    assert len(calls) - c1 <= 2  # 已停在检查点

    assert delete_job(jid) == {"ok": True, "deleted": False}

    # 等 worker finally 清理取消登记（即计算线程已完全退出）
    deadline = time.time() + 10
    while time.time() < deadline:
        with Q._running_lock:
            gone = jid not in Q._cancelled
        if gone:
            break
        time.sleep(0.01)
    assert gone

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled" and row.error == "已被用户取消"
    assert row.result == {}  # 未写入 done 结果
    assert row.progress < 100.0
    assert len(calls) < N  # 循环被中止


def test_cancel_supports_paused_direct(temp_db):
    """无 worker 的 paused 任务 DELETE：置 cancelled 并登记取消，后续写入被拦截。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import delete_job

    db = temp_db()
    job = ExperimentJob(job_type="gp_run", params={}, status="paused", progress=50.0)
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert delete_job(jid) == {"ok": True, "deleted": False}
    with Q._running_lock:
        assert jid in Q._cancelled

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert (
        row.status == "cancelled"
        and row.error == "已被用户取消"
        and row.finished_at is not None
    )

    # 后台晚到的写入全部被条件更新拦截
    assert Q._update(jid, status="done", progress=100.0, result={"x": 1}) == 0
    assert Q._update(jid, progress=80.0) == 0


# ---------------------------------------------------------------------------
# 完成竞态：终态写入 vs 暂停
# ---------------------------------------------------------------------------


def test_completion_race_paused_blocks_terminal_write_then_done(temp_db):
    """完成竞态：worker 计算完成、写终态时暂停介入 → 终态阻塞且 paused 不被覆盖，
    恢复后 done 正常落库。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import resume_job

    db = temp_db()
    job = ExperimentJob(job_type="evaluate", params={}, status="running", progress=99.0)
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    Q._track(jid, 300)
    assert Q.pause_job(jid)["ok"] is True

    # 暂停期间直接 CAS 写终态被拦截（_update 仅匹配 pending/running）
    assert (
        Q._update(
            jid, status="done", progress=100.0, result={"ok": 1}, finished_at=utcnow()
        )
        == 0
    )
    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "paused" and row.progress == 99.0 and row.result == {}

    # worker 线程在终态控制点阻塞（结果已算完，只等恢复落库）
    t = threading.Thread(
        target=lambda: Q._terminal(
            jid, status="done", progress=100.0, result={"ok": 1}, finished_at=utcnow()
        )
    )
    t.start()
    time.sleep(0.2)
    assert t.is_alive()
    db = temp_db()
    assert db.get(ExperimentJob, jid).status == "paused"
    db.close()

    # 恢复后 worker 的终态写入落库
    assert resume_job(jid)["ok"] is True
    t.join(timeout=5)
    assert not t.is_alive()

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done" and row.result == {"ok": 1} and row.progress == 100.0


# ---------------------------------------------------------------------------
# 超时不计暂停
# ---------------------------------------------------------------------------


def test_pause_does_not_count_toward_timeout(temp_db):
    """暂停冻结超时登记：暂停期间 _running 不计时，恢复后剩余时间≈冻结前剩余。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    job = ExperimentJob(job_type="evaluate", params={}, status="running")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    Q._track(jid, 60)
    assert Q.pause_job(jid)["ok"] is True
    with Q._running_lock:
        assert jid not in Q._running  # 暂停冻结：登记已弹出

    time.sleep(1.5)
    with Q._running_lock:
        assert jid not in Q._running  # 暂停时间不计入超时

    assert Q.resume_job(jid)["ok"] is True
    with Q._running_lock:
        deadline, timeout_sec = Q._running[jid]
    remaining = deadline - time.time()
    assert timeout_sec == 60
    assert remaining > 55, remaining  # 冻结前剩余 ~60s，不含暂停的 1.5s


def test_resume_after_timeout_popped_gets_tiny_remaining(temp_db):
    """running 任务但超时登记已被弹出（_running 无条目）→ resume 按极小剩余恢复，
    不重新给满超时（堵住暂停/恢复无限续期漏洞）。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    job = ExperimentJob(job_type="evaluate", params={}, status="running")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    # 模拟：任务 running 但超时登记已被监控线程弹出（_freeze_timeout 无条目可冻结）
    assert Q.pause_job(jid)["ok"] is True
    with Q._running_lock:
        assert jid not in Q._running

    assert Q.resume_job(jid)["ok"] is True
    with Q._running_lock:
        deadline, timeout_sec = Q._running[jid]
    remaining = deadline - time.time()
    assert timeout_sec == 900
    assert remaining <= 5, remaining  # 极小剩余（1s），不是重新给满 900s


def test_resume_pending_origin_gets_full_timeout(temp_db):
    """pending 来源暂停（从未计时）→ resume 重新给满超时（原语义保持，非续期漏洞）。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    job = ExperimentJob(job_type="evaluate", params={}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert Q.pause_job(jid)["ok"] is True
    assert Q.resume_job(jid)["ok"] is True
    with Q._running_lock:
        deadline, timeout_sec = Q._running[jid]
    remaining = deadline - time.time()
    assert timeout_sec == 900
    assert remaining > 890, remaining  # 满超时重新计时


# ---------------------------------------------------------------------------
# 非法状态 / 不存在
# ---------------------------------------------------------------------------


def test_pause_resume_illegal_states(temp_db):
    import app.core.tasks.runner as Q

    db = temp_db()
    done = ExperimentJob(job_type="evaluate", params={}, status="done")
    running = ExperimentJob(job_type="evaluate", params={}, status="running")
    failed = ExperimentJob(job_type="evaluate", params={}, status="failed")
    db.add_all([done, running, failed])
    db.commit()
    done_id, running_id, failed_id = done.id, running.id, failed.id
    db.close()

    # 非 pending/running 不可暂停
    r = Q.pause_job(done_id)
    assert r["ok"] is False and "无法暂停" in r["error"] and r["status"] == "done"
    r = Q.pause_job(failed_id)
    assert r["ok"] is False

    # 非 paused 不可恢复
    r = Q.resume_job(running_id)
    assert r["ok"] is False and r["status"] == "running"
    r = Q.resume_job(done_id)
    assert r["ok"] is False

    # paused 不可重复暂停；resume 后不可再次 resume
    assert Q.pause_job(running_id)["ok"] is True
    r = Q.pause_job(running_id)
    assert r["ok"] is False
    assert Q.resume_job(running_id)["ok"] is True
    r = Q.resume_job(running_id)
    assert r["ok"] is False

    # 不存在 → None（API 层 404）
    assert Q.pause_job(99999) is None
    assert Q.resume_job(99999) is None
    assert Q.pause_job(0) is None


# ---------------------------------------------------------------------------
# list / detail 返回 paused；API 响应语义
# ---------------------------------------------------------------------------


def test_paused_in_list_detail_and_status_filter(temp_db):
    from app.api.experiments import experiment_list_page
    from app.core.tasks.runner import get_job, list_jobs

    db = temp_db()
    db.add(
        ExperimentJob(
            job_type="gp_run",
            params={"expression": "close"},
            status="paused",
            progress=40.0,
        )
    )
    db.add(
        ExperimentJob(
            job_type="gp_run",
            params={"expression": "close"},
            status="done",
            progress=100.0,
        )
    )
    db.commit()
    paused_id = (
        db.query(ExperimentJob).filter(ExperimentJob.status == "paused").one().id
    )
    db.close()

    detail = get_job(paused_id)
    assert detail is not None and detail["status"] == "paused"

    items = list_jobs(10)
    assert {it["status"] for it in items} == {"paused", "done"}

    page = experiment_list_page(limit=50, offset=0, status="paused")
    assert page["total"] == 1 and page["items"][0]["id"] == paused_id

    # active 别名 = pending + running，不含 paused
    page2 = experiment_list_page(limit=50, offset=0, status="active")
    assert page2["total"] == 0


def test_pause_resume_api_response_semantics(temp_db):
    import app.core.tasks.runner as Q

    db = temp_db()
    job = ExperimentJob(job_type="evaluate", params={}, status="running")
    job2 = ExperimentJob(job_type="evaluate", params={}, status="pending")
    db.add_all([job, job2])
    db.commit()
    jid, jid2 = job.id, job2.id
    db.close()

    r = Q.pause_job(jid)
    assert r["status"] == "paused" and r["cooperative"] is True
    r2 = Q.resume_job(jid)
    assert r2["status"] == "running" and r2["cooperative"] is True

    # pending 来源暂停 → 恢复回 pending
    assert Q.pause_job(jid2)["ok"] is True
    r3 = Q.resume_job(jid2)
    assert r3["ok"] is True and r3["status"] == "pending"


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------


def test_pause_resume_routes_registered():
    from app.api.experiments import router as er

    paths = [r.path for r in er.routes]
    assert "/experiments/{job_id}/pause" in paths
    assert "/experiments/{job_id}/resume" in paths
