"""老师自主批改（/grade/{id}/manual）单测：三档判分 + 落库来源 + 幂等。

【钉住什么】
  1. 正确 / 错误 / 部分正确的分数与 is_correct 换算（部分正确走 60% 线，和 AI 建议同口径）
  2. grading_records.grader_type 必须是 teacher —— 人工判的分不能标成 llm
  3. 部分正确必须给分、且分数要落在 (0, 满分) 开区间
  4. 同一条作答不能批两次（否则 BKT 被刷两遍）

【为什么 mock 掉归因与 BKT】
  attribute_error 要调 LLM、update_mastery_node 要连 Neo4j 同步。
  这两个都是既有节点、已有各自单测（test_mastery_bkt / test_grading_graph），
  这里只关心"人工批改把正确的值交给了它们"，所以换成假实现并断言入参。
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.services.manual_grading as mg
from app.models import (
    AuditLog,
    Base,
    Chapter,
    Class,
    GradingRecord,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
    User,
)


@pytest.fixture()
def db(monkeypatch):
    """SQLite 内存库 + 一条 4 分的主观题作答（homework 来源，走 homework_questions 取满分）。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # manual_grade 自己开 session；commit_grade 内部开的是 grading_graph 的
    monkeypatch.setattr(mg, "get_session_factory", lambda: factory)
    import app.graphs.teacher.grading_graph as gg

    monkeypatch.setattr(gg, "get_session_factory", lambda: factory)

    s = factory()
    s.add_all([
        Chapter(id="ch_q", name="一元二次方程", subject="math", grade="初三"),
        KnowledgePoint(id="kp_x", name="配方法", chapter_id="ch_q", subject="math"),
        User(id=1, username="t1", password_hash="x", name="王老师"),
        User(id=2, username="s1", password_hash="x", name="张三"),
        Class(id=1, grade="初三", name="1班"),
    ])
    s.flush()
    s.add(Student(id=11, user_id=2, class_id=1, student_no="2026001"))
    s.add(Question(id=21, subject="math", chapter_id="ch_q", kp_id="kp_x",
                   question_type="subjective", difficulty="medium",
                   content="解方程 x²-5x+6=0", answer="x=2 或 x=3", analysis="三条采分点"))
    s.flush()
    s.add(Homework(id=31, teacher_id=1, class_id=1, title="练习", kp_ids='["kp_x"]'))
    s.flush()
    s.add(HomeworkQuestion(homework_id=31, question_id=21, score=4.0))
    s.add(StudentAnswer(id=41, source_type="homework", source_id=31, question_id=21,
                        student_id=11, content="x=2"))
    s.commit()

    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def stubs(monkeypatch):
    """把归因与 BKT 换成记录型假实现，返回调用记录供断言。"""
    calls = {"error": 0, "mastery": []}

    def fake_attribute(state):
        calls["error"] += 1
        return {"error_type": "概念不清"}

    def fake_mastery(state):
        calls["mastery"].append({"is_correct": state["is_correct"],
                                 "error_type": state.get("error_type")})
        return {"p_known_before": 0.10, "p_known_after": 0.16}

    monkeypatch.setattr(mg, "attribute_error", fake_attribute)
    monkeypatch.setattr(mg, "update_mastery_node", fake_mastery)
    return calls


# =====================================================================
# 纯函数：三档换算
# =====================================================================
@pytest.mark.parametrize("judgement,score,expect", [
    ("correct", None, (4.0, True)),
    ("wrong", None, (0.0, False)),
    ("partial", 3.0, (3.0, True)),    # 75% ≥ 60% → 算答对
    ("partial", 2.0, (2.0, False)),   # 50% < 60% → 算答错
    ("partial", 1.0, (1.0, False)),
])
def test_resolve_score(judgement, score, expect):
    assert mg.resolve_score(judgement, score, 4.0) == expect


def test_resolve_score_partial_needs_score():
    with pytest.raises(mg.ManualGradeError, match="必须给分数"):
        mg.resolve_score("partial", None, 4.0)


@pytest.mark.parametrize("bad", [0, -1, 4, 5])
def test_resolve_score_partial_out_of_range(bad):
    with pytest.raises(mg.ManualGradeError):
        mg.resolve_score("partial", bad, 4.0)


def test_resolve_score_unknown_judgement():
    with pytest.raises(mg.ManualGradeError, match="未知的批改结论"):
        mg.resolve_score("half", 1, 4.0)


# =====================================================================
# 落库：三档各走一遍
# =====================================================================
def test_wrong_gives_zero_and_attributes_error(db, stubs):
    """判错 → 0 分、is_correct False、grader_type=teacher、触发归因。"""
    out = mg.manual_grade(41, "wrong", teacher_user_id=1)

    assert out["final_score"] == 0.0
    assert out["is_correct"] is False
    assert out["full_score"] == 4.0
    assert out["grader_type"] == "teacher"

    answer = db.get(StudentAnswer, 41)
    db.refresh(answer)
    assert answer.score == 0.0
    assert answer.is_correct is False

    rec = db.execute(select(GradingRecord).where(GradingRecord.answer_id == 41)).scalar_one()
    assert rec.grader_type == "teacher", "人工判的分不能标成 llm"
    assert rec.status == "confirmed"
    assert rec.final_score == 0.0
    assert rec.ai_suggested_score is None, "人工批改没有 AI 建议，这列必须是空"

    assert stubs["error"] == 1, "判错必须落错因（S-10 诊断测验要靠它取错题）"
    assert stubs["mastery"][0]["error_type"] == "概念不清", "错因要传给 BKT 替换 slip"


def test_correct_gives_full_score(db, stubs):
    out = mg.manual_grade(41, "correct", teacher_user_id=1)

    assert out["final_score"] == 4.0
    assert out["is_correct"] is True
    assert db.get(StudentAnswer, 41).score == 4.0
    assert stubs["error"] == 0, "答对不该跑错因归因"


def test_partial_gives_the_score_teacher_typed(db, stubs):
    """满分 4 分，部分正确给 1 分 —— 用户点名的场景。"""
    out = mg.manual_grade(41, "partial", score=1, teacher_user_id=1)

    assert out["final_score"] == 1.0
    assert out["is_correct"] is False       # 25% < 60%
    assert db.get(StudentAnswer, 41).score == 1.0

    rec = db.execute(select(GradingRecord).where(GradingRecord.answer_id == 41)).scalar_one()
    assert rec.final_score == 1.0
    assert "部分正确" in (rec.final_comment or "")


def test_partial_above_pass_rate_counts_correct(db, stubs):
    out = mg.manual_grade(41, "partial", score=3, teacher_user_id=1)
    assert out["final_score"] == 3.0
    assert out["is_correct"] is True
    assert stubs["error"] == 0


def test_manual_grade_is_idempotent(db, stubs):
    """同一作答第二次批改直接报错，不重复写 BKT。"""
    mg.manual_grade(41, "wrong", teacher_user_id=1)

    with pytest.raises(mg.ManualGradeError, match="已经批过"):
        mg.manual_grade(41, "correct", teacher_user_id=1)

    assert len(stubs["mastery"]) == 1
    assert db.execute(select(GradingRecord).where(GradingRecord.answer_id == 41)).scalars().all().__len__() == 1


def test_missing_answer_raises(db, stubs):
    with pytest.raises(mg.ManualGradeError, match="不存在"):
        mg.manual_grade(999, "correct", teacher_user_id=1)


def test_audit_written(db, stubs):
    """人工给分是敏感操作，必须留痕。"""
    mg.manual_grade(41, "partial", score=2, teacher_user_id=1)
    logs = db.execute(select(AuditLog)).scalars().all()
    assert any("student_answers:41" in (lg.target or "") for lg in logs)
