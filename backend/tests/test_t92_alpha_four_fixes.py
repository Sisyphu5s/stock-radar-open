"""T-92 alpha 域四修测试(P1-56 后端切换锁 / P1-57 类型转换统一 400 /
P2-36 alpha101_list 单次调用 / P2-38 重计算同步端点受任务闸门约束)。

路由函数直调 + monkeypatch(与 test_optimize/test_combine 同模式),不触碰生产库。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from fastapi import HTTPException

from app.lib.alpha import backend as B


@pytest.fixture()
def numpy_backend(monkeypatch):
    """锁定 numpy 后端,测试结束恢复原配置(gp_backend 非运行时覆盖键,直接赋值)。"""
    from app.config import settings

    monkeypatch.setattr(settings, "gp_backend", "numpy")
    B.init_backend()
    yield
    B.init_backend()


def _fake_panel(S=40, T=120, seed=1):
    rng = np.random.default_rng(seed)
    close = np.exp(np.cumsum(rng.normal(0.0005, 0.01, size=(S, T)), axis=1)) * 10
    return {
        "panel": {"close": close.astype(np.float32)},
        "dates": [f"2024-01-{i % 28 + 1:02d}" for i in range(T)],
        "codes": [f"{600000 + i}.SH" for i in range(S)],
    }


# ---------------------------------------------------------------------------
# P1-57 未捕获类型转换 → 400
# ---------------------------------------------------------------------------


def test_optimize_bad_float_returns_400(monkeypatch, numpy_backend):
    """alpha/optimize 的 max_weight/risk_aversion 非法输入 → 400(原 500)。"""
    from app.api.alpha import alpha_optimize
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)
    base = {"expression": "rank(close)", "dataset_id": 1}

    for bad in ("abc", [0.1], {"v": 0.1}):
        with pytest.raises(HTTPException) as e:
            alpha_optimize({**base, "max_weight": bad})
        assert e.value.status_code == 400, f"max_weight={bad!r} 应 400"
    with pytest.raises(HTTPException) as e:
        alpha_optimize({**base, "method": "mv", "risk_aversion": "abc"})
    assert e.value.status_code == 400


def test_factor_analysis_bad_input_returns_400():
    """factors/analysis 的 factor_ids/horizons/threshold 非法输入 → 400(原 500)。"""
    from app.api.factors import factor_correlation, factor_ic_decay

    with pytest.raises(HTTPException) as e:
        factor_correlation({"factor_ids": [1, "abc"], "dataset_id": 1})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        factor_correlation({"factor_ids": [1, None], "dataset_id": 1})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        factor_correlation({"factor_ids": [1, 2], "dataset_id": 1, "threshold": "abc"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        factor_ic_decay({"expr": "close", "dataset_id": 1, "horizons": [1, "abc"]})
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# P1-56 后端切换并发锁
# ---------------------------------------------------------------------------


def test_backend_concurrent_init_consistent(numpy_backend):
    """并发 init_backend()(后端在 numpy/mlx 间翻转)下 BACKEND/_T 恒为一致对。

    无锁时 _try_mlx 成功路径的两条写(BACKEND 再 _T)可与另一线程的写交错,
    产生 BACKEND 与 _T 不匹配的中间态;持 _init_lock 后不可能出现。
    """
    from app.config import settings

    errors: list[str] = []

    def _toggle():
        try:
            for _ in range(30):
                settings.gp_backend = (
                    "mlx" if settings.gp_backend == "numpy" else "numpy"
                )
                B.init_backend()
                b, t = B.backend_name(), B.xp()
                if b == "numpy":
                    assert t is np, f"numpy 后端 _T 异常: {t!r}"
                elif b == "mlx":
                    assert t is not None and t is not np, f"mlx 后端 _T 异常: {t!r}"
                else:
                    raise AssertionError(f"未知后端 {b!r}")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=_toggle) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors


def test_api_init_backend_holds_backend_lock(monkeypatch, numpy_backend):
    """P1-56: API 路径调用 init_backend 前必须已持 runner._backend_lock。"""
    from app.api import alpha as AA
    from app.core import datasets as AD
    from app.core.tasks import runner as Q

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)

    real_init = B.init_backend
    seen: list[bool] = []

    def spy_init():
        seen.append(Q._backend_lock.locked())
        real_init()

    monkeypatch.setattr(AA.B, "init_backend", spy_init)
    res = AA.alpha_optimize(
        {"expression": "rank(close)", "dataset_id": 1, "method": "min_var"}
    )
    assert res["method"] == "min_var"
    assert seen and all(seen), f"init_backend 应在 _backend_lock 内: {seen}"


def test_apply_backend_and_api_init_serialized(numpy_backend):
    """P1-56 协调段:任务 _apply_backend 与 API 持锁 init_backend 并发互不互害、不死锁。"""
    from app.api.alpha import _init_backend_locked
    from app.core.tasks import runner as Q

    errors: list[str] = []

    def _task_switch():
        try:
            for _ in range(20):
                Q._apply_backend("cpu")  # 切 numpy 并初始化(持 _backend_lock)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def _api_init():
        try:
            for _ in range(20):
                _init_backend_locked()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [
        threading.Thread(target=_task_switch),
        threading.Thread(target=_api_init),
        threading.Thread(target=_task_switch),
        threading.Thread(target=_api_init),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    # 结束后全局状态一致(numpy 后端)
    assert B.backend_name() == "numpy"
    assert B.xp() is np


# ---------------------------------------------------------------------------
# P2-36 alpha101_list 单次调用
# ---------------------------------------------------------------------------


def test_alpha101_list_calls_list_once(monkeypatch):
    """P2-36: alpha101_list 每请求只调用一次 list_alpha101()(原两次)。"""
    import app.lib.alpha.alpha101 as A101
    from app.api.alpha import alpha101_list

    calls = {"n": 0}
    real = A101.list_alpha101

    def spy():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(A101, "list_alpha101", spy)

    res = alpha101_list()
    assert calls["n"] == 1, f"应只调用 1 次,实际 {calls['n']}"
    assert res["total"] == len(res["data"])
    assert res["total"] == len(real())  # 未丢因子

    # category 过滤仍只调用 1 次;分类集合取自全量,不含过滤后唯一类别
    cats = res["categories"]
    assert cats, "应有分类"
    res2 = alpha101_list(category=cats[0])
    assert calls["n"] == 2, f"过滤请求应仍只调用 1 次,实际 {calls['n']}"
    assert res2["data"] and all(a["category"] == cats[0] for a in res2["data"])
    assert len(res2["categories"]) == len(cats)  # 分类集合不被过滤收窄


# ---------------------------------------------------------------------------
# P2-38 重计算同步端点受任务闸门约束
# ---------------------------------------------------------------------------


class _SpySemaphore:
    """记录 acquire/release 次数的闸门包装(内部真信号量,不阻塞语义)。"""

    def __init__(self, inner: threading.Semaphore):
        self.inner = inner
        self.acquired = 0
        self.released = 0

    def acquire(self):
        self.inner.acquire()
        self.acquired += 1

    def release(self):
        self.inner.release()
        self.released += 1


def _patch_gate(monkeypatch):
    from app.core.tasks import runner as Q

    spy = _SpySemaphore(threading.Semaphore(2))
    monkeypatch.setattr(Q, "_CONCURRENCY", spy)
    return spy


def test_optimize_uses_gate_and_validation_does_not(monkeypatch, numpy_backend):
    """P2-38: optimize 重计算 acquire+release 闸门;校验失败路径不占闸门。"""
    from app.api.alpha import alpha_optimize
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)
    spy = _patch_gate(monkeypatch)

    res = alpha_optimize(
        {"expression": "rank(close)", "dataset_id": 1, "method": "min_var"}
    )
    assert res["method"] == "min_var"
    assert spy.acquired == 1 and spy.released == 1

    # 202 早退(冷缓存 miss 提交后台任务)同样不占闸门
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: False)
    from app.core.tasks import runner as Q

    monkeypatch.setattr(Q, "submit_panel_build", lambda ds: 42)
    out = alpha_optimize({"expression": "rank(close)", "dataset_id": 1})
    assert out.status_code == 202
    assert spy.acquired == 1 and spy.released == 1

    # 表达式校验失败 → 400,不占闸门
    with pytest.raises(HTTPException) as e:
        alpha_optimize({"dataset_id": 1})
    assert e.value.status_code == 400
    assert spy.acquired == 1 and spy.released == 1


def test_factor_ic_decay_uses_gate(monkeypatch, numpy_backend):
    """P2-38: factors/analysis/ic-decay 重计算同样 acquire+release 闸门。"""
    from app.api.factors import factor_ic_decay
    from app.core import datasets as AD

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)
    spy = _patch_gate(monkeypatch)

    res = factor_ic_decay({"expr": "rank(close)", "dataset_id": 1, "horizons": [1, 5]})
    assert res["horizons"] == [1, 5]
    assert len(res["ic_means"]) == 2
    assert spy.acquired == 1 and spy.released == 1


def test_compute_gate_blocks_when_saturated(monkeypatch, numpy_backend):
    """P2-38: 闸门饱和时重计算请求阻塞,释放后放行——重计算不再占满线程池。"""
    from app.api.alpha import alpha_optimize
    from app.core import datasets as AD
    from app.core.tasks import runner as Q

    monkeypatch.setattr(AD, "load_panel", lambda ds, features=None: _fake_panel())
    monkeypatch.setattr(AD, "panel_ready", lambda ds, features=None: True)
    gate = threading.Semaphore(1)
    gate.acquire()  # 占满闸门
    monkeypatch.setattr(Q, "_CONCURRENCY", gate)

    results: list[dict] = []

    def _call():
        try:
            results.append(
                alpha_optimize({"expression": "rank(close)", "dataset_id": 1})
            )
        except Exception as e:  # noqa: BLE001
            results.append({"error": repr(e)})

    t = threading.Thread(target=_call)
    t.start()
    time.sleep(0.3)
    assert results == [], "闸门占满时请求应阻塞在 acquire,尚未返回"
    gate.release()  # 放行
    t.join(timeout=15)
    assert len(results) == 1 and "weights" in results[0]
