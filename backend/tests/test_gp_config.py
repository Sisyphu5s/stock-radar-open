"""GP 配置生效测试：features/op_set/op_config 白名单与采样、默认兼容、
数据集 universe 语义（all/top500/hs300/custom + limit 契约）、任务 failed 反馈、取消不回归。"""

from __future__ import annotations

import random
import threading

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.storage.db import Base
from app.storage.models import ExperimentJob, Stock
from app.lib.alpha import backend as B
from app.core.datasets import UNIVERSES, _resolve_codes, build_dataset
from app.lib.alpha.gp import (
    DEFAULT_GP_FEATURES,
    DEFAULT_GP_OP_SET,
    all_nodes,
    evolve,
    mutate,
    random_tree,
)
from app.lib.alpha.operators import (
    EDITABLE_OPS,
    FEATURES,
    OPERATORS,
    TS_PARAM_GRID,
    sample_op_params,
    validate_features,
    validate_op_config,
    validate_op_set,
)


@pytest.fixture(scope="module", autouse=True)
def numpy_backend():
    settings.gp_backend = "numpy"
    B.init_backend()
    yield


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 queue/dataset 的 SessionLocal（不触碰生产库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.datasets as AD

    monkeypatch.setattr(AD, "SessionLocal", Maker)

    import app.core.tasks.runner as Q

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    monkeypatch.setattr(Q, "init_backend", lambda: None)
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()

    yield Maker
    engine.dispose()


# ---------------------------------------------------------------------------
# 白名单校验：features / op_set / op_config
# ---------------------------------------------------------------------------


def test_validate_op_set_whitelist():
    assert validate_op_set(None) is None
    assert validate_op_set(["add", "sub", "add"]) == ["add", "sub"]
    with pytest.raises(ValueError, match="op_set 不能为空"):
        validate_op_set([])
    with pytest.raises(ValueError, match="不支持的算子"):
        validate_op_set(["add", "bogus_op"])
    with pytest.raises(ValueError, match="op_set 不能为空"):
        validate_op_set("add")  # 非列表


def test_validate_features_whitelist():
    assert validate_features(None) is None
    assert validate_features(["close", "volume", "close"]) == ["close", "volume"]
    with pytest.raises(ValueError, match="features 不能为空"):
        validate_features([])
    with pytest.raises(ValueError, match="不支持的字段"):
        validate_features(["close", "eps"])  # T-07: pe 已入 FEATURES 白名单，改用 eps
    with pytest.raises(ValueError, match="features 不能为空"):
        validate_features("close")


def test_validate_op_config_whitelist_and_ranges():
    assert validate_op_config(None) == {}
    assert validate_op_config({}) == {}
    # 规范化：int/float 统一、去重
    assert validate_op_config({"ts_mean": {"window": [3.0, 5, 5, 20]}}) == {
        "ts_mean": {"window": [3, 5, 20]}
    }
    # 仅允许 EDITABLE_OPS 内的参数键
    with pytest.raises(ValueError, match="不支持的算子"):
        validate_op_config({"bogus": {"window": [5]}})
    with pytest.raises(ValueError, match="不支持参数键"):
        validate_op_config({"ts_mean": {"delay": [5]}})
    with pytest.raises(ValueError, match="必须是非空候选数组"):
        validate_op_config({"ts_mean": {"window": []}})
    with pytest.raises(ValueError, match="必须是数字"):
        validate_op_config({"ts_mean": {"window": [True]}})
    with pytest.raises(ValueError, match="候选值必须是数字"):
        validate_op_config({"ts_mean": {"window": ["5"]}})
    with pytest.raises(ValueError, match="正整数"):
        validate_op_config({"ts_mean": {"window": [0, 5]}})
    with pytest.raises(ValueError, match="正整数"):
        validate_op_config({"ts_mean": {"window": [2.5]}})
    with pytest.raises(ValueError, match="必须是对象"):
        validate_op_config(["ts_mean"])


# ---------------------------------------------------------------------------
# 参数候选采样：op_config → 默认网格
# ---------------------------------------------------------------------------


def test_param_candidates_precedence_and_defaults():
    assert sample_op_params("add", None) == {}  # 无参数算子
    assert (
        sample_op_params("signed_power", None) == {}
    )  # 无默认网格 → eval 内默认 power=2
    assert sample_op_params("ts_mean", None)["window"] in TS_PARAM_GRID["window"]
    assert sample_op_params("ts_delay", None)["delay"] in TS_PARAM_GRID["delay"]
    assert (
        sample_op_params("winsorize", None)["pct"] in EDITABLE_OPS["winsorize"]["pct"]
    )
    # op_config 优先于默认网格
    assert sample_op_params("ts_mean", {"ts_mean": {"window": [7]}}) == {"window": 7}
    assert sample_op_params("ts_corr", {"ts_corr": {"window": [30]}}) == {"window": 30}


def test_random_tree_samples_op_config_params(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.9)  # 强制非叶子分支
    tree = random_tree(
        0,
        ["ts_mean"],
        max_depth=1,
        features=["close"],
        op_config={"ts_mean": {"window": [42]}},
    )
    assert tree["name"] == "ts_mean"
    assert tree["params"] == {"window": 42}
    assert tree["args"][0]["name"] == "close"


def test_random_tree_uses_only_selected_features(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.9)
    for _ in range(50):
        tree = random_tree(
            0, ["add", "mul", "ts_mean"], max_depth=3, features=["close"]
        )
        for node in all_nodes(tree):
            if not node.get("args"):  # 叶子
                assert node["name"] == "close"


def test_mutate_resamples_params_from_op_config(monkeypatch):
    monkeypatch.setattr(
        random, "random", lambda: 0.9
    )  # 走参数重采样分支（非浅子树替换）
    tree = {"name": "ts_mean", "params": {"window": 5}, "args": [{"name": "close"}]}
    out = mutate(
        tree, ["ts_mean"], features=["close"], op_config={"ts_mean": {"window": [42]}}
    )
    assert out["name"] == "ts_mean"
    assert out["params"]["window"] == 42
    # 未传 op_config：仍可从默认网格重采样，且叶子替换只使用选中 features
    out2 = mutate(
        {"name": "add", "params": {}, "args": [{"name": "close"}, {"name": "volume"}]},
        ["add", "ts_mean"],
        features=["close", "volume"],
    )
    for node in all_nodes(out2):
        if not node.get("args"):
            assert node["name"] in ("close", "volume")


# ---------------------------------------------------------------------------
# evolve：features/op_config 真实生效 + 默认兼容
# ---------------------------------------------------------------------------


def _evolve_data(S=30, T=48, seed=1):
    rng = np.random.default_rng(seed)
    close = rng.standard_normal((S, T)).astype(np.float32)
    volume = rng.standard_normal((S, T)).astype(np.float32)
    fwd = rng.standard_normal((S, T)).astype(np.float32)
    t1, t2 = int(T * 0.6), int(T * 0.8)
    train = {"close": close[:, :t1], "volume": volume[:, :t1]}
    val = {"close": close[:, t1:t2], "volume": volume[:, t1:t2]}
    return train, val, fwd[:, :t1], fwd[:, t1:t2]


def test_evolve_uses_selected_features_and_op_config():
    train, val, fwd_tr, fwd_vl = _evolve_data()
    features = ["close", "volume"]
    results, evolution = evolve(
        op_set=["add", "sub", "ts_mean", "rank"],
        features=features,
        op_config={"ts_mean": {"window": [3]}},
        pop_size=16,
        generations=2,
        data_train=train,
        data_val=val,
        forward_returns=fwd_tr,
        val_forward_returns=fwd_vl,
    )
    assert results and evolution
    for r in results:
        assert r["rpn"]
        for ins in r["rpn"]:
            if ins["op"] == "__feat__":
                assert ins["params"]["name"] in features, (
                    f"越界特征: {ins['params']['name']}"
                )
            if ins["op"] == "ts_mean":
                assert ins["params"].get("window") == 3, (
                    f"参数未走配置: {ins['params']}"
                )


def test_evolve_defaults_compatible():
    """未传 features/op_set/op_config：默认集合正常工作（默认兼容，不回归）。"""
    rng = np.random.default_rng(2)
    S, T = 24, 48
    t1, t2 = int(T * 0.6), int(T * 0.8)
    base = rng.standard_normal((S, T)).astype(np.float32)
    data_train = {f: base[:, :t1] for f in DEFAULT_GP_FEATURES}
    data_val = {f: base[:, t1:t2] for f in DEFAULT_GP_FEATURES}
    fwd = rng.standard_normal((S, T)).astype(np.float32)
    results, evolution = evolve(
        pop_size=10,
        generations=2,
        data_train=data_train,
        data_val=data_val,
        forward_returns=fwd[:, :t1],
        val_forward_returns=fwd[:, t1:t2],
    )
    assert results is not None and evolution


def test_evolve_rejects_invalid_config():
    train, val, fwd_tr, fwd_vl = _evolve_data()
    with pytest.raises(ValueError, match="不支持的算子"):
        evolve(
            op_set=["bogus"],
            pop_size=4,
            generations=1,
            data_train=train,
            data_val=val,
            forward_returns=fwd_tr,
            val_forward_returns=fwd_vl,
        )
    with pytest.raises(ValueError, match="features 含不支持的字段"):
        evolve(
            features=["eps"],  # T-07: pe 已合法，改用 eps
            pop_size=4,
            generations=1,
            data_train=train,
            data_val=val,
            forward_returns=fwd_tr,
            val_forward_returns=fwd_vl,
        )
    with pytest.raises(ValueError, match="op_config"):
        evolve(
            op_config={"ts_mean": {"window": []}},
            pop_size=4,
            generations=1,
            data_train=train,
            data_val=val,
            forward_returns=fwd_tr,
            val_forward_returns=fwd_vl,
        )
    with pytest.raises(ValueError, match="op_set 不能为空"):
        evolve(
            op_set=[],
            pop_size=4,
            generations=1,
            data_train=train,
            data_val=val,
            forward_returns=fwd_tr,
            val_forward_returns=fwd_vl,
        )


# ---------------------------------------------------------------------------
# 数据集 universe 语义：all/top500/hs300/custom + limit 契约
# ---------------------------------------------------------------------------


def _patch_market(monkeypatch):
    import app.core.datasets as AD

    fake_spot = pd.DataFrame(
        {
            "code": ["600001.SH", "000002.SZ", "300003.SZ", "600004.SH"],
            "amount": [5.0, 9.0, 3.0, 7.0],
        }
    )
    monkeypatch.setattr(AD, "get_spot", lambda refresh=False: fake_spot)
    monkeypatch.setattr(
        AD, "get_provider", lambda: type("P", (), {"name": "akshare"})()
    )
    monkeypatch.setattr(
        AD, "get_hs300_codes", lambda: ["600000.SH", "600001.SH", "600002.SH"]
    )
    return AD


def test_dataset_universes_distinct_and_limit_contract(temp_db, monkeypatch):
    AD = _patch_market(monkeypatch)
    db = temp_db()

    # hs300：真实成分，limit 截断；limit=0 全量
    assert AD._resolve_codes(db, "hs300", 2, None) == ["600000.SH", "600001.SH"]
    assert AD._resolve_codes(db, "hs300", 0, None) == [
        "600000.SH",
        "600001.SH",
        "600002.SH",
    ]

    # top500：成交额 Top N（与快照原始顺序不同）
    assert AD._resolve_codes(db, "top500", 2, None) == ["000002.SZ", "600004.SH"]
    assert AD._resolve_codes(db, "top500", 0, None) == [
        "000002.SZ",
        "600004.SH",
        "600001.SH",
        "300003.SZ",
    ]

    # all：全市场原始顺序（不按成交额排序），与 top500 语义不同
    all_codes = AD._resolve_codes(db, "all", 0, None)
    assert all_codes == ["600001.SH", "000002.SZ", "300003.SZ", "600004.SH"]
    assert AD._resolve_codes(db, "all", 2, None) == ["600001.SH", "000002.SZ"]
    # all 与 top500 不再默认为同一 universe 首 N
    assert AD._resolve_codes(db, "all", 2, None) != AD._resolve_codes(
        db, "top500", 2, None
    )

    # custom：custom_codes 清洗 + limit 截断
    assert AD._resolve_codes(db, "custom", 0, [" 600519.SH ", "", "000001.SZ"]) == [
        "600519.SH",
        "000001.SZ",
    ]
    assert AD._resolve_codes(db, "custom", 1, ["600519.SH", "000001.SZ"]) == [
        "600519.SH"
    ]
    db.close()


def test_dataset_universe_errors_not_silent(temp_db, monkeypatch):
    AD = _patch_market(monkeypatch)
    db = temp_db()
    with pytest.raises(ValueError, match="未知 universe"):
        AD._resolve_codes(db, "shenzhen_all", 10, None)
    with pytest.raises(ValueError, match="不支持 custom_codes"):
        AD._resolve_codes(db, "hs300", 5, ["600519.SH"])
    with pytest.raises(ValueError, match="custom_codes 为空"):
        AD._resolve_codes(db, "custom", 0, [" ", ""])
    with pytest.raises(ValueError, match="需要 custom_codes 或自选股"):
        AD._resolve_codes(
            db, "custom", 0, None
        )  # 无自选股 → 明确报错（不再 mock 兜底）
    db.close()


def test_dataset_custom_uses_watchlist(temp_db, monkeypatch):
    AD = _patch_market(monkeypatch)
    db = temp_db()
    db.add(Stock(code="600519.SH", name="茅台", is_watchlist=True))
    db.add(Stock(code="000001.SZ", name="平安银行", is_watchlist=False))
    db.commit()
    assert AD._resolve_codes(db, "custom", 0, None) == ["600519.SH"]
    db.close()


def test_build_dataset_validates_inputs(temp_db):
    with pytest.raises(ValueError, match="limit 不能为负"):
        build_dataset("x", "top500", limit=-1)
    with pytest.raises(ValueError, match="limit 必须是整数"):
        build_dataset("x", "top500", limit="abc")
    with pytest.raises(ValueError, match="日期范围非法"):
        build_dataset(
            "x", "top500", start_date="2026-08-01", end_date="2025-01-01", limit=5
        )
    with pytest.raises(ValueError, match="未知 universe"):
        build_dataset("x", "bogus", limit=5)


def test_dataset_api_rejects_bad_universe(temp_db, monkeypatch):
    """同步 400：未知 universe / 负数 limit / 日期倒挂 在提交前拦截。"""
    from fastapi import HTTPException
    from app.api.datasets import create_dataset

    AD = _patch_market(monkeypatch)
    for payload, msg in [
        ({"universe": "bogus", "limit": 5}, "未知 universe"),
        ({"universe": "top500", "limit": -3}, "limit 不能为负"),
        ({"universe": "top500", "limit": "x"}, "limit 必须是整数"),
        (
            {
                "universe": "top500",
                "limit": 5,
                "start_date": "2026-08-01",
                "end_date": "2025-01-01",
            },
            "日期范围非法",
        ),
    ]:
        with pytest.raises(HTTPException) as ei:
            create_dataset(payload, db=temp_db())
        assert ei.value.status_code == 400 and msg in str(ei.value.detail)


# ---------------------------------------------------------------------------
# 任务层：配置传入引擎 + result meta 保留 + 非法配置任务 failed
# ---------------------------------------------------------------------------


def test_queue_gp_passes_config_and_meta(temp_db, monkeypatch):
    import app.core.tasks.runner as Q

    calls = {}

    def fake_evolve(**kw):
        calls.update(kw)
        return [], []

    def fake_load_panel(ds_id, features=None):
        rng = np.random.default_rng(0)
        S, T = 10, 40
        panel = {
            f: rng.random((S, T)).astype(np.float32)
            for f in ["close", "open", "high", "low", "volume", "amount", "pct_change"]
        }
        return {"panel": panel, "dates": [f"2026-01-{i + 1:02d}" for i in range(T)]}

    monkeypatch.setattr(Q, "evolve", fake_evolve)
    monkeypatch.setattr(Q, "load_panel", fake_load_panel)

    db = temp_db()
    job = ExperimentJob(job_type="gp_run", params={}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    t = threading.Thread(
        target=Q._run_gp,
        args=(
            jid,
            {
                "dataset_id": 1,
                "backend": "cpu",
                "features": ["close", "volume"],
                "op_set": ["add", "ts_mean"],
                "op_config": {"ts_mean": {"window": [42]}},
            },
        ),
    )
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()

    assert calls.get("features") == ["close", "volume"]
    assert calls.get("op_set") == ["add", "ts_mean"]
    assert calls.get("op_config") == {"ts_mean": {"window": [42]}}

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done"
    meta = row.result["meta"]["config"]
    assert meta == {
        "features": ["close", "volume"],
        "op_set": ["add", "ts_mean"],
        "op_config": {"ts_mean": {"window": [42]}},
    }


def test_queue_gp_bad_features_fails_job(temp_db):
    """非法 features → 任务 failed 且错误信息明确（不静默忽略）。"""
    import app.core.tasks.runner as Q

    db = temp_db()
    job = ExperimentJob(job_type="gp_run", params={}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    t = threading.Thread(
        target=Q._run_gp,
        args=(
            jid,
            {
                "dataset_id": 1,
                "backend": "cpu",
                "features": ["eps"],  # T-07: pe 已合法，改用 eps
                "op_set": ["add"],
            },
        ),
    )
    t.start()
    t.join(timeout=15)
    assert not t.is_alive()

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "failed"
    assert "features 含不支持的字段: eps" in row.error


def test_queue_gp_bad_op_config_fails_job(temp_db):
    import app.core.tasks.runner as Q

    db = temp_db()
    job = ExperimentJob(job_type="gp_run", params={}, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    t = threading.Thread(
        target=Q._run_gp,
        args=(
            jid,
            {
                "dataset_id": 1,
                "backend": "cpu",
                "features": ["close"],
                "op_set": ["add"],
                "op_config": {"ts_mean": {"window": []}},
            },
        ),
    )
    t.start()
    t.join(timeout=15)
    assert not t.is_alive()

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "failed"
    assert "op_config" in row.error


# ---------------------------------------------------------------------------
# 取消逻辑不回归（平行任务并行取消修复保持）
# ---------------------------------------------------------------------------


def test_cancel_blocks_late_terminal_write_no_regression(temp_db):
    """取消竞态加固回归：取消后任何后台写入均被条件更新拦截，终态保持取消。"""
    from app.core.tasks.runner import (
        _cancel_check,
        _cancelled,
        _finish,
        _update,
        JobCancelled,
        delete_job,
    )

    db = temp_db()
    job = ExperimentJob(job_type="gp_run", params={}, status="running", progress=30.0)
    db.add(job)
    db.commit()
    jid = job.id
    db.close()

    assert delete_job(jid) == {"ok": True, "deleted": False}

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled" and row.error == "已被用户取消"

    # 晚到的终态/进度写入全部被 CAS 拦截
    assert _update(jid, status="done", progress=100.0, result={"results": [1]}) == 0
    assert _update(jid, progress=66.0) == 0
    with pytest.raises(JobCancelled):
        _cancel_check(jid)
    assert jid in _cancelled
    assert _finish(jid) is False

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "cancelled" and row.result == {} and row.progress == 30.0
