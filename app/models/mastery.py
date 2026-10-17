"""掌握度域两张表：bkt_state（当前状态）+ mastery_records（时序快照）。

 得到的数据：每个学生每个知识点一行的 BKT 现状 + 每次更新追加一行的历史
 关键设计：bkt_state 用 (student_id, kp_id) 复合主键——"一人一点一行"天然由主键保证，
          UPDATE 即覆盖，不用先查后插；mastery_records 只追加不修改，画趋势线用。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, PrimaryKeyConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BKTState(Base):
    """BKT 当前状态，整个系统最重要的表：实时覆盖更新。"""

    __tablename__ = "bkt_state"
    # 陌生点 PrimaryKeyConstraint：复合主键——两个列合起来当主键（谁单独都不唯一，
    #   "学生42 + 知识点kp_x"这个组合才唯一）。效果：一人一点天然只有一行，
    #   重复插入直接被数据库拒绝，不用先 SELECT 再决定 INSERT 还是 UPDATE
    __table_args__ = (PrimaryKeyConstraint("student_id", "kp_id", name="pk_bkt_state"),)

    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), primary_key=True
    )
    kp_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_points.id", ondelete="CASCADE"), primary_key=True
    )
    p_known: Mapped[float] = mapped_column(Float, default=0.10, comment="当前掌握概率 0~1")
    # 陌生点 onupdate=func.now()：每次 UPDATE 这行时，数据库自动把该列刷成当前时间。
    #   与 server_default（只在 INSERT 时填一次）配套，一个记"出生"、一个记"最近一次改动"
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MasteryRecord(Base):
    """掌握度时序快照：每次 BKT 更新追加一行，只增不改。"""

    __tablename__ = "mastery_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    kp_id: Mapped[str] = mapped_column(ForeignKey("knowledge_points.id", ondelete="CASCADE"), index=True)
    mastery_value: Mapped[float] = mapped_column(Float, comment="本次更新后的 p_known")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
