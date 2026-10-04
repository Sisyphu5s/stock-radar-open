"""C11a 面板冷缓存重建任务化测试：冷缓存 → 202 + panel_build 任务；热缓存 → 200 原样。

覆盖：
- panel_ready 只读探测（内存热缓存含 codes 命中 / 旧版无 codes 失效 / 磁盘冷缓存命中）；
- 6 个同步面板端点（alpha101/evaluate、optimize、combine + factors 3 个 analysis）：
  冷缓存 → 202 {task_id, status: queued, message} + job_type=panel_build 提交；
  热缓存 → 保持原 200 响应结构；
- panel_build 任务端到端：冷缓存 202 → 任务 done（result 摘要）→ 重访端点 200 热数据；
- panel_build 注册（registry/_RUNNERS/_JOB_LABELS/实验白名单）。
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models.jobs import ExperimentJob  # noqa: F401  (注册 experiment_jobs 表)


def _fake_panel(S=40, T=120, seed=1):
    rng = np.random.default_rng(seed)
    close = np.exp(np.cumsum(rng.normal(0.0005, 0.01, size=(S, T)), axis=1)) * 10
    return {
        "panel": {"close": close.astype(np.float32)},
        "dates": [f"2024-01-{i % 28 + 1:02d}" for i in range(T)],
        "codes": [f"{600000 + i}.SH" for i in range(S)],
        "stock_count": S,
    }


def _fake_alpha():
    return {
        "name": "x",
        "formula": "ts_mean(close,5)",
        "desc": "",
        "usage": "",
    }


def _wait_job(job_id: int, timeout: float = 30.0) -> dict:
    """轮询任务至终态（done/failed/cancelled），返回 job dict。"""
    from app.core.tasks.runner import get_job

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_job(job_id)
        if last is not None and last["status"] in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"任务 {job_id} 轮询超时（末态 {last and last['status']}）")


@pytest.fixture()
def temp_task_db(tmp_path, monkeypatch):
    """临时任务库：runner 的 SessionLocal 指向临时 SQLite，隔离任务状态写入。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tasks.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.storage.db as DB
    import app.core.tasks.runner as RUNNER

    monkeypatch.setattr(DB, "SessionLocal", Maker)
    monkeypatch.setattr(RUNNER, "SessionLocal", Maker)
    # 清理跨测试残留的进程级控制状态（取消/暂停集合按 job_id 索引，临时库 id 会重号）
    with RUNNER._running_lock:
        RUNNER._running.clear()
        RUNNER._cancelled.clear()
        RUNNER._paused.clear()
    yield Maker


# ---------------------------------------------------------------------------
# panel_ready 只读探测
# ---------------------------------------------------------------------------


def test_panel_ready_memory_hit_and_stale(monkeypatch):
    """内存热缓存含 codes → 命中；旧版热缓存（无 codes）→ 失效；无缓存 → miss。"""
    from app.core import datasets as AD
    from app.storage.cache import panel_cache

    monkeypatch.setattr(AD, "_load_panel_disk", lambda *a, **k: None)
    panel_cache.clear()
    panel_cache.set(
        "panel:1:close",
        {
            "panel": {},
            "codes": ["600000.SH"],
            "dates": ["2024-01-01"],
            "stock_count": 1,
        },
    )
    assert AD.panel_ready(1, features=["close"]) is True
    panel_cache.clear()
    panel_cache.set("panel:2:close", {"panel": {}})  # 旧版无 codes
    assert AD.panel_ready(2, features=["close"]) is False
    assert AD.panel_ready(3, features=["close"]) is False  # 无任何缓存


def test_panel_ready_disk_hit(monkeypatch):
    """磁盘冷缓存命中（TTL/row_count/特征校验通过）→ True，且不触发重建。"""
    from app.core import datasets as AD

    called = {"load_panel": 0}
    monkeypatch.setattr(
        AD,
        "_load_panel_disk",
        lambda *a, **k: {
            "panel": {"close": np.zeros((2, 3))},
            "dates": ["2024-01-01"],
            "codes": ["600000.SH"],
        },
    )
    assert AD.panel_ready(7) is True


# ---------------------------------------------------------------------------
# 冷缓存 → 202 + panel_build 任务（6 端点全覆盖）
# ---------------------------------------------------------------------------


def test_cold_cache_202_all_endpoints(monkeypatch):
    """6 个同步面板端点冷缓存 → 202 {task_id, status: queued, message} + panel_build。"""
    import app.core.datasets as AD
    import app.core.tasks.runner as RUNNER
    import app.lib.alpha.alpha101 as A101
    from app.api.alpha import alpha101_evaluate, alpha_combine, alpha_optimize
    from app.api.factors import factor_attribution, factor_correlation, factor_ic_decay

    monkeypatch.setattr(A101, "get_alpha", lambda aid: _fake_alpha())
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: False)
    seen: list[int] = []
    monkeypatch.setattr(RUNNER, "submit_panel_build", lambda ds: seen.append(ds) or 42)

    cases = [
        lambda: alpha101_evaluate({"alpha_id": 1, "dataset_id": 1}),
        lambda: alpha_optimize({"expression": "rank(close)", "dataset_id": 1}),
        lambda: alpha_combine({"exprs": ["rank(close)"], "dataset_id": 1}),
        lambda: factor_correlation({"factor_ids": [1], "dataset_id": 1}),
        lambda: factor_ic_decay({"expr": "close", "dataset_id": 1}),
        lambda: factor_attribution({"expr": "close", "dataset_id": 1}),
    ]
    for call in cases:
        resp = call()
        assert isinstance(resp, JSONResponse), f"期望 202 JSONResponse, 实得 {resp!r}"
        assert resp.status_code == 202
        body = json.loads(resp.body)
        assert body["status"] == "queued"
        assert body["task_id"] == 42
        assert isinstance(body["message"], str) and body["message"]
    assert len(seen) == 6 and all(ds == 1 for ds in seen)


def test_alpha101_evaluate_invalid_dataset_400(monkeypatch):
    """ds<=0 保持 400（与 alpha101_score/tune 同口径，不提交任务）。"""
    import app.core.datasets as AD
    import app.lib.alpha.alpha101 as A101
    from app.api.alpha import alpha101_evaluate

    monkeypatch.setattr(A101, "get_alpha", lambda aid: _fake_alpha())
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)
    with pytest.raises(HTTPException) as e:
        alpha101_evaluate({"alpha_id": 1, "dataset_id": 0})
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# 热缓存 → 200 原样
# ---------------------------------------------------------------------------


def test_alpha_optimize_hot_cache_200(monkeypatch):
    """热缓存命中 → 保持原 200 同步响应结构（不提交任务）。"""
    import app.core.datasets as AD
    from app.api.alpha import alpha_optimize

    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)
    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    res = alpha_optimize(
        {"expression": "rank(close)", "dataset_id": 1, "top_n": 20, "method": "min_var"}
    )
    assert isinstance(res, dict) and "weights" in res
    assert len(res["weights"]) == 20
    assert abs(sum(x["weight"] for x in res["weights"]) - 1.0) < 1e-4
    assert res["perf"]["annual_return"] is not None


def test_factor_analysis_hot_cache_200(monkeypatch):
    """factors 分析端点热缓存命中 → 原 200 响应（IC 衰减结构完整）。"""
    import app.core.datasets as AD
    from app.api.factors import factor_ic_decay

    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)
    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel(T=60))
    out = factor_ic_decay({"expr": "rank(close)", "dataset_id": 1})
    assert out["horizons"] == [1, 3, 5, 10, 20]
    assert len(out["ic_means"]) == 5


# ---------------------------------------------------------------------------
# panel_build 任务端到端（验收路径：202 → done → 重访 200 热数据）
# ---------------------------------------------------------------------------


def test_panel_build_task_end_to_end(temp_task_db, monkeypatch):
    """冷缓存 → 202 提交真实任务 → 任务 done（result 摘要）→ 重访端点 200 热数据。"""
    import app.core.datasets as AD
    from app.api.alpha import alpha_optimize

    state = {"ready": False}
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: state["ready"])
    # 端点与任务 handler 均经「函数内 from ..core.datasets import」运行时解析模块属性，
    # 单点 monkeypatch 双路径穿透
    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())

    payload = {"expression": "rank(close)", "dataset_id": 1, "top_n": 20}
    resp = alpha_optimize(payload)
    assert resp.status_code == 202
    job_id = json.loads(resp.body)["task_id"]

    job = _wait_job(job_id)
    assert job["status"] == "done", f"任务失败: {job.get('error')}"
    assert job["result"]["dataset_id"] == 1
    assert job["result"]["stock_count"] == 40
    assert job["result"]["days"] == 120

    # 任务完成后重访同参数 → 200 热数据（验收：同参数重访返回 200）
    state["ready"] = True
    res = alpha_optimize(payload)
    assert isinstance(res, dict) and len(res["weights"]) == 20


def test_panel_build_handler_none_panel_failed(temp_task_db, monkeypatch):
    """数据集无成分/未构建（load_panel 返回 None）→ 任务 failed，不留 done。"""
    import app.core.datasets as AD
    from app.core.tasks.handlers.panel_build import _run_panel_build

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: None)
    job_id = _submit_raw(temp_task_db)
    _run_panel_build(job_id, {"dataset_id": 1})
    job = _wait_job(job_id)
    assert job["status"] == "failed"
    assert "无成分数据" in (job.get("error") or "")


def _submit_raw(temp_task_db) -> int:
    """直接写一条 pending 任务行（不启动 worker），供 handler 单测驱动。"""
    from app.core.tasks.runner import utcnow

    db = temp_task_db()
    from app.storage.models import ExperimentJob

    job = ExperimentJob(
        job_type="panel_build", params={"dataset_id": 1}, status="pending"
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id
    db.close()
    return jid


# ---------------------------------------------------------------------------
# P2-42 面板构建去重：并发冷缓存 miss → 单任务（双份全量重建+双写 npz 防护）
# ---------------------------------------------------------------------------


def test_panel_build_dedup_reuses_active_job(temp_task_db, monkeypatch):
    """已有活动（pending）panel_build 任务时复用其 job_id，不重复提交。"""
    import app.core.tasks.runner as RUNNER

    db = temp_task_db()
    job = ExperimentJob(
        job_type="panel_build", params={"dataset_id": 7}, status="pending"
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    called = []

    def fake_submit(job_type, params):
        called.append((job_type, params))
        return 999

    monkeypatch.setattr(RUNNER, "submit", fake_submit)
    assert RUNNER.submit_panel_build(7) == jid
    assert called == []  # 未重复提交
    assert RUNNER.submit_panel_build(8) == 999  # 无活动任务 → 正常提交
    assert called == [("panel_build", {"dataset_id": 8})]


def test_panel_build_dedup_concurrent(temp_task_db, monkeypatch):
    """双并发冷缓存 miss（同一 dataset）→ 只提交一个 panel_build 任务。

    首个任务的 worker 在 load_panel 阻塞（模拟重建中），并发方在锁内回读活动任务
    复用其 job_id——panel_ready 检查与 submit 的竞态窗口被 per-dataset 锁关闭。
    """
    import threading

    import app.core.datasets as AD
    import app.core.tasks.runner as RUNNER

    hold = threading.Event()
    monkeypatch.setattr(
        AD,
        "load_panel",
        lambda ds, features=None: (hold.wait(timeout=20) or _fake_panel()),
    )

    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def caller():
        barrier.wait()
        jid = RUNNER.submit_panel_build(5)
        with lock:
            results.append(jid)

    ts = [threading.Thread(target=caller) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in ts)

    hold.set()  # 放行首个任务的 worker 完成重建
    deadline = time.time() + 10
    while time.time() < deadline:
        db = temp_task_db()
        n = (
            db.query(ExperimentJob)
            .filter(ExperimentJob.job_type == "panel_build")
            .count()
        )
        db.close()
        if n == 1:
            break
        time.sleep(0.02)

    assert len(results) == 2 and results[0] == results[1]
    db = temp_task_db()
    rows = (
        db.query(ExperimentJob)
        .filter(ExperimentJob.job_type == "panel_build")
        .all()
    )
    db.close()
    assert len(rows) == 1
    assert (rows[0].params or {}).get("dataset_id") == 5


# ---------------------------------------------------------------------------
# 注册与白名单
# ---------------------------------------------------------------------------


def test_panel_build_registered():
    """panel_build 注册：registry 分派 + _RUNNERS 转发 + _JOB_LABELS 文案。"""
    from app.core.tasks import registry, runner

    assert "panel_build" in registry.HANDLERS
    assert callable(registry.HANDLERS["panel_build"])
    assert runner._RUNNERS["panel_build"] == "_run_panel_build"
    assert runner._JOB_LABELS["panel_build"] == "面板构建"
    assert callable(runner._run_panel_build)


def test_panel_build_in_experiment_whitelist(monkeypatch):
    """通用实验创建端点白名单含 panel_build（任务中心可手动创建/重跑）。"""
    from app.api import experiments

    seen = {}

    def fake_submit(job_type, params):
        seen["job_type"] = job_type
        return 1

    monkeypatch.setattr(experiments, "submit", fake_submit)
    res = experiments.create_experiment(
        {"job_type": "panel_build", "params": {"dataset_id": 1}}
    )
    assert seen["job_type"] == "panel_build"
    assert res["status"] == "pending"
