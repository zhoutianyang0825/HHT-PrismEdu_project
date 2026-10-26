"""学习助手作答落库：判完分就写库，错题额外记错因，并回写 BKT 掌握度。

为什么需要这个文件：
    学生端「学习助手」（S-04）的判分结果原本只写进 LangGraph state 的内存里，
    student_answers 和 error_logs 两张表一行都不写，导致两个功能同时拿不到数据：
      ① S-10 诊断测验：三表联查 error_logs JOIN student_answers JOIN questions
         WHERE is_correct=false → 聊天作答不在库里 → 永远"没有错题记录"；
      ② 老师端 T-03 作业提交情况：查 source_type='homework' AND source_id=作业id
         → 聊天作答全记成 'chat' 且不带作业 id → 老师端永远"暂无作答记录"。

本文件的职责边界：
    只做"把一次判分结果落库"这一件事，判分本身在 services/grading.py，
    错因规则在 guess_error_type，掌握度公式在 services/mastery.py。

两种来源的区别：
    homework（学生从某份作业里点进来作答）→ source_id = 作业 id，老师端按它查
    chat    （学生自由练习，不挂在任何作业上）→ source_id = 0，只给诊断测验供数据
"""
import re

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models import ErrorLog, Question, StudentAnswer
from app.services.bkt_repository import get_bkt_state, sync_mastery
from app.services.grading import normalize
from app.services.mastery import update_mastery


# 聊天作答的来源标记：与 homework / quiz / exam 并列，区别在于聊天不挂在任何来源表上
CHAT_SOURCE_TYPE = "chat"
CHAT_SOURCE_ID = 0              # 聊天不对应作业/测验/试卷记录，占位填 0

# 作业作答的来源标记：source_id 填 homeworks.id，老师端 T-03 按 (类型, id) 查全班作答
HOMEWORK_SOURCE_TYPE = "homework"

# 规则归因的固定置信度：规则比 LLM 粗，如实标低，别假装很准
RULE_CONFIDENCE = 0.3


def _skeleton(text: str) -> str:
    """剥掉所有数字，只留答案的"骨架"。

    得到的数据：
        text（已归一化的答案串，类似 "x=2或x=3" / "5/4" / "3"）

    要做的操作：
        re.sub 把所有数字字符删掉

    返回：
        骨架串，类似 "x=或x=" / "/" / ""（纯数字答案剥完是空串）

    为什么：
        "骨架相同、数值不同"说明方法/结构对了只是算错（粗心），
        "骨架不同"说明方法都没掌握（方法不会）——这是零成本的判据。
    """
    return re.sub(r"\d", "", text)


def guess_error_type(student_answer: str, standard_answer: str) -> str:
    """规则粗归因：在 error_logs 四选一里判「粗心」还是「方法不会」。

    得到的数据：
        student_answer（学生作答原文，类似 "x=2或x=4"）；
        standard_answer（标准答案原文，类似 "x=2或x=3"）

    要做的操作：
        归一化后比"骨架"： 骨架相同 → 粗心； 骨架不同 → 方法不会

    返回：
        "粗心" 或 "方法不会"（error_logs.error_type 的合法枚举值）

    为什么：
        聊天是即时反馈场景，LLM 归因实测要 3.9 秒，学生等不起；
        而诊断测验只把 error_logs 当 JOIN 条件、不读 error_type，
        所以这里用零延迟的规则归因，误差可接受（confidence 已如实标 0.3）。
    """
    s = normalize(student_answer)
    t = normalize(standard_answer)

    if not s:
        return "方法不会"        # 空白作答：连尝试都没有，谈不上粗心

    if s == t:
        return "粗心"            # 理论上判对不会走到这里，保守兜底

    # 骨架相同 = 式子结构对、只是数值算错 = 粗心
    # _skeleton(）：剥掉数字只留骨架
    return "粗心" if _skeleton(s) == _skeleton(t) else "方法不会"


def _source_of(homework_id: int | None) -> tuple[str, int]:
    """推导这次作答的"来源"（source_type, source_id）。

    得到的数据：
        homework_id：作业 id，类似 6；学生自由练习时为 None。

    要做的操作：
        有作业 id → ("homework", homework_id)；否则 → ("chat", 0)。

    返回：
        二元组，形如 ("homework", 6) 或 ("chat", 0)。

    为什么：
        老师端 T-03「作业提交情况」是按 (source_type='homework', source_id=作业id)
        查全班作答的，所以学生从作业里点进来作答时必须带上作业 id，
        否则这条记录在老师端永远查不到——这正是"学生做完了老师那边空白"的根因。
    """
    return (HOMEWORK_SOURCE_TYPE, homework_id) if homework_id else (CHAT_SOURCE_TYPE, CHAT_SOURCE_ID)


def record_answer(
    student_id: int,
    question: Question,
    student_answer: str,
    is_correct: bool | None,
    score: float | None = None,
    homework_id: int | None = None,
    attempt_count: int = 0,
) -> int | None:
    """一次作答落库：student_answers 一条 +（答错时）error_logs 一条 + BKT 回写。

    得到的数据：
        student_id（学生档案 id，students 表主键，不是 users.id）；
        question（题目行，已 detach 的 ORM 对象，只读列）；
        student_answer（学生这次提交的原文，类似 "x=2或x=3"）；
        is_correct（判分结果三态：True 对 / False 错 / None 待老师批改）；
        score（本题得分，作业场景取 homework_questions.score，自由练习按百分制）；
        homework_id（作业 id；None 表示自由练习，落成 chat 来源）；
        attempt_count（引导轮数，S-04 的 attempt_count）。

    要做的操作：
        1. 推导来源：homework_id 有值 → homework，否则 chat
        2. 幂等：同 (学生, 题目, 来源, 来源id) 已有行 → 直接返回 None
           ★ 口径是"首次提交定结果"：学生首次答错就记为错，之后引导再答对也不改，
             否则错题会被自己"改正"掉，诊断测验就永远缺数据
        3. 插入 student_answers（is_correct 按本次判分结果）
        4. is_correct 为 False → 规则粗归因，写 error_logs
        5. 首次落库才更新 BKT（对错都更新，答对提升 p_known）

    返回：
        新作答行的 id（int）；
        已记过 或 落库异常 返回 None。

    为什么"答对也要落库"：
        老师端要看"每道题每个人的作答"，不只是错题；
        而且 BKT 只更新错题的话，学生掌握度只会单调下降——答对必须给正反馈。
    """
    source_type, source_id = _source_of(homework_id)

    # 错因只在答错时算（答对没有错因可言，BKT 那边要求 error_type=None）
    error_type = (
        guess_error_type(student_answer, question.answer or "")
        if is_correct is False
        else None
    )

    answer_id: int | None = None
    session = get_session_factory()()
    try:
        # ---- 1. 幂等：同学生同题同来源只记首次 ----
        # 不这么做的话，学生引导后再答一次就多一条，老师端一题出现好几行，
        # 诊断测验的错题频次也会虚高，Top5 知识点排序会被"反复错同一题"带偏
        already = session.execute(
            select(StudentAnswer.id)
            .where(StudentAnswer.student_id == student_id)
            .where(StudentAnswer.question_id == question.id)
            .where(StudentAnswer.source_type == source_type)
            .where(StudentAnswer.source_id == source_id)
        ).first()

        if already is not None:
            return None

        # ---- 2. 作答行 ----
        row = StudentAnswer(
            source_type=source_type,
            source_id=source_id,
            question_id=question.id,
            student_id=student_id,
            content=student_answer,
            is_correct=is_correct,
            score=score,
            attempt_count=attempt_count,
        )
        session.add(row)
        session.flush()             # 拿自增 id：error_logs.answer_id 是外键，要先有值
        answer_id = row.id

        # ---- 3. 错因行（只有答错才有）----
        if is_correct is False:
            session.add(ErrorLog(
                answer_id=answer_id,
                error_type=error_type,
                confidence=RULE_CONFIDENCE,
                evidence=f"学生作答：{student_answer[:80]}",
                explanation_strategy=(
                    "重算一遍，重点核对数值与符号"
                    if error_type == "粗心"
                    else "回到知识点把方法重讲一遍"
                ),
            ))
        session.commit()

    except Exception:
        session.rollback()          # 半路失败整笔回滚，不留脏行
        raise
    finally:
        session.close()

    # ---- 4. BKT 更新：best-effort ----
    # 作答已经落好了，掌握度更新失败不该把整次落库判为失败
    # is_correct 为 None（主观题等老师批改）时不更新：还没判分，无从更新
    if is_correct is not None:
        try:
            p_known, days = get_bkt_state(student_id, question.kp_id)

            new_p = update_mastery(
                p_known=p_known,
                is_correct=is_correct,
                error_type=error_type,      # 答对时是 None，符合 update_mastery 约定
                days_since_last=days,
            )

            sync_mastery(student_id, question.kp_id, new_p)

        except Exception as exc:
            print(f"[wrong_record] ⚠ BKT 更新失败（作答已落库，不影响诊断测验取数）: {exc}")

    return answer_id


def record_answer_full_score(homework_id: int | None, question_id: int) -> float:
    """取本题满分：作业场景读 homework_questions.score，自由练习按百分制。

    得到的数据：
        homework_id（作业 id 或 None）；question_id（题目 id）

    要做的操作：
        有作业 → 查 homework_questions 该题分值；查不到或自由练习 → 100.0

    返回：
        本题满分（float），形如 10.0 或 100.0。

    为什么：
        对话场景原来一律按百分制给分，作业题实际每题 10 分，
        直接按 100 分算会让老师端"得分"列显示 100 而不是 10，与作业配置对不上。
    """
    if not homework_id:
        return 100.0

    from app.models import HomeworkQuestion

    session = get_session_factory()()
    try:
        score = session.execute(
            select(HomeworkQuestion.score)
            .where(HomeworkQuestion.homework_id == homework_id)
            .where(HomeworkQuestion.question_id == question_id)
        ).scalar_one_or_none()

        return float(score) if score is not None else 100.0

    finally:
        session.close()


def record_chat_wrong(
    student_id: int,
    question: Question,
    student_answer: str,
    attempt_count: int = 0,
) -> int | None:
    """自由练习答错的薄封装（保留旧调用点，行为等同 record_answer 的 chat 分支）。

    得到的数据：同 record_answer，只是固定 homework_id=None、is_correct=False。

    要做的操作：转调 record_answer，来源落成 ("chat", 0)。

    返回：新作答行 id；已记过返回 None。

    为什么还留着它：
        调用方只想表达"这是一次自由练习的错题"，不必自己拼 is_correct/来源；
        另一方面 tests/test_wrong_record.py 直接打这个函数，改名会无谓惊动测试。
    """
    return record_answer(
        student_id=student_id,
        question=question,
        student_answer=student_answer,
        is_correct=False,
        score=0.0,
        homework_id=None,
        attempt_count=attempt_count,
    )
