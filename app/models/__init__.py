"""模型统一出口：import 一次拿到全部表，Alembic env.py 只需 import 本文件。

 清点（24 张）：
  user.py     users / user_roles / teachers                          (3)
  org.py      classes / students / teaching_assignments / research_leads (4)
  content.py  chapters / knowledge_points / questions                (3)
  learning.py homeworks / homework_questions / student_answers /
              grading_records / error_logs                           (5)
  mastery.py  bkt_state / mastery_records                            (2)
  exam.py     diagnostic_quizzes / exam_papers / exam_paper_questions /
              exam_paper_classes / exam_attempts                     (5)
  audit.py    audit_logs                                             (1)
  login_log.py login_logs                                            (1)
  合计 24 ✓
"""
from app.db.base import Base
from app.models.audit import AuditLog
from app.models.content import Chapter, KnowledgePoint, Question
from app.models.exam import (
    DiagnosticQuiz,
    ExamAttempt,
    ExamPaper,
    ExamPaperClass,
    ExamPaperQuestion,
)
from app.models.learning import (
    ErrorLog,
    GradingRecord,
    Homework,
    HomeworkQuestion,
    StudentAnswer,
)
from app.models.login_log import LoginLog
from app.models.mastery import BKTState, MasteryRecord
from app.models.org import Class, ResearchLead, Student, TeachingAssignment
from app.models.user import Teacher, User, UserRole

# 写了__all__，import models时
__all__ = [
    "Base",
    "User", "UserRole", "Teacher",
    "Class", "Student", "TeachingAssignment", "ResearchLead",
    "Chapter", "KnowledgePoint", "Question",
    "Homework", "HomeworkQuestion", "StudentAnswer", "GradingRecord", "ErrorLog",
    "BKTState", "MasteryRecord",
    "DiagnosticQuiz", "ExamPaper", "ExamPaperQuestion", "ExamPaperClass", "ExamAttempt",
    "AuditLog",
    "LoginLog",
]
