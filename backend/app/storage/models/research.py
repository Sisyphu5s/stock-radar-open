"""研究子系统模型：数据集 / 因子 / 因子版本 / 神经网络模型。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .stock import utcnow


class Dataset(Base):
    """Alpha 数据集：universe + 时间范围。"""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    universe: Mapped[str] = mapped_column(String(64))  # hs300 / top500 / custom
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    stock_count: Mapped[int] = mapped_column(Integer, default=0)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ready")  # building/ready
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DatasetStock(Base):
    """数据集成分股：dataset_id + code + 顺序号（build_dataset 时写入真实股票池）。"""

    __tablename__ = "dataset_stocks"
    __table_args__ = (Index("ix_dataset_stock", "dataset_id", "seq", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(12))
    seq: Mapped[int] = mapped_column(Integer, default=0)


class Factor(Base):
    """因子：研究子系统产物，与市场子系统唯一的连接点。

    kind='expr' 为传统表达式因子；kind='nn' 为神经网络因子（expression
    存伪表达式 "neural_mlp(model#N)"，实际预测逻辑见 nn_models 表）。
    """

    __tablename__ = "factors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    expression: Mapped[str] = mapped_column(Text)  # 公式字符串（nn 因子为伪表达式）
    kind: Mapped[str] = mapped_column(String(16), default="expr")  # expr/nn
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default="draft"
    )  # draft/published/archived
    dataset_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    versions: Mapped[list["FactorVersion"]] = relationship(back_populates="factor")


class FactorVersion(Base):
    """因子版本：发布流程状态机。"""

    __tablename__ = "factor_versions"
    # (factor_id, version) 唯一约束：仅新库生效（旧库不迁移）
    __table_args__ = (
        UniqueConstraint("factor_id", "version", name="uq_factor_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    factor_id: Mapped[int] = mapped_column(ForeignKey("factors.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    expression: Mapped[str] = mapped_column(Text)
    complexity: Mapped[int] = mapped_column(Integer, default=0)  # 表达式节点数
    # nn 因子：关联的神经网络模型（nn_models.id）；expr 因子为 None
    model_ref: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # 训练/验证/样本外指标
    train_ic: Mapped[float] = mapped_column(Float, default=0.0)
    train_rank_ic: Mapped[float] = mapped_column(Float, default=0.0)
    val_ic: Mapped[float] = mapped_column(Float, default=0.0)
    val_rank_ic: Mapped[float] = mapped_column(Float, default=0.0)
    oos_ic: Mapped[float] = mapped_column(Float, default=0.0)
    oos_rank_ic: Mapped[float] = mapped_column(Float, default=0.0)
    return_annual: Mapped[float] = mapped_column(Float, default=0.0)
    turnover: Mapped[float] = mapped_column(Float, default=0.0)
    stability: Mapped[float] = mapped_column(Float, default=0.0)  # 月度 IC>0 比例

    # 五步发布流程
    oos_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    stability_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    complexity_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    version_released: Mapped[bool] = mapped_column(Boolean, default=False)
    human_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(
        String(16), default="draft"
    )  # draft/candidate/published/rejected

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    factor: Mapped["Factor"] = relationship(back_populates="versions")


class NNModel(Base):
    """神经网络模型产物：训练完成的 checkpoint + 架构 + 评估指标。

    checkpoints 存 backend/cache/models/nn_*.npz（仅权重，不入 git）。
    architecture 存 {"layers": [32,16], "activation": "relu"}；
    train_loss/val_loss 为每 epoch 序列（JSON 列表）。
    """

    __tablename__ = "nn_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checkpoint: Mapped[str] = mapped_column(String(512))  # npz 权重路径
    architecture: Mapped[dict] = mapped_column(JSON, default=dict)
    dataset_id: Mapped[int] = mapped_column(Integer, default=0)
    horizon: Mapped[int] = mapped_column(Integer, default=5)
    epochs: Mapped[int] = mapped_column(Integer, default=0)
    train_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    val_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    train_loss: Mapped[list | None] = mapped_column(JSON, nullable=True)
    val_loss: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
