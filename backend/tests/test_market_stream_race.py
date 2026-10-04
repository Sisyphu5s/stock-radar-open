"""行情推送线程竞态测试(P1-55):断线重连 90s 内不得出现双推送线程。

回归场景:SSE 断开 → 订阅归零触发 _stop,客户端 EventSource 在旧推送线程
退出前重连 subscribe。旧实现中 _stop_thread 锁内置 None + _ensure_thread
clear + 新建顶替,旧线程醒来发现停止信号被清而继续循环 → 双线程并存,
_diff 基准双写、请求翻倍。

本文件用真实线程验证 Hub 生命周期不变量(现有 test_market_stream.py 的
hub fixture 把线程启停 no-op 化,无法覆盖本场景,故独立构造实例):
  1. 任一订阅存在时恰有一个存活推送线程;
  2. 最后一个取消后线程退出且引用清理(_thread → None,可再冷启动);
  3. 归零瞬间重连不产生第二个长期并存线程,推送不中断;
  4. 快速交替订阅/取消(压力)后恒 ≤1 推送线程,无死锁。

线程在 _stop.wait(interval) 中沉睡,不触碰网络/DB;仅 stop 唤醒退出,
故测试无外部副作用。区间极短(90s)不会超时,只有 stop 能驱动退出。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.core.market_stream import MarketStreamHub

_THREAD_NAME = "market-stream-push"


def _alive_push_threads() -> list[threading.Thread]:
    """全局存活的推送线程(线程名唯一;现有测试均 no-op 化线程,无残留)。"""
    return [t for t in threading.enumerate() if t.name == _THREAD_NAME and t.is_alive()]


def _wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _wait_thread_gone(h: MarketStreamHub, timeout: float = 3.0) -> bool:
    """线程退出并完成引用清理(_thread → None)。"""

    def _gone() -> bool:
        with h._lock:
            return h._thread is None

    return _wait_until(_gone, timeout)


@pytest.fixture()
def race_hub():
    """独立 Hub 实例(真实线程);结束后清订阅并等待线程退出,防泄漏。"""
    h = MarketStreamHub()
    yield h
    with h._lock:
        subs = list(h._subs)
    for sid in subs:
        h.unsubscribe(sid)
    _wait_thread_gone(h, timeout=3.0)
    # 本实例线程必须全部退出,否则残留影响后续用例
    assert not _alive_push_threads()


def test_last_unsubscribe_stops_thread_and_clears_ref(race_hub):
    """最后一个取消:线程收到停止信号退出,且 _thread 引用被清理。"""
    h = race_hub
    sid, _ = h.subscribe()
    t = h._thread
    assert t is not None and t.is_alive()
    assert h._ref_count == 1

    h.unsubscribe(sid)

    assert _wait_thread_gone(h), "线程未在超时内退出并清理引用"
    assert not t.is_alive()
    assert h._ref_count == 0
    assert not _alive_push_threads()


def test_reconnect_before_old_thread_exit_keeps_single_thread(race_hub):
    """断线重连核心场景:旧线程退出前立即重连,不得产生双推送线程。

    时序:unsubscribe(归零,set stop)→ subscribe(重连,旧线程可能仍在收尾)。
    旧线程收尾时复查 ref_count > 0 → 让位重启;全程推送线程恒 1 个。
    """
    h = race_hub
    sid1, _ = h.subscribe()
    t1 = h._thread
    assert t1 is not None and t1.is_alive()

    # 断开:触发停止信号,但不等待线程退出(模拟 90s 内快速重连)
    h.unsubscribe(sid1)
    sid2, _ = h.subscribe()

    # 收敛断言:恰一个存活推送线程,且 _thread 指向存活线程
    ok = _wait_until(
        lambda: (
            len(_alive_push_threads()) == 1
            and h._thread is not None
            and h._thread.is_alive()
        ),
        timeout=3.0,
    )
    assert ok, f"收敛失败:alive={len(_alive_push_threads())}"
    assert h._ref_count == 1
    # 观察窗口内不允许两个线程长期并存(旧 bug 两线程都在 wait,恒为 2)。
    # 采样次数受调度影响不固定,故用「1 线程采样占比」断言。
    max_alive, one_cnt, total = 0, 0, 0
    deadline = time.time() + 0.5
    while time.time() < deadline:
        n = len(_alive_push_threads())
        max_alive = max(max_alive, n)
        total += 1
        one_cnt += 1 if n == 1 else 0
        time.sleep(0.01)
    assert max_alive <= 2, "出现长期双线程并存"
    assert total >= 10, f"观察窗口采样不足: {total}"
    assert one_cnt / total >= 0.8, (
        f"大部分采样点应只有 1 个线程,实际 one 采样 {one_cnt}/{total}"
    )


def test_churn_subscribe_unsubscribe_no_deadlock_no_duplicate(race_hub):
    """压力:快速交替订阅/取消,任何时刻不出现双线程并存,且无死锁。"""
    h = race_hub
    sid = None
    for i in range(30):
        sid, _ = h.subscribe()
        # 观察:允许短暂的退出重叠窗口(≤2),不允许长期并存
        assert len(_alive_push_threads()) <= 2
        h.unsubscribe(sid)
        sid = None
    # 结束时无订阅 → 线程应退出并清理
    assert _wait_thread_gone(h), "压力结束后线程未退出"
    assert h._ref_count == 0


def test_reconnected_thread_resumes_pushing(race_hub, monkeypatch):
    """重连后新线程继续推送(验证线程确实存活在循环中,而非静默死去)。"""
    h = race_hub
    calls: list[float] = []
    monkeypatch.setattr(h, "_tick_once", lambda: calls.append(time.time()))
    monkeypatch.setattr(h, "_interval", lambda: 0.02)  # 20ms 一轮,加速观察

    sid1, _ = h.subscribe()
    assert _wait_until(lambda: len(calls) >= 2, timeout=2.0), "首线程未推送"
    n_before = len(calls)

    h.unsubscribe(sid1)  # 断开
    sid2, _ = h.subscribe()  # 立即重连
    assert _wait_until(lambda: len(calls) >= n_before + 2, timeout=2.0), (
        "重连后推送中断:线程未接管"
    )
    assert len(_alive_push_threads()) == 1
    h.unsubscribe(sid2)
    assert _wait_thread_gone(h)


def test_subscribe_after_full_stop_restarts_cold(race_hub):
    """归零线程完全退出后,再次订阅走 _ensure_thread 冷启动新线程。"""
    h = race_hub
    sid1, _ = h.subscribe()
    t1 = h._thread
    h.unsubscribe(sid1)
    assert _wait_thread_gone(h)
    assert not t1.is_alive()

    sid2, _ = h.subscribe()
    assert h._thread is not None and h._thread.is_alive()
    assert h._thread is not t1, "应新建线程而非复用已死线程引用"
    assert len(_alive_push_threads()) == 1
    h.unsubscribe(sid2)
    assert _wait_thread_gone(h)
