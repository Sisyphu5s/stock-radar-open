"""T-50 因子版本 reject 状态守卫 + human_approved 写入测试。

- published 版本 reject → ValueError(状态保持 published,human_approved=True)
- draft/candidate 版本 reject → status=rejected,human_approved=False
- 正常推进到 published → human_approved=True
- rejected 版本再 reject → ValueError

直接调用服务层(不走 API 层 ValueError→400 映射),以断言原始异常。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.db import Base
from app.storage.models import FactorVersion
from app.core.factors import PUBLISH_STEPS, advance_step, create_factor


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """独立临时 SQLite,替换 factors.service.SessionLocal(不触碰生产库)。"""
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


# 达标 metrics:可完整走通三步发布(|OOS IC|≥0.01 / 稳定性≥0.5 / 节点数<41)
_GOOD_METRICS = {"oos_ic": 0.03, "stability": 0.6, "complexity": 5}


def _publish_version(vid: int) -> None:
    for step, _name, _thr in PUBLISH_STEPS:
        advance_step(vid, step, approved=True)


def test_reject_published_version_raises(temp_db):
    created = create_factor("F", "close/volume", metrics=_GOOD_METRICS)
    vid = created["version"]["id"]
    _publish_version(vid)

    # published 版本驳回 → ValueError,状态与 human_approved 保持 published 语义
    with pytest.raises(ValueError, match="已发布版本不可驳回"):
        advance_step(vid, PUBLISH_STEPS[0][0], approved=False)

    db = temp_db()
    v = db.get(FactorVersion, vid)
    db.close()
    assert v.status == "published"
    assert v.human_approved is True


def test_reject_draft_sets_rejected(temp_db):
    created = create_factor("F2", "close*2", metrics=_GOOD_METRICS)
    vid = created["version"]["id"]

    out = advance_step(vid, PUBLISH_STEPS[0][0], approved=False, note="人工驳回")
    assert out["status"] == "rejected"
    assert out["human_approved"] is False
    assert out["note"] == "人工驳回"


def test_reject_candidate_sets_rejected(temp_db):
    created = create_factor("F3", "high-close", metrics=_GOOD_METRICS)
    vid = created["version"]["id"]
    # 推进第一步 → candidate 中间态
    assert advance_step(vid, "oos_verified")["status"] == "candidate"

    out = advance_step(vid, "stability_checked", approved=False)
    assert out["status"] == "rejected"
    assert out["human_approved"] is False


def test_published_sets_human_approved(temp_db):
    created = create_factor("F4", "ts_mean(close,5)", metrics=_GOOD_METRICS)
    vid = created["version"]["id"]
    _publish_version(vid)

    db = temp_db()
    v = db.get(FactorVersion, vid)
    db.close()
    assert v.status == "published"
    assert v.human_approved is True


def test_reject_rejected_version_raises(temp_db):
    created = create_factor("F5", "open*3", metrics=_GOOD_METRICS)
    vid = created["version"]["id"]
    advance_step(vid, PUBLISH_STEPS[0][0], approved=False)

    # rejected 再 reject → 拒绝
    with pytest.raises(ValueError, match="已驳回版本不可重复驳回"):
        advance_step(vid, PUBLISH_STEPS[1][0], approved=False)
