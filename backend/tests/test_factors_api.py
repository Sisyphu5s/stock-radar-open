"""因子 API 聚焦测试：CRUD 冒烟 + 发布状态机（创建→校验→发布步骤→advance）。

模式与 test_todos 一致：临时 SQLite + monkeypatch 服务层 SessionLocal，不触碰生产库。
覆盖 /api/v1/factors 主要端点（路由函数直调）：
- POST /factors 创建（含 400 校验）、GET /factors 列表、表达式去重
- POST /factors/{id}/versions 新版本
- POST /factors/versions/{id}/advance 发布步骤推进（阈值检查/顺序/驳回/published）
- POST /factors/versions/{id}/revert 撤销
- GET /factors/published 已发布列表
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.factors import advance, factors, new_factor, new_version, published, revert
from app.storage.db import Base
from app.core.factors import PUBLISH_STEPS


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite，替换 factors.service.SessionLocal（不触碰生产库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'factors.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.core.factors as FS

    monkeypatch.setattr(FS, "SessionLocal", Maker)
    yield Maker
    engine.dispose()


# 达标 metrics：可完整走通三步发布（|OOS IC|≥0.01 / 稳定性≥0.5 / 节点数<41）
_GOOD_METRICS = {"oos_ic": 0.03, "stability": 0.6, "complexity": 5}


# ---------------------------------------------------------------------------
# 创建 / 列表 / 校验
# ---------------------------------------------------------------------------


def test_create_factor_smoke(temp_db):
    created = new_factor(
        {
            "name": "动量因子",
            "expression": "ts_mean(close,5)/ts_std(close,5)",
            "description": "5日动量",
            "dataset_id": 3,
            "metrics": _GOOD_METRICS,
        }
    )
    assert created["factor"]["name"] == "动量因子"
    assert created["factor"]["status"] == "draft"
    assert created["factor"]["dataset_id"] == 3
    assert created["factor"]["latex"], "应生成 LaTeX 渲染"
    assert created["version"]["version"] == 1
    assert created["version"]["status"] == "draft"
    assert created["version"]["oos_ic"] == 0.03

    lst = factors()["data"]
    assert len(lst) == 1
    assert lst[0]["id"] == created["factor"]["id"]
    assert len(lst[0]["versions"]) == 1


def test_create_factor_validation_errors(temp_db):
    # 空表达式 → 400
    with pytest.raises(HTTPException) as e1:
        new_factor({"name": "x", "expression": ""})
    assert e1.value.status_code == 400
    # 非整数 dataset_id → 400
    with pytest.raises(HTTPException) as e2:
        new_factor({"name": "x", "expression": "close", "dataset_id": "abc"})
    assert e2.value.status_code == 400
    # 校验失败不产生任何因子
    assert factors()["data"] == []


def test_create_duplicate_expression_rejected(temp_db):
    new_factor({"name": "A", "expression": "close*2"})
    # 相同公式禁止重复创建（服务层 ValueError，路由未映射 400）
    with pytest.raises(ValueError, match="表达式已存在"):
        new_factor({"name": "B", "expression": "close*2"})
    assert len(factors()["data"]) == 1


def test_create_version_increments(temp_db):
    created = new_factor(
        {"name": "F", "expression": "close/volume", "metrics": _GOOD_METRICS}
    )
    fid = created["factor"]["id"]
    v2 = new_version(
        fid, {"expression": "close/amount", "metrics": _GOOD_METRICS, "note": "v2"}
    )
    assert v2["version"] == 2
    assert v2["expression"] == "close/amount"
    # 因子主记录表达式同步为最新版本
    lst = factors()["data"][0]
    assert lst["expression"] == "close/amount"
    assert [v["version"] for v in lst["versions"]] == [2, 1]


def test_create_version_metrics_none_does_not_crash(temp_db):
    """metrics 含 None 值：与 create_factor 判空语义一致（跳过不写，走列默认 0），不抛 TypeError。"""
    created = new_factor({"name": "F", "expression": "close/volume"})
    fid = created["factor"]["id"]
    v2 = new_version(
        fid,
        {"expression": "close/amount", "metrics": {"complexity": None, "oos_ic": 0.03}},
    )
    assert v2["version"] == 2
    assert v2["oos_ic"] == 0.03
    # complexity 列非空默认 0（与 create_factor 跳过 None 的行为一致）
    assert v2["complexity"] == 0


def test_advance_complexity_none_rejected_not_crash(temp_db, monkeypatch):
    """版本 complexity 为 None：复杂度检查应报「未达标」400 而非 float(None) TypeError 500。"""
    import app.core.factors as FS

    from app.storage.models import FactorVersion

    created = new_factor(
        {"name": "无复杂度", "expression": "ts_rank(high,5)", "metrics": _GOOD_METRICS}
    )
    vid = created["version"]["id"]
    db = temp_db()
    v = db.get(FactorVersion, vid)
    # 内存置 None 模拟存量脏数据（列 NOT NULL，只能绕过落库）
    v.complexity = None
    v.oos_verified = True
    v.stability_checked = True
    db.close()

    class _FakeSession:
        def get(self, model, pk):
            return v

        def close(self):
            pass

    monkeypatch.setattr(FS, "SessionLocal", lambda: _FakeSession())
    with pytest.raises(HTTPException) as e:
        advance(vid, {"step": "complexity_checked"})
    assert e.value.status_code == 400
    assert "未达标" in str(e.value.detail)


# ---------------------------------------------------------------------------
# 发布状态机：创建 → 校验 → 发布步骤 → advance
# ---------------------------------------------------------------------------


def test_advance_publish_full_flow(temp_db):
    created = new_factor(
        {
            "name": "可发布因子",
            "expression": "ts_rank(close,10)",
            "metrics": _GOOD_METRICS,
        }
    )
    vid = created["version"]["id"]

    final = None
    for step, _name, _thr in PUBLISH_STEPS:
        final = advance(vid, {"step": step, "approved": True, "note": f"过{step}"})
        assert final[step] is True, f"{step} 应被置 True"

    # 全部完成 → published，因子主记录同步 published
    assert final["status"] == "published"
    assert final["approved_at"] is not None
    lst = factors()["data"][0]
    assert lst["status"] == "published"

    # /factors/published 只含已发布版本
    pub = published()["data"]
    assert len(pub) == 1
    assert pub[0]["version_id"] == vid
    assert pub[0]["name"] == "可发布因子"


def test_advance_requires_metrics_threshold(temp_db):
    created = new_factor(
        {
            "name": "未达标",
            "expression": "close-low",
            "metrics": {"oos_ic": 0.001, "stability": 0.1, "complexity": 50},
        }
    )
    vid = created["version"]["id"]
    with pytest.raises(HTTPException) as e:
        advance(vid, {"step": "oos_verified"})
    assert e.value.status_code == 400
    assert "未达标" in str(e.value.detail)


def test_advance_enforces_step_order(temp_db):
    created = new_factor(
        {"name": "乱序", "expression": "high-close", "metrics": _GOOD_METRICS}
    )
    vid = created["version"]["id"]
    # 跳过 oos_verified 直接推进 stability → 前置步骤未完成
    with pytest.raises(HTTPException) as e:
        advance(vid, {"step": "stability_checked"})
    assert e.value.status_code == 400
    assert "前置步骤未完成" in str(e.value.detail)


def test_advance_unknown_step_and_approved_false(temp_db):
    created = new_factor(
        {"name": "S", "expression": "open+close", "metrics": _GOOD_METRICS}
    )
    vid = created["version"]["id"]
    with pytest.raises(HTTPException) as e:
        advance(vid, {"step": "bogus_step"})
    assert e.value.status_code == 400
    assert "未知步骤" in str(e.value.detail)

    # approved=False → 驳回（不校验阈值）
    out = advance(vid, {"step": "oos_verified", "approved": False, "note": "人工驳回"})
    assert out["status"] == "rejected"
    assert out["note"] == "人工驳回"


def test_revert_step_flow(temp_db):
    created = new_factor(
        {"name": "R", "expression": "amount/close", "metrics": _GOOD_METRICS}
    )
    vid = created["version"]["id"]
    advance(vid, {"step": "oos_verified"})

    out = revert(vid, {"step": "oos_verified"})
    assert out["status"] == "draft"
    assert out["oos_verified"] is False

    with pytest.raises(HTTPException) as e:
        revert(vid, {"step": "not_a_step"})
    assert e.value.status_code == 400
