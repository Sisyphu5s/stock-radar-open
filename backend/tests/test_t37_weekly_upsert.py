"""T-37(P0-21)周/月 K 线增量 upsert 历史截断回归测试。

问题：upsert_many 对 weekly/monthly 曾「先删该 code+period 全部旧行再插」，
而增量拉取（_fetch_start 返回缓存最后一根次日）只带回 1~2 根新周期 bar →
缓存历史被截断为 1~2 根，后续重采样/回测缺历史。

修复：删除范围收敛为「与新 df 覆盖周期重叠的旧行」——
  删除下界 = 新 df 最小日期所在周期起点（weekly→所在自然周周一 / monthly→当月 1 日），
  下界之后的旧行会被新聚合 bar 替换（防双 bar），更早历史保留。

用例：
1. monthly：先写 5/6/7/8 月各一根，增量写 08-12 单根 → 5/6/7 月保留、8 月仅新 bar。
2. weekly：先写 07-31/08-07 两根，增量写 08-14（下一周）单根 → 历史保留、无重复。
3. 原防双 bar 语义保留：同周期旧标签（08-07/08-10）被新标签（08-12）替换清空。

基建（仿 test_kcache_intraday.py）：临时 SQLite（monkeypatch kcache.SessionLocal）
+ 冻结 kcache._cn_now 与 session.now_cn（_to_rows 未来标签剔除与新鲜度口径）。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage import klines as kcache
from app.lib import session as sess

# 测试基准：2026-08-12 周三（2026-08-07 周五 / 08-10 周一 / 08-14 周五）


class _FrozenClock:
    """固定 Asia/Shanghai 墙钟（naive）；_FROZEN 可在用例内改。"""

    _FROZEN = datetime(2026, 8, 12, 16, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


@pytest.fixture()
def frozen_cn(monkeypatch):
    monkeypatch.setattr(kcache, "_cn_now", lambda: _FrozenClock._FROZEN)
    monkeypatch.setattr(sess, "now_cn", lambda: _FrozenClock._FROZEN)
    return _FrozenClock


@pytest.fixture()
def kline_db(tmp_path, monkeypatch):
    """临时 SQLite K 线库：替换 kcache.SessionLocal（不触碰生产库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'kcache.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(kcache, "SessionLocal", Maker)
    try:
        yield Maker
    finally:
        engine.dispose()


def _one_bar(d: str) -> pd.DataFrame:
    """单根周期 bar（周/月K重采样输出形态，date 为 'YYYY-MM-DD' 字符串）。"""
    return pd.DataFrame(
        {
            "date": [d],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [100.0],
            "amount": [1000.0],
        }
    )


def _bars(*dates: str) -> pd.DataFrame:
    """多根周期 bar（date 升序，模拟重采样后的完整历史输出）。"""
    return pd.concat([_one_bar(d) for d in dates], ignore_index=True)


def _dates(period: str) -> list[str]:
    return kcache.load_cached(["600000.SH"], period)["600000.SH"]["date"].tolist()


# ---------------------------------------------------------------------------
# 1) monthly：增量写单根新 bar → 更早月份历史保留、当月无双 bar
# ---------------------------------------------------------------------------


def test_monthly_incremental_keeps_history(kline_db, frozen_cn):
    """先写 5/6/7/8 月各一根月 bar（缓存最后 = 08-07），再增量写 08-12 单根
    （增量拉取形态：只带回 1 根新 bar）→ 5/6/7 月保留、8 月仅剩新 bar（无双 bar）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)
    kcache.upsert_many(
        [
            (
                "600000.SH",
                "monthly",
                _bars("2026-05-29", "2026-06-30", "2026-07-31", "2026-08-07"),
            )
        ],
        "test",
    )
    assert _dates("monthly") == ["2026-05-29", "2026-06-30", "2026-07-31", "2026-08-07"]
    # 增量拉取（_fetch_start 返回 08-08 起）只带回 8 月新聚合 bar 08-12
    kcache.upsert_many([("600000.SH", "monthly", _one_bar("2026-08-12"))], "test")
    got = _dates("monthly")
    # 5/6/7 月历史未截断；8 月只有新 bar，旧 08-07 聚合标签被替换
    assert got == ["2026-05-29", "2026-06-30", "2026-07-31", "2026-08-12"]


# ---------------------------------------------------------------------------
# 2) weekly：增量写下一周单根 bar → 更早周历史保留、无重复
# ---------------------------------------------------------------------------


def test_weekly_incremental_keeps_history(kline_db, frozen_cn):
    """先写 07-31/08-07 两根周 bar（分属不同 W-FRI 组），再增量写 08-14
    （下一周组新 bar）→ 07-31/08-07 保留、新周 bar 追加、无重复。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 14, 16, 0)
    kcache.upsert_many(
        [("600000.SH", "weekly", _bars("2026-07-31", "2026-08-07"))], "test"
    )
    assert _dates("weekly") == ["2026-07-31", "2026-08-07"]
    # 增量拉取：缓存最后 08-07 → 从 08-08 拉，只带回周组(08-10..08-14)的新 bar 08-14
    kcache.upsert_many([("600000.SH", "weekly", _one_bar("2026-08-14"))], "test")
    got = _dates("weekly")
    # 早两周历史未截断；新周 bar 追加，无重复
    assert got == ["2026-07-31", "2026-08-07", "2026-08-14"]


# ---------------------------------------------------------------------------
# 3) 原防双 bar 语义保留：同周期旧标签被新标签替换清空
# ---------------------------------------------------------------------------


def test_monthly_label_evolution_clears_old_bar(kline_db, frozen_cn):
    """同月两次写入（08-07 → 08-12 标签演进）→ 仅剩新聚合 bar，旧行被清（防双 bar）。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)
    kcache.upsert_many([("600000.SH", "monthly", _one_bar("2026-08-07"))], "test")
    kcache.upsert_many([("600000.SH", "monthly", _one_bar("2026-08-12"))], "test")
    assert _dates("monthly") == ["2026-08-12"]


def test_weekly_label_evolution_clears_old_bar(kline_db, frozen_cn):
    """同一 W-FRI 周组（08-10 周一与 08-12 周三同组）两次写入 → 仅剩新聚合 bar。"""
    _FrozenClock._FROZEN = datetime(2026, 8, 12, 16, 0)
    kcache.upsert_many([("600000.SH", "weekly", _one_bar("2026-08-10"))], "test")
    kcache.upsert_many([("600000.SH", "weekly", _one_bar("2026-08-12"))], "test")
    assert _dates("weekly") == ["2026-08-12"]
