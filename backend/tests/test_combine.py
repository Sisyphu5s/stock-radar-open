"""T-11 多因子合成测试:combine 数值(打分秩和/IC 加权/等权平均/权重归一/RPN 表达式
可编译且与合成口径一致)、端点(exprs/factor_ids 模式 + 一键入因子库)、输入守卫。"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.lib.alpha.combine import (
    combine_by_equal,
    combine_by_ic_weight,
    combine_by_score,
    ic_weights_from_ic,
    rpn_expression,
)
from app.lib.alpha.operators import compile_rpn, evaluate_rpn
from app.lib.metrics import rank_ic


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite,替换 factors.service.SessionLocal(不触碰生产库)。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'combine.db'}",
        connect_args={"check_same_thread": False},
    )
    from app.storage.db import Base

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.factors as FS

    monkeypatch.setattr(FS, "SessionLocal", Maker)
    yield Maker
    engine.dispose()


def _signals(seed=5, S=60, T=150):
    """三个相关因子信号 + 与收益相关的 fwd(合成后 IC 应提升)。"""
    rng = np.random.default_rng(seed)
    base = rng.normal(size=(S, T))
    s1 = base
    s2 = base + 0.6 * rng.normal(size=(S, T))
    s3 = 0.5 * base + 0.9 * rng.normal(size=(S, T))
    fwd = 0.3 * base + 0.05 * rng.normal(size=(S, T))
    return [s1, s2, s3], fwd


# ---------------------------------------------------------------------------
# 数值:合成函数
# ---------------------------------------------------------------------------


def test_combine_by_score_rank_sum():
    signals, fwd = _signals()
    c = combine_by_score(signals)
    assert c.shape == signals[0].shape
    # 打分法:合成 IC 不低于最差单因子(基础信号强相关)
    ic_c = rank_ic(c, fwd)
    ics = [rank_ic(s, fwd) for s in signals]
    assert ic_c >= min(ics) - 1e-6
    # 等权秩和:值域 [0, n_factors]
    assert float(np.nanmax(c)) <= 3.0 + 1e-9


def test_combine_by_equal_zscore_mean():
    signals, _ = _signals()
    c = combine_by_equal(signals)
    # z-score 平均:逐期横截面均值 ≈ 0
    t = c.shape[1] // 2
    col = c[:, t]
    assert float(np.nanmean(col)) == pytest.approx(0.0, abs=1e-6)


def test_combine_by_ic_weight_sign_and_weights():
    signals, fwd = _signals()
    ics = [rank_ic(s, fwd) for s in signals]
    weights = ic_weights_from_ic(ics)
    assert sum(weights) == pytest.approx(1.0, abs=1e-6)
    assert all(w >= 0 for w in weights) or all(w <= 0 for w in weights)
    c = combine_by_ic_weight(signals, weights)
    ic_c = rank_ic(c, fwd)
    assert ic_c >= 0  # 权重带符号对齐方向后,合成 IC 应为正方向
    # 权重与 signals 数量不一致 → ValueError
    with pytest.raises(ValueError):
        combine_by_ic_weight(signals, [1.0])


def test_ic_weights_from_ic():
    assert ic_weights_from_ic([0.1, -0.3]) == pytest.approx([0.25, -0.75])
    assert ic_weights_from_ic([float("nan"), 0.2]) == pytest.approx([0.0, 1.0])
    # 全 NaN → 等权兜底
    assert ic_weights_from_ic([float("nan"), float("nan")]) == pytest.approx([0.5, 0.5])


def test_combine_empty_signals():
    with pytest.raises(ValueError):
        combine_by_score([])
    with pytest.raises(ValueError):
        combine_by_equal([])
    with pytest.raises(ValueError):
        combine_by_ic_weight([], [])


# ---------------------------------------------------------------------------
# RPN 表达式:可编译,且与合成口径一致(无 NaN 面板)
# ---------------------------------------------------------------------------


def test_rpn_expression_compile_and_match():
    from app.lib.alpha import backend as B
    from app.lib.alpha.evaluate import forward_returns

    B.init_backend()  # rank/ts 算子依赖后端(mlx/numpy)
    rng = np.random.default_rng(9)
    close = np.exp(np.cumsum(rng.normal(0.001, 0.01, size=(40, 150)), axis=1)) * 10
    panel = {"close": close.astype(np.float32)}
    a = evaluate_rpn(compile_rpn("rank(close)"), panel)
    b = evaluate_rpn(compile_rpn("ts_mean(close,5)"), panel)

    expr = rpn_expression(
        ["rank(close)", "ts_mean(close,5)"], [0.6, 0.4], method="score"
    )
    rpn = compile_rpn(expr)
    combined_expr = evaluate_rpn(rpn, panel)
    combined_fn = combine_by_ic_weight([a, b], [0.6, 0.4])
    # 无 NaN 区域:库内合成(rank 归一 + 加权)与表达式求值一致
    # (NaN 处理差异:表达式系统 NaN 传播;库内合成缺失按 0 中性——见 combine 文档)
    mask = np.isfinite(combined_expr) & np.isfinite(combined_fn)
    assert np.allclose(combined_expr[mask], combined_fn[mask], atol=1e-6)

    # 权重 None → 等权;负数权重也生成可编译表达式
    expr_eq = rpn_expression(["rank(close)", "ts_mean(close,5)"])
    compile_rpn(expr_eq)
    expr_neg = rpn_expression(["rank(close)", "ts_mean(close,5)"], [-0.3, 1.3])
    assert compile_rpn(expr_neg)


# ---------------------------------------------------------------------------
# 端点 /alpha/combine(路由函数直调 + monkeypatch load_panel)
# ---------------------------------------------------------------------------


def _fake_panel(S=40, T=120, seed=3):
    rng = np.random.default_rng(seed)
    close = np.exp(np.cumsum(rng.normal(0.0005, 0.01, size=(S, T)), axis=1)) * 10
    return {
        "panel": {"close": close.astype(np.float32)},
        "dates": [f"2024-01-{i % 28 + 1:02d}" for i in range(T)],
        "codes": [f"{600000 + i}.SH" for i in range(S)],
    }


def test_combine_endpoint_exprs(monkeypatch):
    from app.api.alpha import alpha_combine
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    res = alpha_combine(
        {
            "exprs": ["rank(close)", "ts_mean(close,5)"],
            "dataset_id": 1,
            "method": "ic_weight",
        }
    )
    assert res["method"] == "ic_weight"
    assert res["ic"] is not None
    assert len(res["ic_series"]) > 0
    assert len(res["weights"]) == 2
    # IC 加权权重 = IC/Σ|IC|:Σ|w| = 1(方向可同向,和可为 ±1)
    assert sum(abs(x["weight"]) for x in res["weights"]) == pytest.approx(1.0, abs=1e-6)
    assert len(res["combined_signal"]) > 0
    assert "expression" in res
    compile_rpn(res["expression"])  # 生成表达式可编译


def test_combine_endpoint_score_equal(monkeypatch):
    from app.api.alpha import alpha_combine
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    for method in ("score", "equal"):
        res = alpha_combine(
            {
                "exprs": ["rank(close)", "ts_mean(close,5)"],
                "dataset_id": 1,
                "method": method,
            }
        )
        assert res["method"] == method
        assert res["weights"][0]["weight"] == pytest.approx(0.5)


def test_combine_endpoint_guards(monkeypatch):
    from app.api.alpha import alpha_combine
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    # 无因子来源
    with pytest.raises(HTTPException) as e:
        alpha_combine({"dataset_id": 1})
    assert e.value.status_code == 400
    # 双源互斥
    with pytest.raises(HTTPException) as e:
        alpha_combine({"exprs": ["rank(close)"], "factor_ids": [1], "dataset_id": 1})
    assert e.value.status_code == 400
    # method 非法
    with pytest.raises(HTTPException) as e:
        alpha_combine({"exprs": ["rank(close)"], "dataset_id": 1, "method": "nope"})
    assert e.value.status_code == 400
    # dataset_id 非法
    with pytest.raises(HTTPException) as e:
        alpha_combine({"exprs": ["rank(close)"], "dataset_id": 0})
    assert e.value.status_code == 400
    # 表达式解析失败
    with pytest.raises(HTTPException) as e:
        alpha_combine({"exprs": ["rank("], "dataset_id": 1})
    assert e.value.status_code == 400


def test_combine_endpoint_factor_ids_and_save(monkeypatch, temp_db):
    from app.api.alpha import alpha_combine
    from app.core import datasets as AD
    from app.storage.models import Factor

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    db = temp_db()
    db.add_all(
        [
            Factor(
                name="f_a",
                expression="rank(close)",
                description="",
                dataset_id=1,
                kind="expr",
            ),
            Factor(
                name="f_b",
                expression="ts_mean(close,5)",
                description="",
                dataset_id=1,
                kind="expr",
            ),
        ]
    )
    db.commit()
    fids = [f.id for f in db.query(Factor).all()]

    res = alpha_combine(
        {
            "factor_ids": fids,
            "dataset_id": 1,
            "method": "score",
            "save_to_library": True,
        },
        db=db,
    )
    assert [w["name"] for w in res["weights"]] == ["f_a", "f_b"]
    # 入因子库成功
    assert res["factor"]["duplicate"] is False
    assert res["factor"]["id"] is not None
    assert res["factor"]["name"].startswith("合成因子")

    # 重复保存:表达式去重 → duplicate
    res2 = alpha_combine(
        {
            "factor_ids": fids,
            "dataset_id": 1,
            "method": "score",
            "save_to_library": True,
        },
        db=db,
    )
    assert res2["factor"]["duplicate"] is True


def test_combine_endpoint_factor_ids_missing(monkeypatch, temp_db):
    from app.api.alpha import alpha_combine
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(
        AD, "panel_ready", lambda ds, features=None: True
    )  # C11a 热缓存探测
    db = temp_db()
    with pytest.raises(HTTPException) as e:
        alpha_combine({"factor_ids": [999], "dataset_id": 1}, db=db)
    assert e.value.status_code == 400
