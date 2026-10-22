"""M3 回归 · 批改落库路径（commit_grade）单测。

 得到的数据：SQLite 内存库 + 一份最小批改 state（不调 LLM、不连 PG）
 要做的操作：分别跑 reject / reject→confirm / 重复 confirm / 漏写 status 四条路径
 返回：断言通过的用例；关键断言是"驳回不回填分数"和"漏写 status 不 KeyError"
 为什么：reject 分支曾因 state 里没有 status 直接 KeyError、整条链路从没跑通过，
         这四条用例把它钉死；跑法：uv run pytest tests/test_grading_graph.py -v
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.graphs.teacher.grading_graph as gg
from app.models import (
    AuditLog,
    Base,
    Chapter,
    Class,
    GradingRecord,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
    User,
)


@pytest.fixture()
def db(monkeypatch):
    """建一个 SQLite 内存库，并把模块里的 get_session_factory 指过去。

    得到的数据：monkeypatch（pytest 内置，用例结束自动还原）
    要做的操作：建库建表 → 造最小数据（1 用户/1 班/1 学生/1 题/2 条作答）
    返回：(Session, sessionmaker) —— 用例用前者查库、用后者开新 session 验证提交结果
    为什么：commit_grade 内部自己开 session，必须让它连到测试库而不是真 PG
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(gg, "get_session_factory", lambda: factory)

    s = factory()
    s.add(Chapter(id="ch_quadratic", name="一元二次方程", subject="math", grade="初三"))
    s.add(KnowledgePoint(id="kp_crossmult", name="十字相乘法",
                         chapter_id="ch_quadratic", subject="math"))
    s.add(User(username="t1", password_hash="x", name="王老师", is_active=True))
    s.add(Class(grade="初三", name="2班"))
    s.flush()
    s.add(Student(user_id=1, class_id=1))
    s.flush()
    s.add(Question(subject="math", chapter_id="ch_quadratic", kp_id="kp_crossmult",
                   question_type="subjective", difficulty="medium",
                   content="解方程 x²-5x+6=0", answer="x=2 或 x=3", analysis="采分点三条"))
    s.flush()
    s.add(StudentAnswer(source_type="exam", source_id=0, question_id=1,
                        student_id=1, content="x=2"))
    s.add(StudentAnswer(source_type="exam", source_id=0, question_id=1,
                        student_id=1, content="x=3"))
    s.commit()

    yield s, factory

    s.close()
    engine.dispose()


def make_state(answer_id: int, **overrides) -> dict:
    """造一份批改 state（只保留 commit_grade 需要的字段）。

    得到的数据：answer_id；overrides 覆盖任意字段（如 status / teacher_decision）
    要做的操作：拼一个普通 dict 当 GradingState 用
    返回：state dict，形如 {"answer_id": 1, "status": "rejected", ...}
    为什么：commit_grade 只读 dict 的键，不需要真跑图就能测落库逻辑
    """
    base = {
        "answer_id": answer_id,
        "teacher_user_id": 1,
        "question_id": 1,
        "student_id": 1,
        "kp_id": "kp_crossmult",
        "question_type": "subjective",
        "question_content": "解方程 x²-5x+6=0",
        "student_answer": "x=2",
        "standard_answer": "x=2 或 x=3",
        "reference_analysis": "采分点三条",
        "full_score": 100.0,
        "ai_score": 50.0,
        "ai_comment": "答对一半",
        "ai_scoring_basis": ["少一个根"],
        "ai_confidence": 0.8,
    }
    base.update(overrides)
    return base


def _answer_ids(db_tuple) -> list[int]:
    """取两条作答的 id（按插入顺序）。"""
    s, _ = db_tuple
    return list(s.execute(select(StudentAnswer.id).order_by(StudentAnswer.id)).scalars())


def test_reject_writes_record_but_keeps_score_null(db):
    """路径 1：驳回 → 写 rejected 记录，但绝不回填分数。

    得到的数据：一条作答 + status=rejected / final_score=None 的 state
    要做的操作：调 commit_grade
    返回：无返回，断言记录落库、is_correct 与 score 仍为 NULL
    为什么：is_correct 保持 NULL 是"待批队列还能再看到它"的唯一依据，
            一旦被驳回时写了 False，这道题就永远从待批列表消失了
    """
    aid = _answer_ids(db)[0]
    out = gg.commit_grade(make_state(
        aid, teacher_decision={"action": "reject"},
        status="rejected", final_score=None, final_comment=None, is_correct=None,
    ))

    s, factory = db
    s.expire_all()
    answer = s.get(StudentAnswer, aid)
    record = s.execute(select(GradingRecord).where(GradingRecord.answer_id == aid)).scalar_one()

    assert out == {"status": "rejected", "final_score": None}
    assert record.status == "rejected" and record.final_score is None
    assert answer.is_correct is None and answer.score is None


def test_reject_then_confirm_closes_loop(db):
    """路径 2：先驳回、再确认 → 两条记录共存，分数最终生效。

    得到的数据：同一条作答，先跑 reject 再跑 confirm
    要做的操作：连续调两次 commit_grade
    返回：无返回，断言记录序列为 [rejected, confirmed] 且分数已回填
    为什么：验证"驳回不会锁死题目"，也验证幂等查询只认 confirmed（否则第二次会被挡）
    """
    aid = _answer_ids(db)[0]
    gg.commit_grade(make_state(
        aid, teacher_decision={"action": "reject"},
        status="rejected", final_score=None, final_comment=None, is_correct=None,
    ))
    gg.commit_grade(make_state(
        aid, teacher_decision={"action": "confirm"},
        status="confirmed", final_score=50.0, final_comment="采纳", is_correct=True,
    ))

    s, _ = db
    s.expire_all()
    statuses = list(s.execute(
        select(GradingRecord.status).where(GradingRecord.answer_id == aid)
        .order_by(GradingRecord.id)
    ).scalars())
    answer = s.get(StudentAnswer, aid)

    assert statuses == ["rejected", "confirmed"]
    assert answer.is_correct is True and answer.score == 50.0


def test_confirm_is_idempotent(db):
    """路径 3：连点两次确认 → 只留一条 confirmed，不重复写。

    得到的数据：同一条作答，confirm 跑两次
    要做的操作：连续调两次 commit_grade（参数完全相同）
    返回：无返回，断言记录数不增加
    为什么：老师手抖双击很常见；重复写会连带把 BKT 更新两遍，掌握度被"刷"高
    """
    aid = _answer_ids(db)[0]
    state = make_state(aid, teacher_decision={"action": "confirm"},
                       status="confirmed", final_score=50.0,
                       final_comment="采纳", is_correct=True)
    gg.commit_grade(state)
    gg.commit_grade(state)

    s, _ = db
    records = s.execute(
        select(GradingRecord).where(GradingRecord.answer_id == aid)
    ).scalars().all()

    assert len(records) == 1


def test_missing_status_falls_back_to_reject(db):
    """路径 4：state 里没有 status → 按老师决定兜底，不抛 KeyError。

    得到的数据：state 不含 status 键，teacher_decision 为 reject
    要做的操作：调 commit_grade（修复前这里会 KeyError: 'status'）
    返回：无返回，断言兜底成 rejected 并正常落库
    为什么：LangGraph 不会给"声明过但从未赋值"的 channel 填默认值，
            读 state["status"] 直接 KeyError；兜底是最后一道防线
    """
    aid = _answer_ids(db)[1]
    state = make_state(aid, teacher_decision={"action": "reject"},
                       final_score=None, final_comment=None, is_correct=None)
    assert "status" not in state

    out = gg.commit_grade(state)

    s, _ = db
    s.expire_all()
    record = s.execute(
        select(GradingRecord).where(GradingRecord.answer_id == aid)
    ).scalar_one()
    answer = s.get(StudentAnswer, aid)

    assert out["status"] == "rejected"
    assert record.status == "rejected"
    assert answer.is_correct is None


def test_reject_writes_audit_log(db):
    """附带：驳回也要留审计（PRD 2.4 敏感操作留痕）。

    得到的数据：一条作答 + reject state
    要做的操作：调 commit_grade 后查 audit_logs
    返回：无返回，断言存在 grading.reject 记录且 target 指向该作答
    为什么：驳回是敏感操作，没有审计就无法追责，也无法统计 AI 建议被否率
    """
    aid = _answer_ids(db)[0]
    gg.commit_grade(make_state(
        aid, teacher_decision={"action": "reject"},
        status="rejected", final_score=None, final_comment=None, is_correct=None,
    ))

    s, _ = db
    logs = s.execute(select(AuditLog)).scalars().all()
    assert any(
        log.action == "grading.reject" and log.target == f"student_answers:{aid}"
        for log in logs
    )
