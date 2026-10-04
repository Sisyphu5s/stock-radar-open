"""审计修复第二轮回归测试（A1/A2/A3/A4/A6/A7/A9）。

覆盖：
- A1: scanner 同 bar 时点重复命中只更新不新增时，score/evidence 必须落库
- A2: _inject_kline_context 注入 code/turnover_rate（limit_up 阈值按前缀、turnover 可用）
- A3: 非日线 period 分支不再静默丢弃 status 筛选（list_events / list_events_page）
- A4/C13: _ak_call 硬超时 + 异常原样回传 + 并发不串行（requests 默认超时注入替代 socket 全局锁）
- A6: scanner 加载既有事件 IN 子句按 400/批循环查询
- A7: _infer_horizon 推断值 clamp 到 [1, 60]
- A9: _extract_const_params 非整数标量参数编译期报错
临时 SQLite，不触碰生产库。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import SignalEvent


@pytest.fixture(scope="module", autouse=True)
def numpy_backend():
    from app.config import settings
    from app.lib.alpha import backend as B

    settings.gp_backend = "numpy"
    B.init_backend()
    yield


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB，替换 scanner.SessionLocal（扫描/信号写入走临时库）。"""
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

    import app.core.scanning as SC

    monkeypatch.setattr(SC, "SessionLocal", Maker)

    from app.api import signals as S

    S._sector_codes_cache.clear()

    yield Maker
    engine.dispose()


# ---------------------------------------------------------------------------
# K 线 / 扫描桩
# ---------------------------------------------------------------------------


def _daily_kline(
    n: int = 60,
    last_date: str = "2026-08-10",
    last_pct: float = 1.0,
    end_close: float = 11.0,
):
    close = np.linspace(10.0, end_close, n)
    return pd.DataFrame(
        {
            "date": pd.bdate_range(end=last_date, periods=n)
            .strftime("%Y-%m-%d")
            .tolist(),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
            "pct_change": np.r_[np.zeros(n - 1), [last_pct]],
        }
    )


def _minute_kline(
    n: int = 60, last_time: str = "2026-08-10 14:35", last_pct: float = 1.0
):
    close = np.linspace(10.0, 11.0, n)
    return pd.DataFrame(
        {
            "date": pd.date_range(end=last_time, periods=n, freq="min")
            .strftime("%Y-%m-%d %H:%M")
            .tolist(),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
            "pct_change": np.r_[np.zeros(n - 1), [last_pct]],
        }
    )


def _patch_scanner(monkeypatch, kline: pd.DataFrame):
    import types
    import app.core.scanning as SC

    # 适配快照合成（scanner._synthesize_daily_bar）：spot 价量对齐 K 线最后真实 bar
    # （排序去重后）→ 触发「无新交易」判定 → 跳过合成，既有 bar 时点断言不随真实
    # 时钟漂移（本文件 K 线固定止于 2026-08-10，真实今天恒晚于它）。
    k = kline.copy()
    k["_dt"] = pd.to_datetime(k["date"], errors="coerce")
    k = k.dropna(subset=["_dt"]).sort_values("_dt").drop_duplicates("_dt", keep="last")
    spot = pd.DataFrame(
        {
            "code": ["600519.SH"],
            "name": ["贵州茅台"],
            "price": [float(k["close"].iloc[-1])],
            "pct_change": [float(k["pct_change"].iloc[-1])],
            "volume": [float(k["volume"].iloc[-1])],
            "amount": [1e8],
            "turnover_rate": [0.5],
        }
    )
    monkeypatch.setattr(SC, "get_spot", lambda: spot.copy())
    monkeypatch.setattr(
        SC,
        "fetch_many",
        lambda codes, period="daily", workers=12, progress_cb=None, start_date=None: {
            c: kline.copy() for c in codes
        },
    )
    monkeypatch.setattr(SC, "get_provider", lambda: types.SimpleNamespace(name="test"))
    return SC


# ---------------------------------------------------------------------------
# A1: 扫描同 bar 时点重复命中 → 只更新不新增，score/evidence 必须落库
# ---------------------------------------------------------------------------


def test_scan_same_bar_update_persists(temp_db, monkeypatch):
    """同一股票+bar 时点再次扫描：事件数保持 1，库中 evidence 已刷新。

    修复前 commit 仅当 hits_rows（新增行）非空时执行——纯更新路径的修改
    随 session close 丢弃，新 session 读到的仍是旧 evidence。
    """
    from app.core.scanning import scan_once
    import app.core.scanning as SC

    _patch_scanner(
        monkeypatch, kline=_daily_kline(last_date="2026-08-10", end_close=11.0)
    )
    assert scan_once()["events"] == 1

    # 同一 bar（2026-08-10）再次扫描，收盘价变化 → evidence["price"] 应刷新
    monkeypatch.setattr(
        SC,
        "fetch_many",
        lambda codes, period="daily", workers=12, progress_cb=None, start_date=None: {
            c: _daily_kline(last_date="2026-08-10", end_close=25.0).copy()
            for c in codes
        },
    )
    # 适配快照合成：第二轮快照价量对齐新缓存最后 bar（end_close=25.0）→ 无新交易
    # → 跳过合成，事件仍停在同一 bar 时点（08-10），只刷新 evidence
    monkeypatch.setattr(
        SC,
        "get_spot",
        lambda: pd.DataFrame(
            {
                "code": ["600519.SH"],
                "name": ["贵州茅台"],
                "price": [25.0],
                "pct_change": [1.0],
                "volume": [100.0],
                "amount": [1e8],
                "turnover_rate": [0.5],
            }
        ),
    )
    assert scan_once()["events"] == 1

    db = temp_db()
    try:
        assert db.query(SignalEvent).count() == 1
        ev = db.query(SignalEvent).one()
        assert ev.triggered_at == datetime(2026, 8, 10, 15, 0)
        assert ev.evidence["price"] == 25.0, "更新路径的 evidence 必须落库"
        assert ev.status == "观察"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# A2: _inject_kline_context 注入 code（_limit_pct 阈值按前缀）与 turnover_rate
# ---------------------------------------------------------------------------


def test_limit_pct_code_prefix_thresholds():
    """_limit_pct 的 code 分支：300/301/302/688 → 19.9，4/8(北交所) → 30，其余 9.9。"""
    from app.lib.signals.engine import _limit_pct

    def df_for(code):
        return pd.DataFrame({"code": [code], "close": [10.0], "pct_change": [0.0]})

    assert _limit_pct(df_for("300750.SZ"), {}) == 19.9
    assert _limit_pct(df_for("301000.SZ"), {}) == 19.9
    assert _limit_pct(df_for("302000.SZ"), {}) == 19.9
    assert _limit_pct(df_for("688256.SH"), {}) == 19.9
    assert _limit_pct(df_for("430047.BJ"), {}) == 30.0
    assert _limit_pct(df_for("830799.BJ"), {}) == 30.0
    assert _limit_pct(df_for("600519.SH"), {}) == 9.9
    assert _limit_pct(df_for("000001.SZ"), {}) == 9.9
    # 参数 pct 优先；缺 code 列 → 默认 9.9
    assert _limit_pct(df_for("600519.SH"), {"pct": 7.0}) == 7.0
    assert _limit_pct(pd.DataFrame({"close": [10.0]}), {}) == 9.9


def test_check_one_injects_code_and_turnover():
    """scanner._check_one（A2 迁移后）：注入 code 列（_limit_pct 阈值按前缀生效）；
    turnover_map 注入 turnover_rate 列（快照常量覆盖 K 线列，语义 = 该股当前换手率）。"""
    from app.core import scanning as SC

    df = _daily_kline(last_pct=10.0)
    # 换手率序列：末根 15%，前 5 根 1% → 换手突增
    df_tr = df.assign(turnover_rate=[1.0] * (len(df) - 1) + [15.0])

    # 主板 600519（阈值 9.9%）：+10% → limit_up 命中，证明 code 列已注入并生效
    res = SC._check_one("600519.SH", {"600519.SH": df}, {}, "daily")
    assert res is not None
    _, _, _, hits, _ = res
    assert "limit_up" in hits, "主板 +10% 应命中涨停（code 前缀阈值 9.9% 生效）"
    assert "turnover_spike" not in hits, "缺 turnover_rate 列时 turnover_spike 不命中"

    # 创业板 300750（阈值 19.9%）：同 +10% 不构成涨停 → 前缀阈值差异可见
    res_cyb = SC._check_one("300750.SZ", {"300750.SZ": df}, {}, "daily")
    assert res_cyb is not None
    _, _, _, hits_cyb, _ = res_cyb
    assert "limit_up" not in hits_cyb, "创业板 +10% < 19.9% 不构成涨停"

    # K 线自带逐日换手率序列：透传 → 换手突增命中（turnover_rate 列未被丢弃）
    res_keep = SC._check_one("600519.SH", {"600519.SH": df_tr}, {}, "daily")
    assert res_keep is not None
    _, _, _, hits_keep, _ = res_keep
    assert "turnover_spike" in hits_keep, "K 线自带换手率序列时 turnover_spike 应命中"

    # turnover_map 注入快照常量：覆盖为全列同值 → 无突增（注入语义 = 当前换手率）
    res_inj = SC._check_one(
        "600519.SH", {"600519.SH": df_tr}, {"600519.SH": 15.0}, "daily"
    )
    assert res_inj is not None
    _, _, _, hits_inj, _ = res_inj
    assert "turnover_spike" not in hits_inj, "常量注入下无换手突增（cur==avg）"


# ---------------------------------------------------------------------------
# A3: 非日线 status 筛选（不再静默丢弃）
# ---------------------------------------------------------------------------


def _seed_status_events(db):
    """3 只股票各 1 条 period='5' 种子事件：600519/000001 观察，300750 已忽略。"""
    base = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
    for i, (code, status) in enumerate(
        [
            ("600519.SH", "观察"),
            ("000001.SZ", "观察"),
            ("300750.SZ", "已忽略"),
        ]
    ):
        db.add(
            SignalEvent(
                stock_code=code,
                signals=["price_up"],
                status=status,
                evidence={},
                triggered_at=base,
                period="5",
            )
        )
    db.commit()


def test_events_page_non_daily_status_filter(temp_db):
    """period=5 + status：返回正确子集（观察 2 股 / 已忽略 1 股）；
    旧端点 list_events 同样生效。"""
    from app.api.signals import list_events, list_events_page

    db = temp_db()
    try:
        _seed_status_events(db)

        # 全量（无 status）：3 股 = 3 行
        r_all = list_events_page(limit=50, offset=0, period="5", db=db)
        assert r_all["total"] == 3

        # status=观察：仅 2 只观察股的 2 行
        r_obs = list_events_page(limit=50, offset=0, period="5", status="观察", db=db)
        assert r_obs["total"] == 2
        assert {it["stock_code"] for it in r_obs["items"]} == {"600519.SH", "000001.SZ"}

        # status=已忽略：仅 1 行（300750）
        r_ign = list_events_page(limit=50, offset=0, period="5", status="已忽略", db=db)
        assert r_ign["total"] == 1
        assert {it["stock_code"] for it in r_ign["items"]} == {"300750.SZ"}

        # 旧端点 /events 同样生效
        r_old = list_events(limit=50, status="已忽略", period="5", db=db)
        assert len(r_old["data"]) == 1
        assert r_old["data"][0]["stock_code"] == "300750.SZ"
        assert r_old["data"][0]["status"] == "已忽略"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# A4/C13: _ak_call 硬超时 + 异常原样回传 + 并发不串行（requests 默认超时注入）
# ---------------------------------------------------------------------------


def test_ak_call_requests_default_timeout_injected(monkeypatch):
    """C13：requests 未显式传 timeout 的调用被注入默认 10s（替代旧 socket 全局
    超时 + 模块级锁）；显式传 timeout 的调用不被覆盖；fn 异常原样回传。"""
    import socket
    import requests as _requests
    from app.core.sources import _ak_call

    seen: list = []

    def fake_send(self, request, *args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise ConnectionError("mock 断网")

    monkeypatch.setattr(_requests.adapters.HTTPAdapter, "send", fake_send)

    with pytest.raises(ConnectionError, match="mock 断网"):
        _ak_call(lambda: _requests.get("http://127.0.0.1:1"), timeout=5)
    assert seen == [10.0], "未显式传 timeout 的请求应注入默认 10s"
    assert socket.getdefaulttimeout() is None, "全局 socket 默认超时不被触碰"

    seen.clear()
    with pytest.raises(ConnectionError, match="mock 断网"):
        _ak_call(lambda: _requests.get("http://127.0.0.1:1", timeout=3), timeout=5)
    assert seen == [3], "显式 timeout 不被注入覆盖"


def test_ak_call_concurrent_not_serialized():
    """C13：并发 _ak_call 不再被进程级锁串行化——两路同时进入 fn、各耗时 0.4s，
    总耗时应接近单路（旧实现串行 = 0.8s）。"""
    import threading
    import time
    from app.core.sources import _ak_call

    barrier = threading.Barrier(2)

    def slow():
        barrier.wait(timeout=2)  # 两路同时进入 fn
        time.sleep(0.4)
        return 1

    results: list = []
    start = time.monotonic()

    def run():
        results.append(_ak_call(slow, timeout=5))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    elapsed = time.monotonic() - start
    assert results == [1, 1]
    assert elapsed < 0.7, f"并发调用被串行化：总耗时 {elapsed:.2f}s（期望 < 0.7s）"


def test_ak_call_timeout_still_raises():
    """硬超时兜底不回归：fn 超时仍抛 TimeoutError。"""
    import time
    from app.core.sources import _ak_call

    with pytest.raises(TimeoutError, match="超时"):
        _ak_call(lambda: time.sleep(2), timeout=0.2)


# ---------------------------------------------------------------------------
# A6: scanner 既有事件加载 IN 子句分批（400/批）
# ---------------------------------------------------------------------------


def test_load_existing_events_batches_in_clause():
    from sqlalchemy.dialects import sqlite
    import app.core.scanning as SC

    sizes: list[int] = []

    class FakeQuery:
        def filter(self, *conds):
            for c in conds:
                sql = str(
                    c.compile(
                        dialect=sqlite.dialect(),
                        compile_kwargs={"render_postcompile": True},
                    )
                )
                sizes.append(sql.count("?"))
            return self

        def all(self):
            return []

    class FakeDB:
        def query(self, *args, **kwargs):
            return FakeQuery()

    # 950 个 code → 400/400/150 三批
    SC._load_existing_events(FakeDB(), [str(i) for i in range(950)])
    assert sizes == [400, 400, 150], f"IN 子句必须分批，实际 {sizes}"

    # 小集合不回归：单批
    SC._load_existing_events(FakeDB(), ["600519.SH", "000001.SZ"])
    assert sizes[-1] == 2


# ---------------------------------------------------------------------------
# A7: _infer_horizon 推断值 clamp 到 [1, 60]
# ---------------------------------------------------------------------------


def test_infer_horizon_clamps_and_infers():
    from app.lib.alpha.evaluate import _infer_horizon

    rng = np.random.default_rng(0)
    # 常规：尾部 5 列全 NaN → 5
    a = rng.standard_normal((10, 100))
    a[:, -5:] = np.nan
    assert _infer_horizon(a) == 5
    # 全 NaN 面板 → 推断值=整列数(100) → clamp 60（修复前返回 100）
    assert _infer_horizon(np.full((10, 100), np.nan)) == 60
    # 尾部 80 列全 NaN → clamp 60
    b = rng.standard_normal((10, 200))
    b[:, -80:] = np.nan
    assert _infer_horizon(b) == 60
    # 无全 NaN 尾列 → h=0 → 下界 1
    assert _infer_horizon(rng.standard_normal((10, 100))) == 1
    # 退化输入：1D / 空列 → 默认 5
    assert _infer_horizon(rng.standard_normal(100)) == 5
    assert _infer_horizon(np.zeros((10, 0))) == 5


# ---------------------------------------------------------------------------
# A9: _extract_const_params 非整数标量参数编译期报错
# ---------------------------------------------------------------------------


def test_extract_const_params_rejects_non_integer():
    from app.lib.alpha.operators import compile_rpn, evaluate_rpn

    # 整数参数正常
    rpn = compile_rpn("ts_mean(close,5)")
    out = evaluate_rpn(rpn, {"close": np.ones((2, 10), dtype=np.float32)})
    assert out.shape == (2, 10)
    assert compile_rpn("delta(close,1)")
    assert compile_rpn("signed_power(close,2)")

    # 非整数 → ValueError（带函数上下文，非静默截断）
    with pytest.raises(ValueError, match=r"ts_mean.*整数"):
        compile_rpn("ts_mean(close,5.5)")
    with pytest.raises(ValueError, match=r"delta.*整数"):
        compile_rpn("delta(close,1.5)")
