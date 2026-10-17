"""学习过程域五张表：homeworks / homework_questions / student_answers / grading_records / error_logs。

 得到的数据：作业、作业题目分值、作答记录（三处作答共用）、批改记录、错因归因
 关键设计：student_answers.source_type ∈ {homework, quiz, exam}——
          作业/诊断测验/考试三种作答共用一张表，后续统计不用 UNION 三张表。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Homework(Base):
    __tablename__ = "homeworks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    title: Mapped[str] = mapped_column(String(128))
    kp_ids: Mapped[str] = mapped_column(Text, comment="覆盖知识点 id 的 JSON 数组，如 [\"kp_crossmult\"]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HomeworkQuestion(Base):
    __tablename__ = "homework_questions"
    __table_args__ = (UniqueConstraint("homework_id", "question_id", name="uq_homework_question"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    homework_id: Mapped[int] = mapped_column(ForeignKey("homeworks.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)
    score: Mapped[float] = mapped_column(Float, comment="该题分值")


class StudentAnswer(Base):
    __tablename__ = "student_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # source_type ∈ {homework, quiz, exam}；source_id 指向 homeworks / diagnostic_quizzes / exam_papers 的 id
    source_type: Mapped[str] = mapped_column(String(16), index=True)
    source_id: Mapped[int] = mapped_column(Integer, comment="来源记录 id")
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    content: Mapped[str] = mapped_column(Text, comment="学生作答原文")
    # 陌生点：`bool | None` 三态——NULL=还没判分，True=对，False=错。
    #   千万别用 False 顶替"未判分"，统计正确率时 NULL 行要排除而不是算错
    is_correct: Mapped[bool | None] = mapped_column(Boolean, comment="判分后写；未判分为 NULL")
    score: Mapped[float | None] = mapped_column(Float, comment="得分；主观题待老师确认后写")
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, comment="引导轮数，S-04 累计")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GradingRecord(Base):
    """批改记录：客观题直判 confirmed；主观题 LLM 建议 pending → 老师确认 confirmed / 驳回 rejected。"""

    __tablename__ = "grading_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("student_answers.id", ondelete="CASCADE"), index=True)
    # grader_type ∈ {rule, llm, teacher}
    grader_type: Mapped[str] = mapped_column(String(16), comment="rule/llm/teacher")
    ai_suggested_score: Mapped[float | None] = mapped_column(Float, comment="LLM 建议分（主观题）")
    ai_comment: Mapped[str | None] = mapped_column(Text, comment="LLM 评语")
    final_score: Mapped[float | None] = mapped_column(Float, comment="最终生效分")
    final_comment: Mapped[str | None] = mapped_column(Text)
    # status ∈ {pending, confirmed, rejected}
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ErrorLog(Base):
    """错因归因结果（S-05）：错因不直接改掌握度，只替换本次 p_slip 参与 BKT 更新。"""

    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("student_answers.id", ondelete="CASCADE"), index=True)
    # error_type ∈ {概念不清, 方法不会, 粗心, 审题失误}
    error_type: Mapped[str] = mapped_column(String(16), comment="概念不清/方法不会/粗心/审题失误")
    confidence: Mapped[float] = mapped_column(Float, comment="归因置信度 0~1")
    evidence: Mapped[str] = mapped_column(Text, comment="归因依据，须引用学生作答的具体片段")
    explanation_strategy: Mapped[str] = mapped_column(String(64), comment="讲解策略")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
