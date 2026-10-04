"""warmup 启动竞态测试：连续两次快速调用 start_warmup 只允许启动一个后台线程。

回归背景：旧实现先置 status="idle" 再在锁外启线程，两次快速调用会在
「idle 且线程未 running」窗口内双双通过检查 → 双线程并发拉取。
修复后置 running 与线程启动处于同一临界区，第二次调用必然被拒。
"""

from __future__ import annotations

import threading
import time

from app.core import warmup

_WORKER_NAME = "hs300-warmup"


def test_start_warmup_race_only_one_thread(monkeypatch):
    # 用阻塞假实现替换真实拉取，保证测试不碰网络、且线程存活可观测
    entered: list[str] = []
    gate = threading.Event()

    def fake_run_warmup() -> None:
        entered.append(threading.current_thread().name)
        gate.wait(3)

    monkeypatch.setattr(warmup, "_run_warmup", fake_run_warmup)

    try:
        warmup.start_warmup(background=True)
        warmup.start_warmup(background=True)  # 第二次应在临界区被拒

        assert len(entered) == 1, "第二次调用不应再启动线程"
        assert warmup.get_warmup_status()["status"] == "running"
        live = [
            t for t in threading.enumerate() if t.name == _WORKER_NAME and t.is_alive()
        ]
        assert len(live) == 1, f"期望仅 1 个预热线程，实际 {len(live)}"
    finally:
        gate.set()  # 放行假实现线程
        warmup._state["status"] = "idle"  # 复位全局状态，避免污染其他测试
        time.sleep(0.05)
