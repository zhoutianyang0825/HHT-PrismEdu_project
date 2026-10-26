"""聊天判错落库（wrong_record）单测：钉住"S-04 答错 → 诊断测验有数据"这条链。

 得到的数据：SQLite 内存库 + 一份最小题目数据（不调 LLM、不连 PG / Neo4j）
 要做的操作：跑首次判错 / 重复判错 / 归因规则 三类用例
 返回：断言通过的用例；关键断言是"两表都落"和"同题不重复落"
 为什么：聊天链路原来一张表都不写，S-10 诊断测验的错题取数是
         error_logs JOIN student_answers JOIN questions，缺任何一行都取不到；
         这条链断过一次（学生做错多少题都不进库），必须钉死
 跑法：uv run pytest tests/test_wrong_record.py -v
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.services.wrong_record as wr
from app.models import (
    Base,
    Chapter,
    ErrorLog,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
    User,
)


@pytest.fixture()
def db(monkeypatch):
    """建 SQLite 内存库，把 wrong_record 里的 session 工厂指过去。

    得到的数据：monkeypatch（pytest 内置，用例结束自动还原）
    要做的操作：建库建表 → 造最小数据（1 用户 / 1 学生 / 1 题）
    返回：(Session, factory)
    为什么：record_chat_wrong 内部自己开 session，必须让它连测试库而不是真 PG
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(wr, "get_session_factory", lambda: factory)

    # BKT 两步在单测里换成 no-op：这里只验"错题落库"，掌握度算法另有 test_mastery_bkt.py
    monkeypatch.setattr(wr, "get_bkt_state", lambda sid, kp: (0.5, 1.0))
    monkeypatch.setattr(wr, "sync_mastery", lambda sid, kp, p: None)

    s = factory()
    s.add(Chapter(id="ch_quadratic", name="一元二次方程", subject="math", grade="初三"))
    s.add(KnowledgePoint(id="kp_peifang", name="配方法",
                         chapter_id="ch_quadratic", subject="math"))
    s.add(User(username="s1", password_hash="x", name="张三", is_active=True))
    s.add(Student(user_id=1, class_id=1, student_no="S001"))
    s.flush()
    s.add(Question(subject="math", chapter_id="ch_quadratic", kp_id="kp_peifang",
                   question_type="fill", difficulty="easy",
                   content="解方程 x²-6x+5=0", answer="x1=2，x2=3",
                   analysis="配方得 (x-3)²=4"))
    s.commit()

    yield s, factory

    s.close()
    engine.dispose()


def _question(s) -> Question:
    """取那道测试题（每条用例都重新查，避免拿着过期对象）。"""
    return s.execute(select(Question)).scalar_one()


# =====================================================================
# 一、落库：判错必须补上两张表
# =====================================================================
def test_first_wrong_writes_both_tables(db):
    """★ 核心用例：首次判错 → student_answers 和 error_logs 各落一行。"""
    s, _ = db
    q = _question(s)

    answer_id = wr.record_chat_wrong(
        student_id=1, question=q, student_answer="x1=5，x2=9", attempt_count=0,
    )
    assert answer_id is not None

    answer = s.execute(select(StudentAnswer)).scalar_one()
    assert answer.id == answer_id
    assert answer.student_id == 1
    assert answer.question_id == q.id
    assert answer.source_type == "chat"
    assert answer.is_correct is False          # 诊断测验正是按 False 取数
    assert answer.score == 0.0
    assert answer.content == "x1=5，x2=9"

    log = s.execute(select(ErrorLog)).scalar_one()
    assert log.answer_id == answer_id          # 三表联查靠这条外键
    assert log.error_type in ("概念不清", "方法不会", "粗心", "审题失误")
    assert log.evidence.startswith("学生作答：")


def test_second_wrong_same_question_does_not_duplicate(db):
    """★ 幂等：同一道题再判错一次，不新增行（否则诊断测验的错题频次虚高）。"""
    s, _ = db
    q = _question(s)

    first = wr.record_chat_wrong(1, q, "x1=5，x2=9", attempt_count=0)
    second = wr.record_chat_wrong(1, q, "x1=1，x2=9", attempt_count=1)

    assert first is not None
    assert second is None                      # 已记过 → 直接返回 None
    assert len(s.execute(select(StudentAnswer)).all()) == 1
    assert len(s.execute(select(ErrorLog)).all()) == 1


def test_different_question_records_separately(db):
    """不同题目的错题要各自成行（幂等的边界：只挡同题，不挡不同题）。"""
    s, factory = db
    q = _question(s)

    s.add(Question(subject="math", chapter_id="ch_quadratic", kp_id="kp_peifang",
                   question_type="fill", difficulty="easy",
                   content="解方程 x²-5x+6=0", answer="x1=2，x2=3", analysis=""))
    s.commit()

    wr.record_chat_wrong(1, q, "x1=5，x2=9")
    q2 = s.execute(select(Question).where(Question.id == 2)).scalar_one()
    wr.record_chat_wrong(1, q2, "x1=7，x2=8")

    assert len(s.execute(select(StudentAnswer)).all()) == 2
    assert len(s.execute(select(ErrorLog)).all()) == 2


# =====================================================================
# 二、归因规则：粗心 vs 方法不会
# =====================================================================
def test_guess_error_type_careless_when_skeleton_matches():
    """骨架相同、只是数值不同 → 粗心。"""
    assert wr.guess_error_type("x1=5，x2=9", "x1=2，x2=3") == "粗心"
    assert wr.guess_error_type("(x-3)^2=4", "(x-3)^2=4") == "粗心"


def test_guess_error_type_method_missing_when_skeleton_differs():
    """骨架不同 → 方法不会（连式子结构都没对上）。"""
    assert wr.guess_error_type("3", "x1=2，x2=3") == "方法不会"
    assert wr.guess_error_type("不知道", "x1=2，x2=3") == "方法不会"


def test_guess_error_type_empty_answer_is_method_missing():
    """空白作答 → 方法不会（连尝试都没有，谈不上粗心）。"""
    assert wr.guess_error_type("", "x1=2，x2=3") == "方法不会"
    assert wr.guess_error_type("   ", "x1=2，x2=3") == "方法不会"


def test_guess_error_type_always_in_enum():
    """归因结果必须落在 error_logs.error_type 的四选一里（否则写库语义就错了）。"""
    samples = [
        ("5/4", "5/4"), ("3", "5/4"), ("", "7；10"),
        ("x_1=3，x_2=2", "x1=2，x2=3"), ("随便写", "x=1"),
    ]
    allowed = {"概念不清", "方法不会", "粗心", "审题失误"}
    for student, standard in samples:
        assert wr.guess_error_type(student, standard) in allowed


# =====================================================================
# 三、端到端：落库后诊断测验的取数 SQL 能查到
# =====================================================================
def test_wrong_answer_is_visible_to_quiz_query(db):
    """★ 落库后，S-10 那条三表联查真能查到这条错题（这是整件事的目的）。

    照抄 quiz_graph.fetch_error_kps 的查询形态（去掉时间窗，单测不控时钟）。
    """
    s, _ = db
    q = _question(s)
    wr.record_chat_wrong(1, q, "x1=5，x2=9")

    rows = s.execute(
        select(Question.kp_id)
        .join(StudentAnswer, StudentAnswer.question_id == Question.id)
        .join(ErrorLog, ErrorLog.answer_id == StudentAnswer.id)
        .where(StudentAnswer.student_id == 1)
        .where(StudentAnswer.is_correct == False)      # noqa: E712 故意用 ==
    ).all()

    assert [r[0] for r in rows] == ["kp_peifang"]
