"""M7 考试派发域单测：定稿推送 → 学生交卷 → 老师按题分组 → 总分回写。

【这组测试钉住的是什么】
  老版 /exam/{id}/assign 只写 audit_logs 就返回，定稿之后的链路是断的：
  学生端查不到卷、老师端收不到主观题、R-04 拿不到作答。
  下面 8 个用例逐段钉住新链路，任何一段被改坏都会红。

【为什么不走 HTTP】
  守卫（谁能看谁能考）由 tests/test_scope_matrix.py 用真路由覆盖；
  这里只测"数据怎么流"，所以直接调 services 层，用 sqlite 沙盒。
  交卷的客观题判分依赖批改图（要 LLM/checkpointer），因此这里的手写作答
  直接给出 is_correct/score，模拟"批改图已经跑完"的库内状态。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import (
    Class,
    ExamAttempt,
    ExamPaper,
    ExamPaperClass,
    ExamPaperQuestion,
    Question,
    Student,
    StudentAnswer,
    User,
)
from app.services import exam_delivery as ed


# =====================================================================
# 夹具：一份 4 题卷（2 选 1 填 1 解） + 一个班 + 一个学生
# =====================================================================
@pytest.fixture()
def scene(session):
    """造最小场景：1 个班 2 个学生，一份卷 4 题（含 1 道解答题）。"""
    session.add_all([
        Class(id=1, grade="初二", name="1班"),
        User(id=101, username="S001", password_hash="x", name="张三"),
        User(id=102, username="S002", password_hash="x", name="李四"),
        Student(id=201, user_id=101, class_id=1, student_no="2026001"),
        Student(id=202, user_id=102, class_id=1, student_no="2026002"),
    ])
    session.flush()

    # 题目：故意按"解答题在前"给出，验证卷面会重排成 选择→填空→解答
    session.add_all([
        Question(id=301, chapter_id="ch_x", kp_id="kp_x", question_type="subjective",
                 difficulty="hard", content="解方程并写出过程", answer="x=1",
                 analysis="移项后因式分解"),
        Question(id=302, chapter_id="ch_x", kp_id="kp_x", question_type="choice",
                 difficulty="easy", content="选一个", answer="A",
                 options='["A. 1", "B. 2"]'),
        Question(id=303, chapter_id="ch_x", kp_id="kp_x", question_type="fill",
                 difficulty="easy", content="填一个", answer="x=2"),
        Question(id=304, chapter_id="ch_x", kp_id="kp_x", question_type="choice",
                 difficulty="medium", content="再选一个", answer="B",
                 options='["A. 1", "B. 2"]'),
    ])
    session.flush()

    paper = ExamPaper(
        id=401, title="初二数学月考卷", grade="初二", subject="math",
        exam_type="monthly", status="draft",
        quota_plan_json={
            "plan": {"exam_type": "monthly", "total_score": 100, "items": []},
            # 抽题顺序是乱的（解答题排第一），卷面必须重排
            "question_ids": [301, 302, 303, 304],
            "gaps": [],
        },
    )
    session.add(paper)
    session.commit()
    return session


# =====================================================================
# 1. 分值分账
# =====================================================================
def test_allocate_scores_sums_to_total():
    """4 题（2选1填1解）分摊 100 分，必须严格等于 100 且每题 ≥ 1 分。"""
    scores = ed.allocate_scores(["choice", "choice", "fill", "subjective"], 100)

    assert len(scores) == 4
    assert sum(scores) == 100
    assert all(s >= 1 for s in scores)
    # 解答题权重最高 → 分最多
    assert max(scores) == scores[3]


def test_allocate_scores_handles_empty():
    assert ed.allocate_scores([], 100) == []


# =====================================================================
# 2. 卷面固化（幂等 + 排序 + 分值落库）
# =====================================================================
def test_ensure_paper_questions_idempotent_and_ordered(scene):
    """第一次固化按 选择→填空→解答 排；第二次调用不再重新分（幂等）。"""
    paper = scene.get(ExamPaper, 401)

    rows = ed.ensure_paper_questions(scene, paper)
    assert [r.question_id for r in rows] == [302, 304, 303, 301]
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert sum(r.score for r in rows) == 100

    before = [r.score for r in rows]
    again = ed.ensure_paper_questions(scene, paper)
    assert [r.score for r in again] == before, "重复定稿不该重算分值"


# =====================================================================
# 3. 定稿推送
# =====================================================================
def test_publish_paper_writes_classes_and_impact(scene):
    """定稿写 exam_paper_classes，并回出"几个班/几个学生/几道题"。"""
    paper = scene.get(ExamPaper, 401)

    out = ed.publish_paper(scene, paper, [1])
    scene.commit()

    assert out["status"] == "published"
    assert out["question_count"] == 4
    assert out["full_score"] == 100
    assert out["student_count"] == 2
    assert out["classes"][0]["name"] == "1班"
    assert paper.published_at is not None

    pushed = scene.query(ExamPaperClass).filter_by(paper_id=401).all()
    assert [p.class_id for p in pushed] == [1]

    # 幂等：同一批班再推一次不会插重复行
    ed.publish_paper(scene, paper, [1])
    scene.commit()
    assert scene.query(ExamPaperClass).filter_by(paper_id=401).count() == 1


def test_publish_paper_rejects_unknown_class(scene):
    paper = scene.get(ExamPaper, 401)
    with pytest.raises(ValueError, match="班级不存在"):
        ed.publish_paper(scene, paper, [999])


# =====================================================================
# 4. 学生取卷
# =====================================================================
def test_student_papers_only_sees_pushed(scene):
    """没推送 → 列表空；推送后 → 列表有这份卷且 attempt 为空。"""
    student = scene.get(Student, 201)
    paper = scene.get(ExamPaper, 401)

    assert ed.student_papers(scene, student) == []
    assert ed.student_can_take(scene, paper, student) is False

    ed.publish_paper(scene, paper, [1])
    scene.commit()

    items = ed.student_papers(scene, student)
    assert len(items) == 1
    assert items[0]["paper_id"] == 401
    assert items[0]["question_count"] == 4
    assert items[0]["attempt"] is None
    assert ed.student_can_take(scene, paper, student) is True


def test_exam_detail_hides_answers(scene):
    """取卷绝不能带标准答案/解析（泄题），但要带每题分值与题号。"""
    paper = scene.get(ExamPaper, 401)
    ed.publish_paper(scene, paper, [1])
    scene.commit()

    data = ed.exam_detail(scene, paper, scene.get(Student, 201))
    q = data["questions"][0]

    assert "answer" not in q and "analysis" not in q
    assert q["seq"] == 1 and q["score"] > 0
    assert data["full_score"] == 100


# =====================================================================
# 5. 交卷落库 + 考试记录
# =====================================================================
class _Ans:
    """模拟 ExamAnswerItem（服务层只读 question_id/content/attachments）。"""

    def __init__(self, question_id, content="", attachments=None):
        self.question_id = question_id
        self.content = content
        self.attachments = attachments or []


def test_save_exam_answers_fills_missing_objective(scene):
    """漏答的客观题补空行（判 0 分），主观题不补（不占老师队列）。"""
    paper = scene.get(ExamPaper, 401)
    ed.publish_paper(scene, paper, [1])
    scene.commit()

    student = scene.get(Student, 201)
    ids = ed.save_exam_answers(scene, paper, student, [
        _Ans(302, "A"),
        _Ans(303, "x=2", ["/uploads/answer/202610/a.jpg"]),
    ])
    scene.commit()

    assert set(ids) == {302, 303, 304}      # 301（主观题）没作答 → 不建行
    rows = {a.question_id: a for a in
            scene.query(StudentAnswer).filter_by(source_type="exam", source_id=401).all()}
    assert rows[304].content == ""           # 漏答的客观题补了空行
    assert rows[303].attachment_json == ["/uploads/answer/202610/a.jpg"]


def test_create_attempt_rejects_duplicate(scene):
    paper = scene.get(ExamPaper, 401)
    ed.publish_paper(scene, paper, [1])
    scene.commit()
    student = scene.get(Student, 201)

    first = ed.create_attempt(scene, paper, student, 60.0, 100.0, subjective_pending=1)
    scene.commit()
    assert first.status == "submitted"       # 还有主观题待批

    with pytest.raises(ValueError, match="已经交过"):
        ed.create_attempt(scene, paper, student, 60.0, 100.0, subjective_pending=0)


def test_create_attempt_all_objective_is_graded(scene):
    """纯客观卷（无主观题）交卷即 graded。"""
    paper = scene.get(ExamPaper, 401)
    ed.publish_paper(scene, paper, [1])
    scene.commit()

    row = ed.create_attempt(scene, paper, scene.get(Student, 202), 88.0, 100.0,
                            subjective_pending=0)
    assert row.status == "graded"
    assert row.total_score == 88.0


# =====================================================================
# 6. 总分回写（老师批完主观题）
# =====================================================================
def test_recalc_attempt_after_subjective_graded(scene):
    """主观题批完 → 主观分并入总分，状态转 graded。"""
    paper = scene.get(ExamPaper, 401)
    ed.publish_paper(scene, paper, [1])
    scene.commit()
    student = scene.get(Student, 201)

    # 客观题：302 对（25 分），304 错（0），303 对（25 分）；主观题 301 待批（50 分）
    scene.add_all([
        StudentAnswer(source_type="exam", source_id=401, question_id=302,
                      student_id=201, content="A", is_correct=True, score=25.0),
        StudentAnswer(source_type="exam", source_id=401, question_id=304,
                      student_id=201, content="A", is_correct=False, score=0.0),
        StudentAnswer(source_type="exam", source_id=401, question_id=303,
                      student_id=201, content="x=2", is_correct=True, score=25.0),
        StudentAnswer(source_type="exam", source_id=401, question_id=301,
                      student_id=201, content="移项得 x=1", is_correct=None, score=None),
    ])
    ed.create_attempt(scene, paper, student, 50.0, 100.0, subjective_pending=1)
    scene.commit()

    attempt = ed.recalc_attempt(scene, 401, 201)
    scene.commit()
    assert attempt.status == "submitted"
    assert attempt.objective_score == 50.0
    assert attempt.subjective_score == 0.0
    assert attempt.total_score == 50.0

    # 老师给主观题 40 分 → 总分 90，全卷判完
    subj = scene.query(StudentAnswer).filter_by(
        source_type="exam", source_id=401, question_id=301).one()
    subj.is_correct = True
    subj.score = 40.0
    scene.flush()

    attempt = ed.recalc_attempt(scene, 401, 201)
    scene.commit()
    assert attempt.status == "graded"
    assert attempt.subjective_score == 40.0
    assert attempt.total_score == 90.0
    assert attempt.graded_at is not None


# =====================================================================
# 7. 老师端：卷列表 + 按题分组
# =====================================================================
def test_teacher_papers_counts(scene):
    paper = scene.get(ExamPaper, 401)
    ed.publish_paper(scene, paper, [1])
    scene.add(StudentAnswer(source_type="exam", source_id=401, question_id=301,
                            student_id=201, content="x=1", is_correct=None))
    ed.create_attempt(scene, paper, scene.get(Student, 201), 50.0, 100.0, subjective_pending=1)
    scene.commit()

    rows = ed.teacher_papers(scene, [1])
    assert len(rows) == 1
    assert rows[0]["paper_id"] == 401
    assert rows[0]["student_count"] == 2
    assert rows[0]["submitted_count"] == 1
    assert rows[0]["pending_count"] == 1

    # 只教别的班 → 看不到这份卷
    assert ed.teacher_papers(scene, [99]) == []


def test_group_grading_groups_by_question(scene):
    """按题分组：只有主观题成组，组内挂全班作答。"""
    paper = scene.get(ExamPaper, 401)
    ed.publish_paper(scene, paper, [1])
    scene.add_all([
        StudentAnswer(source_type="exam", source_id=401, question_id=301,
                      student_id=201, content="我的解法一", is_correct=None),
        StudentAnswer(source_type="exam", source_id=401, question_id=301,
                      student_id=202, content="我的解法二", is_correct=True, score=45.0),
        StudentAnswer(source_type="exam", source_id=401, question_id=302,
                      student_id=201, content="A", is_correct=True, score=25.0),
    ])
    scene.commit()

    data = ed.group_grading(scene, paper, [1])

    assert len(data["groups"]) == 1                 # 客观题不成组
    g = data["groups"][0]
    assert g["question_id"] == 301
    assert len(g["answers"]) == 2
    assert g["pending"] == 1 and g["graded"] == 1

    names = {a["student"] for a in g["answers"]}
    assert names == {"张三(2026001)", "李四(2026002)"}
    # 老师要看清题，题干不能截断
    assert g["content"] == "解方程并写出过程"
    assert g["answer"] == "x=1"
