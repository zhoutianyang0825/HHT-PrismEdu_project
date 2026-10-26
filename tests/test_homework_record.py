"""作业作答落库单测：钉住"学生做完作业 → 老师端查得到"这条链。

 得到的数据：SQLite 内存库 + 1 份作业（2 题，每题 10 分）+ 1 个学生
 要做的操作：跑来源推导 / 对错都落 / 幂等 / 作业分值 / 主观题待批改 五类用例
 返回：断言通过的用例
 为什么：
     root cause 是前端从来不带 homework_id、后端只把答错记成 chat 来源，
     于是老师端「作业提交情况」按 (source_type='homework', source_id=作业id)
     永远查不到——学生做完了，老师那边一片空白。
     这条链断过一次，必须钉死。
 跑法：uv run pytest tests/test_homework_record.py -v
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.services.wrong_record as wr
from app.models import (
    Base,
    Chapter,
    ErrorLog,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
    User,
)


HOMEWORK_ID = 6


@pytest.fixture()
def db(monkeypatch):
    """建 SQLite 内存库，把 wrong_record 的 session 工厂指过去。

    与 test_wrong_record.py 的区别：多造一份作业（homework + homework_questions），
    用来验证"带作业上下文"这条分支。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(wr, "get_session_factory", lambda: factory)

    # BKT 换成可控桩：这里只验"作答落库 + 掌握度方向"，不验 BKT 公式
    monkeypatch.setattr(wr, "get_bkt_state", lambda sid, kp: (0.5, 1.0))
    synced = []
    monkeypatch.setattr(wr, "sync_mastery", lambda sid, kp, p: synced.append(p))

    s = factory()
    s.add(Chapter(id="ch_quadratic", name="一元二次方程", subject="math", grade="初三"))
    s.add(KnowledgePoint(id="kp_peifang", name="配方法",
                         chapter_id="ch_quadratic", subject="math"))
    s.add(User(username="s1", password_hash="x", name="张三", is_active=True))
    s.add(Student(id=1, user_id=1, class_id=1, student_no="S001"))
    s.flush()

    # 两道客观题 + 一道主观题，作业里每题 10 分
    s.add(Question(id=1, subject="math", chapter_id="ch_quadratic", kp_id="kp_peifang",
                   question_type="fill", difficulty="easy",
                   content="解方程 x²-6x+5=0", answer="x1=2，x2=3",
                   analysis="配方得 (x-3)²=4"))
    s.add(Question(id=2, subject="math", chapter_id="ch_quadratic", kp_id="kp_peifang",
                   question_type="fill", difficulty="easy",
                   content="解方程 (x-1)²=4", answer="x1=3，x2=-1",
                   analysis="开平方"))
    s.add(Question(id=3, subject="math", chapter_id="ch_quadratic", kp_id="kp_peifang",
                   question_type="subjective", difficulty="medium",
                   content="写出配方法的完整步骤", answer="略", analysis="略"))
    s.flush()

    s.add(Homework(id=HOMEWORK_ID, teacher_id=1, class_id=1,
                   title="随堂作业", kp_ids='["kp_peifang"]'))
    s.flush()
    for qid in (1, 2, 3):
        s.add(HomeworkQuestion(homework_id=HOMEWORK_ID, question_id=qid, score=10.0))
    s.commit()

    yield s, factory, synced

    s.close()
    engine.dispose()


def _q(s, qid: int = 1) -> Question:
    """取测试题（每次重查，避免拿到过期对象）。"""
    return s.get(Question, qid)


# =====================================================================
# 来源推导：有作业 → homework，没有 → chat
# =====================================================================
def test_answer_with_homework_lands_as_homework_source(db):
    """★ 核心用例：带作业 id 作答 → 落 homework 来源，老师端才查得到。"""
    s, _, _ = db
    wr.record_answer(student_id=1, question=_q(s), student_answer="x=5",
                     is_correct=False, score=0.0, homework_id=HOMEWORK_ID)

    row = s.execute(select(StudentAnswer)).scalar_one()
    assert row.source_type == "homework"
    assert row.source_id == HOMEWORK_ID
    assert row.is_correct is False


def test_answer_without_homework_lands_as_chat_source(db):
    """自由练习（没作业上下文）→ 落 chat 来源，只给诊断测验供数据。"""
    s, _, _ = db
    wr.record_answer(student_id=1, question=_q(s), student_answer="x=5",
                     is_correct=False, score=0.0)

    row = s.execute(select(StudentAnswer)).scalar_one()
    assert row.source_type == "chat"
    assert row.source_id == 0


# =====================================================================
# 对错都落：老师端要看每题每人的作答，不只是错题
# =====================================================================
def test_correct_answer_is_also_recorded(db):
    """答对也要落库——老师端看的是"这道题他答了什么"，不是只看错题。"""
    s, _, _ = db
    wr.record_answer(student_id=1, question=_q(s), student_answer="x1=2，x2=3",
                     is_correct=True, score=10.0, homework_id=HOMEWORK_ID)

    row = s.execute(select(StudentAnswer)).scalar_one()
    assert row.is_correct is True
    assert row.score == 10.0


def test_correct_answer_does_not_create_error_log(db):
    """答对不写错因：error_logs 只记错题，答对也写会污染错因统计。"""
    s, _, _ = db
    wr.record_answer(student_id=1, question=_q(s), student_answer="x1=2，x2=3",
                     is_correct=True, score=10.0, homework_id=HOMEWORK_ID)

    assert s.execute(select(ErrorLog)).first() is None


def test_wrong_answer_creates_error_log(db):
    """答错 → 一条错因（诊断测验靠它 JOIN 取知识点）。"""
    s, _, _ = db
    wr.record_answer(student_id=1, question=_q(s), student_answer="x=5",
                     is_correct=False, score=0.0, homework_id=HOMEWORK_ID)

    log = s.execute(select(ErrorLog)).scalar_one()
    assert log.error_type in ("粗心", "方法不会")
    assert "x=5" in log.evidence


# =====================================================================
# 幂等：首次提交定结果
# =====================================================================
def test_same_question_same_homework_is_idempotent(db):
    """★ 同题同作业第二次提交不新增：引导后改对，不能把首次的错题刷成对的。"""
    s, _, _ = db
    first = wr.record_answer(student_id=1, question=_q(s), student_answer="x=5",
                             is_correct=False, score=0.0, homework_id=HOMEWORK_ID)
    second = wr.record_answer(student_id=1, question=_q(s), student_answer="x1=2，x2=3",
                              is_correct=True, score=10.0, homework_id=HOMEWORK_ID)

    assert first is not None
    assert second is None
    assert len(s.execute(select(StudentAnswer)).scalars().all()) == 1

    row = s.execute(select(StudentAnswer)).scalar_one()
    assert row.is_correct is False        # 首次结果锁定
    assert row.content == "x=5"


def test_same_question_different_homework_are_separate(db):
    """同一道题出现在两份作业里 → 两条独立记录（各算各的作业）。"""
    s, _, _ = db
    other = Homework(id=7, teacher_id=1, class_id=1, title="另一份", kp_ids='["kp_peifang"]')
    s.add(other)
    s.commit()

    wr.record_answer(student_id=1, question=_q(s), student_answer="x=5",
                     is_correct=False, score=0.0, homework_id=HOMEWORK_ID)
    wr.record_answer(student_id=1, question=_q(s), student_answer="x=5",
                     is_correct=False, score=0.0, homework_id=7)

    assert len(s.execute(select(StudentAnswer)).scalars().all()) == 2


# =====================================================================
# 主观题：落库但待批改
# =====================================================================
def test_subjective_answer_recorded_as_pending(db):
    """主观题 is_correct=None 落库：老师端要看到"交了、待批改"，
    否则主观题作业在老师端永远显示成"没交"。"""
    s, _, synced = db
    wr.record_answer(student_id=1, question=_q(s, 3), student_answer="先移项再配方",
                     is_correct=None, score=None, homework_id=HOMEWORK_ID)

    row = s.execute(select(StudentAnswer)).scalar_one()
    assert row.is_correct is None
    assert row.score is None
    assert synced == []          # 没判分就不该动掌握度


# =====================================================================
# 满分取值：作业读每题真分值，自由练习按百分制
# =====================================================================
def test_full_score_reads_homework_question_score(db):
    """作业题每题 10 分 → 满分就是 10，不是 100（否则老师端得分列显示 100）。"""
    s, _, _ = db
    assert wr.record_answer_full_score(HOMEWORK_ID, 1) == 10.0


def test_full_score_defaults_to_100_without_homework(db):
    """自由练习没有作业语境 → 按百分制。"""
    s, _, _ = db
    assert wr.record_answer_full_score(None, 1) == 100.0


def test_full_score_falls_back_when_question_not_in_homework(db):
    """题目不在这份作业里（异常调用）→ 退回百分制，不抛异常。"""
    s, _, _ = db
    assert wr.record_answer_full_score(HOMEWORK_ID, 999) == 100.0


# =====================================================================
# BKT：答对要涨，答错要跌
# =====================================================================
def test_correct_answer_raises_mastery(db):
    """★ 答对必须提升掌握度——原来只有答错才更新，掌握度只会单调下降。"""
    s, _, synced = db
    wr.record_answer(student_id=1, question=_q(s), student_answer="x1=2，x2=3",
                     is_correct=True, score=10.0, homework_id=HOMEWORK_ID)

    assert synced and synced[0] > 0.5


def test_wrong_answer_lowers_mastery(db):
    """答错 → 掌握度下降。"""
    s, _, synced = db
    wr.record_answer(student_id=1, question=_q(s), student_answer="x=5",
                     is_correct=False, score=0.0, homework_id=HOMEWORK_ID)

    assert synced and synced[0] < 0.5


# =====================================================================
# grade_submit：作业上下文从 config / state 进来
# =====================================================================
def test_grade_submit_reads_homework_id_from_config(monkeypatch):
    """★ grade_submit 优先读 config.configurable.homework_id。

    为什么不能只读 state：图挂在 interrupt 上时 resume 走 Command(resume=...)，
    不带任何 state 更新，只有 config 里这份是本次请求的新鲜值。
    """
    import app.graphs.student.nodes as nodes

    class _Q:
        id = 1
        question_type = "fill"
        answer = "x1=2，x2=3"
        kp_id = "kp_peifang"
        content = "解方程 x²-6x+5=0"

    captured = {}

    monkeypatch.setattr(nodes, "_fetch_question", lambda qid: _Q())
    monkeypatch.setattr(nodes, "_last_human_text", lambda st: "x1=2，x2=3")
    monkeypatch.setattr(nodes, "record_answer_full_score", lambda hid, qid: 10.0)

    def fake_record(**kw):
        captured.update(kw)
        return 1

    monkeypatch.setattr(nodes, "record_answer", fake_record)

    state = {"student_id": 1, "question_id": 1, "messages": [],
             "homework_id": 999}          # state 里是旧值
    config = {"configurable": {"homework_id": HOMEWORK_ID}}

    out = nodes.grade_submit(state, config)

    assert out["is_correct"] is True
    assert captured["homework_id"] == HOMEWORK_ID      # config 覆盖 state
    assert captured["score"] == 10.0


def test_grade_submit_falls_back_to_state_without_config(monkeypatch):
    """只给 state（测试/旧调用点直接调函数）也能拿到作业上下文。"""
    import app.graphs.student.nodes as nodes

    class _Q:
        id = 1
        question_type = "fill"
        answer = "x1=2，x2=3"
        kp_id = "kp_peifang"
        content = "解方程 x²-6x+5=0"

    captured = {}

    monkeypatch.setattr(nodes, "_fetch_question", lambda qid: _Q())
    monkeypatch.setattr(nodes, "_last_human_text", lambda st: "x1=2，x2=3")
    monkeypatch.setattr(nodes, "record_answer_full_score", lambda hid, qid: 10.0)
    monkeypatch.setattr(nodes, "record_answer", lambda **kw: captured.update(kw))

    nodes.grade_submit({"student_id": 1, "question_id": 1, "messages": [],
                        "homework_id": HOMEWORK_ID})

    assert captured["homework_id"] == HOMEWORK_ID
