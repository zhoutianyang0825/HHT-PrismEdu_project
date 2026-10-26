"""测验与考试域五张表：diagnostic_quizzes + exam_papers + 卷面/推送/作答三件套。

 清点（5 张）：
   diagnostic_quizzes    S-10 诊断测验
   exam_papers           R-08 试卷（配额表 + 状态）
   exam_paper_questions  卷面题目（题号 + 分值）—— 定稿时从 quota_plan_json.question_ids 固化
   exam_paper_classes    推送目标班级 —— 定稿后谁该看到、谁该考
   exam_attempts         学生考试记录 —— 提交那一下建行，主观题批完转 graded

 得到的数据：诊断测验记录、试卷（含配额表）、卷面、推送范围、考试记录
 关键设计：两处用 JSON 字段存"可解释性留痕"——
          target_points_json 存 Top5 知识点及入选原因；quota_plan_json 存 R-08 配额表。
          用 SQLAlchemy 通用 JSON 类型（PG 上是 JSONB，SQLite 上退化为 TEXT），单测可跑。

【为什么题目/分值要单独建表，而不是继续塞 quota_plan_json】
  抽题结果在 JSON 里只是 [7, 12, 20...] 一串 id，没有分值、没有题号。
  学生端要逐题渲染、老师端要按题分组批改、统计要算得分率，都需要「这题值几分」。
  塞 JSON 里每次读都得整段 parse 再算，还没法按题查——固化成一表一行才查得动。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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

    # subject：学科。推送时要按「老师的任教科目」过滤（数学老师不该收到英语卷），
    #   早先只存 grade 不存 subject，多学科教研组一多就分不清这份卷该给谁。
    subject: Mapped[str] = mapped_column(String(16), default="math", index=True, comment="学科")

    # exam_type ∈ {monthly, midterm, final}
    exam_type: Mapped[str] = mapped_column(String(16), comment="monthly/midterm/final")
    quota_plan_json: Mapped[dict | None] = mapped_column(JSON, comment="QuotaPlan 配额表")
    
    # status ∈ {draft, published, archived}
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), comment="创建人（教研组长）")
    remark: Mapped[str | None] = mapped_column(Text, comment="审核报告 / 人工修改备注")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="定稿推送时间（draft 时为空）")


class ExamPaperQuestion(Base):
    """卷面题目：一题一行，带题号与分值（定稿时固化，之后改卷不应悄悄改分）。"""

    __tablename__ = "exam_paper_questions"
    __table_args__ = (
        UniqueConstraint("paper_id", "question_id", name="uq_exam_paper_question"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)

    # seq：卷面题号（1 起）。抽题结果按知识点分组返回，直接按 id 排会打乱
    #   「先易后难」的卷面顺序 —— 学生看到的第 1 题可能是一道解答题。
    seq: Mapped[int] = mapped_column(Integer, comment="卷面题号，从 1 开始")
    score: Mapped[float] = mapped_column(Float, comment="该题分值（全卷之和 = 卷面满分）")


class ExamPaperClass(Base):
    """推送目标班级：定稿那一刻写死「这份卷给哪几个班」，学生端据此判可见性。

    旧版 /assign 只把 class_ids 写进 audit_logs 的 detail 字段，等于"记了一笔账"——
    学生端和老师端都查不到自己该不该看到这份卷，这就是定稿之后链路断掉的地方。
    """

    __tablename__ = "exam_paper_classes"
    __table_args__ = (
        UniqueConstraint("paper_id", "class_id", name="uq_exam_paper_class"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), index=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamAttempt(Base):
    """学生考试记录：交卷那一下建行，一行 = 一次「我考过这份卷」。

    为什么不用 student_answers 直接当记录：
        student_answers 是「逐题作答」，一份卷 25 题就 25 行；
        学生端列表要的是「我考过哪几场、多少分、老师批完没有」——得有一行汇总；
        主观题批改完还要回写总分，也必须有落点。
    """

    __tablename__ = "exam_attempts"
    __table_args__ = (
        UniqueConstraint("paper_id", "student_id", name="uq_exam_attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True)

    # status ∈ {submitted, graded}
    #   submitted：已交卷，主观题还在老师那儿
    #   graded   ：全卷判完（客观题即时判 + 主观题老师确认完）
    status: Mapped[str] = mapped_column(String(16), default="submitted", index=True)

    objective_score: Mapped[float] = mapped_column(Float, default=0.0, comment="客观题得分")
    subjective_score: Mapped[float] = mapped_column(Float, default=0.0, comment="主观题得分")
    total_score: Mapped[float] = mapped_column(Float, default=0.0, comment="卷面得分")
    full_score: Mapped[float] = mapped_column(Float, default=100.0, comment="卷面满分")

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    graded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="全卷判完的时间")
