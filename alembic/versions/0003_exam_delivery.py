"""0003：考试派发三件套——定稿之后的链路（2026-10-26）。

背景：教研端定稿生成的试卷以后，流程直接断了。
实际场景是：试卷生成后要按年级推送给年级的科任老师和学生，学生在考试模块全量作答并提交，客观题当场判、主观题按题归堆给老师批。

旧实现断在哪：
  POST /exam/{id}/assign 只把 class_ids 写进 audit_logs.detail 字符串就返回了。
  没有一张表回答「哪个班该考这份卷」「这题值几分」「谁交过卷」——
  学生端查不到卷、老师端收不到主观题、R-04 拿不到作答数据。

本迁移做四件事：
  1. exam_papers 加 subject / published_at
     （推送要按学科过滤科任老师；published_at 给学生端排序用）
  2. 新表 exam_paper_questions —— 卷面题目 + 题号 + 每题分值
  3. 新表 exam_paper_classes   —— 推送目标班级
  4. 新表 exam_attempts        —— 学生考试记录（交卷建行，批完转 graded）
  5. student_answers 加 attachment_json —— 手写解题步骤图片地址列表

应用：uv run alembic upgrade head

回滚：uv run alembic downgrade -1（只删本次新增的表与列，不动历史数据）
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_exam_delivery"
down_revision = "0002_teacher_overview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. exam_papers 补两列 ----
    # server_default='math'：历史卷子没有学科，直接加 NOT NULL 列会因存量行失败
    op.add_column("exam_papers",
                  sa.Column("subject", sa.String(16), nullable=False,
                            server_default="math", comment="学科"))
    op.add_column("exam_papers",
                  sa.Column("published_at", sa.DateTime(timezone=True),
                            nullable=True, comment="定稿推送时间（draft 时为空）"))
    op.create_index("ix_exam_papers_subject", "exam_papers", ["subject"])

    # ---- 2. 卷面题目 ----
    op.create_table(
        "exam_paper_questions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("paper_id", sa.Integer(),
                  sa.ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Integer(),
                  sa.ForeignKey("questions.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, comment="卷面题号，从 1 开始"),
        sa.Column("score", sa.Float(), nullable=False, comment="该题分值"),
        sa.UniqueConstraint("paper_id", "question_id", name="uq_exam_paper_question"),
    )
    op.create_index("ix_exam_paper_questions_paper_id", "exam_paper_questions", ["paper_id"])
    op.create_index("ix_exam_paper_questions_question_id", "exam_paper_questions", ["question_id"])

    # ---- 3. 推送目标班级 ----
    op.create_table(
        "exam_paper_classes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("paper_id", sa.Integer(),
                  sa.ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("class_id", sa.Integer(),
                  sa.ForeignKey("classes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("paper_id", "class_id", name="uq_exam_paper_class"),
    )
    op.create_index("ix_exam_paper_classes_paper_id", "exam_paper_classes", ["paper_id"])
    op.create_index("ix_exam_paper_classes_class_id", "exam_paper_classes", ["class_id"])

    # ---- 4. 学生考试记录 ----
    op.create_table(
        "exam_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("paper_id", sa.Integer(),
                  sa.ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_id", sa.Integer(),
                  sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="submitted",
                  comment="submitted 待主观题批改 / graded 全卷判完"),
        sa.Column("objective_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("subjective_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("full_score", sa.Float(), nullable=False, server_default="100"),
        sa.Column("submitted_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("paper_id", "student_id", name="uq_exam_attempt"),
    )
    op.create_index("ix_exam_attempts_paper_id", "exam_attempts", ["paper_id"])
    op.create_index("ix_exam_attempts_student_id", "exam_attempts", ["student_id"])
    op.create_index("ix_exam_attempts_status", "exam_attempts", ["status"])

    # ---- 5. 作答附件（手写图）----
    op.add_column("student_answers",
                  sa.Column("attachment_json", sa.JSON(), nullable=True,
                            comment="作答附件（手写图 URL 列表），无图为 NULL"))


def downgrade() -> None:
    op.drop_column("student_answers", "attachment_json")

    op.drop_index("ix_exam_attempts_status", table_name="exam_attempts")
    op.drop_index("ix_exam_attempts_student_id", table_name="exam_attempts")
    op.drop_index("ix_exam_attempts_paper_id", table_name="exam_attempts")
    op.drop_table("exam_attempts")

    op.drop_index("ix_exam_paper_classes_class_id", table_name="exam_paper_classes")
    op.drop_index("ix_exam_paper_classes_paper_id", table_name="exam_paper_classes")
    op.drop_table("exam_paper_classes")

    op.drop_index("ix_exam_paper_questions_question_id", table_name="exam_paper_questions")
    op.drop_index("ix_exam_paper_questions_paper_id", table_name="exam_paper_questions")
    op.drop_table("exam_paper_questions")

    op.drop_index("ix_exam_papers_subject", table_name="exam_papers")
    op.drop_column("exam_papers", "published_at")
    op.drop_column("exam_papers", "subject")
