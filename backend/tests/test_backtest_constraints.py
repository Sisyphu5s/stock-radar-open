"""T-02 回测交易约束测试:涨跌停(按板块)/一字板/停牌/滑点/佣金/印花税。

覆盖:
- limit_pct_by_code 板块阈值与 signals.engine._limit_pct 单一事实源一致;
- can_buy/can_sell 纯函数:涨停拒买/跌停拒卖/一字板/停牌/反向可交易;
- effective_price 滑点数值(买升卖降);
- resolve_cost_rate 拆分模型折算(默认 0/2.5/5 bps → 0.001,与旧 cost_rate 一致);
- run_backtest 集成:涨停股排除出多头、跌停股排除出空头、整日停牌跳过调仓、
  个股停牌不可交易、无 ohlc/codes 时约束不生效(旧行为兼容)。
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.lib.alpha.backtest import (
    can_buy,
    can_sell,
    effective_price,
    limit_pct_by_code,
    resolve_cost_rate,
    run_backtest,
)
from app.lib.alpha.evaluate import forward_returns


def _make_panel(S=30, T=30, seed=1):
    rng = np.random.RandomState(seed)
    rets = rng.randn(S, T) * 0.02
    close = np.cumprod(1.0 + rets, axis=1) * 100.0
    open_ = close * (1.0 + rng.randn(S, T) * 0.005)
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    volume = rng.rand(S, T) * 1e6 + 1e5
    return {
        "close": close.astype(np.float64),
        "open": open_.astype(np.float64),
        "high": high.astype(np.float64),
        "low": low.astype(np.float64),
        "volume": volume.astype(np.float64),
    }


def _codes(S=30):
    """S 只主板代码(600xxx/000xxx 前缀 → 阈值 9.9)。"""
    return [f"600{i:03d}.SH" for i in range(S)]


def _top_factor(S=30, T=30, top_idx=(0,)):
    """因子:全局时间轴线性,指定股票(t≥4 后)恒为最高分 → 必进 top 组合。

    起点取 t=4 覆盖首个调仓日 t=5(使目标股在首个调仓日即处于最高分)。
    """
    f = np.zeros((S, T))
    for i in range(S):
        f[i, :] = i / S
    for i in top_idx:
        f[i, 4:] = 10.0  # t≥4 后最高
    return f


# ---------------------------------------------------------------------------
# 板块阈值(与 signals.engine._limit_pct 一致性)
# ---------------------------------------------------------------------------


def test_limit_pct_by_code_aligns_with_signals_engine():
    """板块阈值与 lib/signals/engine._limit_pct 单一事实源一致(同 code 前缀同数值)。"""
    import pandas as pd
    from app.lib.signals.engine import _limit_pct

    for code in [
        "600519.SH",
        "000001.SZ",
        "300750.SZ",
        "301000.SZ",
        "302000.SZ",
        "688256.SH",
        "430047.BJ",
        "830799.BJ",
        "002415.SZ",
        "603000.SH",
    ]:
        df = pd.DataFrame({"code": [code], "close": [10.0]})
        assert limit_pct_by_code(code) == _limit_pct(df, {}), code


def test_limit_pct_board_rules():
    assert limit_pct_by_code("600519.SH") == 9.9  # 主板
    assert limit_pct_by_code("000001.SZ") == 9.9  # 主板
    assert limit_pct_by_code("300750.SZ") == 19.9  # 创业板
    assert limit_pct_by_code("301000.SZ") == 19.9
    assert limit_pct_by_code("302000.SZ") == 19.9
    assert limit_pct_by_code("688256.SH") == 19.9  # 科创板
    assert limit_pct_by_code("430047.BJ") == 30.0  # 北交所
    assert limit_pct_by_code("830799.BJ") == 30.0


# ---------------------------------------------------------------------------
# can_buy / can_sell 纯函数
# ---------------------------------------------------------------------------


def test_can_buy_rejects_limit_up():
    """涨停(涨幅≥阈值 且 close≈high)→ 不可买。"""
    assert can_buy(100.0, 110.0, 100.5, 110.0, 1e6, 9.9) is False
    # 涨停但未触板(close < high,盘中冲高回落)→ 可买
    assert can_buy(100.0, 111.0, 100.5, 109.0, 1e6, 9.9) is True


def test_can_sell_rejects_limit_down():
    """跌停(跌幅≥阈值 且 close≈low)→ 不可卖。"""
    assert can_sell(100.0, 100.5, 90.0, 90.0, 1e6, 9.9) is False
    assert can_sell(100.0, 100.5, 89.0, 92.0, 1e6, 9.9) is True


def test_can_buy_sell_one_price_board():
    """一字板(开=高=低=收)不可买不可卖。"""
    assert can_buy(100.0, 110.0, 110.0, 110.0, 1e6, 9.9) is False
    assert can_sell(100.0, 110.0, 110.0, 110.0, 1e6, 9.9) is False
    # 一字板但涨幅未达阈值(如复牌平开)→ 仍不可买不可卖(任务卡口径)
    assert can_buy(100.0, 100.0, 100.0, 100.0, 1e6, 9.9) is False
    assert can_sell(100.0, 100.0, 100.0, 100.0, 1e6, 9.9) is False


def test_can_buy_sell_suspended():
    """停牌(volume≤0 或缺失)→ 不可买不可卖。"""
    assert can_buy(100.0, 100.5, 99.5, 100.0, 0.0, 9.9) is False
    assert can_sell(100.0, 100.5, 99.5, 100.0, -1.0, 9.9) is False
    assert can_buy(100.0, 100.5, 99.5, 100.0, float("nan"), 9.9) is False


def test_can_buy_sell_opposite_side_ok():
    """涨停可卖(卖家不受限)、跌停可买(买家不受限)。"""
    assert can_sell(100.0, 110.0, 100.5, 110.0, 1e6, 9.9) is True  # 涨停卖得出
    assert can_buy(100.0, 100.5, 90.0, 90.0, 1e6, 9.9) is True  # 跌停买得到


def test_can_buy_sell_missing_prev_close():
    """前收缺失(新股首日)不拦截:仅一字板/停牌生效。"""
    assert can_buy(float("nan"), 100.0, 99.0, 100.0, 1e6, 9.9) is True
    assert can_sell(float("nan"), 100.0, 99.0, 100.0, 1e6, 9.9) is True


# ---------------------------------------------------------------------------
# 滑点 / 成本折算
# ---------------------------------------------------------------------------


def test_effective_price_slippage():
    """滑点:买入抬价、卖出压价,1bp=1e-4。"""
    assert effective_price("buy", 100.0, 10.0) == pytest.approx(100.1)
    assert effective_price("sell", 100.0, 10.0) == pytest.approx(99.9)
    assert effective_price("buy", 100.0, 0.0) == pytest.approx(100.0)


def test_resolve_cost_rate():
    """拆分成本模型:总成本 = |Δw|·(滑点+佣金+印花税),印花税仅卖出(÷2)。"""
    # 默认 0/2.5/5 → 2×(0+0.00025)+0.0005 = 0.001(与旧默认 cost_rate 一致)
    rate, split = resolve_cost_rate(0.001, 0.0, 2.5, 5.0)
    assert rate == pytest.approx(0.001) and split is True
    # 滑点 10bp 单边:2×0.001 = 0.002
    rate, _ = resolve_cost_rate(0.001, 10.0, 0.0, 0.0)
    assert rate == pytest.approx(0.002)
    # 印花税 5bp 卖出单边:0.0005
    rate, _ = resolve_cost_rate(0.001, 0.0, 0.0, 5.0)
    assert rate == pytest.approx(0.0005)
    # 佣金 2.5bp 双边:2×0.00025 = 0.0005
    rate, _ = resolve_cost_rate(0.001, 0.0, 2.5, 0.0)
    assert rate == pytest.approx(0.0005)
    # 全 None → 旧 cost_rate(兼容)
    rate, split = resolve_cost_rate(0.001)
    assert rate == 0.001 and split is False
    # 任一显式即拆分:滑点 0 也进入拆分(未传的按 0)
    rate, split = resolve_cost_rate(0.001, 0.0, None, None)
    assert rate == pytest.approx(0.0) and split is True


# ---------------------------------------------------------------------------
# run_backtest 集成:涨跌停/停牌约束
# ---------------------------------------------------------------------------


def test_limit_up_excluded_from_long():
    """涨停股在调仓日被排除出多头组合(top 内但不可买)。"""
    panel = _make_panel()
    c = panel["close"]
    # 股票 0 在 t=5 涨停:close 跳涨 10% 且触板
    c[0, 5] = c[0, 4] * 1.10
    panel["high"][0, 5] = c[0, 5]
    panel["low"][0, 5] = c[0, 4] * 1.095
    f = _top_factor(S=30, T=30, top_idx=(0,))
    fwd = forward_returns(c, 5)
    out = run_backtest(
        f,
        c,
        fwd,
        top_pct=0.2,
        bottom_pct=0.2,
        trade_interval=5,
        mode="long",
        direction="long",
        cost_rate=0.001,
        codes=_codes(),
        ohlc=panel,
        _debug_weights=True,
    )
    # t=5 调仓:股票 0 涨停 → 权重 0;组合其余 5 只等权 1/5
    w_t5 = out["long_w"][:, 5]
    assert w_t5[0] == 0.0
    assert w_t5[w_t5 > 0].sum() == pytest.approx(1.0, abs=1e-9)
    assert np.count_nonzero(w_t5 > 0) == 5
    # 次日(非调仓日)保持同样剔除后的组合
    np.testing.assert_allclose(out["long_w"][:, 6], out["long_w"][:, 5])


def test_limit_down_excluded_from_short():
    """跌停股在调仓日被排除出空头组合。"""
    panel = _make_panel()
    c = panel["close"]
    # 股票 1 在 t=5 跌停:close 跌 10% 且触底
    c[1, 5] = c[1, 4] * 0.90
    panel["low"][1, 5] = c[1, 5]
    panel["high"][1, 5] = c[1, 4] * 0.905
    # 因子:股票 1 恒最低 → 必进 bottom
    f = np.zeros((30, 30))
    for i in range(30):
        f[i, :] = i / 30
    f[1, 6:] = -10.0
    fwd = forward_returns(c, 5)
    out = run_backtest(
        f,
        c,
        fwd,
        top_pct=0.2,
        bottom_pct=0.2,
        trade_interval=5,
        mode="short",
        direction="long",
        cost_rate=0.001,
        codes=_codes(),
        ohlc=panel,
        _debug_weights=True,
    )
    w_t5 = out["short_w"][:, 5]
    assert w_t5[1] == 0.0
    assert np.count_nonzero(w_t5 < 0) == 5


def test_suspended_day_skips_rebalance():
    """整日无成交(volume 全 0)→ 跳过调仓,保持前次持仓。

    因子:股票 0 在 t<10 恒最高(进入 top),t≥10 回落到低分位 → 正常调仓 t=10
    会将其卖出;停牌跳过则保留 t=5 的持仓。
    """
    panel = _make_panel()
    c = panel["close"]
    f = np.zeros((30, 30))
    for i in range(30):
        f[i, :] = i / 30
    f[0, :10] = 10.0
    fwd = forward_returns(c, 5)

    # 对照组:volume 正常,t=10 正常调仓 → 股票 0 被卖出
    ctrl = run_backtest(
        f,
        c,
        fwd,
        trade_interval=5,
        mode="long",
        direction="long",
        codes=_codes(),
        ohlc=panel,
        _debug_weights=True,
    )
    assert ctrl["long_w"][0, 5] > 0.0 and ctrl["long_w"][0, 10] == 0.0

    # 停牌组:t=10 全市场无成交 → 跳过调仓,保留股票 0 持仓
    panel["volume"][:, 10] = 0.0
    out = run_backtest(
        f,
        c,
        fwd,
        trade_interval=5,
        mode="long",
        direction="long",
        codes=_codes(),
        ohlc=panel,
        _debug_weights=True,
    )
    np.testing.assert_allclose(out["long_w"][:, 10], out["long_w"][:, 5])
    assert out["long_w"][0, 10] == pytest.approx(1.0 / 6.0)
    # t=15 恢复 → 重新调仓
    assert out["long_w"][0, 15] == 0.0


def test_suspended_stock_excluded():
    """个股停牌(volume=0)→ 调仓日不可买(未持仓股,排除后剩余等权)。"""
    panel = _make_panel()
    panel["volume"][2, 5] = 0.0  # 股票 2 在首个调仓日 t=5 停牌(t=0 未持仓)
    c = panel["close"]
    f = _top_factor(S=30, T=30, top_idx=(2,))
    fwd = forward_returns(c, 5)
    out = run_backtest(
        f,
        c,
        fwd,
        trade_interval=5,
        mode="long",
        direction="long",
        codes=_codes(),
        ohlc=panel,
        _debug_weights=True,
    )
    assert out["long_w"][2, 5] == 0.0
    # 停牌日组合 = 其余 5 只 top 股等权 1/5;t=10 恢复交易后正常买入
    w_t5 = out["long_w"][:, 5]
    assert np.count_nonzero(w_t5 > 0) == 5
    assert w_t5[w_t5 > 0].sum() == pytest.approx(1.0, abs=1e-9)
    assert out["long_w"][2, 10] > 0.0


def test_no_ohlc_no_constraint_legacy():
    """不传 codes/ohlc → 约束不生效(旧行为),涨停股照常买入。"""
    panel = _make_panel()
    c = panel["close"]
    c[0, 5] = c[0, 4] * 1.10
    f = _top_factor(S=30, T=30, top_idx=(0,))
    fwd = forward_returns(c, 5)
    out = run_backtest(
        f,
        c,
        fwd,
        trade_interval=5,
        mode="long",
        direction="long",
        cost_rate=0.001,
        _debug_weights=True,
    )
    assert out["long_w"][0, 5] > 0.0  # 无约束 → 照常买入


def test_cost_split_matches_legacy_default():
    """拆分成本模型默认值(0/2.5/5)与旧 cost_rate=0.001 数值一致(无约束场景)。"""
    panel = _make_panel()
    c = panel["close"]
    f = _top_factor(S=30, T=30)
    fwd = forward_returns(c, 5)
    legacy = run_backtest(
        f,
        c,
        fwd,
        trade_interval=5,
        mode="long_short",
        direction="long",
        cost_rate=0.001,
    )
    split = run_backtest(
        f,
        c,
        fwd,
        trade_interval=5,
        mode="long_short",
        direction="long",
        cost_rate=0.001,
        slippage_bps=0.0,
        commission_bps=2.5,
        stamp_tax_bps=5.0,
    )
    np.testing.assert_allclose(
        np.asarray(legacy["combo_nav"], dtype=float),
        np.asarray(split["combo_nav"], dtype=float),
        rtol=1e-9,
        atol=1e-9,
    )
    assert split["params"]["slippage_bps"] == 0.0
    assert split["params"]["commission_bps"] == 2.5
    assert split["params"]["stamp_tax_bps"] == 5.0


def test_cost_split_legacy_params_null():
    """新参数全 None → params 输出 None,与旧行为一致。"""
    panel = _make_panel()
    c = panel["close"]
    f = _top_factor(S=30, T=30)
    fwd = forward_returns(c, 5)
    out = run_backtest(f, c, fwd, trade_interval=5, mode="long", direction="long")
    assert out["params"]["slippage_bps"] is None
    assert out["params"]["commission_bps"] is None
    assert out["params"]["stamp_tax_bps"] is None


# ---------------------------------------------------------------------------
# handler 端到端:T-02 参数透传(monkeypatch load_panel,真实回测计算)
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite:替换 runner.SessionLocal(worker 与查询同一临时库)。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    from app.storage.db import Base
    from app.storage.models import ExperimentJob  # noqa: F401  注册 models 建表

    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.tasks.runner as Q

    monkeypatch.setattr(Q, "SessionLocal", Maker)
    with Q._running_lock:
        Q._running.clear()
        Q._cancelled.clear()
    with Q._control_lock:
        Q._paused.clear()
        Q._control.clear()
    yield Maker
    engine.dispose()


def _install_panel_fake(monkeypatch, panel):
    """打桩 runner.load_panel:面板 + codes(与行序对齐),验证约束路径拿到代码。"""
    import app.core.tasks.runner as Q

    monkeypatch.setattr(
        Q,
        "load_panel",
        lambda ds_id, features=None: {
            "panel": panel,
            "dates": [str(i) for i in range(panel["close"].shape[1])],
            "codes": _codes(panel["close"].shape[0]),
        },
    )


def _spawn_job(maker, params, job_type="backtest") -> int:
    from app.storage.models import ExperimentJob

    db = maker()
    job = ExperimentJob(job_type=job_type, params=params, status="pending")
    db.add(job)
    db.commit()
    jid = job.id
    db.close()
    return jid


def test_handler_passes_cost_and_constraint_params(temp_db, monkeypatch):
    """_run_backtest 透传 T-02 参数:显式新参 → 拆分成本 + 涨跌停约束生效。"""
    import app.core.tasks.runner as Q

    panel = _make_panel()
    _install_panel_fake(monkeypatch, panel)
    params = {
        "dataset_id": 1,
        "expression": "rank(close)",
        "horizon": 5,
        "modes": ["long"],
        "direction": "long",
        "top_pct": 0.2,
        "bottom_pct": 0.2,
        "trade_interval": 5,
        "cost_rate": 0.001,
        "slippage_bps": 5.0,
        "commission_bps": 2.5,
        "stamp_tax_bps": 5.0,
    }
    jid = _spawn_job(temp_db, params)
    Q._run_backtest(jid, params)

    from app.storage.models import ExperimentJob

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    bt = row.result["backtest"]
    # 新参透传进结果 params(拆分模型生效)
    assert bt["params"]["slippage_bps"] == 5.0
    assert bt["params"]["commission_bps"] == 2.5
    assert bt["params"]["stamp_tax_bps"] == 5.0
    assert bt["modes"] and bt["modes"][0]["mode"] == "long"


def test_handler_legacy_params_no_new_cost(temp_db, monkeypatch):
    """不传新参 → params 输出 None(旧 cost_rate 路径兼容,结果不崩)。"""
    import app.core.tasks.runner as Q

    panel = _make_panel()
    _install_panel_fake(monkeypatch, panel)
    params = {
        "dataset_id": 1,
        "expression": "rank(close)",
        "horizon": 5,
        "modes": ["long_short", "long", "short"],
    }
    jid = _spawn_job(temp_db, params)
    Q._run_backtest(jid, params)

    from app.storage.models import ExperimentJob

    db = temp_db()
    row = db.get(ExperimentJob, jid)
    db.close()
    assert row.status == "done", row.error
    bt = row.result["backtest"]
    assert bt["params"]["slippage_bps"] is None
    assert bt["params"]["commission_bps"] is None
    assert bt["params"]["stamp_tax_bps"] is None
    assert bt["params"]["cost_rate"] == 0.001
