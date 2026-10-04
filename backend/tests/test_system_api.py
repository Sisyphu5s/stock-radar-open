"""系统 API 聚焦测试：/system/cache-stats 结构 + /system/exit 幂等（双调不炸）。

/sytem/exit 会真实执行「停调度 → dispose 生产库引擎 → os._exit(0)」，
测试通过 monkeypatch 屏蔽全部副作用（stop_scanner / engine / os._exit / Timer），
并让关闭线程同步执行，保证断言确定性。不触碰生产库。
"""

from __future__ import annotations

import threading as _th
import types

import pytest

import app.api.system as SYS
from app.api.system import cache_stats, deploy_status, exit_system

# 预导入(T-81 修复既有隐患):neutralized fixture 会 patch 全局 threading.Thread,
# 若 storage.cache/scanning 在 patch 后才首次 import,TTLCache 的磁盘后台线程
# (while True)同步 start → 单文件跑 pytest 死锁。必须先于任何测试完成真实导入。
import app.core.scanning  # noqa: F401  (exit 测试 stop_scanner 的 monkeypatch 目标)
import app.storage.cache  # noqa: F401  (exit 测试 flush 的懒加载目标)


@pytest.fixture()
def neutralized(monkeypatch):
    """屏蔽 /system/exit 的真实副作用；返回被替换的 SYS 模块。"""
    monkeypatch.setattr(SYS, "_exiting", _th.Event())
    monkeypatch.setattr("app.core.scanning.stop_scanner", lambda: None)
    monkeypatch.setattr(
        "app.storage.db.engine", types.SimpleNamespace(dispose=lambda: None)
    )

    class _FakeTimer:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    class _SyncThread:
        def __init__(
            self,
            group=None,
            target=None,
            name=None,
            args=(),
            kwargs=None,
            *,
            daemon=None,
        ):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(SYS.threading, "Timer", _FakeTimer)
    monkeypatch.setattr(SYS.threading, "Thread", _SyncThread)
    monkeypatch.setattr("os._exit", lambda code: None)
    return SYS


# ---------------------------------------------------------------------------
# /system/cache-stats
# ---------------------------------------------------------------------------


def test_cache_stats_structure(neutralized):
    from app.storage import cache as C

    out = cache_stats()
    assert "data" in out
    data = out["data"]
    # 与 _CACHE_INSTANCES 当前集合对齐：每个实例都在统计内且结构完整
    for name, _inst in C._CACHE_INSTANCES:
        assert name in data, f"缺少缓存实例 {name}"
        stats = data[name]
        assert set(stats) == {"size", "hits", "misses", "hit_rate"}, stats
        assert stats["size"] >= 0 and stats["hits"] >= 0 and stats["misses"] >= 0
        assert 0.0 <= stats["hit_rate"] <= 1.0
    total = data["total"]
    assert set(total) == {"size", "hits", "misses", "hit_rate"}
    # 合计 = 各实例之和
    names = [n for n in data if n != "total"]
    assert total["size"] == sum(data[n]["size"] for n in names)
    assert total["hits"] == sum(data[n]["hits"] for n in names)


def test_cache_stats_records_hits(neutralized):
    from app.storage.cache import panel_cache

    panel_cache.get("no-such-key")  # 计入 miss
    stats = cache_stats()["data"]["panel"]
    assert stats["misses"] >= 1


# ---------------------------------------------------------------------------
# /system/exit 幂等（双调不炸）
# ---------------------------------------------------------------------------


def test_exit_first_call(neutralized):
    out = exit_system(reason="pytest")
    assert out == {"ok": True, "status": "exiting", "reason": "pytest"}


def test_exit_idempotent_second_call(neutralized):
    first = exit_system(reason="manual")
    second = exit_system(reason="again")
    assert first["ok"] is True and first["status"] == "exiting"
    # 已处于退出中：第二次调用短路返回，不重复执行关闭流程（T-112 后 reason 恒输出）
    assert second == {"ok": True, "status": "exiting", "reason": "again"}


# ---------------------------------------------------------------------------
# T-81 /system/deploy-status + /health 版本字段（read_deploy_state 聚合）
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_deploy_root(tmp_path, monkeypatch):
    """把 BASE_DIR 指向临时目录,构造部署根(BASE_DIR.parent)与 .run/frontend/dist 结构。"""
    import app.config as CFG

    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    monkeypatch.setattr(CFG, "BASE_DIR", backend_dir)
    monkeypatch.setattr(CFG.settings, "frontend_dist_dir", "")
    return tmp_path


def test_read_deploy_state_all_missing(fake_deploy_root):
    """部署文件全缺（dev 直跑）→ 全 None,不抛错。"""
    st = SYS.read_deploy_state()
    assert st["deployed_commit"] is None
    assert st["dist_commit"] is None
    assert st["last_build_ok"] is None
    assert deploy_status()["frontend_synced"] is None


def test_deploy_status_synced_true(fake_deploy_root):
    run = fake_deploy_root / ".run"
    run.mkdir()
    (run / "deployed-commit").write_text("abc123\n", encoding="utf-8")
    dist = fake_deploy_root / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "build-manifest.json").write_text(
        '{"commit": "abc123", "builtAt": "2026-08-15T12:00:00Z"}', encoding="utf-8"
    )
    st = deploy_status()
    assert st["deployed_commit"] == "abc123"
    assert st["dist_commit"] == "abc123"
    assert st["dist_built_at"] == "2026-08-15T12:00:00Z"
    assert st["frontend_synced"] is True


def test_deploy_status_synced_false(fake_deploy_root):
    run = fake_deploy_root / ".run"
    run.mkdir()
    (run / "deployed-commit").write_text("new456\n", encoding="utf-8")
    dist = fake_deploy_root / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "build-manifest.json").write_text(
        '{"commit": "old123", "builtAt": "2026-08-14T02:41:00Z"}', encoding="utf-8"
    )
    st = deploy_status()
    assert st["frontend_synced"] is False
    assert st["dist_commit"] == "old123"


def test_deploy_status_last_build_failed(fake_deploy_root):
    run = fake_deploy_root / ".run"
    run.mkdir()
    (run / "last-build.json").write_text(
        '{"ok": false, "at": "2026-08-15T10:51:31Z", "error_tail": "TS2307 Cannot find module"}',
        encoding="utf-8",
    )
    st = deploy_status()
    assert st["last_build_ok"] is False
    assert "TS2307" in st["last_build_error_tail"]


def test_deploy_status_corrupt_manifest_ignored(fake_deploy_root):
    """manifest 损坏 → 字段 None,不抛错(管道诊断不能因坏文件 500)。"""
    dist = fake_deploy_root / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "build-manifest.json").write_text("{broken", encoding="utf-8")
    st = deploy_status()
    assert st["dist_commit"] is None
    assert st["frontend_synced"] is None
