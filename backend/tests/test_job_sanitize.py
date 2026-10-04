"""任务结果 JSON 卫生测试：evaluate stability NaN 守卫（P1-1）、_sanitize_json
递归清洗（_terminal 落库前）、_terminal DB 异常兜底（P1-3）。

临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import ExperimentJob, utcnow


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue.SessionLocal（计算线程/API 走同一临时库）。"""
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


def _seed_running(maker) -> int:
    db = maker()
    job = ExperimentJob(job_type="evaluate", params={}, status="running")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


# ---------------------------------------------------------------------------
# P1-1：evaluate stability NaN 守卫
# ---------------------------------------------------------------------------


def test_evaluate_stability_nan_guarded():
    """短面板（有效 IC 期数 < 10）→ stability 为 None 而非 NaN（详情端点 json 不再 500）。"""
    from app.lib.alpha.evaluate import evaluate_factor
    from app.lib.alpha.operators import compile_rpn

    rpn = compile_rpn("close")
    data = {
        "close": np.arange(20.0).reshape(4, 5)
    }  # 4 股 × 5 期（恒值 fwd → 全 NaN IC）
    fwd = np.ones((4, 5))
    ev = evaluate_factor(rpn, data, fwd)
    assert ev["stability"] is None
    # 所有数值字段均无 NaN/Inf（json 可安全编码）
    for v in ev.values():
        assert not (isinstance(v, float) and not math.isfinite(v))


# ---------------------------------------------------------------------------
# P1-1：_sanitize_json 递归清洗
# ---------------------------------------------------------------------------


def test_sanitize_json_nan_to_none():
    from app.core.tasks.runner import _sanitize_json

    out = _sanitize_json(
        {
            "metrics": {"stability": float("nan"), "ic": float("inf")},
            "nested": {"v": np.float32("nan")},
            "ok": [1.5, float("nan"), np.float64("inf")],
            "ints": np.int64(3),
            "bool": np.bool_(True),
            "keep": "文本",
        }
    )
    assert out["metrics"] == {"stability": None, "ic": None}
    assert out["nested"] == {"v": None}
    assert out["ok"] == [1.5, None, None]
    assert out["ints"] == 3
    assert out["bool"] is True
    assert out["keep"] == "文本"


def test_terminal_sanitizes_nan_result(temp_db):
    """_terminal 落库前递归清洗 result：NaN/Inf → None，DB 中为合法 JSON。"""
    import app.core.tasks.runner as Q

    jid = _seed_running(temp_db)
    Q._track(jid, 300)
    Q._terminal(
        jid,
        status="done",
        progress=100.0,
        result={
            "metrics": {"stability": float("nan"), "ic": float("inf")},
            "nested": {"v": np.float32("nan")},
            "ok": [1.5, float("nan")],
        },
        finished_at=utcnow(),
    )

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done"
    r = row.result
    assert r["metrics"] == {"stability": None, "ic": None}
    assert r["nested"] == {"v": None}
    assert r["ok"] == [1.5, None]


# ---------------------------------------------------------------------------
# P1-3：_terminal DB 异常守卫（重试 + 兜底 failed）
# ---------------------------------------------------------------------------


def test_terminal_forces_failed_after_db_failure(temp_db, monkeypatch):
    """_update 连抛 3 次（SQLite 锁竞争）→ 重试 2 次后兜底独立写入 failed，
    任务不会永久卡 running、worker 不静默死亡。"""
    import app.core.tasks.runner as Q

    calls = []
    orig_update = Q._update

    def flaky(job_id, **fields):
        calls.append(fields)
        if len(calls) <= 3:  # 首次写入 + 2 次重试均失败
            raise OperationalError("statement", {}, Exception("database is locked"))
        return orig_update(job_id, **fields)  # 第 4 次兜底调用真正落库

    monkeypatch.setattr(Q, "_update", flaky)

    jid = _seed_running(temp_db)
    Q._track(jid, 300)
    Q._terminal(
        jid,
        status="done",
        progress=100.0,
        result={"ok": 1},
        finished_at=utcnow(),
    )

    # 首次 + 重试1 + 重试2（均 done 字段）失败，第 4 次兜底 failed 成功
    assert len(calls) == 4
    assert calls[0]["status"] == "done"
    assert calls[-1]["status"] == "failed"
    assert "强制" in calls[-1]["error"]

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "failed"
    assert "强制" in (row.error or "")


def test_terminal_propagates_cancelled(temp_db, monkeypatch):
    """_checkpoint 抛 JobCancelled（用户取消）不被 DB 守卫吞掉：向上传播保持取消语义。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import JobCancelled

    jid = _seed_running(temp_db)
    Q._track(jid, 300)
    with Q._running_lock:
        Q._cancelled.add(jid)
    with pytest.raises(JobCancelled):
        Q._terminal(
            jid,
            status="done",
            progress=100.0,
            result={"ok": 1},
            finished_at=utcnow(),
        )
    with Q._running_lock:
        Q._cancelled.discard(jid)
    # 未写入任何终态（DB 状态保持 running，由 delete_job 负责置 cancelled）
    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "running"
