"""组织域四张表：classes / students / teaching_assignments / research_leads。

 得到的数据：班级、学生档案、任教关系、教研组长任命
 关键设计：teaching_assignments 是 teacher 明细通道（detail scope）的数据源；
          research_leads 是 researcher 聚合通道（aggregate scope）的数据源。
          改这两张表 = 权限即时生效，这是整个权限模型的地面。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Class(Base):
    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grade: Mapped[str] = mapped_column(String(16), comment="年级，如 初二", index=True)
    name: Mapped[str] = mapped_column(String(32), comment="班名，如 2班")
    # 陌生点：`int | None` + 外键组合 = "可空外键"。班主任可以暂时没指派：
    #   数据库生成 NULL 列，插行时不传就存 NULL；不写 | None 则必须传值
    head_teacher_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), comment="班主任，可空")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # __table_args__ 复合唯一约束的用法详见 user.py 的 uq_user_role 注释：
    #   (grade, name) 组合唯一 → "初三2班"全校只允许一条，换年级可重名
    __table_args__ = (UniqueConstraint("grade", "name", name="uq_grade_class_name"),)


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    student_no: Mapped[str | None] = mapped_column(String(32), comment="学籍号")


class TeachingAssignment(Base):
    """teacher 角色的明细 scope 权限清单：王老师教哪些班、什么学科。"""

    __tablename__ = "teaching_assignments"
    __table_args__ = (UniqueConstraint("teacher_id", "class_id", "subject", name="uq_teaching"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(32), comment="学科，如 math")


class ResearchLead(Base):
    """researcher 角色的任命表：教研组 = 年级 × 学科，不建实体组表。"""

    __tablename__ = "research_leads"
    __table_args__ = (UniqueConstraint("user_id", "grade", "subject", name="uq_research_lead"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    grade: Mapped[str] = mapped_column(String(16), comment="负责年级，如 初二")
    subject: Mapped[str] = mapped_column(String(32), comment="负责学科，如 math")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
