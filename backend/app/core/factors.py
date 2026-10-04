"""因子服务（core 层，bt-fac）：因子 + 版本的创建、评估记录、三步发布流程。

由原 factors/service.py 合流而来，职责并入本模块；
测试 monkeypatch（patch FS.SessionLocal）直接穿透到本模块实现层。

发布步骤阈值唯一来源在 advance_step 内联：
oos_verified 要求 |OOS IC| ≥ 0.01，stability_checked 要求稳定性 ≥ 0.5，
complexity_checked 要求复杂度节点数 < 41。勿在此处维护第二份常量，避免数值漂移。
"""

from __future__ import annotations

from ..lib.codes import in_chunks
from ..lib.timex import to_market_naive
from ..storage.db import SessionLocal
from ..storage.models import Factor, FactorVersion, utcnow

# 三步发布流程步骤（面向个人使用：评估 → 回测 → 三步检查 → 发布）
PUBLISH_STEPS = [
    ("oos_verified", "样本外验证", "|OOS IC| ≥ 0.01"),
    ("stability_checked", "稳定性检查", "IC>0 占比 ≥ 50%"),
    ("complexity_checked", "复杂度检查", "节点数 < 41"),
]


def create_factor(
    name: str,
    expression: str,
    description: str = "",
    dataset_id: int = 0,
    metrics: dict | None = None,
    kind: str = "expr",
    model_ref: int | None = None,
) -> dict:
    expression = expression.strip()
    db = SessionLocal()
    try:
        # 表达式去重：相同公式禁止重复创建（避免同名/近名因子堆积）
        existing = db.query(Factor).filter(Factor.expression == expression).first()
        if existing is not None:
            raise ValueError(
                f"表达式已存在于因子「{existing.name}」(# {existing.id})，请勿重复创建"
            )
        f = Factor(
            name=name,
            expression=expression,
            description=description,
            dataset_id=dataset_id,
            kind=kind,
        )
        db.add(f)
        db.commit()
        db.refresh(f)
        # 自动创建 v1 版本（nn 因子携带 model_ref 关联 nn_models.id）
        v = FactorVersion(
            factor_id=f.id, version=1, expression=expression, model_ref=model_ref
        )
        if metrics:
            for k in (
                "train_ic",
                "train_rank_ic",
                "val_ic",
                "val_rank_ic",
                "oos_ic",
                "oos_rank_ic",
                "return_annual",
                "turnover",
                "stability",
                "complexity",
            ):
                if k in metrics and metrics[k] is not None:
                    setattr(v, k, float(metrics[k]))
        db.add(v)
        db.commit()
        return {"factor": factor_dict(f), "version": version_dict(v)}
    finally:
        db.close()


def list_factors(include_archived: bool = False) -> list[dict]:
    db = SessionLocal()
    try:
        q = db.query(Factor)
        if not include_archived:
            q = q.filter(Factor.status != "archived")
        factors = q.order_by(Factor.created_at.desc()).all()
        # N+1 修复：一次批量查回全部版本后内存分组（修复前逐因子一条版本查询）；
        # 因子数可能超 SQLite 变量上限，in_ 按 400/批
        ids = [f.id for f in factors]
        versions_by_factor: dict[int, list] = {}
        for chunk in in_chunks(ids):
            for v in (
                db.query(FactorVersion)
                .filter(FactorVersion.factor_id.in_(chunk))
                .order_by(FactorVersion.version.desc())
                .all()
            ):
                versions_by_factor.setdefault(v.factor_id, []).append(v)
        out = []
        for f in factors:
            d = factor_dict(f)
            d["versions"] = [version_dict(v) for v in versions_by_factor.get(f.id, [])]
            out.append(d)
        return out
    finally:
        db.close()


def factor_dict(f: Factor) -> dict:
    from ..lib.alpha.latex import cached_latex

    return {
        "id": f.id,
        "name": f.name,
        "expression": f.expression,
        "kind": f.kind,
        "latex": cached_latex(f.expression),
        "description": f.description,
        "status": f.status,
        "dataset_id": f.dataset_id,
        "created_at": (
            to_market_naive(f.created_at).isoformat() if f.created_at else None
        ),
    }


def version_dict(v: FactorVersion) -> dict:
    from ..lib.alpha.latex import cached_latex

    return {
        "id": v.id,
        "factor_id": v.factor_id,
        "version": v.version,
        "expression": v.expression,
        "model_ref": v.model_ref,
        "latex": cached_latex(v.expression),
        "complexity": v.complexity,
        "train_ic": v.train_ic,
        "train_rank_ic": v.train_rank_ic,
        "val_ic": v.val_ic,
        "val_rank_ic": v.val_rank_ic,
        "oos_ic": v.oos_ic,
        "oos_rank_ic": v.oos_rank_ic,
        "return_annual": v.return_annual,
        "turnover": v.turnover,
        "stability": v.stability,
        "oos_verified": v.oos_verified,
        "stability_checked": v.stability_checked,
        "complexity_checked": v.complexity_checked,
        "version_released": v.version_released,
        "human_approved": v.human_approved,
        "status": v.status,
        "note": v.note,
        "created_at": (
            to_market_naive(v.created_at).isoformat() if v.created_at else None
        ),
        "approved_at": (
            to_market_naive(v.approved_at).isoformat() if v.approved_at else None
        ),
    }


def create_version(
    factor_id: int, expression: str, metrics: dict | None = None, note: str = ""
) -> dict:
    db = SessionLocal()
    try:
        f = db.get(Factor, factor_id)
        if f is None:
            raise ValueError(f"因子 {factor_id} 不存在")
        latest = (
            db.query(FactorVersion)
            .filter(FactorVersion.factor_id == factor_id)
            .order_by(FactorVersion.version.desc())
            .first()
        )
        version = (latest.version + 1) if latest else 1
        v = FactorVersion(
            factor_id=factor_id, version=version, expression=expression, note=note
        )
        if metrics:
            for k in (
                "train_ic",
                "train_rank_ic",
                "val_ic",
                "val_rank_ic",
                "oos_ic",
                "oos_rank_ic",
                "return_annual",
                "turnover",
                "stability",
                "complexity",
            ):
                if k in metrics and metrics[k] is not None:
                    setattr(v, k, float(metrics[k]))
        f.expression = expression
        db.add(v)
        db.commit()
        db.refresh(v)
        return version_dict(v)
    finally:
        db.close()


def advance_step(
    version_id: int, step: str, approved: bool = True, note: str = ""
) -> dict:
    """推进发布流程。step 必须是 PUBLISH_STEPS 中的字段名。"""
    db = SessionLocal()
    try:
        v = db.get(FactorVersion, version_id)
        if v is None:
            raise ValueError(f"版本 {version_id} 不存在")
        if not approved:
            # 状态守卫：仅 draft/candidate 可驳回，已发布/已驳回版本不可再驳回
            if v.status == "published":
                raise ValueError("已发布版本不可驳回")
            if v.status == "rejected":
                raise ValueError("已驳回版本不可重复驳回")
            v.status = "rejected"
            v.human_approved = False
            v.note = note or "人工驳回"
            db.commit()
            return version_dict(v)

        # 步骤自带检查
        checks = []
        if step == "oos_verified":
            # 方向不限：|OOS IC| ≥ 阈值（负 IC 因子同样有效，可反向使用）
            checks.append(
                ("样本外 IC", abs(v.oos_ic) if v.oos_ic is not None else None, 0.01)
            )
        elif step == "stability_checked":
            checks.append(("稳定性", v.stability, 0.5))
        elif step == "complexity_checked":
            checks.append(("复杂度节点数", v.complexity, 41.0, "lt"))

        for name, val, thr, *op in checks:
            cmp = (
                (lambda x, y: x < y)
                if (op and op[0] == "lt")
                else (lambda x, y: x >= y)
            )
            if val is None or not cmp(float(val), float(thr)):
                raise ValueError(f"步骤 {name} 未达标: 当前 {val}, 阈值 {thr}")

        # 强制顺序：前置步骤必须已完成（不允许跳过/自动补全）
        names = [s[0] for s in PUBLISH_STEPS]
        step_names = {k: n for k, n, _ in PUBLISH_STEPS}
        idx = names.index(step)
        for prior in names[:idx]:
            if not getattr(v, prior):
                raise ValueError(f"前置步骤未完成: {step_names[prior]}")

        setattr(v, step, True)
        if note:
            v.note = note

        # 全部完成 → published（人工确认发布）
        if all(getattr(v, n) for n in names):
            v.status = "published"
            v.human_approved = True
            v.approved_at = utcnow()
            f = db.get(Factor, v.factor_id)
            if f:
                f.status = "published"
        else:
            v.status = "candidate"
        db.commit()
        db.refresh(v)
        return version_dict(v)
    finally:
        db.close()


def revert_step(version_id: int, step: str) -> dict:
    """撤销某一步骤（仅 draft/candidate 状态允许）。"""
    names = [s[0] for s in PUBLISH_STEPS]
    if step not in names:
        raise ValueError(f"未知步骤 {step}")
    db = SessionLocal()
    try:
        v = db.get(FactorVersion, version_id)
        if v is None:
            raise ValueError(f"版本 {version_id} 不存在")
        if v.status == "published":
            raise ValueError("已发布版本不可撤销步骤")
        setattr(v, step, False)
        v.status = "draft"
        db.commit()
        return version_dict(v)
    finally:
        db.close()


def published_factors() -> list[dict]:
    """只有 published 版本才进入市场信号系统的可选因子列表。"""
    db = SessionLocal()
    try:
        vs = (
            db.query(FactorVersion)
            .filter(FactorVersion.status == "published")
            .order_by(FactorVersion.id.desc())
            .all()
        )
        # N+1 修复（P2-43）：一次批量查回全部关联因子（IN 分批，规避 SQLite 变量上限），
        # 内存建名映射；修复前逐版本 db.get(Factor, ...) 一条一查
        ids = list({v.factor_id for v in vs})
        name_by_id: dict[int, str] = {}
        for chunk in in_chunks(ids):
            for f in db.query(Factor).filter(Factor.id.in_(chunk)).all():
                name_by_id[f.id] = f.name
        out = []
        for v in vs:
            name = name_by_id.get(v.factor_id)
            out.append(
                {
                    "version_id": v.id,
                    "factor_id": v.factor_id,
                    "name": name if name else f"因子#{v.factor_id}",
                    "expression": v.expression,
                    "version": v.version,
                    "oos_ic": v.oos_ic,
                    "stability": v.stability,
                    "complexity": v.complexity,
                    "approved_at": (
                        to_market_naive(v.approved_at).isoformat()
                        if v.approved_at
                        else None
                    ),
                }
            )
        return out
    finally:
        db.close()
