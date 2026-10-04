"""时间契约测试：API 输出 naive ISO 一律为 Asia/Shanghai 市场时区。

存储保持 UTC naive（storage.models.utcnow 不变），仅序列化输出前经 lib.timex.to_market_naive
统一转为 Asia/Shanghai naive（docs/archive/DESIGN.md §1.7）。信号路径时间（triggered_at/as_of）不受影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.storage.models import ExperimentJob, TodoItem, utcnow
from app.lib.timex import to_market_naive


def test_to_market_naive_fixed_utc_moment():
    """固定 UTC 时刻 → 上海 naive（UTC+8，中国无夏令时）。"""
    assert to_market_naive(datetime(2026, 8, 12, 3, 0)) == datetime(2026, 8, 12, 11, 0)
    assert to_market_naive(datetime(2026, 8, 12, 15, 0)) == datetime(2026, 8, 12, 23, 0)
    # 跨日：上海 0 点 = UTC 前一日 16:00
    assert to_market_naive(datetime(2026, 8, 12, 16, 30)) == datetime(
        2026, 8, 13, 0, 30
    )


def test_to_market_naive_always_plus_eight_hours():
    """任意 UTC naive 输入转换前后恒差 +8h（全年，无夏令时漂移）。"""
    for base in (datetime(2026, 1, 1, 0, 0), datetime(2026, 6, 1, 12, 0), utcnow()):
        assert to_market_naive(base) - base == timedelta(hours=8)


def test_job_dict_created_at_is_market_naive():
    """真实序列化点：job 字典（list_jobs 用的 _job_to_dict）created_at 为上海 naive。"""
    from app.core.tasks.runner import _job_to_dict

    job = ExperimentJob(
        job_type="gp_run",
        status="done",
        progress=100.0,
        phase="",
        params={"expression": "x+y"},
        result={},
        error="",
    )
    job.id = 1
    job.created_at = utcnow()
    d = _job_to_dict(job)
    assert d["created_at"] == to_market_naive(job.created_at).isoformat()
    parsed = datetime.fromisoformat(d["created_at"])
    assert parsed - job.created_at == timedelta(hours=8)


def test_todo_serialize_times_are_market_naive():
    """真实序列化点：待办 dict（list/create/update 共用 _serialize）时间为上海 naive。"""
    from app.api.todos import _serialize

    item = TodoItem(title="x")
    item.id = 1
    item.created_at = utcnow()
    item.updated_at = utcnow()
    d = _serialize(item)
    assert d["created_at"] == to_market_naive(item.created_at).isoformat()
    assert d["updated_at"] == to_market_naive(item.updated_at).isoformat()
    assert datetime.fromisoformat(d["created_at"]) - item.created_at == timedelta(
        hours=8
    )


def test_to_market_naive_rejects_none_input():
    """None 输入直接抛错（调用点负责 None 守卫，避免静默吞掉缺值）。"""
    try:
        to_market_naive(None)  # type: ignore[arg-type]
    except (TypeError, AttributeError):
        pass
    else:
        raise AssertionError("to_market_naive(None) 应抛错而非返回 None")
