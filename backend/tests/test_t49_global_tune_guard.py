"""T-49 指标调优全局参数守卫测试。

- PUT /tune/global：空 params / 非法键（格式、未知指标、非法目标、v2 键）/
  不可解析值一律 400；合法 legacy 键正常保存（坏写入曾可静默覆盖已保存参数）
- tune/tune_combined 的 global_saved：v2 全局（ind:v2:{ind}）也应识别为已保存
  （原只查 legacy 键 ind:{ind}:{target}，v2 保存后恒 False）
- GET /indicators/{code} 的 days 参数：Query(ge=1)，days=0/负数 → 422
  （原无下限，tail(-n) 反向返回全历史）
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import AppParam  # noqa: F401  (注册表)
from app.storage.models.jobs import ExperimentJob  # noqa: F401  (tune 任务化后需注册 experiment_jobs 表)


def _synthetic_df(
    n: int = 300, seed: int = 7, trend: float = 0.0002, vol: float = 0.02
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 50 * np.exp(np.cumsum(rng.normal(trend, vol, n)))
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n).astype(str),
            "open": close * (1 + rng.normal(0, 0.005, n)),
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": rng.uniform(5e5, 2e6, n),
        }
    )


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB：替换 app.storage.db.SessionLocal + 合成 K 线 + 清空全局缓存。"""
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

    import app.storage.db as DB

    monkeypatch.setattr(DB, "SessionLocal", Maker)
    # tune 任务化后走任务队列:runner 的 SessionLocal 须指向临时库(与 test_indicator_params 同策略)
    import app.core.tasks.runner as RUNNER

    monkeypatch.setattr(RUNNER, "SessionLocal", Maker)
    # 清理跨测试残留的进程级控制状态(取消/暂停集合按 job_id 索引,临时库 id 会重号)
    with RUNNER._running_lock:
        RUNNER._running.clear()
        RUNNER._cancelled.clear()
        RUNNER._paused.clear()
    # 直接覆盖 app.api.indicators 命名空间里的 kcache 绑定（模块顶部 import 固定了引用，
    # 只 patch kcache 模块本身在 indicators 先被其他测试文件导入时不生效）
    import app.api.indicators as I

    monkeypatch.setattr(I, "cached_kline", lambda code, period, **kw: _synthetic_df())
    monkeypatch.setattr(I, "get_version", lambda *a, **k: 0)
    from app.storage import cache as CACHE

    CACHE.indicator_cache.clear()
    CACHE.tune_cache.clear()
    yield Maker


# ---------------------------------------------------------------------------
# 1) PUT /tune/global 键值校验
# ---------------------------------------------------------------------------


def test_tune_global_put_accepts_legacy_keys(temp_db):
    from app.api.indicators import tune_global_put
    from app.storage.appparams import get_app_params

    r = tune_global_put({"params": {"ind:rsi:ic": "6", "ind:macd:ic": "6,13,5"}})
    assert r["ok"] is True
    assert set(r["saved"]) == {"ind:rsi:ic", "ind:macd:ic"}
    stored = get_app_params("ind:")
    assert stored["ind:rsi:ic"] == "6"
    assert stored["ind:macd:ic"] == "6,13,5"


def test_tune_global_put_rejects_bad_payloads(temp_db):
    from app.api.indicators import tune_global_put

    def raises400(payload, needle=""):
        with pytest.raises(HTTPException) as ei:
            tune_global_put(payload)
        assert ei.value.status_code == 400, payload
        if needle:
            assert needle in str(ei.value.detail)

    raises400({"params": {}}, "不能为空")  # 零键：静默 no-op 会误导前端
    raises400({"params": None}, "不能为空")
    raises400({}, "不能为空")
    raises400({"params": "abc"}, "dict")  # 非 dict
    raises400({"params": {"foo": "1"}}, "ind:")  # 键非 ind: 前缀
    raises400({"params": {"ind:rsi": "6"}}, "ind:")  # 缺目标段
    raises400({"params": {"ind:rsi:ic:extra": "6"}}, "ind:")  # 多段
    raises400({"params": {"ind:nope:ic": "6"}}, "未知指标")  # 未知指标
    raises400({"params": {"ind:rsi:badtarget": "6"}}, "目标")  # 非法目标
    raises400(
        {"params": {"ind:v2:rsi": '{"values": {"window": 21}}'}}, "未知指标"
    )  # v2 键走 /params/{indicator}
    raises400({"params": {"ind:rsi:ic": "abc"}}, "无法解析")  # 非数值
    raises400({"params": {"ind:rsi:ic": "6,10"}}, "无法解析")  # 参数个数不匹配
    raises400(
        {"params": {"ind:rsi:ic": "6", "ind:nope:ic": "1"}}
    )  # 混合合法+非法:整包拒绝


def test_tune_global_put_reject_is_atomic(temp_db):
    """非法键存在时整包拒绝，且不写入任何键（避免半保存覆盖已调优参数）。"""
    from app.api.indicators import tune_global_put
    from app.storage.appparams import get_app_params, set_app_params

    set_app_params({"ind:rsi:ic": "6"})  # 已保存的合法参数
    with pytest.raises(HTTPException):
        tune_global_put({"params": {"ind:rsi:ic": "9", "ind:bad:ic": "1"}})
    assert get_app_params("ind:")["ind:rsi:ic"] == "6"  # 未被污染


# ---------------------------------------------------------------------------
# 2) global_saved：v2 全局也应识别（原只查 legacy 键恒 False）
# ---------------------------------------------------------------------------


def _wait_job(job_id: int, timeout: float = 60.0) -> dict:
    """轮询任务至终态（tune 任务化后提交/轮询语义，同 test_indicator_params）。"""
    import time

    from app.core.tasks.runner import get_job

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_job(job_id)
        if last is not None and last["status"] in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 轮询超时（末态 {last and last['status']}）")


def test_global_saved_detects_v2_and_legacy(temp_db):
    from app.api.indicators import put_indicator_params, tune, tune_combined
    from app.storage.appparams import set_app_params

    # 无任何全局 → False
    r = tune("600519.SH", {"indicator": "rsi", "horizon": 5, "target": "ic"})
    assert r["status"] == "pending" and isinstance(r["job_id"], int)
    j = _wait_job(r["job_id"])
    assert j["status"] == "done", j.get("error")
    assert j["result"]["global_saved"] is False

    # 仅 legacy 全局 → True（既有行为保持）
    set_app_params({"ind:rsi:ic": "6"})
    r = tune("600519.SH", {"indicator": "rsi", "horizon": 5, "target": "ic"})
    j = _wait_job(r["job_id"])
    assert j["status"] == "done", j.get("error")
    assert j["result"]["global_saved"] is True

    # 仅 v2 全局（无 legacy）→ True（修复点）
    put_indicator_params(
        "macd",
        {
            "values": {"fast": 8, "slow": 17, "signal": 9},
            "source": "global",
            "target": "ic",
            "horizon": 5,
        },
    )
    r = tune("600519.SH", {"indicator": "macd", "horizon": 5, "target": "ic"})
    j = _wait_job(r["job_id"])
    assert j["status"] == "done", j.get("error")
    assert j["result"]["global_saved"] is True

    # combined 同样识别 v2
    r2 = tune_combined(
        "600519.SH", {"indicators": ["macd", "kdj"], "horizon": 5, "target": "ic"}
    )
    j2 = _wait_job(r2["job_id"])
    assert j2["status"] == "done", j2.get("error")
    assert j2["result"]["global_saved"] is True


# ---------------------------------------------------------------------------
# 3) GET /indicators/{code} days 参数下限（Query ge=1）
# ---------------------------------------------------------------------------


def _indicators_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.indicators as I

    monkeypatch.setattr(I, "cached_kline", lambda code, period, **kw: _synthetic_df())
    monkeypatch.setattr(I, "get_version", lambda *a, **k: 0)
    from app.storage import cache as CACHE

    CACHE.indicator_cache.clear()
    app = FastAPI()
    app.include_router(I.router)
    return TestClient(app)


def test_indicators_days_requires_positive(temp_db, monkeypatch):
    client = _indicators_client(monkeypatch)
    ok = client.get("/indicators/600519.SH?days=300")
    assert ok.status_code == 200
    assert len(ok.json()["dates"]) == 300
    # days=0 / 负数：Query(ge=1) 校验 → 422（原实现 tail(-n) 反向返回全历史）
    assert client.get("/indicators/600519.SH?days=0").status_code == 422
    assert client.get("/indicators/600519.SH?days=-5").status_code == 422
    assert client.get("/indicators/600519.SH?days=abc").status_code == 422
    # days 上限（P2-35，SR_MAX_INDICATOR_DAYS，默认 1000）：超限 422，防极值全量重算
    from app.config import settings

    cap = settings.max_indicator_days
    assert client.get(f"/indicators/600519.SH?days={cap}").status_code == 200
    assert client.get(f"/indicators/600519.SH?days={cap + 1}").status_code == 422
