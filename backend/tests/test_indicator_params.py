"""具名多参数契约测试：IndicatorSpec 白名单 / MACD 三参数与 BOLL 两参数真实影响评分 /
参数优先级（manual > v2 global > legacy global > defaults）/ reset / 旧格式兼容 / 非法约束。

用临时 SQLite（不触碰生产库）+ 合成 K 线（不依赖网络），直接调用 API 处理函数验证。
"""

from __future__ import annotations

import json
import logging

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
    import app.core.tasks.runner as RUNNER

    monkeypatch.setattr(DB, "SessionLocal", Maker)
    # 任务队列(worker/submit/get_job)的 SessionLocal 是 runner 模块 import 时绑定，
    # 须同步指向临时库，否则提交任务会写生产库（P1-38b 任务化后 tune 走队列）
    monkeypatch.setattr(RUNNER, "SessionLocal", Maker)
    # 清理跨测试残留的进程级控制状态（取消/暂停集合按 job_id 索引，临时库 id 会重号）
    with RUNNER._running_lock:
        RUNNER._running.clear()
        RUNNER._cancelled.clear()
        RUNNER._paused.clear()
    # patch 使用点：indicators 模块顶部 `from ..storage.klines import cached_kline,
    # get_version` 在 import 时即固定了引用，只 patch klines 模块本身在 indicators
    # 已被其他测试文件导入时不生效 → 与 test_t49_global_tune_guard 同策略，直接覆盖
    # app.api.indicators 命名空间里的绑定（任务 handler 定义在同一模块，同样穿透）。
    import app.api.indicators as I

    monkeypatch.setattr(I, "cached_kline", lambda code, period, **kw: _synthetic_df())
    monkeypatch.setattr(I, "get_version", lambda *a, **k: 0)
    # 隔离全局缓存（磁盘+内存），避免跨测试同 key 命中旧结果
    from app.storage import cache as CACHE

    CACHE.indicator_cache.clear()
    CACHE.tune_cache.clear()
    yield Maker


def _wait_job(job_id: int, timeout: float = 60.0) -> dict:
    """轮询任务至终态（done/failed/cancelled），返回 job dict（P1-38b 任务提交/轮询）。"""
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


# ---------------------------------------------------------------------------
# 1) IndicatorSpec 白名单契约
# ---------------------------------------------------------------------------


def test_spec_whitelist_covers_all_tuneable():
    from app.core.indicators.tune import SPECS, TUNEABLE

    assert set(SPECS) == set(TUNEABLE)
    for key, spec in SPECS.items():
        assert spec.key == key and spec.name
        assert spec.params and spec.candidates
        for p in spec.params:
            for attr in ("key", "label", "type", "default", "min", "max", "step"):
                assert hasattr(p, attr)
            assert p.type in ("int", "float")
            assert p.min <= p.default <= p.max
        # candidates 为完整具名 params 对象（键与 params 完全一致）
        keys = tuple(p.key for p in spec.params)
        for cand in spec.candidates:
            assert set(cand) == set(keys)
            assert cand == spec.defaults() or cand is not None


def test_spec_macd_boll_defaults():
    from app.core.indicators.tune import SPECS

    macd = SPECS["macd"]
    assert [p.key for p in macd.params] == ["fast", "slow", "signal"]
    assert macd.defaults() == {"fast": 12, "slow": 26, "signal": 9}
    boll = SPECS["boll"]
    assert [p.key for p in boll.params] == ["window", "multiplier"]
    assert boll.defaults() == {"window": 20, "multiplier": 2.0}
    assert SPECS["rsi"].defaults() == {"window": 14}


def test_catalog_returns_specs():
    from app.api.indicators import catalog
    from app.core.indicators.tune import SPECS, TARGETS

    out = catalog()
    assert set(out["tuneable"]) == set(SPECS)
    assert out["targets"] == TARGETS
    by_key = {s["key"]: s for s in out["specs"]}
    assert set(by_key) == set(SPECS)
    macd = by_key["macd"]
    assert macd["defaults"] == {"fast": 12, "slow": 26, "signal": 9}
    assert all(set(c) == {"fast", "slow", "signal"} for c in macd["candidates"])
    boll = by_key["boll"]
    assert {p["key"] for p in boll["params"]} == {"window", "multiplier"}


# ---------------------------------------------------------------------------
# 2) MACD 三参数 / BOLL 两参数真实影响评分
# ---------------------------------------------------------------------------


def test_macd_signal_really_affects_value_and_score():
    from app.core.indicators.tune import SPECS, _compute_values, tune_indicator

    df = _synthetic_df()
    v5 = _compute_values("macd", df, {"fast": 12, "slow": 26, "signal": 5})
    v21 = _compute_values("macd", df, {"fast": 12, "slow": 26, "signal": 21})
    assert not np.allclose(v5.to_numpy(), v21.to_numpy())
    r = tune_indicator("macd", df, horizon=5, target="ic")
    assert r["defaults"] == {"fast": 12, "slow": 26, "signal": 9}
    assert all(set(x["params"]) == {"fast", "slow", "signal"} for x in r["results"])
    assert "param" in r["best"] and r["best"]["param"]  # 旧 param 字符串保留
    # 网格内 signal 取多个值，且各参数组合得分有差异 → signal 真实参与评价
    signals = {x["params"]["signal"] for x in r["results"]}
    assert len(signals) >= 2
    assert len({x["score"] for x in r["results"]}) >= 2
    # 候选含 (12,26,9) 默认组合
    assert {"fast": 12, "slow": 26, "signal": 9} in SPECS["macd"].candidates


def test_tune_algorithm_sampling():
    from app.core.indicators.tune import SPECS, tune_indicator

    df = _synthetic_df()
    full = tune_indicator("macd", df, horizon=5, target="ic", algorithm="grid")
    fast = tune_indicator("macd", df, horizon=5, target="ic", algorithm="fast")
    rnd = tune_indicator("macd", df, horizon=5, target="ic", algorithm="random")
    n_full, n_fast, n_rnd = (
        len(full["results"]),
        len(fast["results"]),
        len(rnd["results"]),
    )
    assert n_fast < n_full  # fast = 1/3 间隔采样
    assert n_fast > 0
    assert 0 < n_rnd <= 200  # random 随机采样 ≤200
    # 采样信息字段（覆盖度标注）
    assert full["algorithm"] == "grid" and full["evaluated"] == n_full
    assert full["candidates_total"] == len(SPECS["macd"].candidates)
    # random 固定种子：两次调用结果完全一致（可复现）
    rnd2 = tune_indicator("macd", df, horizon=5, target="ic", algorithm="random")
    assert [r["params"] for r in rnd["results"]] == [
        r["params"] for r in rnd2["results"]
    ]
    # 非法算法拒绝
    with pytest.raises(ValueError):
        tune_indicator("macd", df, horizon=5, target="ic", algorithm="bad")


def test_boll_multiplier_really_affects_value_and_score():
    from app.core.indicators.tune import SPECS, _compute_values, tune_indicator

    df = _synthetic_df()
    b1 = _compute_values("boll", df, {"window": 20, "multiplier": 2.0})
    b2 = _compute_values("boll", df, {"window": 20, "multiplier": 4.0})
    assert not np.allclose(b1.to_numpy(), b2.to_numpy())
    r = tune_indicator("boll", df, horizon=5, target="ic")
    assert all(set(x["params"]) == {"window", "multiplier"} for x in r["results"])
    multipliers = {x["params"]["multiplier"] for x in r["results"]}
    assert len(multipliers) >= 2
    assert len({x["score"] for x in r["results"]}) >= 2
    assert {"window": 20, "multiplier": 2.5} in SPECS["boll"].candidates


# ---------------------------------------------------------------------------
# 3) 优先级：manual > v2 global > legacy global > defaults
# ---------------------------------------------------------------------------


def test_priority_defaults_then_legacy_then_v2_then_manual(temp_db):
    from app.api.indicators import indicator_params, indicators, put_indicator_params
    from app.storage.appparams import set_app_params

    # 无任何全局 → effective 用 spec 默认
    p = indicator_params()
    assert p["effective"]["macd"] == {"fast": 12, "slow": 26, "signal": 9}
    assert p["effective"]["rsi"] == {"window": 14}
    r = indicators("600519.SH", days=300, fields="rsi")
    assert r["effective_params"] == {}  # 无覆盖 → 默认

    # legacy 全局（旧格式字符串）生效
    set_app_params({"ind:rsi:ic": "6"})
    p2 = indicator_params()
    assert p2["effective"]["rsi"] == {"window": 6}
    r2 = indicators("600519.SH", days=300, fields="rsi")
    assert r2["effective_params"]["rsi"] == {"window": 6}

    # v2 全局优先于 legacy
    put_indicator_params(
        "rsi",
        {"values": {"window": 21}, "source": "global", "target": "ic", "horizon": 5},
    )
    p3 = indicator_params()
    assert p3["effective"]["rsi"] == {"window": 21}
    assert p3["global"]["rsi"]["values"] == {"window": 21}
    r3 = indicators("600519.SH", days=300, fields="rsi")
    assert r3["effective_params"]["rsi"] == {"window": 21}

    # manual（请求 custom）优先于 v2
    r4 = indicators("600519.SH", days=300, fields="rsi", custom=json.dumps({"rsi": 9}))
    assert r4["effective_params"]["rsi"] == {"window": 9}


# ---------------------------------------------------------------------------
# 4) PUT / params / DELETE(reset) / 非法约束
# ---------------------------------------------------------------------------


def test_put_params_persist_json_and_reset(temp_db):
    from app.api.indicators import (
        delete_indicator_params,
        indicator_params,
        put_indicator_params,
    )
    from app.storage.appparams import get_app_param

    put_indicator_params(
        "macd",
        {
            "values": {"fast": 6, "slow": 13, "signal": 5},
            "source": "global",
            "target": "sharpe",
            "horizon": 10,
        },
    )
    stored = json.loads(get_app_param("ind:v2:macd"))
    assert stored["values"] == {"fast": 6, "slow": 13, "signal": 5}
    assert (
        stored["source"] == "global"
        and stored["target"] == "sharpe"
        and stored["horizon"] == 10
    )
    p = indicator_params()
    assert p["global"]["macd"]["values"]["fast"] == 6
    assert p["effective"]["macd"] == {"fast": 6, "slow": 13, "signal": 5}

    # DELETE → 恢复系统默认
    r = delete_indicator_params("macd")
    assert r["defaults"] == {"fast": 12, "slow": 26, "signal": 9}
    assert get_app_param("ind:v2:macd") is None
    p2 = indicator_params()
    assert "macd" not in p2["global"]
    assert p2["effective"]["macd"] == {"fast": 12, "slow": 26, "signal": 9}


def test_illegal_constraints_400(temp_db):
    from app.api.indicators import delete_indicator_params, put_indicator_params

    def raises400(**kwargs):
        with pytest.raises(HTTPException) as ei:
            put_indicator_params(**kwargs)
        assert ei.value.status_code == 400

    raises400(
        indicator="macd",
        payload={
            "values": {"fast": 999, "slow": 26, "signal": 9},
            "source": "global",
            "target": "ic",
            "horizon": 5,
        },
    )  # 超 max
    raises400(
        indicator="macd",
        payload={
            "values": {"fast": 12, "slow": 26},
            "source": "global",
            "target": "ic",
            "horizon": 5,
        },
    )  # 缺参数
    raises400(
        indicator="macd",
        payload={
            "values": {"fast": 12.5, "slow": 26, "signal": 9},
            "source": "global",
            "target": "ic",
            "horizon": 5,
        },
    )  # int 型小数
    raises400(
        indicator="boll",
        payload={
            "values": {"window": 20, "multiplier": 2.05},
            "source": "global",
            "target": "ic",
            "horizon": 5,
        },
    )  # 不满足步长 0.1
    raises400(
        indicator="rsi",
        payload={
            "values": {"window": 14},
            "source": "weird",
            "target": "ic",
            "horizon": 5,
        },
    )  # source 非法
    raises400(
        indicator="rsi",
        payload={
            "values": {"window": 14},
            "source": "global",
            "target": "bad_target",
            "horizon": 5,
        },
    )  # target 非法
    raises400(
        indicator="rsi",
        payload={
            "values": {"window": 14},
            "source": "global",
            "target": "ic",
            "horizon": 0,
        },
    )  # horizon 越界
    raises400(
        indicator="rsi",
        payload={
            "values": {"window": 14},
            "source": "global",
            "target": "ic",
            "horizon": True,
        },
    )  # horizon 布尔
    raises400(
        indicator="unknown_ind",
        payload={"values": {}, "source": "global", "target": "ic", "horizon": 5},
    )

    with pytest.raises(HTTPException) as ei:
        delete_indicator_params("unknown_ind")
    assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# 5) 旧格式兼容：tune 响应契约 / compute_all 数字·数组·v2 对象 / 图表多线输出
# ---------------------------------------------------------------------------


def test_tune_response_contract_and_validation(temp_db):
    """P1-38b：tune 提交后台任务（返回 job_id），job.result 保持原同步契约字段；
    非法参数同步 400（校验不提交任务）。"""
    from app.api.indicators import tune
    from app.storage.appparams import set_app_params

    set_app_params({"ind:macd:ic": "6,13,5"})  # legacy 全局
    r = tune("600519.SH", {"indicator": "macd", "horizon": 5, "target": "ic"})
    assert r["status"] == "pending" and isinstance(r["job_id"], int)
    j = _wait_job(r["job_id"])
    assert j["status"] == "done", j.get("error")
    res = j["result"]
    for field in ("spec", "defaults", "current", "global"):
        assert field in res
    assert res["defaults"] == {"fast": 12, "slow": 26, "signal": 9}
    assert res["current"] == {
        "fast": 6,
        "slow": 13,
        "signal": 5,
    }  # legacy 生效（无 v2）
    assert res["global"] is None
    assert res["global_saved"] is True
    assert "params" in res["best"] and "param" in res["best"]
    assert set(res["best"]["params"]) == {"fast", "slow", "signal"}

    # 非法 indicator / horizon / target → 400
    with pytest.raises(HTTPException) as ei:
        tune("600519.SH", {"indicator": "nope", "horizon": 5, "target": "ic"})
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        tune("600519.SH", {"indicator": "macd", "horizon": 0, "target": "ic"})
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        tune("600519.SH", {"indicator": "macd", "horizon": "abc", "target": "ic"})
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        tune("600519.SH", {"indicator": "macd", "horizon": 5, "target": "nope"})
    assert ei.value.status_code == 400


def test_tune_submit_and_poll_contract(temp_db):
    """P1-38b 专项：提交返回 {job_id, status=pending}；轮询 GET /experiments/{job_id}
    拿到完整结果（job_type=indicator_tune、progress=100、results/best 契约齐全）。"""
    from app.api.indicators import tune

    r = tune(
        "600519.SH",
        {
            "indicator": "rsi",
            "horizon": 5,
            "target": "ic",
            "algorithm": "fast",
        },
    )
    assert r["status"] == "pending" and isinstance(r["job_id"], int)
    j = _wait_job(r["job_id"])
    assert j["job_type"] == "indicator_tune"
    assert j["status"] == "done", j.get("error")
    assert j["progress"] == 100.0
    assert "results" in j["result"] and "best" in j["result"]
    assert j["result"]["indicator"] == "rsi"
    assert j["result"]["algorithm"] == "fast"


def test_compute_all_legacy_and_v2_formats():
    from app.lib.indicators.compute import compute_all

    df = _synthetic_df()

    # 旧数字 / 旧数组
    out = compute_all(df, fields=["macd", "rsi"], custom={"macd": [8, 17, 9], "rsi": 9})
    assert "macd_dif" in out and "macd_hist" in out and "macd_dea" in out
    assert len(out["macd_dif"]) == len(df)
    # v2 具名对象 → 与旧数组等价
    out2 = compute_all(
        df, fields=["macd"], custom={"macd": {"fast": 8, "slow": 17, "signal": 9}}
    )
    assert out2["macd_dif"] == out["macd_dif"]
    # BOLL 旧数组与 v2 对象
    ob = compute_all(df, fields=["boll"], custom={"boll": [20, 2.5]})
    ob2 = compute_all(
        df, fields=["boll"], custom={"boll": {"window": 20, "multiplier": 2.5}}
    )
    assert np.allclose(ob["boll_upper"], ob2["boll_upper"], equal_nan=True)
    # 非法 custom 静默忽略
    oi = compute_all(df, fields=["macd"], custom={"macd": {"fast": 8}, "rsi": [1, 2]})
    assert oi["macd_hist"] == compute_all(df, fields=["macd"])["macd_hist"]


def test_saved_params_really_affect_chart_output():
    from app.lib.indicators.compute import (
        bias,
        compute_all,
        ema,
        mtm,
        rsi,
        trix,
        wr,
    )

    df = _synthetic_df()

    # RSI 自定义窗口 → rsi6/12/24 多线同步使用自定义窗口
    r = compute_all(df, fields=["rsi"], custom={"rsi": 9})
    assert "rsi6" in r and "rsi12" in r and "rsi24" in r and "rsi" in r
    assert r["rsi6"] == r["rsi12"] == r["rsi24"] == rsi(df["close"], 9).tolist()

    # WR / BIAS 多线同步
    w = compute_all(df, fields=["wr"], custom={"wr": 14})
    assert w["wr10"] == w["wr6"] == wr(df["high"], df["low"], df["close"], 14).tolist()
    b = compute_all(df, fields=["bias"], custom={"bias": 24})
    assert b["bias6"] == b["bias12"] == b["bias24"] == bias(df["close"], 24).tolist()

    # MTM / TRIX 信号线一致性
    m = compute_all(df, fields=["mtm"], custom={"mtm": 12})
    assert "mtmma" in m and m["mtmma"] == mtm(df["close"], 12)["mtmma"].tolist()
    t = compute_all(df, fields=["trix"], custom={"trix": 12})
    assert "matrix" in t and t["matrix"] == trix(df["close"], 12)["matrix"].tolist()

    # EMA 补全：输出 ema{n} 并覆盖主图 ema12/ema26
    e = compute_all(df, fields=["ema12", "ema26"], custom={"ema": 26})
    assert "ema26" in e and e["ema26"] == ema(df["close"], 26).tolist()
    assert e["ema12"] == ema(df["close"], 26).tolist()

    # MA 旧格式数字列表仍可用
    ma_out = compute_all(df, fields=["ma5"], custom={"ma": [5, 21]})
    assert "ma5" in ma_out and "ma21" in ma_out


def test_compute_all_default_branch_emits_signal_pairs():
    """默认分支（无 custom）请求 mtm/trix/atr/emv 时成对输出信号线字段：
    副图配置依赖 mtmma/matrix/tr/emvma（旧实现仅 custom 分支输出，默认分支缺失导致副图第二条线无数据）。"""
    from app.lib.indicators.compute import compute_all

    df = _synthetic_df(60)  # ≥30 根即可；与文件内其他用例共用合成数据
    out = compute_all(df, ["mtm", "trix", "atr", "emv"])
    for f in ("mtmma", "matrix", "tr", "emvma"):
        assert f in out
        assert len(out[f]) == len(df)  # NaN 允许，长度必须对齐
    # 与直接调用函数结果一致（口径对齐）
    from app.lib.indicators.compute import atr, emv, mtm, trix

    assert out["mtmma"] == mtm(df["close"])["mtmma"].tolist()
    assert out["matrix"] == trix(df["close"])["matrix"].tolist()
    assert out["tr"] == atr(df["high"], df["low"], df["close"])["tr"].tolist()
    assert out["emvma"] == emv(df["high"], df["low"], df["volume"])["emvma"].tolist()


def test_compute_all_default_branch_group_keys():
    """默认分支请求 wr/bias 组名时输出组名键（P1-27 修复：与 custom 分支口径一致，
    旧实现仅 custom 分支输出 wr/bias，默认分支缺组名键）。"""
    from app.lib.indicators.compute import bias, compute_all, wr

    df = _synthetic_df(60)
    out = compute_all(df, ["wr", "bias"])
    assert "wr" in out and len(out["wr"]) == len(df)
    assert out["wr"] == wr(df["high"], df["low"], df["close"], 10).tolist()
    assert "bias" in out and len(out["bias"]) == len(df)
    assert out["bias"] == bias(df["close"], 6).tolist()
    # 仅请求字段名（wr10）不产出组名键（与 rsi 分支口径一致）
    out2 = compute_all(df, ["wr10"])
    assert "wr" not in out2


def test_compute_all_macd_fast_ge_slow_rejected(caplog):
    """反向 MACD（fast>=slow）校验：跳过该键并记录日志，不产出错误曲线（P1-27）。"""
    from app.lib.indicators.compute import compute_all

    df = _synthetic_df(60)
    base = compute_all(df, ["macd"])
    with caplog.at_level(logging.WARNING, logger="stockradar.indicators.compute"):
        out = compute_all(
            df, ["macd"], custom={"macd": {"fast": 26, "slow": 12, "signal": 9}}
        )
    assert out["macd_dif"] == base["macd_dif"]  # 非法参数被忽略 → 回退默认
    assert any(
        "macd" in r.message and "fast" in r.message and "slow" in r.message
        for r in caplog.records
    ), "非法 macd 参数必须留痕日志"


# ---------------------------------------------------------------------------
# 6) GET /indicators/params 形状
# ---------------------------------------------------------------------------


def test_params_endpoint_shape(temp_db):
    from app.api.indicators import indicator_params
    from app.core.indicators.tune import SPECS

    p = indicator_params()
    assert set(p["specs"][0]) == {"key", "name", "params", "defaults", "candidates"}
    assert set(p["defaults"]) == set(SPECS)
    assert set(p["effective"]) == set(SPECS)
    assert isinstance(p["global"], dict) and isinstance(p["legacy"], dict)


# ---------------------------------------------------------------------------
# 7) tune 缓存命中也要实时计算 current/global（保存新全局后不得返回缓存旧值）
# ---------------------------------------------------------------------------


def test_tune_cache_hit_recomputes_current_global(temp_db, monkeypatch):
    from app.api.indicators import put_indicator_params, tune
    from app.storage.appparams import set_app_params
    import app.core.indicators.tune as T

    set_app_params({"ind:macd:ic": "6,13,5"})  # legacy 全局
    r1 = tune("600519.SH", {"indicator": "macd", "horizon": 5, "target": "ic"})
    j1 = _wait_job(r1["job_id"])
    assert j1["status"] == "done", j1.get("error")
    assert j1["result"]["current"] == {"fast": 6, "slow": 13, "signal": 5}
    assert j1["result"]["global"] is None

    # 第二次提交应命中缓存（handler 不重算网格）：重算计数保持 0
    calls = {"n": 0}
    real = T.tune_indicator

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(T, "tune_indicator", counting)

    # 保存 v2 全局后再次 tune：命中同一缓存键，但 current/global 必须反映新全局
    put_indicator_params(
        "macd",
        {
            "values": {"fast": 8, "slow": 17, "signal": 9},
            "source": "global",
            "target": "ic",
            "horizon": 5,
        },
    )
    r2 = tune("600519.SH", {"indicator": "macd", "horizon": 5, "target": "ic"})
    j2 = _wait_job(r2["job_id"])
    assert j2["status"] == "done", j2.get("error")
    assert calls["n"] == 0  # 走缓存命中分支，未重算网格
    assert j2["result"]["current"] == {"fast": 8, "slow": 17, "signal": 9}  # v2 优先
    assert j2["result"]["global"] == {
        "values": {"fast": 8, "slow": 17, "signal": 9},
        "source": "global",
        "target": "ic",
        "horizon": 5,
    }


# ---------------------------------------------------------------------------
# 8) 整体调优（多指标参数组合联合评分）
# ---------------------------------------------------------------------------


def _manual_comb_score(df, indicators, params_map, horizon=5, target="ic"):
    """手算组合序列评分：z-score 标准化（非 NaN 部分）→ 等权平均（NaN 处用其余指标均值）
    → _score_series。与 tune_indicators_combined 实现口径一致。"""
    from app.core.indicators.tune import _compute_values, _score_series

    normed = []
    for ind in indicators:
        v = _compute_values(ind, df, params_map[ind]).reset_index(drop=True).to_numpy()
        finite = v[np.isfinite(v)]
        if finite.size == 0:
            return None
        mean = float(np.mean(finite))
        std = float(np.std(finite))
        if std < 1e-12:
            return None
        normed.append((v - mean) / std)
    m = np.stack(normed)
    fin = np.isfinite(m)
    cnt = fin.sum(axis=0)
    v_comb = np.where(cnt > 0, np.where(fin, m, 0.0).sum(axis=0) / cnt, np.nan)
    close = df["close"].to_numpy()
    fwd = np.full(len(close), np.nan)
    fwd[:-horizon] = close[horizon:] / close[:-horizon] - 1
    return _score_series(v_comb, fwd, horizon, target)


def test_combined_tune_contract(temp_db):
    from app.api.indicators import tune_combined

    r = tune_combined(
        "600519.SH", {"indicators": ["macd", "kdj"], "horizon": 5, "target": "ic"}
    )
    assert r["status"] == "pending" and isinstance(r["job_id"], int)
    j = _wait_job(r["job_id"])
    assert j["status"] == "done", j.get("error")
    res = j["result"]
    assert res["combined"] is True
    assert res["indicators"] == ["macd", "kdj"]
    assert res["target"] == "ic" and res["horizon"] == 5 and res["algorithm"] == "grid"
    assert res["best"] is not None
    assert res["evaluated"] == len(res["results"]) > 0
    # results[0].params_by_indicator 键集 = 传入指标
    assert set(res["results"][0]["params_by_indicator"]) == {"macd", "kdj"}
    for rres in res["results"]:
        assert set(rres["params_by_indicator"]["macd"]) == {"fast", "slow", "signal"}
        assert set(rres["params_by_indicator"]["kdj"]) == {"window"}
        for f in (
            "ic",
            "icir",
            "rank_ic",
            "stability",
            "win_rate",
            "ls_annual",
            "sharpe",
            "max_drawdown",
            "composite",
            "score",
            "last_value",
        ):
            assert f in rres
    assert set(res["specs"]) == {"macd", "kdj"}
    assert res["specs"]["macd"]["name"] and res["specs"]["macd"]["defaults"] == {
        "fast": 12,
        "slow": 26,
        "signal": 9,
    }
    assert set(res["singles"]) == {"macd", "kdj"}
    assert isinstance(res["singles"]["macd"]["best_ic"], float)
    assert isinstance(res["singles"]["macd"]["best_score"], float)
    assert set(res["defaults"]) == {"macd", "kdj"}


def test_combined_validation(temp_db):
    from app.api.indicators import tune_combined

    def raises400(payload):
        with pytest.raises(HTTPException) as ei:
            tune_combined("600519.SH", payload)
        assert ei.value.status_code == 400

    raises400({"indicators": ["ma", "ema", "rsi", "macd"]})  # 4 指标
    raises400({"indicators": []})  # 空列表
    raises400({"indicators": ["macd", "nope"]})  # 未知指标
    raises400({"indicators": "macd"})  # 非 list
    raises400({"indicators": ["macd", "kdj"], "horizon": 0})  # horizon 越界
    raises400({"indicators": ["macd", "kdj"], "horizon": "abc"})
    raises400({"indicators": ["macd", "kdj"], "target": "nope"})
    raises400({"indicators": ["macd", "kdj"], "algorithm": "bad"})


def test_combined_cache_hit_recomputes_current_global(temp_db, monkeypatch):
    from app.api.indicators import put_indicator_params, tune_combined
    from app.storage.appparams import set_app_params
    import app.core.indicators.tune as T

    set_app_params({"ind:macd:ic": "6,13,5"})  # legacy 全局
    payload = {"indicators": ["macd", "kdj"], "horizon": 5, "target": "ic"}
    r1 = tune_combined("600519.SH", payload)
    j1 = _wait_job(r1["job_id"])
    assert j1["status"] == "done", j1.get("error")
    assert j1["result"]["current"]["macd"] == {"fast": 6, "slow": 13, "signal": 5}
    assert j1["result"]["current"]["kdj"] == {"window": 9}  # 无全局 → 默认
    assert j1["result"]["global"] == {}
    assert j1["result"]["global_saved"] is True

    calls = {"n": 0}
    real = T.tune_indicators_combined

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(T, "tune_indicators_combined", counting)

    # 保存 v2 全局后再次提交：命中同一缓存键，current/global 反映新全局
    put_indicator_params(
        "macd",
        {
            "values": {"fast": 8, "slow": 17, "signal": 9},
            "source": "global",
            "target": "ic",
            "horizon": 5,
        },
    )
    r2 = tune_combined("600519.SH", payload)
    j2 = _wait_job(r2["job_id"])
    assert j2["status"] == "done", j2.get("error")
    assert calls["n"] == 0  # 走缓存命中分支，未重算组合网格
    assert j2["result"]["current"]["macd"] == {"fast": 8, "slow": 17, "signal": 9}
    assert j2["result"]["global"]["macd"]["values"] == {
        "fast": 8,
        "slow": 17,
        "signal": 9,
    }
    assert j2["result"]["global_saved"] is True


def test_combined_score_manual(temp_db):
    """逐组手算 z-score 等权组合序列评分，与返回 results 完全对齐。"""
    from app.core.indicators.tune import SPECS, tune_indicators_combined

    df = _synthetic_df()
    r = tune_indicators_combined(
        ["macd", "kdj"], df, horizon=5, target="ic", algorithm="grid"
    )
    assert r["combined"] is True
    # 组合候选数 = 两指标候选数乘积（grid 全量，无裁剪）
    total = len(SPECS["macd"].candidates) * len(SPECS["kdj"].candidates)
    assert r["candidates_total"] == total
    assert r["evaluated"] <= total
    # score 降序
    scores = [x["score"] for x in r["results"]]
    assert scores == sorted(scores, reverse=True)
    # 逐组手算比对（score/ic 精确一致；其余键只验存在）
    for res in r["results"]:
        manual = _manual_comb_score(
            df, ["macd", "kdj"], res["params_by_indicator"], horizon=5, target="ic"
        )
        assert manual is not None
        assert res["score"] == manual["score"]
        assert res["ic"] == manual["ic"]
        assert res["composite"] == manual["composite"]
        assert res["max_drawdown"] == manual["max_drawdown"]
    # singles 手算对齐：各指标独立最优 score
    for ind in r["indicators"]:
        best_score = best_ic = None
        for cand in SPECS[ind].candidates:
            manual = _manual_comb_score(df, [ind], {ind: cand}, horizon=5, target="ic")
            if manual is None:
                continue
            if best_score is None or manual["score"] > best_score:
                best_score = manual["score"]
                best_ic = manual["ic"]
        assert r["singles"][ind]["best_score"] == best_score
        assert r["singles"][ind]["best_ic"] == best_ic


# ---------------------------------------------------------------------------
# 9) P2-35：缓存 key 字段序无关（字段乱序命中同一缓存）+ 评分向量化一致性
# ---------------------------------------------------------------------------


def test_indicator_cache_key_field_order(temp_db, monkeypatch):
    """字段顺序不同的请求命中同一缓存 key（排序归一），不重复计算。"""
    import app.api.indicators as I

    calls = {"n": 0}
    real = I.compute_all

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(I, "compute_all", counting)
    r1 = I.indicators("600519.SH", days=300, fields="macd,rsi,kdj")
    assert calls["n"] == 1
    r2 = I.indicators("600519.SH", days=300, fields="kdj,macd,rsi")
    assert calls["n"] == 1  # 字段乱序 → 命中同一缓存，未重算
    assert r1 == r2
    r3 = I.indicators("600519.SH", days=300, fields="macd,kdj")
    assert calls["n"] == 2  # 不同字段集不共享缓存


def test_score_series_vectorized_matches_semantics():
    """_score_series 向量化后语义一致：随机含 NaN 序列下仍产出稳定度量与 score。"""
    from app.core.indicators.tune import _score_series

    df = _synthetic_df()
    close = df["close"].to_numpy()
    fwd = np.full(len(close), np.nan)
    fwd[:-5] = close[5:] / close[:-5] - 1
    v = np.where(np.arange(len(close)) < 30, np.nan, np.random.default_rng(1).normal(0, 1, len(close)))
    v[100:105] = np.nan
    s = _score_series(v, fwd, horizon=5, target="ic")
    assert s is not None
    for k in ("ic", "icir", "rank_ic", "stability", "win_rate", "ls_annual", "sharpe", "max_drawdown", "composite", "score"):
        assert s[k] is not None
    # 全空窗口 → None（调用方跳过该候选）
    assert _score_series(np.full(len(close), np.nan), fwd, 5, "ic") is None


# ---------------------------------------------------------------------------
# 10) P2-46：指标调优任务协作取消（cancel_check 抛 JobCancelled → 及时释放 CPU）
# ---------------------------------------------------------------------------


def test_indicator_tune_cancel_mid_run(temp_db, monkeypatch):
    """运行中取消：delete_job 登记取消后，网格循环首处 checkpoint 抛 JobCancelled，
    任务以 cancelled 终态结束（不写 done/failed 覆盖取消）。"""
    import time

    import app.core.indicators.tune as T
    from app.api.indicators import tune
    from app.core.tasks.runner import delete_job

    real = T.tune_indicator
    holder: dict = {}

    def canceling(*a, **k):
        deadline = time.time() + 5
        while "job_id" not in holder and time.time() < deadline:
            time.sleep(0.001)
        if "job_id" in holder:
            delete_job(holder["job_id"])
        return real(*a, **k)

    monkeypatch.setattr(T, "tune_indicator", canceling)
    r = tune("600519.SH", {"indicator": "macd", "horizon": 5, "target": "ic"})
    holder["job_id"] = r["job_id"]
    j = _wait_job(r["job_id"])
    assert j["status"] == "cancelled", j.get("error")


def test_indicator_tune_combined_cancel_mid_run(temp_db, monkeypatch):
    import time

    import app.core.indicators.tune as T
    from app.api.indicators import tune_combined
    from app.core.tasks.runner import delete_job

    real = T.tune_indicators_combined
    holder: dict = {}

    def canceling(*a, **k):
        deadline = time.time() + 5
        while "job_id" not in holder and time.time() < deadline:
            time.sleep(0.001)
        if "job_id" in holder:
            delete_job(holder["job_id"])
        return real(*a, **k)

    monkeypatch.setattr(T, "tune_indicators_combined", canceling)
    r = tune_combined(
        "600519.SH", {"indicators": ["macd", "kdj"], "horizon": 5, "target": "ic"}
    )
    holder["job_id"] = r["job_id"]
    j = _wait_job(r["job_id"])
    assert j["status"] == "cancelled", j.get("error")


def test_cancel_check_propagates_in_tune_fns():
    from app.core.indicators.tune import tune_indicator, tune_indicators_combined
    from app.core.tasks.errors import JobCancelled

    df = _synthetic_df()
    n = {"calls": 0}

    def check():
        n["calls"] += 1
        if n["calls"] >= 2:
            raise JobCancelled("job 1 已被用户取消")

    with pytest.raises(JobCancelled):
        tune_indicator("macd", df, horizon=5, target="ic", cancel_check=check)
    assert n["calls"] >= 2
    with pytest.raises(JobCancelled):
        tune_indicators_combined(
            ["macd", "kdj"], df, horizon=5, target="ic", cancel_check=check
        )
