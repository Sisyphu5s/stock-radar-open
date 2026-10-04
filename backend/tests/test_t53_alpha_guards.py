"""T-53 Alpha 研究链路守卫测试。

- dataset.py 面板对齐回退：只比长度不比日期会让停牌股（同长度异日期）混入面板
  → 回退路径同样校验日期相等（修复后错位股被剔除）
- gp.py evolve 增加 seed：同 seed 两次演化结果完全一致（原无 seed 不可复现）
- backtest.py run_backtest_quantile：n_q 无上限校验 → 超大值直接 ValueError
  （每分位独立权重矩阵，大值 OOM，~29GB 级）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.storage.db import Base
from app.storage.models import Dataset, DatasetStock


@pytest.fixture(scope="module", autouse=True)
def numpy_backend():
    from app.lib.alpha import backend as B

    settings.gp_backend = "numpy"
    B.init_backend()
    yield


# ---------------------------------------------------------------------------
# 1) dataset.py：对齐回退校验日期相等
# ---------------------------------------------------------------------------


def _kline(dates):
    n = len(dates)
    rng = np.random.default_rng(0)
    close = 50 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.ones(n),
            "amount": np.ones(n),
            "pct_change": np.zeros(n),
        }
    )


def _dates(start, n=60):
    return pd.date_range(start, periods=n).astype(str).tolist()


@pytest.fixture()
def panel_db(tmp_path, monkeypatch):
    """独立临时 DB + 打桩磁盘/缓存/K线，直接驱动 load_panel 重建路径。"""
    import app.core.datasets as AD
    from app.storage import cache as CACHE

    engine = create_engine(
        f"sqlite:///{tmp_path / 'panel.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(AD, "SessionLocal", Maker)
    monkeypatch.setattr(AD, "_load_panel_disk", lambda *a, **k: None)
    monkeypatch.setattr(AD, "_save_panel_disk", lambda *a, **k: None)
    # T-07: load_panel 重建路径默认全量特征含估值列 → 打桩快照（不可用），
    # 避免真实网络拉取 + 后台行业构建线程干扰测试（估值特征记 NaN）
    monkeypatch.setattr(AD, "get_spot", lambda refresh=False: pd.DataFrame())
    CACHE.panel_cache.clear()
    yield Maker
    engine.dispose()


def _seed_dataset(maker, codes):
    db = maker()
    ds = Dataset(
        name="t",
        universe="custom",
        start_date="2024-01-01",
        end_date="2024-12-31",
        status="ready",
    )
    db.add(ds)
    db.flush()
    for i, c in enumerate(codes):
        db.add(DatasetStock(dataset_id=ds.id, code=c, seq=i))
    db.commit()
    did = ds.id
    db.close()
    return did


def test_panel_fallback_excludes_misaligned_same_length(panel_db, monkeypatch):
    """回退路径：A/B 长度相同但日期错位（停牌模拟）→ B 被剔除，不再按位置错位对齐。"""
    import app.core.datasets as AD

    did = _seed_dataset(panel_db, ["A", "B"])
    da, db_dates = _dates("2024-01-02"), _dates("2024-01-03")
    monkeypatch.setattr(
        AD,
        "cached_kline",
        lambda code, period, **kw: _kline(da if code == "A" else db_dates),
    )
    info = AD.load_panel(did)
    assert info is not None
    assert info["stock_count"] == 1, "日期错位的同长度股票不得进入面板"
    assert info["dates"] == da


def test_panel_fallback_keeps_matched_dates(panel_db, monkeypatch):
    """对照：A/B 日期完全一致 → 两股都进入面板（回退/主路径都不误杀）。"""
    import app.core.datasets as AD

    did = _seed_dataset(panel_db, ["A", "B"])
    da = _dates("2024-01-02")
    monkeypatch.setattr(AD, "cached_kline", lambda code, period, **kw: _kline(da))
    info = AD.load_panel(did)
    assert info is not None
    assert info["stock_count"] == 2
    assert info["dates"] == da


def _seed_dataset_no_constituents(maker, universe: str = "custom"):
    """只建 Dataset、不写 DatasetStock（模拟成分缺失）。"""
    db = maker()
    ds = Dataset(
        name="t",
        universe=universe,
        start_date="2024-01-01",
        end_date="2024-12-31",
        status="ready",
    )
    db.add(ds)
    db.commit()
    did = ds.id
    db.close()
    return did


def test_panel_missing_constituents_rebuilds_from_universe(panel_db, monkeypatch):
    """成分缺失（DatasetStock 全空）→ 按 Dataset.universe 重新解析（P1-32），
    不再静默回退成交额 top300 猜测池——hs300 数据集不得悄悄变成 top300 池。"""
    import app.core.datasets as AD

    did = _seed_dataset_no_constituents(panel_db, universe="hs300")

    calls = {}

    def fake_resolve(db_, universe, limit, custom_codes):
        calls.update(universe=universe, limit=limit, custom_codes=custom_codes)
        return ["A", "B"]

    monkeypatch.setattr(AD, "_resolve_codes", fake_resolve)
    da = _dates("2024-01-02")
    monkeypatch.setattr(AD, "cached_kline", lambda code, period, **kw: _kline(da))
    info = AD.load_panel(did)
    assert info is not None
    assert info["stock_count"] == 2
    assert calls == {"universe": "hs300", "limit": 0, "custom_codes": None}, (
        "成分缺失必须按 universe 语义解析，禁止换池"
    )


def test_panel_missing_constituents_universe_failure_returns_none(
    panel_db, monkeypatch
):
    """成分缺失且 universe 解析失败（如 hs300 成分获取失败）→ load_panel 返回 None，
    不静默换池、不抛 500。"""
    import app.core.datasets as AD

    did = _seed_dataset_no_constituents(panel_db, universe="hs300")

    def boom(db_, universe, limit, custom_codes):
        raise ValueError("universe 'hs300' 成分获取失败")

    monkeypatch.setattr(AD, "_resolve_codes", boom)
    assert AD.load_panel(did) is None


def test_panel_features_subset_built_and_cached(panel_db, monkeypatch):
    """features 子集生效（P2-15）：重建只构建请求特征子集，
    返回 panel 只含子集列，冷缓存按子集指纹落盘。"""
    import app.core.datasets as AD

    did = _seed_dataset(panel_db, ["A", "B"])
    da = _dates("2024-01-02")
    monkeypatch.setattr(AD, "cached_kline", lambda code, period, **kw: _kline(da))

    saved = {}

    def fake_save(dataset_id, panel, dates, codes, features=None):
        saved.update(panel=set(panel), features=features)

    monkeypatch.setattr(AD, "_save_panel_disk", fake_save)
    info = AD.load_panel(did, features=["close", "volume"])
    assert info is not None
    assert set(info["panel"]) == {"close", "volume"}
    assert saved == {"panel": {"close", "volume"}, "features": ["close", "volume"]}


def test_panel_cold_cache_subset_files_independent(tmp_path, monkeypatch):
    """冷缓存按特征指纹分文件（P2-15）：不同子集互不覆盖、各自命中。"""
    import json
    import time

    import numpy as np

    import app.core.datasets as AD

    monkeypatch.setattr(AD, "PANEL_DIR", tmp_path)
    monkeypatch.setattr(AD, "_klines_row_count", lambda: 5)
    did = 7
    dates = ["2026-01-01"] * 5
    codes = ["600519.SH", "000001.SZ"]

    AD._save_panel_disk(
        did,
        {"close": np.zeros((2, 5)), "volume": np.zeros((2, 5))},
        dates,
        codes,
        features=["close", "volume"],
    )
    AD._save_panel_disk(
        did, {"close": np.ones((2, 5))}, dates, codes, features=["close"]
    )
    full = AD._load_panel_disk(did, want=["close", "volume"])
    assert full is not None and set(full["panel"]) == {"close", "volume"}
    sub = AD._load_panel_disk(did, want=["close"])
    assert sub is not None and set(sub["panel"]) == {"close"}
    # 子集重建不得覆盖另一子集文件
    assert AD._load_panel_disk(did, want=["close", "volume"]) is not None


# ---------------------------------------------------------------------------
# 2) gp.py：evolve seed 可复现
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


def test_evolve_seed_reproducible():
    """同 seed 两次演化结果完全一致（修复前无 seed，演化不可复现）。"""
    from app.lib.alpha.gp import evolve

    kw = dict(
        op_set=["add", "sub", "ts_mean", "rank"],
        features=["close", "volume"],
        op_config={"ts_mean": {"window": [3]}},
        pop_size=16,
        generations=3,
    )
    train, val, fwd_tr, fwd_vl = _evolve_data()
    r1, e1 = evolve(
        seed=42,
        data_train=train,
        data_val=val,
        forward_returns=fwd_tr,
        val_forward_returns=fwd_vl,
        **kw,
    )
    r2, e2 = evolve(
        seed=42,
        data_train=train,
        data_val=val,
        forward_returns=fwd_tr,
        val_forward_returns=fwd_vl,
        **kw,
    )
    assert [x["expression"] for x in r1] == [x["expression"] for x in r2]
    assert [x["val_ic"] for x in r1] == [x["val_ic"] for x in r2]
    assert [g["best_expression"] for g in e1] == [g["best_expression"] for g in e2]
    assert r1 and e1  # 有实际结果


def test_evolve_without_seed_still_runs():
    """seed=None（默认）保持既有行为：正常跑通。"""
    from app.lib.alpha.gp import evolve

    train, val, fwd_tr, fwd_vl = _evolve_data()
    results, evolution = evolve(
        op_set=["add", "sub"],
        features=["close"],
        pop_size=8,
        generations=1,
        data_train=train,
        data_val=val,
        forward_returns=fwd_tr,
        val_forward_returns=fwd_vl,
    )
    assert evolution


# ---------------------------------------------------------------------------
# 3) backtest.py：n_q 上限校验
# ---------------------------------------------------------------------------


def test_backtest_quantile_nq_cap():
    from app.lib.alpha.backtest import run_backtest_quantile

    rng = np.random.default_rng(0)
    S, T = 30, 100
    f = rng.standard_normal((S, T))
    c = np.cumprod(1 + 0.01 * rng.standard_normal((S, T)), axis=1)
    fwd = rng.standard_normal((S, T))

    with pytest.raises(ValueError, match="超过上限"):
        run_backtest_quantile(f, c, fwd, n_q=100000)  # 修复前 ~29GB OOM

    r = run_backtest_quantile(f, c, fwd, n_q=5)
    assert len(r["quantiles"]) == 5  # 正常分层不受影响
