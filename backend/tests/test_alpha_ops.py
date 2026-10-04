"""Alpha 算子语义测试：验证 ts_delay/delta/shift 无前瞻偏差、evaluate_batch 功能等价。"""

import numpy as np
import pytest

from app.config import settings
from app.lib.alpha import backend as B
from app.lib.alpha.backtest import backtest_expression, run_backtest
from app.lib.alpha.operators import compile_rpn, evaluate_batch, evaluate_rpn


@pytest.fixture(scope="module", autouse=True)
def numpy_backend():
    settings.gp_backend = "numpy"
    B.init_backend()
    yield


def test_shift_lag_uses_past():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = B.to_numpy(B.shift(x, 1))
    assert np.isnan(out[0])
    np.testing.assert_allclose(out[1:], [1.0, 2.0, 3.0, 4.0])


def test_shift_lead_uses_future():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = B.to_numpy(B.shift(x, -1))
    np.testing.assert_allclose(out[:-1], [2.0, 3.0, 4.0, 5.0])
    assert np.isnan(out[-1])


def test_ts_delay_via_rpn_is_past_values():
    x = np.arange(1, 6, dtype=np.float32)[None, :]
    rpn = compile_rpn("ts_delay(close,1)")
    out = evaluate_rpn(rpn, {"close": x})
    assert np.isnan(out[0, 0])
    np.testing.assert_allclose(out[0, 1:], [1.0, 2.0, 3.0, 4.0])


def test_delta_via_rpn_is_current_minus_past():
    x = np.array([[1.0, 2.0, 4.0, 7.0, 11.0]], dtype=np.float32)
    rpn = compile_rpn("delta(close,1)")
    out = evaluate_rpn(rpn, {"close": x})
    assert np.isnan(out[0, 0])
    np.testing.assert_allclose(out[0, 1:], [1.0, 2.0, 3.0, 4.0])
    assert np.isfinite(out[0, 1:]).all()


def test_alpha101_formulas_have_no_lookahead():
    """ts_delay(close,5)/close-1（alpha101 #58 结构）的 t 时刻只依赖 t-5 及以前的信息。"""
    rng = np.random.default_rng(42)
    x = rng.standard_normal((10, 100)).astype(np.float32)
    rpn = compile_rpn("ts_delay(close,5) / close - 1")
    out = evaluate_rpn(rpn, {"close": x})
    assert np.isnan(out[:, :5]).all()
    assert np.isfinite(out[:, 5:]).all()


def test_evaluate_batch_matches_evaluate_rpn():
    rng = np.random.default_rng(7)
    x = rng.standard_normal((5, 50)).astype(np.float32)
    data = {"close": x, "volume": x + 1.0}
    rpn1 = compile_rpn("rank(ts_delay(close,1))")
    rpn2 = compile_rpn("delta(close,2) / ts_mean(close,5)")
    outs = evaluate_batch([rpn1, rpn2], data)
    assert len(outs) == 2
    np.testing.assert_allclose(outs[0], evaluate_rpn(rpn1, data))
    np.testing.assert_allclose(outs[1], evaluate_rpn(rpn2, data))


def test_backtest_combo_return_uses_prev_day_weights():
    """回测时序：权重在 t 日收盘决定、t+1 日生效。

    t=0 的因子把股票 0 排在首位，t=1 因子反转。若权重未后移（前视），
    组合第 1 日会按 t=1 的反转因子持仓，收益为 0；正确实现应吃到 t=0 持仓的 +1%。
    """
    n_s, n_t = 30, 12
    close = np.full((n_s, n_t), 100.0)
    close[0, 1] = 101.0  # 股票0 第1日 +1%（t=0 权重应吃到）
    close[8, 2] = 102.0  # 股票8 第2日 +2%（t=1 翻转后的权重应吃到）
    factor = np.zeros((n_s, n_t))
    factor[0, 0], factor[1, 0] = 3.0, 2.0
    factor[8, 0], factor[9, 0] = -2.0, -3.0
    factor[0, 1], factor[1, 1] = -3.0, -2.0  # t=1 因子反转
    factor[8, 1], factor[9, 1] = 3.0, 2.0
    r = run_backtest(
        factor,
        close,
        np.zeros((n_s, n_t)),
        top_pct=0.2,
        bottom_pct=0.2,
        cost_rate=0.0,
        trade_interval=1,
        mode="long",
        direction="long",
    )
    nav = r["combo_nav"]
    assert nav[0] == 1.0  # 首日收益为 0，不含因子当日收益
    n_long = max(1, int(n_s * 0.2))
    exp1 = 0.01 / n_long
    exp2 = 0.02 / n_long
    np.testing.assert_allclose(nav[1], 1.0 + exp1, atol=1e-6)
    np.testing.assert_allclose(nav[2], (1.0 + exp1) * (1.0 + exp2), atol=1e-6)


def test_backtest_direction_aliases():
    """前端 positive/negative 与后端 long/short 等价。"""
    rng = np.random.default_rng(3)
    n_s, n_t = 40, 40
    rets = rng.normal(0.001, 0.01, (n_s, n_t))
    close = 100.0 * np.cumprod(1 + rets, axis=1)
    panel = {"close": close}
    rpn = compile_rpn("ts_delay(close,1)")
    kw = dict(
        top_pct=0.2,
        bottom_pct=0.2,
        cost_rate=0.0,
        trade_interval=3,
        horizon=5,
        mode="long_short",
    )
    pos = backtest_expression(rpn, panel, direction="positive", **kw)
    long = backtest_expression(rpn, panel, direction="long", **kw)
    assert pos["combo_nav"] == long["combo_nav"]
    assert pos["params"]["direction"] == "long"
    neg = backtest_expression(rpn, panel, direction="negative", **kw)
    short = backtest_expression(rpn, panel, direction="short", **kw)
    assert neg["combo_nav"] == short["combo_nav"]
    assert neg["params"]["direction"] == "short"


def test_rank_preserves_nan():
    x = np.array([[1.0, 5.0], [3.0, 1.0], [np.nan, np.nan], [2.0, 4.0]])
    out = B.rank(x)
    assert np.isnan(out[2]).all()  # NaN 保持 NaN
    np.testing.assert_allclose(out[0], [0.0, 1.0])
    np.testing.assert_allclose(out[1], [1.0, 0.0])
    np.testing.assert_allclose(out[3], [0.5, 0.5])
    v = out[np.isfinite(out)]
    assert ((v >= 0) & (v <= 1)).all()
    # 单有效值：无秩可排，返回中性 0.5
    x2 = np.array([[1.0, np.nan], [np.nan, np.nan]])
    out2 = B.rank(x2)
    assert out2[0, 0] == 0.5
    assert np.isnan(out2).sum() == 3


def test_ts_mean_window_larger_than_series():
    x = np.random.default_rng(1).standard_normal((3, 100))
    out = B.ts_mean(x, 5000)  # w > T：不抛异常，返回全 NaN
    assert np.isnan(out).all()
    assert np.isnan(B.ts_max(x, 0)).all()  # _rolling_reduce w<=0 防御
    assert np.isnan(B.ts_rank(x, 1)).all()  # ts_rank w=1 不除零


def test_signed_power_matches_square():
    rng = np.random.default_rng(5)
    x = rng.standard_normal((4, 50)).astype(np.float32)
    rpn = compile_rpn("signed_power(close,2)")
    out = evaluate_rpn(rpn, {"close": x})
    # signed_power 保持符号：sign(x)·|x|^2 = sign(x)·x^2
    np.testing.assert_allclose(out, np.sign(x) * x**2, atol=1e-6)
    # alpha101 #84 嵌套式表达式（ts_min/signed_power/ts_argmax 常量参数提取）
    rpn2 = compile_rpn("rank(ts_argmax(signed_power(close - ts_min(low,5),2),5)) - 0.5")
    out2 = evaluate_rpn(rpn2, {"close": x, "low": x - 0.1})
    assert out2.shape == x.shape
    assert np.isfinite(out2).any()


def test_alpha101_all_compile_and_coverage():
    """Alpha101 全覆盖：id 集合 == 1..101，全部公式可编译。"""
    import re as _re
    from app.lib.alpha.alpha101 import ALPHA101
    from app.lib.alpha.operators import compile_rpn

    ids = {a["id"] for a in ALPHA101}
    assert ids == set(range(1, 102)), f"缺失 id: {sorted(set(range(1, 102)) - ids)}"

    failed = []
    for a in ALPHA101:
        try:
            compile_rpn(a["formula"])
        except Exception as e:
            failed.append((a["id"], str(e)[:80]))
    assert not failed, f"编译失败: {failed}"
    # 覆盖四分类
    cats = {a["category"] for a in ALPHA101}
    assert {"动量", "反转", "波动", "量价"} <= cats


# ---------- ts_mean/ts_std/ts_corr NaN 语义（P0-24，T-40） ----------


def _naive_roll_any_nan(a, w, fn):
    """朴素滑动窗口期望：窗口内任一 NaN → NaN；前缀不足 w（t<w-1）→ NaN。"""
    T = a.shape[-1]
    out = np.full(T, np.nan)
    for t in range(w - 1, T):
        win = a[t - w + 1 : t + 1]
        if np.isnan(win).any():
            continue
        out[t] = fn(win)
    return out


def test_ts_mean_nan_does_not_poison_later_windows():
    """中途 NaN 不再永久污染后续窗口（原 cumsum 技巧的 NaN 传染）。"""
    a = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
    out = B.to_numpy(B.ts_mean(a, 2))
    assert np.isnan(out[0])  # t0 前缀不足 w
    assert out[1] == 1.5  # 窗口 [1,2]
    assert np.isnan(out[2])  # 窗口 [2,nan] → NaN
    assert np.isnan(out[3])  # 窗口 [nan,4] → NaN
    assert out[4] == 4.5  # 窗口 [4,5] 不被污染


def test_ts_mean_std_corr_match_naive_window():
    """含 NaN 随机序列下 ts_mean/ts_std/ts_corr 与朴素窗口公式一致。"""
    rng = np.random.RandomState(0)
    a = rng.randn(20)
    b = rng.randn(20)
    for x in (a, b):  # 各随机置 3-5 个 NaN
        x[rng.choice(20, size=rng.randint(3, 6), replace=False)] = np.nan
    for w in (3, 5):
        m = B.to_numpy(B.ts_mean(a, w))
        np.testing.assert_allclose(
            m, _naive_roll_any_nan(a, w, np.mean), rtol=1e-6, atol=1e-8, equal_nan=True
        )
        s = B.to_numpy(B.ts_std(a, w))
        exp_s = _naive_roll_any_nan(
            a, w, lambda win: np.sqrt(max(np.mean(win * win) - np.mean(win) ** 2, 0.0))
        )
        np.testing.assert_allclose(s, exp_s, rtol=1e-6, atol=1e-8, equal_nan=True)
        c = B.to_numpy(B.ts_corr(a, b, w))
        exp_c = np.full(20, np.nan)
        for t in range(w - 1, 20):
            wa, wb = a[t - w + 1 : t + 1], b[t - w + 1 : t + 1]
            if np.isnan(wa).any() or np.isnan(wb).any():
                continue
            ma, mb = np.mean(wa), np.mean(wb)
            cov = np.mean(wa * wb) - ma * mb
            va = np.sqrt(max(np.mean(wa * wa) - ma * ma, 0.0))
            vb = np.sqrt(max(np.mean(wb * wb) - mb * mb, 0.0))
            exp_c[t] = cov / (va * vb) if abs(va * vb) >= 1e-12 else np.nan
        np.testing.assert_allclose(c, exp_c, rtol=1e-6, atol=1e-8, equal_nan=True)


def test_ts_mean_nan_edge_cases():
    """边界保留：w>T 全 NaN（既有测试继续覆盖）、w<=1 恒等。"""
    a = np.array([1.0, np.nan, 3.0])
    np.testing.assert_allclose(B.to_numpy(B.ts_mean(a, 1)), a, equal_nan=True)
    np.testing.assert_allclose(B.to_numpy(B.ts_mean(a, 0)), a, equal_nan=True)
