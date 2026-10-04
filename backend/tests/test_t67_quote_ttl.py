"""T-67 顺带项:快照 TTL 同口径修复测试。

- TencentProvider.get_spot 原用裸 settings.quote_cache_ttl(60s),与 Sina/Akshare
  的 session_spot_ttl(盘中 90s / 非交易时段基础值 ×30)不同步 → 对齐为 session_spot_ttl。
- quote.get_spot 的 _spot_industry_cache 原固定 quote_cache_ttl(60s),与 provider
  快照 TTL 不同步 → 改用 session_spot_ttl 同口径。

基建:patch session.session_spot_ttl 固定返回值,构造超过/未超过 TTL 的时间差断言
缓存命中与重拉;不触碰生产库/网络。
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pandas as pd
import pytest


class _FakeDB:
    """TencentProvider.get_spot 拉取路径的最小 DB 桩(无股票列表 → 快速抛错,
    证明 TTL 超时确实走了重拉路径而非命中缓存)。"""

    def query(self, model):
        return self

    def limit(self, n):
        return self

    def all(self):
        return []

    def close(self):
        pass


def test_tencent_spot_ttl_uses_session_ttl(monkeypatch):
    """TencentProvider.get_spot 走 session_spot_ttl 同口径:TTL 内命中缓存,
    超过 TTL 触发重拉(不再固定裸 quote_cache_ttl 60s)。"""
    from app.core import sources as Q
    from app.lib import session as sess

    df = pd.DataFrame({"code": ["600519.SH"], "price": [1.0]})
    prov = Q.TencentProvider()
    prov._spot_cache = df
    monkeypatch.setattr(sess, "session_spot_ttl", lambda base, now=None: 90)
    monkeypatch.setattr("app.storage.db.SessionLocal", lambda: _FakeDB())
    # 89s 前写入 → TTL(90s) 内 → 命中缓存(不触发重拉)
    prov._spot_ts = time.time() - 89
    assert prov.get_spot() is df
    # 91s 前 → 超过 TTL → 重拉(DB 无股票列表 → 抛错,证明未走缓存)
    prov._spot_ts = time.time() - 91
    with pytest.raises(RuntimeError, match="股票代码列表"):
        prov.get_spot()


def test_get_spot_industry_ttl_syncs_session(monkeypatch):
    """_spot_industry_cache TTL 与快照 TTL 同口径(session_spot_ttl):
    TTL 内复用已映射结果,超过 TTL 重新 apply(修复前固定 quote_cache_ttl 60s)。"""
    from app.core import sources as Q
    from app.lib import session as sess
    from app.core import sources as srcmod

    df_spot = pd.DataFrame(
        {
            "code": ["600519.SH"],
            "name": ["贵州茅台"],
            "price": [1.0],
            "industry": [""],
        }
    )
    apply_calls = {"n": 0}

    def fake_apply(df):
        apply_calls["n"] += 1
        out = df.copy()
        out["industry"] = ["白酒"]
        return out

    with (
        patch.object(Q, "_instance") as mi,
        patch.object(srcmod, "spot_candidates", return_value=["akshare"]),
        patch.object(srcmod, "active_key", return_value="akshare"),
        patch.object(srcmod, "record_spot_success"),
        patch.object(srcmod, "set_active"),
        patch.object(Q.settings, "quote_cache_ttl", 60),
        patch.object(Q, "_spot_industry_cache", None),
        patch.object(Q, "_spot_industry_ts", 0.0),
        patch("app.storage.industry.apply_industry", side_effect=fake_apply),
    ):
        monkeypatch.setattr(sess, "session_spot_ttl", lambda base, now=None: 90)
        mi.return_value.get_spot.return_value = df_spot
        Q.get_spot()  # 首次 → apply
        Q._spot_industry_ts = time.time() - 89  # TTL(90s) 内 → 复用,不重新 apply
        Q.get_spot()
        Q._spot_industry_ts = time.time() - 91  # 超过 TTL → 重新 apply
        Q.get_spot()
    assert apply_calls["n"] == 2
