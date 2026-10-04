"""T-95 providers 域三修测试：P1-61 热路径缓存 / P2-48 共享 Session 并发 / P2-49 腾讯快照落盘。

覆盖：
- P1-61：Settings.__getattribute__ 钩子读运行时覆盖键（rt:*）时，1 秒 TTL 缓存
  保证热路径不重复开 SQLite 会话；写路径（set/delete_app_params）失效后立即可见。
- P2-48：模块级 SESSION 改为 thread-local 代理——每线程私有 Session 实例，
  并发调用不被串行化、无共享竞态；同线程内复用同一实例。
- P2-49：TencentProvider.get_spot 成功后调用 save_snapshot_disk("tencent")，
  全源断网离线兜底时 tencent.pkl 存在且可加载。

全部走临时 SQLite / mock，不触碰网络与生产库。
"""

from __future__ import annotations

import threading
import time

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.storage.appparams as AP
import app.storage.db as DB
from app.config import settings
from app.core.runtime_config import env_default
from app.storage.db import Base
from app.storage.models import AppParam, Stock  # noqa: F401 (注册表)


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 DB：替换 app.storage.db.SessionLocal + 建全部表。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 't95.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(DB, "SessionLocal", Maker)
    return Maker


# ---------------------------------------------------------------------------
# P1-61：热路径会话创建次数
# ---------------------------------------------------------------------------


def test_settings_hot_path_single_db_session(temp_db, monkeypatch):
    """读 settings 运行时覆盖键：1 秒 TTL 内只开 1 次 SQLite 会话。

    修复前每次属性读取都 get_app_param → SessionLocal() 新建会话查询；
    修复后缓存命中直接返回，热路径（3 源竞速 × 每轮快照）零重复 I/O。
    """
    calls = {"n": 0}
    real = DB.SessionLocal

    def counting_maker():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(DB, "SessionLocal", counting_maker)
    monkeypatch.setattr(AP._ParamCache, "_data", {})

    # 首次读 → 1 次会话
    settings.quote_cache_ttl
    assert calls["n"] == 1
    # 热路径重复读（20 次）→ 全部命中缓存，不新增会话
    for _ in range(20):
        settings.quote_cache_ttl
    assert calls["n"] == 1


def test_runtime_override_still_takes_effect(temp_db, monkeypatch):
    """配置变更仍生效：写路径主动失效缓存，set/delete 后立即读到新值。"""
    calls = {"n": 0}
    real = DB.SessionLocal

    def counting_maker():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(DB, "SessionLocal", counting_maker)
    monkeypatch.setattr(AP._ParamCache, "_data", {})

    base = env_default("quote_cache_ttl")
    assert settings.quote_cache_ttl == base
    assert calls["n"] == 1

    # 写入覆盖 → 立即可见（写路径失效缓存，再次读建 1 次新会话）
    from app.storage.appparams import delete_app_params, set_app_params

    set_app_params({"rt:quote_cache_ttl": "123"})
    assert calls["n"] == 2  # 写入本身 1 次会话
    assert settings.quote_cache_ttl == 123
    assert calls["n"] == 3  # 缓存已失效 → 重新查询 1 次
    # 命中新缓存 → 不再新增会话
    settings.quote_cache_ttl
    assert calls["n"] == 3

    # 删除覆盖 → 回落 env 基线
    delete_app_params(["rt:quote_cache_ttl"])
    assert calls["n"] == 4
    assert settings.quote_cache_ttl == base
    assert calls["n"] == 5


def test_cache_expiry_re_queries(temp_db, monkeypatch):
    """TTL 过期后重新查询（不缓存死）：1 秒后读 → 再次开会话查 DB。"""
    calls = {"n": 0}
    real = DB.SessionLocal

    def counting_maker():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(DB, "SessionLocal", counting_maker)
    monkeypatch.setattr(AP._ParamCache, "_data", {})
    monkeypatch.setattr(AP._ParamCache, "_TTL_S", 0.05)

    settings.quote_cache_ttl
    assert calls["n"] == 1
    settings.quote_cache_ttl  # 未过期 → 命中
    assert calls["n"] == 1
    time.sleep(0.1)  # 过期
    settings.quote_cache_ttl  # 重新查
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# P2-48：thread-local Session 并发安全
# ---------------------------------------------------------------------------


def test_session_thread_local_isolation():
    """每线程私有 Session 实例：不同线程 id 不同，同线程内复用同一实例。"""
    from app.storage.providers import base as PB

    N = 8
    barrier = threading.Barrier(N)
    ids: set[int] = set()
    lock = threading.Lock()

    def worker():
        s = PB.SESSION._session()
        with lock:
            ids.add(id(s))
        barrier.wait()

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == N  # 每线程独立实例，无共享竞态

    # 同线程内复用同一实例（连接池有效复用）
    a = PB.SESSION._session()
    b = PB.SESSION._session()
    assert a is b


def test_session_concurrent_get_not_serialized(monkeypatch):
    """并发调用不被全局锁串行化：N 线程可同时进入 get（max 并发 = N）。"""
    from app.storage.providers import base as PB

    N = 8
    cnt = {"cur": 0, "max": 0}
    cnt_lock = threading.Lock()

    class _Resp:
        def __init__(self):
            self.text = "{}"
            self.content = b"{}"
            self.status_code = 200

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, timeout=None):
        with cnt_lock:
            cnt["cur"] += 1
            cnt["max"] = max(cnt["max"], cnt["cur"])
        time.sleep(0.05)
        with cnt_lock:
            cnt["cur"] -= 1
        return _Resp()

    # 测试经 monkeypatch 直接替换代理实例方法（既有测试同款 mock 方式）
    monkeypatch.setattr(PB.SESSION, "get", fake_get)
    barrier = threading.Barrier(N)

    def worker():
        barrier.wait()
        PB.SESSION.get("https://example.test")

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cnt["max"] == N  # 全部并发进入，未被串行化


def test_session_headers_preserved():
    """thread-local Session 仍带默认 headers（UA + 新浪 Referer）。"""
    from app.storage.providers import base as PB

    s = PB.SESSION._session()
    assert "User-Agent" in s.headers
    assert "finance.sina.com.cn" in s.headers.get("Referer", "")


# ---------------------------------------------------------------------------
# P2-49：腾讯快照落盘
# ---------------------------------------------------------------------------


def _tencent_line(symbol: str, code: str, name: str, price: str) -> str:
    """构造腾讯 qt.gtimg.cn 批量报价行（≥51 字段，关键索引对齐 tencent.py 解析）。"""
    f = [""] * 51
    f[1] = name
    f[2] = code
    f[3] = price
    f[32] = "2.50"  # pct_change
    f[36] = "30000"  # volume（手）
    f[37] = "4500000"  # amount（万元）
    f[38] = "0.50"  # turnover_rate
    f[39] = "30.0"  # pe
    f[44] = "18800.0"  # float_cap（亿）
    f[45] = "18800.0"  # market_cap（亿）
    f[46] = "9.0"  # pb
    return f'v_{symbol}="{"~".join(f)}";'


class _Resp:
    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("gbk", errors="ignore")
        self.status_code = 200

    def raise_for_status(self):
        pass


def test_tencent_snapshot_saved_to_disk(tmp_path, monkeypatch):
    """腾讯 get_spot 成功后落盘 tencent.pkl，离线兜底可加载。"""
    from app.storage import snapshots as SNAP
    from app.storage.providers import base as PB
    from app.storage.providers.tencent import TencentProvider

    monkeypatch.setattr(SNAP, "_SNAP_DIR", tmp_path)

    # 临时 DB：插入 2 只股票（腾讯源代码列表来源）
    engine = create_engine(
        f"sqlite:///{tmp_path / 'stocks.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Maker()
    db.add_all(
        [
            Stock(code="600519.SH", name="贵州茅台"),
            Stock(code="000001.SZ", name="平安银行"),
        ]
    )
    db.commit()
    db.close()
    monkeypatch.setattr(DB, "SessionLocal", Maker)

    # mock 网络层：腾讯批量报价返回两行
    def fake_get(url, timeout=None):
        assert "qt.gtimg.cn" in url
        return _Resp(
            _tencent_line("sh600519", "600519", "贵州茅台", "1500.00")
            + _tencent_line("sz000001", "000001", "平安银行", "11.00")
        )

    monkeypatch.setattr(PB.SESSION, "get", fake_get)

    p = TencentProvider()
    out = p.get_spot()
    assert out is not None and len(out) == 2
    assert set(out["code"]) == {"600519.SH", "000001.SZ"}
    assert out["source"].unique().tolist() == ["tencent"]

    # 落盘断言：文件存在 + 可加载且内容正确
    path = tmp_path / "tencent.pkl"
    assert path.exists(), "腾讯快照未落盘（P2-49 未修复）"
    loaded = SNAP.load_snapshot_disk("tencent")
    assert loaded is not None and len(loaded) == 2
    assert loaded.iloc[0]["code"] == "600519.SH"
