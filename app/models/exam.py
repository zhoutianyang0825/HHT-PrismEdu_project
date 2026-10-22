"""测验与考试域两张表：diagnostic_quizzes（S-10 诊断测验）+ exam_papers（R-08 试卷）。

 得到的数据：诊断测验记录、试卷（含配额表）
 关键设计：两处都用 JSON 字段存"可解释性留痕"——
          target_points_json 存 Top5 知识点及入选原因；quota_plan_json 存 R-08 配额表。
          用 SQLAlchemy 通用 JSON 类型（PG 上是 JSONB，SQLite 上退化为 TEXT），单测可跑。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DiagnosticQuiz(Base):
    """S-10 诊断测验：错题知识点 → 图谱外扩 → BKT Top5 → 配题 8 道。"""

    __tablename__ = "diagnostic_quizzes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    # 形如 [{"kp_id": "kp_crossmult", "name": "十字相乘法", "p_known": 0.33, "reason": "近30天连错3次"}, ...]
    # 陌生点 JSON 列类型：SQLAlchemy 的通用 JSON 类型是"一鱼两吃"——
    #   在 PG 上建 JSONB 列（可索引可查询），在 SQLite 单测库上自动退化为 TEXT。
    #   Mapped[list] / Mapped[dict] 声明的是 Python 侧读出来的类型：存进去自动
    #   dump 成 JSON 字符串，读出来自动 parse 回列表/字典，不用手动 json.loads
    target_points_json: Mapped[list] = mapped_column(JSON, comment="Top5 目标知识点及入选原因")
    question_ids: Mapped[list] = mapped_column(JSON, comment="最终选出的题目 id 列表，8 题")
    generated_reason: Mapped[str | None] = mapped_column(Text, comment="面向学生的一句话解释")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamPaper(Base):
    """R-08 试卷：配额表可打印、可人工改后再触发重抽。"""

    __tablename__ = "exam_papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128))
    grade: Mapped[str] = mapped_column(String(16), index=True, comment="年级，如 初二")

    # exam_type ∈ {monthly, midterm, final}
    exam_type: Mapped[str] = mapped_column(String(16), comment="monthly/midterm/final")
    quota_plan_json: Mapped[dict | None] = mapped_column(JSON, comment="QuotaPlan 配额表")
    
    # status ∈ {draft, published, archived}
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), comment="创建人（教研组长）")
    remark: Mapped[str | None] = mapped_column(Text, comment="审核报告 / 人工修改备注")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
