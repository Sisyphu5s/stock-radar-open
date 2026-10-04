"""T-09 因子生命周期分析测试:相关性矩阵 / 冗余聚类 / IC 衰减半衰期 / 风格归因。

覆盖:
- correlation_matrix 数值(已知相关的 IC 序列对);
- cluster_redundant 分组(单链传递闭包 / NaN 边 / 阈值边界);
- _half_life 半衰期判定逻辑 + ic_decay 结构(确定性衰减面板);
- style_attribution 回归数值(确定性构造:行业系数 0.1、市值系数 0、残差 IC≈0);
- API 端点冒烟(monkeypatch load_panel + 临时因子库)。
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.lib.alpha import backend as _B
from app.lib.alpha.factor_analysis import (
    _half_life,
    cluster_redundant,
    correlation_matrix,
    ic_decay,
    style_attribution,
)


@pytest.fixture(autouse=True)
def _init_backend():
    _B.init_backend()


# ---------------------------------------------------------------------------
# 相关性矩阵
# ---------------------------------------------------------------------------


def test_correlation_matrix_pearson_values():
    rng = np.random.RandomState(42)
    a = np.sin(np.linspace(0, 25, 120)) + rng.normal(0, 0.05, 120)
    b = 3 * a + rng.normal(0, 0.08, 120)  # 强线性相关
    c = rng.normal(0, 1, 120)  # 无关
    m = correlation_matrix([a, b, c])
    assert m.shape == (3, 3)
    np.testing.assert_allclose(np.diag(m), 1.0)
    assert abs(m[0, 1]) > 0.99
    assert abs(m[0, 2]) < 0.3
    # 公共有限值对齐:插 NaN 不破坏
    a2 = a.copy()
    a2[10:30] = np.nan
    m2 = correlation_matrix([a2, b])
    assert abs(m2[0, 1]) > 0.99
    # 数据不足(公共值 < 5)→ NaN
    m3 = correlation_matrix(
        [np.array([1.0, 2.0, np.nan]), np.array([1.0, 2.0, np.nan])]
    )
    assert np.isnan(m3[0, 1])
    # 零方差 → NaN
    m4 = correlation_matrix([np.ones(10), np.linspace(0, 1, 10)])
    assert np.isnan(m4[0, 1])


# ---------------------------------------------------------------------------
# 冗余聚类
# ---------------------------------------------------------------------------


def test_cluster_redundant_basic():
    m = np.array([[1, 0.9, 0.1], [0.9, 1, 0.2], [0.1, 0.2, 1]])
    assert cluster_redundant(m, 0.8) == [[0, 1], [2]]


def test_cluster_redundant_single_link_transitive():
    """单链:0-1 与 1-2 相关 ≥ 阈值,0-2 低于阈值 → 三者仍同组(传递闭包)。"""
    m = np.array([[1, 0.9, 0.1], [0.9, 1, 0.85], [0.1, 0.85, 1]])
    assert cluster_redundant(m, 0.8) == [[0, 1, 2]]


def test_cluster_redundant_threshold_boundary_and_nan():
    m = np.array([[1, 0.8, 0.79], [0.8, 1, np.nan], [0.79, np.nan, 1]])
    # 恰好等于阈值归组;低于阈值不归组;NaN 边视为无边
    assert cluster_redundant(m, 0.8) == [[0, 1], [2]]
    # 空矩阵 / 单因子
    assert cluster_redundant(np.zeros((1, 1)), 0.5) == [[0]]


# ---------------------------------------------------------------------------
# IC 衰减 / 半衰期
# ---------------------------------------------------------------------------


def test_half_life_logic():
    assert _half_life([1, 3, 5, 10, 20], [0.2, 0.15, 0.09, 0.05, 0.02]) == 5
    # 负 IC 因子按绝对值(方向无关)
    assert _half_life([1, 3, 5], [-0.2, -0.12, -0.06]) == 5
    # 未衰减到峰值一半 → None
    assert _half_life([1, 3, 5], [0.2, 0.15, 0.11]) is None
    # 无有效 IC → None
    assert _half_life([1, 3], [None, None]) is None
    # None 跳过,取后续首个跌破点
    assert _half_life([1, 3, 5], [None, 0.2, 0.05]) == 5


def _decay_panel(S=40, T=60, seed=7):
    """mock 面板:close 随机游走 + open 特征承载因子值(与收益同源)。

    注:常数 alpha 的 IC 数学上随 h 增强(信号线性累积、噪声平方根累积),
    真实衰减需时变 alpha——ic_decay 的半衰期装配逻辑改由 monkeypatch
    ic_series 注入单调递减序列测试(_half_life 纯逻辑单独覆盖)。
    """
    rng = np.random.RandomState(seed)
    f = rng.randn(S)  # 股票固定个性分
    T_ = T
    rets = f[:, None] * 0.05 + rng.randn(S, T_) * 0.02
    close = np.cumprod(1.0 + rets, axis=1) * 100.0
    panel = {"close": close.astype(np.float32)}
    return panel, f


def _factor_panel(panel, f):
    """open 特征承载因子值(在 FEATURES 白名单,可被 compile_rpn 解析)。"""
    out = dict(panel)
    out["open"] = np.broadcast_to(
        f[:, None], (f.shape[0], panel["close"].shape[1])
    ).astype(np.float32)
    return out


def test_ic_decay_structure():
    """真实面板:结构与 IC 有效性,不做单调性断言。"""
    panel, f = _decay_panel()
    factor_panel = _factor_panel(panel, f)
    out = ic_decay("rank(open)", factor_panel, horizons=[1, 3, 5, 10, 20])
    assert out["horizons"] == [1, 3, 5, 10, 20]
    assert len(out["ic_means"]) == 5
    # 全部 horizon 有有效 IC(足够样本)
    assert all(m is not None and np.isfinite(m) for m in out["ic_means"])
    assert out["half_life"] is None or out["half_life"] in out["horizons"]


def test_ic_decay_half_life_assembly(monkeypatch):
    """半衰期装配:ic_series 按 horizon 顺序注入单调衰减序列 → half_life 命中。

    ic_decay 函数内 `from .evaluate import ic_series`,运行时从 evaluate 模块解析,
    monkeypatch evaluate.ic_series 生效(forward_returns 保持真实路径)。
    """
    import app.lib.alpha.evaluate as EV

    panel, f = _decay_panel()
    factor_panel = _factor_panel(panel, f)
    # 各 horizon 的 IC 序列(按调用顺序消费):均值 0.2→0.02 单调衰减,
    # 峰值 0.2、半峰值 0.1 → 0.09(h=5)首次跌破 → half_life=5
    series = iter(
        np.full(40, v, dtype=np.float64) for v in (0.2, 0.15, 0.09, 0.05, 0.02)
    )

    def fake_ics(factor, fwd):
        return next(series)

    monkeypatch.setattr(EV, "ic_series", fake_ics)
    out = ic_decay("rank(open)", factor_panel, horizons=[1, 3, 5, 10, 20])
    assert out["ic_means"] == pytest.approx([0.2, 0.15, 0.09, 0.05, 0.02])
    assert out["half_life"] == 5

    # 全序列均 ≥ 峰值一半 → 未衰减 → None
    series2 = iter(np.full(40, 0.2, dtype=np.float64) for _ in range(5))
    monkeypatch.setattr(EV, "ic_series", lambda f, w: next(series2))
    out2 = ic_decay("rank(open)", factor_panel, horizons=[1, 3, 5, 10, 20])
    assert out2["half_life"] is None


def test_ic_decay_bad_expr_raises():
    panel, _ = _decay_panel()
    with pytest.raises(ValueError):
        ic_decay("??not-an-expr??", panel)


# ---------------------------------------------------------------------------
# 风格归因
# ---------------------------------------------------------------------------


def _attribution_data(seed=3):
    """确定性构造:S 只股票、3 个行业、行业 1 股票因子暴露 +0.1、市值系数 0。

    行业编码 {1,2,3}(0=未知剔除);市值 = 基础 + 行业相关随机值(与因子独立);
    未来收益为纯噪声(与因子无关)→ 残差 IC ≈ 0。
    """
    rng = np.random.RandomState(seed)
    S, T = 60, 24
    industry = np.tile(np.array([1, 1, 2, 2, 3, 3], dtype=float), S // 6)
    assert len(industry) == S
    ind = np.broadcast_to(industry[:, None], (S, T)).copy()
    factor = np.zeros((S, T))
    factor[industry == 1] = 0.1
    factor += rng.normal(0, 0.01, (S, T))  # 小噪声 → 残差非零
    market_cap = np.abs(rng.normal(1e2, 2e1, (S, T)))
    fwd_ret = rng.normal(0, 0.02, (S, T))
    return factor, fwd_ret, ind, market_cap


def test_style_attribution_regression_values():
    factor, fwd_ret, ind, cap = _attribution_data()
    out = style_attribution(factor, fwd_ret, ind, cap)
    assert out["n_periods"] == 24
    ex = out["industry_exposures"]
    # 行业 1 系数 ≈ 0.1,行业 2/3 ≈ 0
    assert ex["行业#1"] == pytest.approx(0.1, abs=0.02)
    assert abs(ex["行业#2"]) < 0.02
    assert abs(ex["行业#3"]) < 0.02
    # 市值系数 ≈ 0(与因子独立)
    assert abs(out["size_exposure"]) < 0.02
    # 残差 IC ≈ 0(收益为纯噪声)
    assert abs(out["residual_ic"]) < 0.15
    # 行业名映射
    out2 = style_attribution(
        factor, fwd_ret, ind, cap, industry_names={1: "白酒", 2: "银行", 3: "医药"}
    )
    assert set(out2["industry_exposures"]) == {"白酒", "银行", "医药"}
    assert out2["industry_exposures"]["白酒"] == pytest.approx(0.1, abs=0.02)


def test_style_attribution_shape_mismatch_raises():
    with pytest.raises(ValueError):
        style_attribution(
            np.zeros((5, 10)), np.zeros((5, 10)), np.zeros((5, 9)), np.zeros((5, 10))
        )


def test_style_attribution_no_valid_periods():
    """全 NaN / 全未知行业 → 空结果,不抛错。"""
    S, T = 30, 10
    out = style_attribution(
        np.full((S, T), np.nan),
        np.zeros((S, T)),
        np.zeros((S, T)),  # 全部行业 0(未知)
        np.ones((S, T)),
    )
    assert out["n_periods"] == 0
    assert out["industry_exposures"] == {}
    assert out["size_exposure"] is None
    assert out["residual_ic"] is None


# ---------------------------------------------------------------------------
# API 端点冒烟
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite,替换 factors 服务 SessionLocal(不触碰生产库)。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'factors_analysis.db'}",
        connect_args={"check_same_thread": False},
    )

    import app.storage.models  # noqa: F401  先注册 models 到 Base,create_all 才建表

    from app.storage.db import Base

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.factors as FS
    import app.storage.db as DB

    # 双路 patch:core.factors 模块级绑定 + storage.db 源头(factor_correlation
    # 端点函数内 `from ..storage.db import SessionLocal` 从源头模块解析)
    monkeypatch.setattr(FS, "SessionLocal", Maker)
    monkeypatch.setattr(DB, "SessionLocal", Maker)
    yield Maker
    engine.dispose()


def _install_panel_fake(monkeypatch, panel):
    """以 sys.modules fake 模块替换 app.core.datasets,提供 load_panel。

    隔离原因:T-08 并行实例正在编辑 storage/providers/akshare.py(未提交中间态,
    缩进损坏导致 import 即崩),真实 datasets 导入链(sources → providers)当前
    不可用;端点函数内 `from ..core.datasets import load_panel` 运行时从
    sys.modules 解析,替换后即生效,且不触碰 T-08 领地的文件。
    """
    import sys
    import types

    fake = types.ModuleType("app.core.datasets")
    fake.load_panel = lambda ds_id, features=None: {
        "panel": panel,
        "dates": [str(i) for i in range(panel["close"].shape[1])],
        "stock_count": panel["close"].shape[0],
    }
    # C11a:端点冷缓存守卫(C11a)会先探测 panel_ready,探测命中才走 load_panel;
    # fake 模块须一并提供,否则端点 `from ..core.datasets import panel_ready`
    # 运行时 AttributeError
    fake.panel_ready = lambda ds_id, features=None: True
    monkeypatch.setitem(sys.modules, "app.core.datasets", fake)


def test_correlation_api_smoke(temp_db, monkeypatch):
    from app.api.factors import factor_correlation, new_factor

    panel, f = _decay_panel(S=40, T=60)
    panel["open"] = np.broadcast_to(
        f[:, None], (f.shape[0], panel["close"].shape[1])
    ).astype(np.float32)
    _install_panel_fake(monkeypatch, panel)

    f1 = new_factor({"name": "因子A", "expression": "rank(open)"})
    f2 = new_factor(
        {"name": "因子B", "expression": "close/close"}
    )  # 常量 → 零方差 IC NaN
    f3 = new_factor({"name": "因子C", "expression": "-rank(open)"})  # 反向强相关
    ids = [f1["factor"]["id"], f2["factor"]["id"], f3["factor"]["id"]]
    out = factor_correlation({"factor_ids": ids, "dataset_id": 1})
    assert out["names"] == ["因子A", "因子B", "因子C"]
    assert np.asarray(out["matrix"]).shape == (3, 3)
    # A 与 C 负强相关 |corr|≈1 → 单链 |corr| 语义下聚类用原始值:相关 ≥ 0.8 才归组,
    # -1 不归组;A-B(零方差)NaN
    assert out["clusters"] == [
        {"members": ["因子A"]},
        {"members": ["因子B"]},
        {"members": ["因子C"]},
    ]

    # 校验:空 factor_ids → 400;因子不存在 → 400;nn 因子 → 400
    with pytest.raises(HTTPException) as e1:
        factor_correlation({"factor_ids": [], "dataset_id": 1})
    assert e1.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        factor_correlation({"factor_ids": [9999], "dataset_id": 1})
    assert e2.value.status_code == 400


def test_ic_decay_api_smoke(temp_db, monkeypatch):
    from app.api.factors import factor_ic_decay

    panel, f = _decay_panel(S=40, T=60)
    panel["open"] = np.broadcast_to(
        f[:, None], (f.shape[0], panel["close"].shape[1])
    ).astype(np.float32)
    _install_panel_fake(monkeypatch, panel)

    out = factor_ic_decay({"expr": "rank(open)", "dataset_id": 1})
    assert out["horizons"] == [1, 3, 5, 10, 20]
    assert len(out["ic_means"]) == 5
    assert out["half_life"] is None or out["half_life"] in out["horizons"]

    with pytest.raises(HTTPException) as e:
        factor_ic_decay({"expr": "   ", "dataset_id": 1})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        factor_ic_decay({"expr": "close", "dataset_id": 0})
    assert e2.value.status_code == 400


def test_attribution_api_smoke(temp_db, monkeypatch):
    from app.api.factors import factor_attribution

    factor, fwd_ret, ind, cap = _attribution_data()
    close = np.cumprod(1.0 + fwd_ret, axis=1) * 100.0
    panel = {
        "close": close.astype(np.float32),
        "industry": ind.astype(np.float32),
        "market_cap": cap.astype(np.float32),
    }
    _install_panel_fake(monkeypatch, panel)

    out = factor_attribution({"expr": "close/close + industry*0.1", "dataset_id": 1})
    assert out["n_periods"] > 0
    assert isinstance(out["industry_exposures"], dict)
    assert "size_exposure" in out and "residual_ic" in out

    # 数据集缺风格特征 → 400
    _install_panel_fake(monkeypatch, {"close": close.astype(np.float32)})
    with pytest.raises(HTTPException) as e:
        factor_attribution({"expr": "close", "dataset_id": 1})
    assert e.value.status_code == 400
    assert "风格特征" in str(e.value.detail)
