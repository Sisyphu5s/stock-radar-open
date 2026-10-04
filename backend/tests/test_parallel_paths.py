"""并行路径聚焦测试：扫描/评分/调优/GP/数据集 的并行与串行一致性、
JobCancelled 传播、numpy 后端并行开关。临时 SQLite，不触碰生产库。"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.storage.db import Base
from app.storage.models import Dataset, DatasetStock, ExperimentJob, SignalEvent
from app.lib.alpha import backend as B


def _temp_maker(tmp_path, monkeypatch, name: str = "test.db"):
    """独立临时 DB（每调用一个文件）+ 替换 queue.SessionLocal；清理 queue 残留状态。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
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
    return Maker


@pytest.fixture()
def numpy_backend(monkeypatch):
    """强制 numpy 后端（并行开关生效的基础）。"""
    monkeypatch.setattr(settings, "gp_backend", "numpy")
    B.init_backend()
    yield


# ---------------------------------------------------------------------------
# scanner：并行 vs 串行一致性 + JobCancelled 传播
# ---------------------------------------------------------------------------


def _kline_df(n: int = 60) -> pd.DataFrame:
    close = np.linspace(10, 11 + n * 0.02, n)
    volume = np.full(n, 100.0)
    volume[-1] = 1000.0  # 触发 volume_surge
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": volume,
            "pct_change": np.full(n, 1.0),
            "amount": np.full(n, 1e6),
        }
    )


def _scan_setup(monkeypatch, maker, n_codes: int):
    import app.core.scanning as SC

    monkeypatch.setattr(SC, "SessionLocal", maker)
    codes = [f"6000{i:02d}.SH" for i in range(1, n_codes + 1)]
    monkeypatch.setattr(
        SC,
        "get_spot",
        lambda: pd.DataFrame(
            {
                "code": codes,
                "name": [f"股票{i}" for i in codes],
                "price": [10.0] * n_codes,
                "pct_change": [1.0] * n_codes,
                "volume": [100.0] * n_codes,
                "amount": [1000.0] * n_codes,
                "turnover_rate": [1.0] * n_codes,
            }
        ),
    )
    monkeypatch.setattr(
        SC, "fetch_many", lambda c, period, **kw: {code: _kline_df() for code in c}
    )
    return SC, codes


def _dump_events(maker) -> list[tuple]:
    db = maker()
    try:
        rows = db.query(SignalEvent).all()
        return sorted(
            (
                e.stock_code,
                e.triggered_at.replace(microsecond=0),
                tuple(sorted(e.signals)),
                tuple(sorted(e.evidence.items())),
            )
            for e in rows
        )
    finally:
        db.close()


def test_scan_parallel_matches_serial(tmp_path, monkeypatch):
    """并行（workers=16）与串行（workers=1）扫描产出完全一致的事件集合。"""
    n = 12
    import app.core.scanning as SC

    maker_p = _temp_maker(tmp_path, monkeypatch, name="p.db")
    _scan_setup(monkeypatch, maker_p, n)
    monkeypatch.setattr(SC, "_signal_workers", lambda: 16)
    res_p = SC.scan_once(full_universe=True)

    maker_s = _temp_maker(tmp_path, monkeypatch, name="s.db")
    _scan_setup(monkeypatch, maker_s, n)
    monkeypatch.setattr(SC, "_signal_workers", lambda: 1)
    res_s = SC.scan_once(full_universe=True)

    assert res_p["scanned"] == res_s["scanned"] == n
    assert res_p["events"] == res_s["events"] > 0
    assert _dump_events(maker_p) == _dump_events(maker_s)


def test_scan_parallel_propagates_jobcancelled(tmp_path, monkeypatch):
    """并行路径进度回调抛 JobCancelled 必须向上传播且不落库任何事件。"""
    from app.core.tasks.runner import JobCancelled

    n = 60
    import app.core.scanning as SC

    maker = _temp_maker(tmp_path, monkeypatch, name="c.db")
    _scan_setup(monkeypatch, maker, n)
    monkeypatch.setattr(SC, "_signal_workers", lambda: 16)
    calls = {"n": 0}

    def cb(pct, msg):
        calls["n"] += 1
        raise JobCancelled("job 1 已被用户取消")

    with pytest.raises(JobCancelled):
        SC.scan_once(full_universe=True, progress_cb=cb)
    assert calls["n"] >= 1
    db = maker()
    try:
        assert db.query(SignalEvent).count() == 0  # 取消后不得落库
    finally:
        db.close()


def test_engine_indicator_memo_keeps_results_identical():
    """engine 指标 memo：开启与关闭时 evaluate_all_signals 输出完全一致，且确实命中缓存。"""
    from app.lib.signals.engine import evaluate_all_signals, indicator_memo

    df = _kline_df(80).assign(code="600519.SH")

    plain_hits, plain_ev = evaluate_all_signals(df)
    memo: dict = {}
    with indicator_memo(memo):
        hits, ev = evaluate_all_signals(df)
        hits2, ev2 = evaluate_all_signals(df)
    assert (hits, ev) == (plain_hits, plain_ev) and (hits2, ev2) == (
        plain_hits,
        plain_ev,
    )
    assert len(memo) > 0  # 确实发生了缓存（rsi/macd 等指标被复用）


def test_scan_parallel_installs_indicator_memo(tmp_path, monkeypatch):
    """并行 worker 内按股安装指标 memo（P1-19 修复）：worker 线程计算时 memo
    已注入（engine.indicator_memo 基于 threading.local，修复前未安装 → 全部直算）。"""
    from app.core import scanning as SC
    from app.lib.signals import engine as ENG

    n = 12
    maker = _temp_maker(tmp_path, monkeypatch, name="memo.db")
    _scan_setup(monkeypatch, maker, n)
    monkeypatch.setattr(SC, "_signal_workers", lambda: 16)
    stats = {"with_memo": 0, "without": 0}
    orig = SC._check_one

    def spy(code, klines, turnover_map, period="daily"):
        if ENG._get_memo() is None:
            stats["without"] += 1
        else:
            stats["with_memo"] += 1
        return orig(code, klines, turnover_map, period)

    monkeypatch.setattr(SC, "_check_one", spy)
    res = SC.scan_once(full_universe=True)
    assert res["scanned"] == n
    assert stats["with_memo"] > 0, "并行 worker 必须安装指标 memo"
    assert stats["without"] == 0, "worker 内不得出现直算（memo 未安装）"


# ---------------------------------------------------------------------------
# queue：alpha101_score / factor_tune 并行 vs 串行 + 取消
# ---------------------------------------------------------------------------


def _install_alpha101_fakes(monkeypatch, n: int = 20, evaluate=None):
    import app.lib.alpha.alpha101 as A101
    import app.core.datasets as AD
    import app.lib.alpha.evaluate as AE
    import app.lib.alpha.operators as AO

    def fake_list_alpha101():
        return [{"id": i, "name": f"f{i}", "formula": f"f{i}"} for i in range(n)]

    def fake_compile_rpn(f):
        return f

    def fake_load_panel(ds_id, features=None):
        return {"panel": {"close": np.zeros((5, 40))}, "dates": ["2026-01-01"] * 40}

    def fake_forward_returns(close, horizon=5):
        return close

    if evaluate is None:

        def evaluate(rpn, panel, fwd, horizon=5, _memo=None):  # noqa: E731
            i = int(str(rpn)[1:])
            return {"ic": 0.1 + i * 0.01, "stability": 0.4 + (i % 5) * 0.1}

    monkeypatch.setattr(A101, "list_alpha101", fake_list_alpha101)
    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(AE, "evaluate_factor", evaluate)


def _run_alpha101(maker, n: int = 20) -> list[dict]:
    import app.core.tasks.runner as Q

    db = maker()
    job = ExperimentJob(
        job_type="alpha101_score",
        params={"dataset_id": 1, "limit": n},
        status="pending",
    )
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    t = threading.Thread(
        target=Q._run_alpha101_score, args=(jid, {"dataset_id": 1, "limit": n})
    )
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    db = maker()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    return row.result["results"]


def test_alpha101_score_parallel_matches_serial(tmp_path, monkeypatch, numpy_backend):
    n = 20
    import app.core.tasks.runner as Q

    maker_p = _temp_maker(tmp_path, monkeypatch, name="p.db")
    _install_alpha101_fakes(monkeypatch, n=n)
    monkeypatch.setattr(Q, "_parallel_workers", lambda: 8)
    par = _run_alpha101(maker_p, n=n)

    maker_s = _temp_maker(tmp_path, monkeypatch, name="s.db")
    _install_alpha101_fakes(monkeypatch, n=n)
    monkeypatch.setattr(Q, "_parallel_workers", lambda: 1)
    ser = _run_alpha101(maker_s, n=n)

    assert par == ser
    assert len(par) == n


def test_alpha101_parallel_cancel_aborts_and_cleans(
    tmp_path, monkeypatch, numpy_backend
):
    """并行评分取消：循环中止、终态保持取消、_cancelled 被清理。"""
    import app.core.tasks.runner as Q
    from app.core.tasks.runner import delete_job, submit

    calls = []
    N = 24

    def slow_eval(rpn, panel, fwd, horizon=5):
        calls.append(rpn)
        time.sleep(0.02)
        return {"ic": 0.1, "stability": 0.5}

    maker = _temp_maker(tmp_path, monkeypatch, name="c.db")
    _install_alpha101_fakes(monkeypatch, n=N, evaluate=slow_eval)
    monkeypatch.setattr(Q, "_parallel_workers", lambda: 4)

    jid = submit("alpha101_score", {"dataset_id": 1, "limit": N})
    deadline = time.time() + 5
    while time.time() < deadline:
        db = maker()
        row = db.get(ExperimentJob, jid)
        db.close()
        if row is not None and row.status == "running":
            break
        time.sleep(0.01)
    assert row.status == "running"

    assert delete_job(jid) == {"ok": True, "deleted": False}
    deadline = time.time() + 10
    while time.time() < deadline:
        with Q._running_lock:
            gone = jid not in Q._cancelled
        if gone:
            break
        time.sleep(0.01)
    assert gone

    db = maker()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled" and row.error == "已被用户取消"
    assert row.result == {}
    assert len(calls) < N


def _install_tune_fakes(monkeypatch):
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

    def fake_evaluate_rpn(rpn_i, panel):
        return np.zeros((5, 32))

    def fake_evaluate_rpn_memo(rpn_i, panel, memo=None):
        return fake_evaluate_rpn(rpn_i, panel)

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

    def fake_stability(vals):
        return 0.5

    monkeypatch.setattr(AO, "compile_rpn", fake_compile_rpn)
    monkeypatch.setattr(AO, "collect_tunable_params", fake_collect_tunable_params)
    monkeypatch.setattr(AO, "set_tunable_param", fake_set_tunable_param)
    monkeypatch.setattr(AO, "evaluate_rpn", fake_evaluate_rpn)
    monkeypatch.setattr(AO, "evaluate_rpn_memo", fake_evaluate_rpn_memo)
    monkeypatch.setattr(AO, "expr_str", fake_expr_str)
    monkeypatch.setattr(AE, "evaluate_factor", fake_evaluate_factor)
    monkeypatch.setattr(AE, "forward_returns", fake_forward_returns)
    monkeypatch.setattr(AE, "stability", fake_stability)
    # _run_factor_tune 使用 queue 模块顶部的 load_panel 绑定，必须连同源模块一起替换
    monkeypatch.setattr(AD, "load_panel", fake_load_panel)
    monkeypatch.setattr(Q, "load_panel", fake_load_panel)


def _run_tune(maker) -> list[dict]:
    import app.core.tasks.runner as Q

    params = {
        "dataset_id": 1,
        "expression": "ts_mean(close,5)",
        "target": "ic",
        "param": {
            "name": "ts_mean.window",
            "min": 1,
            "max": 5,
            "step": 1,
            "is_int": True,
        },
    }
    db = maker()
    job = ExperimentJob(job_type="factor_tune", params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    t = threading.Thread(target=Q._run_factor_tune, args=(jid, params))
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    db = maker()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    return row.result["grid"]


def test_factor_tune_parallel_matches_serial(tmp_path, monkeypatch, numpy_backend):
    import app.core.tasks.runner as Q

    maker_p = _temp_maker(tmp_path, monkeypatch, name="p.db")
    _install_tune_fakes(monkeypatch)
    monkeypatch.setattr(Q, "_parallel_workers", lambda: 8)
    par = _run_tune(maker_p)

    maker_s = _temp_maker(tmp_path, monkeypatch, name="s.db")
    _install_tune_fakes(monkeypatch)
    monkeypatch.setattr(Q, "_parallel_workers", lambda: 1)
    ser = _run_tune(maker_s)

    assert par == ser  # 并行按 param_value 恢复原始网格顺序，排序后输出与串行逐点一致
    # target=ic → 按 train_ic(=0.01*v) 降序
    assert [g["param_value"] for g in par] == [5, 4, 3, 2, 1]


# ---------------------------------------------------------------------------
# gp：代内 fitness 并行 vs 串行
# ---------------------------------------------------------------------------


def _evolve_data(S=30, T=48):
    rng = np.random.default_rng(1)
    close = rng.standard_normal((S, T)).astype(np.float32)
    volume = rng.standard_normal((S, T)).astype(np.float32)
    fwd = rng.standard_normal((S, T)).astype(np.float32)
    t1, t2 = int(T * 0.6), int(T * 0.8)
    train = {"close": close[:, :t1], "volume": volume[:, :t1]}
    val = {"close": close[:, t1:t2], "volume": volume[:, t1:t2]}
    return train, val, fwd[:, :t1], fwd[:, t1:t2]


def test_evolve_parallel_matches_serial(monkeypatch, numpy_backend):
    from app.lib.alpha import gp as GP

    train, val, fwd_tr, fwd_vl = _evolve_data()
    kwargs = dict(
        op_set=["add", "sub", "ts_mean", "rank"],
        features=["close", "volume"],
        pop_size=24,
        generations=3,
        data_train=train,
        data_val=val,
        forward_returns=fwd_tr,
        val_forward_returns=fwd_vl,
    )

    monkeypatch.setattr(GP, "_fitness_workers", lambda: 8)
    random.seed(7)
    par_results, par_evo = GP.evolve(**kwargs)

    monkeypatch.setattr(GP, "_fitness_workers", lambda: 1)
    random.seed(7)
    ser_results, ser_evo = GP.evolve(**kwargs)

    assert par_results == ser_results
    assert par_evo == ser_evo


# ---------------------------------------------------------------------------
# numpy 后端并行开关：mlx 串行、numpy 并行
# ---------------------------------------------------------------------------


def test_alpha101_backend_gate_controls_pool(tmp_path, monkeypatch):
    """mlx 后端不建线程池（串行）；numpy 后端建线程池（并行）。"""
    import app.core.tasks.runner as Q

    instances: list[dict] = []

    class SpyPool:
        def __init__(self, *a, **k):
            instances.append(k)
            self._pool = ThreadPoolExecutor(*a, **k)

        def __enter__(self):
            self._inner = self._pool.__enter__()
            return self._inner

        def __exit__(self, *exc):
            return self._pool.__exit__(*exc)

        def submit(self, fn, *a, **k):
            return self._pool.submit(fn, *a, **k)

    monkeypatch.setattr(Q, "ThreadPoolExecutor", SpyPool)
    monkeypatch.setattr(Q, "_parallel_workers", lambda: 4)

    def run_one(name: str):
        maker = _temp_maker(tmp_path, monkeypatch, name=name)
        _install_alpha101_fakes(monkeypatch, n=6)
        _run_alpha101(maker, n=6)

    # mlx：串行，不实例化池
    monkeypatch.setattr(Q, "backend_name", lambda: "mlx")
    instances.clear()
    run_one("mlx.db")
    assert instances == []

    # numpy：并行，实例化池
    monkeypatch.setattr(Q, "backend_name", lambda: "numpy")
    instances.clear()
    run_one("numpy.db")
    assert len(instances) == 1
    assert instances[0]["max_workers"] == 4


# ---------------------------------------------------------------------------
# dataset：build_dataset 并行 vs 串行
# ---------------------------------------------------------------------------


def _fake_cached_kline(
    code, period="daily", max_rows=400, refresh_if_stale=True, start_date=None
) -> pd.DataFrame:
    n = 65
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": np.linspace(10, 20, n),
            "high": np.linspace(10.5, 20.5, n),
            "low": np.linspace(9.5, 19.5, n),
            "close": np.linspace(10, 20, n),
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1e6),
        }
    )


def _run_build(monkeypatch, maker, workers: int, codes: list[str]) -> dict:
    import app.core.datasets as AD

    monkeypatch.setattr(AD, "SessionLocal", maker)
    monkeypatch.setattr(AD, "cached_kline", _fake_cached_kline)
    monkeypatch.setattr(AD, "_build_workers", lambda: workers)
    return AD.build_dataset(
        name="ds",
        universe="custom",
        start_date="2025-01-01",
        end_date="2026-06-30",
        limit=0,
        custom_codes=codes,
    )


def test_dataset_build_parallel_matches_serial(tmp_path, monkeypatch):
    codes = [f"6000{i:02d}.SH" for i in range(1, 9)]

    maker_p = _temp_maker(tmp_path, monkeypatch, name="p.db")
    res_p = _run_build(monkeypatch, maker_p, workers=8, codes=codes)

    maker_s = _temp_maker(tmp_path, monkeypatch, name="s.db")
    res_s = _run_build(monkeypatch, maker_s, workers=1, codes=codes)

    assert res_p["stock_count"] == res_s["stock_count"] == len(codes)
    assert res_p["row_count"] == res_s["row_count"] == 65 * len(codes)

    for maker in (maker_p, maker_s):
        db = maker()
        try:
            rows = db.query(DatasetStock).order_by(DatasetStock.seq.asc()).all()
            assert [r.code for r in rows] == codes  # 成分顺序与 codes 一致
            assert db.query(Dataset).one().stock_count == len(codes)
        finally:
            db.close()


def test_dataset_build_parallel_progress_reaches_total(tmp_path, monkeypatch):
    codes = [f"6000{i:02d}.SH" for i in range(1, 7)]
    maker = _temp_maker(tmp_path, monkeypatch, name="p.db")
    import app.core.datasets as AD

    monkeypatch.setattr(AD, "SessionLocal", maker)
    monkeypatch.setattr(AD, "cached_kline", _fake_cached_kline)
    monkeypatch.setattr(AD, "_build_workers", lambda: 8)

    seen: list[tuple[int, int]] = []
    AD.build_dataset(
        name="ds",
        universe="custom",
        start_date="2025-01-01",
        end_date="2026-06-30",
        limit=0,
        custom_codes=codes,
        progress_cb=lambda d, t: seen.append((d, t)),
    )
    assert seen and seen[-1] == (len(codes), len(codes))
    assert seen[0][1] == len(codes)


def test_dataset_build_add_all_chunked(tmp_path, monkeypatch):
    """成分写入分批提交（P2-17）：add_all 每批 ≤400 行，seq 全局连续且与 codes 顺序一致。

    修复前 limit=0 全量时一次性 add_all 数千行 → SQLite too many SQL variables。
    """
    from sqlalchemy.orm import Session

    import app.core.datasets as AD

    codes = [f"6000{i:03d}.SH" for i in range(1, 451)]  # 450 行 > 400 → 触发分批
    maker = _temp_maker(tmp_path, monkeypatch, name="chunk.db")
    monkeypatch.setattr(AD, "SessionLocal", maker)
    monkeypatch.setattr(AD, "cached_kline", _fake_cached_kline)
    monkeypatch.setattr(AD, "_build_workers", lambda: 8)

    batch_sizes: list[int] = []
    orig_add_all = Session.add_all

    def counted(self, instances):
        batch_sizes.append(len(instances))
        return orig_add_all(self, instances)

    monkeypatch.setattr(Session, "add_all", counted)

    res = AD.build_dataset(
        name="ds",
        universe="custom",
        start_date="2025-01-01",
        end_date="2026-06-30",
        limit=0,
        custom_codes=codes,
    )
    assert res["stock_count"] == len(codes)
    assert batch_sizes and max(batch_sizes) <= 400, "单批 add_all 不得超过 400 行"
    assert sum(batch_sizes) == len(codes)

    db = maker()
    try:
        rows = db.query(DatasetStock).order_by(DatasetStock.seq.asc()).all()
        assert len(rows) == len(codes)
        assert [r.code for r in rows] == codes  # 分批不得打乱成分顺序
        assert [r.seq for r in rows] == list(range(len(codes)))  # seq 全局连续
    finally:
        db.close()
