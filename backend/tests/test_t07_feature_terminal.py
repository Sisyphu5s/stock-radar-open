"""T-07 特征终端扩展测试。

- FEATURES 白名单：新增 pe/pb/ps/market_cap/float_cap/turnover/industry（7→13），
  validate_features 校验通过/非法拒绝/去重
- 新叶子 RPN 编译与求值（注册表机制自动支持）
- align_panel 缺失特征列 → 整列 NaN 容错；估值列按交易日对齐（非快照日 NaN）
- industry_codes 整数编码纯计算（同行业同名同码、未知=0）
- load_panel 重建路径注入估值特征：面板 13 列、估值仅快照日有值、行业为整数编码
- Alpha101 全库评估回归：101 条公式编译 + 在 OHLCV 面板上求值无异常
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.lib.alpha import backend as B
from app.lib.alpha.operators import (
    FEATURES,
    compile_rpn,
    evaluate_rpn,
    validate_features,
)
from app.lib.alpha.features import (
    ESTIMATE_FEATURES,
    INDUSTRY_FEATURE,
    align_panel,
    industry_codes,
)
from app.storage.db import Base
from app.storage.models import Dataset, DatasetStock

NEW_FEATURES = ["pe", "pb", "ps", "market_cap", "float_cap", "turnover", "industry"]
OHLCV_FEATURES = ["open", "high", "low", "close", "volume", "amount", "pct_change"]


@pytest.fixture(scope="module", autouse=True)
def numpy_backend():
    settings.gp_backend = "numpy"
    B.init_backend()
    yield


# ---------------------------------------------------------------------------
# 1) FEATURES 白名单扩展 + validate_features
# ---------------------------------------------------------------------------


def test_features_terminal_extended_to_13():
    # 规格 1 明确列举 7 个新特征（含 industry）：7 原 + 7 新 = 14 个叶子
    assert len(FEATURES) == 14
    for f in NEW_FEATURES:
        assert f in FEATURES
    for f in OHLCV_FEATURES:
        assert f in FEATURES
    assert list(FEATURES) == OHLCV_FEATURES + NEW_FEATURES


def test_validate_features_new_whitelist():
    assert validate_features(None) is None
    assert validate_features(NEW_FEATURES) == NEW_FEATURES
    assert validate_features(["close", "close", "pe"]) == ["close", "pe"]
    with pytest.raises(ValueError, match="features 含不支持的字段"):
        validate_features(["eps"])
    with pytest.raises(ValueError, match="features 含不支持的字段"):
        validate_features(["close", "sales"])
    with pytest.raises(ValueError, match="features 不能为空"):
        validate_features([])


# ---------------------------------------------------------------------------
# 2) 新叶子：RPN 编译与求值（注册表机制自动支持）
# ---------------------------------------------------------------------------


def _feature_data(s: int = 3, t: int = 10) -> dict:
    rng = np.random.default_rng(7)
    return {f: rng.standard_normal((s, t)).astype(np.float32) for f in FEATURES}


def test_rpn_new_leaf_compile_and_evaluate():
    data = _feature_data()
    for expr in ("pe / pb", "rank(pe)", "ts_mean(market_cap,5)", "ps - turnover"):
        out = evaluate_rpn(compile_rpn(expr), data)
        assert out.shape == (3, 10)
    # 树形表达（_tree_to_rpn）路径
    tree = {
        "name": "mul",
        "args": [{"name": "pe", "args": []}, {"name": "pb", "args": []}],
    }
    # 树形表达（_tree_to_rpn）路径：与 gp.py 同款用法（直接传 dict 树，新叶子可编译）
    from app.lib.alpha.operators import _tree_to_rpn

    tree = {
        "name": "mul",
        "args": [{"name": "pe", "args": []}, {"name": "pb", "args": []}],
    }
    out = evaluate_rpn(_tree_to_rpn(tree), data)
    assert out.shape == (3, 10)
    # 字符串表达式解析（_shunting_yard）路径
    assert evaluate_rpn(compile_rpn("industry * 1"), data).shape == (3, 10)


def test_rpn_estimate_nan_propagates():
    """估值特征历史 NaN：ts_mean 窗口含 NaN → 输出 NaN（不抛错）。"""
    data = _feature_data()
    data["pe"][:, :-1] = np.nan  # 仅最后一天有值
    out = evaluate_rpn(compile_rpn("ts_mean(pe,5)"), data)
    assert out.shape == (3, 10)
    assert np.isnan(out[:, -1]).all()  # 窗口内全 NaN


# ---------------------------------------------------------------------------
# 3) align_panel：缺失列容错 + 估值列对齐
# ---------------------------------------------------------------------------


def _kline(n: int, start: str = "2024-01-02") -> pd.DataFrame:
    dates = pd.date_range(start, periods=n).astype(str).tolist()
    close = 50.0 * (1.0 + np.linspace(0, 0.1, n))
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


def test_align_panel_missing_feature_is_nan():
    k = _kline(10)
    res = align_panel([("A", k), ("B", k.copy())], ["close", "pe", "turnover"])
    assert res is not None
    assert set(res["panel"]) == {"close", "pe", "turnover"}
    assert res["panel"]["close"].shape == (2, 10)
    assert np.isnan(res["panel"]["pe"]).all()  # 缺失列 → 整列 NaN
    assert np.isnan(res["panel"]["turnover"]).all()
    assert res["stock_count"] == 2


def test_align_panel_estimate_column_last_day_only():
    k1, k2 = _kline(10), _kline(10)
    for k in (k1, k2):
        k["pe"] = np.nan
        k["turnover"] = np.nan
    k1.loc[k1.index[-1], "pe"] = 8.0
    k1.loc[k1.index[-1], "turnover"] = 1.5
    k2.loc[k2.index[-1], "pe"] = 12.0
    k2.loc[k2.index[-1], "turnover"] = 2.5
    res = align_panel([("A", k1), ("B", k2)], ["close", "pe", "turnover"])
    assert res is not None
    pe = res["panel"]["pe"]
    assert np.isnan(pe[:, :-1]).all()  # 非快照日 NaN
    np.testing.assert_allclose(pe[:, -1], [8.0, 12.0])
    np.testing.assert_allclose(res["panel"]["turnover"][:, -1], [1.5, 2.5])


# ---------------------------------------------------------------------------
# 4) 行业整数编码纯计算
# ---------------------------------------------------------------------------


def test_industry_codes_pure():
    table = {"银行": 1, "白酒": 2}
    out = industry_codes(["银行", "白酒", "银行", "", None, "半导体", "nan"], table)
    np.testing.assert_allclose(out, [1, 2, 1, 0, 0, 0, 0])
    assert out.dtype == np.float32


def test_industry_constants():
    assert ESTIMATE_FEATURES == (
        "pe",
        "pb",
        "ps",
        "market_cap",
        "float_cap",
        "turnover",
    )
    assert INDUSTRY_FEATURE == "industry"


# ---------------------------------------------------------------------------
# 5) load_panel 重建路径：估值特征注入（13 列 / 对齐 / 行业编码）
# ---------------------------------------------------------------------------


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


def _spot_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "600000.SH",
                "pe": 8.0,
                "pb": 0.9,
                "ps": 1.2,
                "market_cap": 2500.0,
                "float_cap": 2000.0,
                "turnover_rate": 1.5,
                "industry": "银行",
            },
            {
                "code": "000001.SZ",
                "pe": 12.0,
                "pb": 1.1,
                "ps": 1.5,
                "market_cap": 3000.0,
                "float_cap": 2500.0,
                "turnover_rate": 2.5,
                "industry": "银行",
            },
        ]
    )


def test_load_panel_injects_estimate_features(panel_db, monkeypatch):
    import app.core.datasets as AD

    did = _seed_dataset(panel_db, ["600000.SH", "000001.SZ"])
    k = _kline(60)
    monkeypatch.setattr(AD, "cached_kline", lambda code, period, **kw: k.copy())
    monkeypatch.setattr(AD, "get_spot", lambda refresh=False: _spot_df())
    info = AD.load_panel(did)
    assert info is not None
    assert info["stock_count"] == 2
    # 13 个特征列齐备
    assert set(info["panel"]) == set(FEATURES)
    # 估值仅最后一个交易日有值（快照时点对齐），其余 NaN
    pe = info["panel"]["pe"]
    assert np.isnan(pe[:, :-1]).all()
    np.testing.assert_allclose(pe[:, -1], [8.0, 12.0])
    np.testing.assert_allclose(info["panel"]["pb"][:, -1], [0.9, 1.1])
    np.testing.assert_allclose(info["panel"]["ps"][:, -1], [1.2, 1.5])
    np.testing.assert_allclose(info["panel"]["market_cap"][:, -1], [2500.0, 3000.0])
    np.testing.assert_allclose(info["panel"]["float_cap"][:, -1], [2000.0, 2500.0])
    # turnover 特征 ← 快照 turnover_rate
    np.testing.assert_allclose(info["panel"]["turnover"][:, -1], [1.5, 2.5])
    # industry 整数编码（同行业同名同码；非快照日 NaN）
    ind = info["panel"]["industry"]
    assert np.isnan(ind[:, :-1]).all()
    np.testing.assert_allclose(ind[:, -1], [1.0, 1.0])


def test_load_panel_no_spot_keeps_nan_columns(panel_db, monkeypatch):
    """快照不可用 → 估值特征整列 NaN（不抛错，align_panel 缺失列容错兜底）。"""
    import app.core.datasets as AD

    did = _seed_dataset(panel_db, ["600000.SH", "000001.SZ"])
    k = _kline(60)
    monkeypatch.setattr(AD, "cached_kline", lambda code, period, **kw: k.copy())

    def _boom(*a, **kw):
        raise RuntimeError("snapshot down")

    monkeypatch.setattr(AD, "get_spot", _boom)
    info = AD.load_panel(did)
    assert info is not None
    assert set(info["panel"]) == set(FEATURES)
    for f in NEW_FEATURES:
        assert np.isnan(info["panel"][f]).all()
    # OHLCV 不受影响
    assert np.isfinite(info["panel"]["close"]).all()


def test_load_panel_partial_spot_coverage(panel_db, monkeypatch):
    """部分股票不在快照 → 该股估值整列 NaN，其余正常注入。"""
    import app.core.datasets as AD

    did = _seed_dataset(panel_db, ["600000.SH", "999999.SH"])
    k = _kline(60)
    monkeypatch.setattr(AD, "cached_kline", lambda code, period, **kw: k.copy())
    monkeypatch.setattr(AD, "get_spot", lambda refresh=False: _spot_df())
    info = AD.load_panel(did)
    assert info is not None
    pe = info["panel"]["pe"]
    # 600000 注入成功（第一行），999999 不在快照 → NaN
    np.testing.assert_allclose(pe[0, -1], 8.0)
    assert np.isnan(pe[1]).all()
    assert np.isnan(pe[:, :-1]).all()


# ---------------------------------------------------------------------------
# 6) Alpha101 全库评估回归（新特征不影响：全库只用 OHLCV 叶子）
# ---------------------------------------------------------------------------


def test_alpha101_full_library_evaluates_on_ohlcv():
    """101 条公式全部可编译，并在 OHLCV 面板上求值无异常（锁定评估路径绿）。"""
    from app.lib.alpha.alpha101 import ALPHA101

    rng = np.random.default_rng(42)
    s, t = 30, 130  # t >= 120 覆盖 ts_mean(close,120)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0001, 0.01, (s, t)), axis=1))
    data = {
        "open": close * (1 + rng.normal(0, 0.002, (s, t))),
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": rng.lognormal(10.0, 0.5, (s, t)).astype(np.float32),
        "amount": rng.lognormal(13.0, 0.5, (s, t)).astype(np.float32),
        "pct_change": rng.normal(0, 1.0, (s, t)).astype(np.float32),
    }
    for a in ALPHA101:
        rpn = compile_rpn(a["formula"])  # 编译失败 → 测试失败
        out = evaluate_rpn(rpn, data)  # 求值异常 → 测试失败
        assert out.shape == (s, t)
    assert len(ALPHA101) == 101
