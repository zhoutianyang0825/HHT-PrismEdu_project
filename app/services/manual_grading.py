"""app/services/manual_grading.py
老师完全自主批改：不给 AI 建议，老师直接判 正确 / 错误 / 部分正确 并给分。

【为什么需要这条路径】
  原链路只有「AI 批改写建议 → 老师采纳/改分/驳回」。
  但真实批改场景里有两类题根本不该先跑 AI：
    ① 手写图片题 —— 系统没接图像识别，AI 只能看到"另附 1 张图"，判了也是瞎判；
    ② AI 明显判偏的题 —— 老师要先跑一遍 AI、再点改分，多两次点击，
       还会把 AI 的错误建议写进 grading_records.ai_suggested_score 留痕（看着像老师采纳了）。
  所以补一个纯人工入口：老师说了算，库里如实记 grader_type='teacher'。

【复用而不是重写】
  落库、BKT、错因归因三件事已经有现成的节点函数（批改图里的 commit_grade /
  attribute_error / update_mastery_node）。这里直接按顺序调用这三个函数，
  保证人工批改和 AI 批改写的是同一套表、同一套 BKT 参数、同一套审计埋点。
  另写一套写库逻辑 = 两份口径，迟早出现"AI 批的算掌握度、人工批的不算"。

【一次人工批改的数据流】
  t1 取作答 + 题目 → full_score（作业题读 homework_questions，考试题读 exam_paper_questions）
  t2 三档判分：correct=满分 / wrong=0 / partial=老师填的分（0 < x < 满分）
  t3 commit_grade      写 grading_records(grader_type=teacher) + student_answers + audit_logs
  t4 attribute_error   答错（含部分正确但不到 60%）才跑：写 error_logs 供 S-10/T-03 取数
  t5 update_mastery_node  BKT 三步更新 + 三处同步
"""
from sqlalchemy import select

from app.db.session import get_session_factory
from app.graphs.teacher.grading_graph import (
    SUBJECTIVE_PASS_RATE,
    _fetch_full_score,
    attribute_error,
    commit_grade,
    update_mastery_node,
)
from app.models import GradingRecord, Question, StudentAnswer

JUDGEMENTS = ("correct", "wrong", "partial")


class ManualGradeError(ValueError):
    """人工批改的前置校验失败（转 HTTP 400/409 用）。"""


def resolve_score(judgement: str, score: float | None, full_score: float) -> tuple[float, bool]:
    """把「正确 / 错误 / 部分正确」翻成 (最终分, is_correct)。

    得到的数据：
        judgement="partial"；score=1；full_score=4.0

    要做的操作：
        t1 correct → (满分, True)
        t2 wrong   → (0, False)
        t3 partial → 校验 0 < score < 满分，再按 60% 得分率线判 is_correct

    返回：
        (4.0, True) / (0.0, False) / (1.0, False)

    为什么部分正确也走 60% 线：
        和主观题 AI 建议的判定口径保持一条线（apply_teacher_decision 用的是同一个
        SUBJECTIVE_PASS_RATE）。否则同一道题"AI 批 3/4 算答对、老师给 3/4 算答错"，
        BKT 会跟着抖。
    """
    if judgement == "correct":
        return float(full_score), True

    if judgement == "wrong":
        return 0.0, False

    if judgement != "partial":
        raise ManualGradeError(f"未知的批改结论：{judgement}")

    if score is None:
        raise ManualGradeError("选「部分正确」必须给分数")

    got = round(float(score), 1)
    if got <= 0:
        raise ManualGradeError("部分正确的分数要大于 0；全错请选「错误」")
    if got >= float(full_score):
        raise ManualGradeError(f"部分正确的分数要小于本题满分 {full_score}；全对请选「正确」")

    return got, got >= float(full_score) * SUBJECTIVE_PASS_RATE


def manual_grade(
    answer_id: int,
    judgement: str,
    score: float | None = None,
    comment: str | None = None,
    teacher_user_id: int | None = None,
) -> dict:
    """老师自主给分（不经过 AI 建议）。

    得到的数据：
        answer_id=261（student_answers 主键）；
        judgement="partial"；score=1；full_score 由来源表查（可能 4.0 / 9.0 / 100）

    要做的操作：
        t1 取作答与题目；取不到直接抛 ManualGradeError
        t2 已有 confirmed 批改记录 → 抛错（409），避免重复写 BKT
        t3 resolve_score 三档判分
        t4 commit_grade（grader_type 显式写 teacher）
        t5 判错才跑 attribute_error（LLM 归因，失败自动降级"方法不会"）
        t6 update_mastery_node 回写掌握度

    返回：
        {"answer_id": 261, "final_score": 1.0, "is_correct": False,
         "full_score": 4.0, "status": "confirmed",
         "p_known_before": 0.10, "p_known_after": 0.16}

    为什么先查"已批改"而不是靠 commit_grade 内部幂等：
        commit_grade 的幂等是"静默返回旧结果"，前端拿到的分数会和老师刚填的不一致。
        这里显式拦下来，老师能看到"这题已经批过了"。
    """
    session = get_session_factory()()
    try:
        answer = session.get(StudentAnswer, answer_id)
        if answer is None:
            raise ManualGradeError(f"作答 {answer_id} 不存在")

        question = session.get(Question, answer.question_id)
        if question is None:
            raise ManualGradeError(f"题目 {answer.question_id} 不存在")

        # 幂等保护：同一作答只允许一条生效中的批改结论
        already = session.execute(
            select(GradingRecord)
            .where(GradingRecord.answer_id == answer_id)
            .where(GradingRecord.status == "confirmed")
        ).scalar_one_or_none()
        if already is not None:
            raise ManualGradeError(
                f"这条作答已经批过了（{already.final_score} 分，来源 {already.grader_type}），"
                "需要改分请重新裁决，不要重复提交")

        full_score = _fetch_full_score(answer, question)
        final_score, is_correct = resolve_score(judgement, score, full_score)

        state = {
            "answer_id": answer_id,
            "teacher_user_id": teacher_user_id,
            "question_id": question.id,
            "student_id": answer.student_id,
            "kp_id": question.kp_id,
            "question_type": question.question_type,
            "question_content": question.content,
            "student_answer": answer.content or "",
            "standard_answer": question.answer or "",
            "reference_analysis": question.analysis or "",
            "full_score": full_score,
            # 人工批改没有 AI 建议：ai_score / ai_comment 留 None，
            #   grading_records 里这两列就该是空的——不能拿老师的分冒充 AI 建议
            "ai_score": None,
            "ai_comment": None,
            "final_score": final_score,
            "final_comment": comment or f"老师人工批改：{_label(judgement, final_score, full_score)}",
            "status": "confirmed",
            "is_correct": is_correct,
            "grader_type": "teacher",
            # action=edit 是给"老师手填分数"用的既有语义，commit_grade 里只读 action
            #   决定 grader_type（已被上面显式覆盖），这里用它表示"不是采纳 AI 建议"
            "teacher_decision": {"action": "edit", "score": final_score, "comment": comment},
        }

        commit_grade(state)                     # 三表一个事务（自己开 session）
        if is_correct is False:
            # attribute_error 的返回值是"增量 dict"，不是就地改 state ——
            #   不 update 回去，下面 BKT 就拿不到 error_type，错因替换 slip 会失效
            #   （粗心本该掉得轻，结果按默认重罚，掌握度被多扣一截）
            state.update(attribute_error(state))   # 写 error_logs，供 S-10 诊断测验取错题
        out = update_mastery_node(state)        # BKT + 三处同步

        return {
            "answer_id": answer_id,
            "final_score": final_score,
            "full_score": round(float(full_score), 1),
            "is_correct": is_correct,
            "status": "confirmed",
            "grader_type": "teacher",
            "p_known_before": out.get("p_known_before"),
            "p_known_after": out.get("p_known_after"),
        }
    finally:
        session.close()


def _label(judgement: str, final_score: float, full_score: float) -> str:
    """给前端/学生看的一句话结论（评语为空时兜底）。"""
    if judgement == "correct":
        return "正确（满分）"
    if judgement == "wrong":
        return "错误（0 分）"
    return f"部分正确（{final_score} / {full_score} 分）"
