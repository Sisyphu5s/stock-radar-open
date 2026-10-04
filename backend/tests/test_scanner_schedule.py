"""调度入口回归：注册表驱动多周期 + 非交易时段降频路径不得抛 UnboundLocalError。

覆盖（P1-33）：① start_scanner 按调度注册表（Settings.scan_schedule / _SCHEDULE）
逐周期注册 interval job，各自带间隔秒与周期参数；② 非交易时段全局降频（约 29min，
避开快照 TTL 1800s 边界相撞）——首次触发执行、间隙内再次触发被拦截；
③ 交易时段每次触发都执行并刷新 _last_scheduled。
"""

from __future__ import annotations

import app.core.scanning as scanner_mod
from app.lib import session as session_mod


class FakeScheduler:
    """捕获 add_job 的 job 函数与参数，不真正启动调度器。"""

    instances: list["FakeScheduler"] = []

    def __init__(self, **kwargs):
        self.jobs = []  # [(fn, args, kwargs), ...]
        FakeScheduler.instances.append(self)

    def add_job(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))

    def start(self):
        pass

    def shutdown(self, wait=False, **kwargs):
        pass


def _install(monkeypatch, trading: bool) -> tuple[list[str], list]:
    calls: list[str] = []
    FakeScheduler.instances = []
    monkeypatch.setattr(scanner_mod, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(
        scanner_mod, "scan_once", lambda period: calls.append(f"scan:{period}")
    )
    monkeypatch.setattr(session_mod, "in_trading_session", lambda: trading)
    scanner_mod._scheduler = None
    scanner_mod._last_scheduled = None
    scanner_mod.start_scanner()
    return calls, FakeScheduler.instances[0].jobs


def test_start_scanner_registers_multi_period_jobs(monkeypatch):
    """调度注册表驱动：每个非零间隔周期注册一个 interval job，参数按表取值。"""
    calls, jobs = _install(monkeypatch, trading=True)
    schedule = scanner_mod._SCHEDULE
    active = {p: i for p, i in schedule.items() if i > 0}
    # 默认表 8 个周期全部非零
    assert len(jobs) == len(active) == 8
    periods = set()
    for fn, args, kwargs in jobs:
        assert kwargs["id"].startswith("market_scan:")
        assert kwargs["max_instances"] == 1
        assert kwargs["coalesce"] is True
        assert kwargs["next_run_time"] is not None, "首轮应稍后执行（缓存预热）"
        p = kwargs["args"][0]
        periods.add(p)
        assert kwargs["seconds"] == active[p], f"{p} 间隔应取自调度注册表"
    assert periods == set(active), "注册表每个周期都应有对应 job"


def test_schedule_zero_interval_skipped(monkeypatch):
    """间隔 0 = 不调度：该周期不注册 job。"""
    saved = scanner_mod._SCHEDULE
    try:
        scanner_mod._SCHEDULE = {"daily": 300, "5": 0, "weekly": 86400}
        calls, jobs = _install(monkeypatch, trading=True)
        assert len(jobs) == 2
        args_all = [j[2]["args"][0] for j in jobs]
        assert "5" not in args_all and "daily" in args_all and "weekly" in args_all
        assert all(j[2]["seconds"] > 0 for j in jobs)
    finally:
        scanner_mod._SCHEDULE = saved


def test_scheduled_scan_non_trading_downgrades(monkeypatch):
    """非交易时段：首触发执行扫描，29min 内再次触发被降频拦截（避开 spot TTL 边界）。
    修复前该路径在首次赋值 _last_scheduled 时抛 UnboundLocalError。"""
    calls, jobs = _install(monkeypatch, trading=False)
    fn, _, kwargs = jobs[0]
    fn(kwargs["args"][0])  # 首触发：执行
    assert calls == [f"scan:{kwargs['args'][0]}"]
    fn(kwargs["args"][0])  # 29min 内：降频跳过
    assert len(calls) == 1, "非交易时段 29min 内不应重复扫描"
    # 任意周期 job 共享降频：daily 触发后分钟 job 同样被拦截
    for fn2, _, kw2 in jobs[1:]:
        fn2(kw2["args"][0])
    assert len(calls) == 1, "非交易时段降频对所有周期生效"


def test_scheduled_scan_trading_hours_executes(monkeypatch):
    """交易时段：每次触发都执行扫描并刷新 _last_scheduled。"""
    calls, jobs = _install(monkeypatch, trading=True)
    fn, _, kwargs = jobs[0]
    fn(kwargs["args"][0])
    fn(kwargs["args"][0])
    assert calls == [f"scan:{kwargs['args'][0]}", f"scan:{kwargs['args'][0]}"]


def test_scheduled_scan_period_param_forwarded(monkeypatch):
    """job 触发时周期参数透传 scan_once：分钟 job 不误扫 daily。"""
    calls, jobs = _install(monkeypatch, trading=True)
    period_map = {j[2]["args"][0]: j[0] for j in jobs}
    period_map["5"]("5")
    period_map["weekly"]("weekly")
    assert calls == ["scan:5", "scan:weekly"], "周期参数应透传给 scan_once"
