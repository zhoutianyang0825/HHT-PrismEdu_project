"""M6-T4 · 演示环境重置脚本：清作答、留题库，可反复演示。

【清什么 / 留什么】（"清作答留题库"的字面落地）
  清：学习过程与产物
      error_logs / grading_records / student_answers / mastery_records /
      bkt_state / diagnostic_quizzes / homework_questions / homeworks /
      exam_papers / audit_logs
      + LangGraph 会话状态（checkpoints / blobs / writes / store，可 --keep-threads 保留）
  留：题库与身份（重置后马上能再演示，不用重灌数据）
      questions / knowledge_points / chapters / users / user_roles /
      classes / students / teaching_assignments / research_leads

【为什么顺序不能乱】
  FK 链：error_logs → student_answers → students → users
        homework_questions → homeworks；bkt_state/mastery_records → students & kps
  先删子表再删父表，一次过；反过来会被 FK 拦下（M1 wipe 的老坑）。

【用法】
  uv run python scripts/reset_demo.py                # 清学习数据 + 清会话状态（标准重置）
  uv run python scripts/reset_demo.py --keep-threads # 保留 LangGraph 挂起会话
  uv run python scripts/reset_demo.py --with-users   # 连账号/班级关系一起清（题库仍保留）
  重置后想恢复完整演示数据：uv run python scripts/gen_demo_data.py
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    BKTState,
    DiagnosticQuiz,
    ErrorLog,
    ExamPaper,
    GradingRecord,
    Homework,
    HomeworkQuestion,
    MasteryRecord,
    ResearchLead,
    Student,
    StudentAnswer,
    TeachingAssignment,
    User,
    UserRole,
)

# 学习过程表，删的顺序 = FK 依赖从子到父
LEARNING_TABLES = [
    ErrorLog, GradingRecord, StudentAnswer, MasteryRecord, BKTState,
    DiagnosticQuiz, HomeworkQuestion, Homework, ExamPaper, AuditLog,
]

# 账号表（--with-users 才清）；classes/chapters/kps/questions 任何情况都保留
USER_TABLES = [
    TeachingAssignment, ResearchLead, Student, UserRole, User,
]

# LangGraph 内部表（checkpointer / store 的 setup() 建的），会话挂起状态在这里。
#   这些表不是 SQLAlchemy 模型，用原生 SQL 清；不存在就跳过（还没跑过图的库）。
THREAD_TABLES = ["checkpoint_writes", "checkpoint_blobs", "checkpoints", "store"]


def _wipe(session, models) -> dict[str, int]:
    """按序清一批表，返回 {表名: 删除行数}。"""
    counts = {}
    for model in models:
        counts[model.__tablename__] = session.query(model).delete()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="HHT棱镜课堂 演示数据重置")
    parser.add_argument("--keep-threads", action="store_true",
                        help="保留 LangGraph 挂起会话（默认清掉）")
    parser.add_argument("--with-users", action="store_true",
                        help="连账号/授课关系/学生档案一起清（题库仍然保留）")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        # 1) 学习过程表
        counts = _wipe(session, LEARNING_TABLES)
        for name, n in counts.items():
            print(f"[reset] {name:22s} 删除 {n} 行")

        # 2) LangGraph 会话状态：独立会话逐表兜底。
        #    为什么不并进主事务：PG 里语句一失败整个事务就 aborted，
        #    表不存在的报错会把前面学习表的删除一起毒死；独立会话各管各的提交。
        if not args.keep_threads:
            s2 = SessionLocal()
            try:
                for name in THREAD_TABLES:
                    try:
                        res = s2.execute(text(f"DELETE FROM {name}"))
                        s2.commit()
                        print(f"[reset] {name:22s} 删除 {res.rowcount} 行（会话状态）")
                    except Exception as exc:   # noqa: BLE001
                        s2.rollback()
                        print(f"[reset] {name:22s} 跳过（表不存在/不可清：{type(exc).__name__}）")
            finally:
                s2.close()
        else:
            print("[reset] --keep-threads：挂起中的会话保留")

        # 3) 可选：账号体系
        if args.with_users:
            # classes.head_teacher_id 外键指 users：先摘掉，不然删用户会被 FK 拦
            session.execute(text("UPDATE classes SET head_teacher_id = NULL"))
            counts = _wipe(session, USER_TABLES)
            for name, n in counts.items():
                print(f"[reset] {name:22s} 删除 {n} 行（--with-users）")

        session.commit()

        left = {
            "questions": session.execute(text("SELECT count(*) FROM questions")).scalar(),
            "users": session.execute(text("SELECT count(*) FROM users")).scalar(),
        }
        print(f"\n[OK] 重置完成。保留：questions={left['questions']} 题 / users={left['users']} 人")
        print("[OK] 恢复演示数据：uv run python scripts/gen_demo_data.py")
    except Exception:
        session.rollback()
        print("[FAIL] 已回滚，未做任何改动")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
