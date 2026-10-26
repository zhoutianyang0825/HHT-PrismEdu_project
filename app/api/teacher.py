"""
M5-T1/T2 · 老师端端点组：布置作业 / 触发批改 / 🔴 主观题确认（第二个 HITL 场景）/ 错题汇总。

【主观题批改跨 HTTP 的时间线】（对照学生端 SSE，这边的 interrupt 走同步 JSON）
  t1  老师点"批改" → POST /api/teacher/grade/1001

  t2  批改图 invoke：fetch_answer → route_by_type → llm_grade_subjective

  t3  LLM 出建议分 → interrupt() 挂起 → invoke 返回值带 __interrupt__
      → 接口立刻返回 {"status": "pending_confirm", "suggestion": {...AI 建议...}}

  t4  老师页面看到建议分/评语/采分点 → 点"采纳confirm/改分edit/驳回reject"

  t5  POST /api/teacher/grade/1001/confirm {"action": "edit", "score": 8.5}
      → Command(resume=decision) 恢复同一 thread → apply → commit → BKT 回写
      → 返回终局结果（final_score / p_known_before / p_known_after）

【越权守卫（双通道纪律的落地）】
  本组全部挂 require_any("teacher")；
  数据层再加一道 _ensure_class_in_scope（）的校验:
        class_id ∈ detail scope明细通道权限 ，王老师只教 3、7 班，调 5 班学生的作答明细即使角色对了也要 403。

  GET /answers/{answer_id} 是 M5 完成判据的 403 验证口（researcher这个角色，不能走detail通道，看到某个学生的具体明显，调它必 403）。
"""

from functools import lru_cache
import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from langgraph.types import Command
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_scopes,
    require_any,
    require_detail_channel,
)
from app.db.session import get_db
from app.models import (
    Chapter,
    Class,
    ErrorLog,
    ExamPaper,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
    User,
)
from app.schemas.api import GradeConfirmIn, HomeworkCreateIn, ManualGradeIn

# 接口调用的最前置校验：角色的守卫
# 本组角色守卫：teacher 或 admin 才能进（挂 router 级，接口级角色校验）
router = APIRouter(dependencies=[Depends(require_any("teacher"))])


@lru_cache
def _get_grading_graph():
    """批改图单例（与学生端共用同一套 build；lru_cache 保证进程内只有一份）。"""
    from app.db.checkpointer import get_checkpointer
    from app.graphs.teacher.grading_graph import build_grading_graph

    return build_grading_graph(checkpointer=get_checkpointer())


# =====================================================================
# detail 通道的scope班级集合
# =====================================================================
def _detail_class_ids(scopes: list[dict]) -> set[int]:
    """从 scope 权限列表抽 detail 明显通道的 class_ids，获取当前teacher的任教班级和科目。

    得到的数据：scopes = [{"type": "detail", "class_ids": [3, 7], ...}, ...]
    返回：{3, 7}；researcher-only 的 scopes 里没有 detail 通道 → 空集合（调用方转 403）
    """
    return {cid for s in scopes if s["type"] == "detail" for cid in s.get("class_ids", [])}


def _ensure_class_in_scope(scopes: list[dict], user: CurrentUser, class_id: int) -> None:
    """班级级越权守卫：admin 恒放行； 否则 class_id 必须在自己的任教班集合里。"""
    if "admin" in user.roles:
        return
    
    if class_id not in _detail_class_ids(scopes):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail=f"forbidden: 班级 {class_id} 不在你的任教范围内")


def _answer_class_id(db: Session, answer_id: int) -> int:
    """通过作答记录 → 回捞学生档案 → 再回捞学生所在的class_id（class_id用来做 教师的scope 鉴权）。"""
    answer = db.get(StudentAnswer, answer_id)
    if answer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"作答 {answer_id} 不存在")
    
    # 通过回答，回捞这个学生
    profile = db.get(Student, answer.student_id)

    # 返回学生的class_id  或者 直接返回-1，-1恒不在detail scope中
    return profile.class_id if profile else -1


# =====================================================================
# T-01 布置作业（复用组卷引擎抽题，可复现）
# =====================================================================
@router.post("/homework/create")
def homework_create(body: HomeworkCreateIn, user: CurrentUser = Depends(get_current_user),
                    scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """按知识点抽题布置作业。

    得到的数据：
        class_id / title / kp_ids / per_kp / score

    要做的操作：
      t1 越权校验：这个班必须在我任教范围内
      t2 selection_engine.fetch_candidates_by_kp 备料 → select_quiz_questions 抽题
         （target_points 伪装成 p_known=0.5 → 全 medium 档；作业不是诊断测验，取中档）
      t3 一个事务写 homeworks + homework_questions，审计埋点

    为什么复用 S-10 的抽题函数：
        同一套"单点 ≤2、缺口不乱补"的确定性规则，老师布置的作业也能做到"重跑同题"，家长问起来说得清。
    """
    _ensure_class_in_scope(scopes, user, body.class_id)

    from app.services.selection_engine import fetch_candidates_by_kp, select_quiz_questions

    target_points = [{"kp_id": kp, "p_known": 0.5, "reason": "作业布置"} for kp in body.kp_ids]
    candidates = fetch_candidates_by_kp(body.kp_ids)
    picked, gaps = select_quiz_questions(
        target_points=target_points, candidates_by_kp=candidates,
        total=body.per_kp * len(body.kp_ids), per_kp_cap=body.per_kp,
    )
    if not picked:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"题库抽不到题：{gaps}（宁可少题不错题，请先补题库）")

    hw = Homework(teacher_id=user.id, class_id=body.class_id, title=body.title,
                  kp_ids=json.dumps(body.kp_ids, ensure_ascii=False))
    db.add(hw)
    db.flush()
    for q in picked:
        db.add(HomeworkQuestion(homework_id=hw.id, question_id=q["id"], score=body.score))

    from app.services.audit_service import record_audit
    record_audit(db, user.id, "homework.create", f"homeworks:{hw.id}",
                 json.dumps({"class_id": body.class_id, "kp_ids": body.kp_ids,
                             "question_ids": [q["id"] for q in picked],
                             "gaps": gaps}, ensure_ascii=False))
    db.commit()
    return {"homework_id": hw.id, "question_ids": [q["id"] for q in picked], "gaps": gaps}


def _resolve_homework(db: Session, ref: str) -> Homework | None:
    """作业 id 或作业名 → 作业行。

    得到的数据：
        ref（字符串，作业 id 如 "12"，或作业名如 "随堂作业"）。

    要做的操作：
        纯数字按 id 精确查；
        否则按 title 模糊查（ilike），重名取 id 最大的一条。

    返回：
        Homework 行；
        查不到返回 None。

    为什么：老师手上通常只有作业名，强制先查 id 会多一步事。
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    
    if ref.isdigit():
        return db.get(Homework, int(ref))
    
    return db.execute(
        select(Homework).where(Homework.title.ilike(f"%{ref}%"))
        .order_by(Homework.id.desc()).limit(1)
    ).scalar_one_or_none()


@router.get("/homework/{homework_ref}/submissions")
def homework_submissions(homework_ref: str,
                         page: int = Query(default=1, ge=1),
                         size: int = Query(default=20, ge=1, le=100),
                         user: CurrentUser = Depends(get_current_user),
                         scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """某份作业的全班作答情况（老师只看自己班），按页返回。

    得到的数据：
        homework_ref（作业 id 或作业名）、
        page（页码）、
        size（每页条数，默认 20）；
        作业行由 _resolve_homework 解析，班级归属由 _ensure_class_in_scope 校验。

    要做的操作：
        按 (source_type='homework', source_id=作业 id) 统计总数，
        再左连 students/users/questions 取学号、姓名、题干，按学生-题目顺序切出一页。

    返回：
        {"total": 100, "page": 1, "size": 20,
         "homework": {"id": 4, "title": "演示历史作业", "class_id": 2},
         "summary": {"question_total": 10, "students": 18, "completed": 12, "partial": 6},
         "items": [{"answer_id": 1, "student": "演示学生01(20260066)", ...}]}

    为什么：一份作业 20 人 × 5 题就有 100 条作答，全量拉走没必要，所以按页取。
        summary 单独给"做齐 / 做一半"人数：学生端是逐题实时入库的，
        "有作答"不等于"交了作业"，只看 total 会把做 1 题就走的人算成已交。
    """
    hw = _resolve_homework(db, homework_ref)

    if hw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到作业：{homework_ref}")
    _ensure_class_in_scope(scopes, user, hw.class_id)

    where = [
        StudentAnswer.source_type == "homework",
        StudentAnswer.source_id == hw.id,
    ]
    total = db.execute(
        select(func.count()).select_from(StudentAnswer).where(*where)
    ).scalar_one()

    # ---- 完成度汇总：做齐全部题才算"完成"，只做一部分算"部分完成" ----
    # 逐题实时入库后，"有作答"不再等于"交了作业"——学生做 1 题就走也会有条目，
    # 必须按"该生作答数 vs 作业总题数"分开算，否则老师看到的提交率虚高
    question_total = db.execute(
        select(func.count()).select_from(HomeworkQuestion)
        .where(HomeworkQuestion.homework_id == hw.id)
    ).scalar_one()
    per_student = db.execute(
        select(StudentAnswer.student_id,
               func.count(func.distinct(StudentAnswer.question_id)))
        .where(*where)
        .group_by(StudentAnswer.student_id)
    ).all()
    completed = sum(1 for _, n in per_student if question_total and n >= question_total)

    summary = {
        "question_total": question_total,
        "students": len(per_student),          # 至少答过一题的人数
        "completed": completed,                # 全做齐的人数
        "partial": len(per_student) - completed,
    }

    rows = db.execute(
        select(StudentAnswer, Student.student_no, User.name, Question.content)
        .join(Student, Student.id == StudentAnswer.student_id)
        .join(User, User.id == Student.user_id)
        .join(Question, Question.id == StudentAnswer.question_id)
        .where(*where)
        .order_by(StudentAnswer.student_id, StudentAnswer.question_id)
        .offset((page - 1) * size).limit(size)
    ).all()
    
    return {
        "total": total, "page": page, "size": size,
        "homework": {"id": hw.id, "title": hw.title, "class_id": hw.class_id},
        "summary": summary,
        "items": [
            {"answer_id": a.id, "student": f"{name}({no or '-'})", "question": content[:40],
             "content": a.content, "is_correct": a.is_correct, "score": a.score,
             "submitted_at": str(a.submitted_at)}
            for a, no, name, content in rows
        ],
    }


# =====================================================================
# 🔴 批改两步走：触发（可能挂起）→ 确认（resume）
# =====================================================================
# 触发批改，调用此批改接口，router进行接口级角色校验
@router.post("/grade/{answer_id}")
def grade_trigger(answer_id: int, 
                  user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(get_scopes), 
                  db: Session = Depends(get_db)
    ):
    
    """触发批改。 客观题直接返回终局；主观题返回 AI 建议（pending_confirm）等裁决。"""

    # 校验 class_id 是否属于 detail scope，不属于直接raise 403，后面都不执行
    _ensure_class_in_scope(scopes, user, _answer_class_id(db, answer_id))

    # 校验通过后，开始获得批改图
    graph = _get_grading_graph()
    cfg = {"configurable": {"thread_id": f"grading_{answer_id}"}}


    out = graph.invoke({"answer_id": answer_id, "teacher_user_id": user.id}, cfg)

    if "__interrupt__" in out:
        # 主观题：图挂起在 llm_grade_subjective，AI 建议原样给前端渲染确认框
        return {"answer_id": answer_id, "status": "pending_confirm",
                "suggestion": out["__interrupt__"][0].value}


    # 客观题（或已批改过的题，幂等）：直接返回终局
    return {"answer_id": answer_id, "status": out.get("status"),
            "is_correct": out.get("is_correct"), "final_score": out.get("final_score"),
            "p_known_after": out.get("p_known_after")}


# 老师点击确认，调此接口
@router.post("/grade/{answer_id}/confirm")
def grade_confirm(answer_id: int, body: GradeConfirmIn,
                  user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """老师裁决：confirm 采纳 / edit 改分 / reject 驳回。Command(resume=...) 恢复批改图。

    完整时间线（edit 场景）：
      t1  body {"action": "edit", "score": 8.5} → 校验 score ≤ 满分
      t2  get_state 确认 thread 确实挂着（没挂说明重复确认 → 409）
      t3  graph.invoke(Command(resume={"action": "edit", "score": 8.5}), cfg)
          → apply_teacher_decision → commit_grade（写三表+审计）→ BKT 回写 → END
      t4  返回终局：final_score / p_known_before / p_known_after
    """
    _ensure_class_in_scope(scopes, user, _answer_class_id(db, answer_id))

    graph = _get_grading_graph()
    cfg = {"configurable": {"thread_id": f"grading_{answer_id}"}}

    # ---- t2 幂等保护：没挂起的图 resume 会直接抛错，先查状态给出友好 409 ----
    snap = graph.get_state(cfg)
    if not snap.next:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="该作答没有待确认的批改（可能已处理过）")

    # edit 必须带分：Pydantic 只能管"有值"，管不了"按 action 联动必填"
    if body.action == "edit" and body.score is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="action=edit 必须给 score")

    decision = {"action": body.action, "score": body.score, "comment": body.comment}
    out = graph.invoke(Command(resume=decision), cfg)

    # ---- t4 考试卷要回写该生的考试记录总分 ----
    # 批改图只管"这一条作答"，不知道它属于某场考试；
    # 学生端考试记录里的总分必须由这里重算，否则永远只显示客观题得分。
    attempt = _recalc_exam_attempt(db, answer_id)

    resp = {"answer_id": answer_id, "status": out.get("status"),
            "final_score": out.get("final_score"),
            "p_known_before": out.get("p_known_before"),
            "p_known_after": out.get("p_known_after")}
    if attempt is not None:
        resp["attempt"] = {
            "status": attempt.status,
            "objective_score": attempt.objective_score,
            "subjective_score": attempt.subjective_score,
            "total_score": attempt.total_score,
            "full_score": attempt.full_score,
        }
    return resp


def _recalc_exam_attempt(db: Session, answer_id: int):
    """这条作答若是考试卷的，重算对应学生的考试记录；否则返回 None。

    得到的数据：answer_id

    要做的操作：查作答 → 不是 source_type='exam' 就直接返回 None →
                是就调 exam_delivery.recalc_attempt 并 commit

    返回：更新后的 ExamAttempt 或 None
    """
    answer = db.get(StudentAnswer, answer_id)
    if answer is None or answer.source_type != "exam":
        return None

    from app.services.exam_delivery import recalc_attempt

    attempt = recalc_attempt(db, answer.source_id, answer.student_id)
    db.commit()
    return attempt


# =====================================================================
# 🟢 老师自主批改：不经过 AI，直接判 正确 / 错误 / 部分正确 并给分
# =====================================================================
@router.post("/grade/{answer_id}/manual")
def grade_manual(answer_id: int, body: ManualGradeIn,
                 user: CurrentUser = Depends(get_current_user),
                 scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """老师完全自主给分（不需要先跑 AI）。

    得到的数据：
        answer_id=261；body.judgement ∈ {correct, wrong, partial}
        body.score 只在 partial 时有意义（如满分 8 分给 3.5）
        body.comment 可选评语

    要做的操作：
        t1 班级越权守卫（和我教的班必须对得上）
        t2 exam_delivery.recalc 需要的考试记录先不动 —— 判完再回写总分
        t3 manual_grade 服务：算分 → commit_grade 写三表 → 判错才归因 → BKT 回写
        t4 该作答若是考试卷的，重算该生考试总分

    返回：
        {"answer_id": 261, "final_score": 1.0, "full_score": 4.0,
         "is_correct": false, "status": "confirmed", "grader_type": "teacher",
         "p_known_before": 0.1, "p_known_after": 0.16,
         "attempt": {...}}   # 只有考试作答才有

    为什么单独一个端点而不是复用 /grade/{id}/confirm：
        confirm 的语义是"resume 挂起的批改图"，图没挂着它直接 409。
        老师想自己判一道题时，图根本没跑过——硬塞进 confirm 会逼老师
        "先点 AI 批改、再点改分"，还让 AI 的建议分进了库。
    """
    _ensure_class_in_scope(scopes, user, _answer_class_id(db, answer_id))

    from app.services.manual_grading import ManualGradeError, manual_grade

    try:
        out = manual_grade(
            answer_id=answer_id,
            judgement=body.judgement,
            score=body.score,
            comment=body.comment,
            teacher_user_id=user.id,
        )
    except ManualGradeError as exc:
        # 文案里带"已经批过了"的走 409（状态冲突），其余是参数问题走 400
        code = (status.HTTP_409_CONFLICT if "已经批过" in str(exc)
                else status.HTTP_400_BAD_REQUEST)
        raise HTTPException(code, detail=str(exc))

    attempt = _recalc_exam_attempt(db, answer_id)
    if attempt is not None:
        out["attempt"] = {
            "status": attempt.status,
            "objective_score": attempt.objective_score,
            "subjective_score": attempt.subjective_score,
            "total_score": attempt.total_score,
            "full_score": attempt.full_score,
        }
    return out


# 展示待批改，待确认列表
@router.get("/grade/pending")
def pending_grading(user: CurrentUser = Depends(get_current_user),
                    scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """待批改/待确认列表：老师端页面首屏展示。

    归母两类记录：
      1) student_answers 里 is_correct IS NULL 的（还没判分）；
      2) 主观题已触发批改但图还挂着的，即：thread 级状态，逐条查 is_correct IS NULL 且有 grading_records.pending 的行。
    """
    class_ids = sorted(_detail_class_ids(scopes)) if "admin" not in user.roles else None
    stmt = (
        select(StudentAnswer, Student.student_no, User.name, Question.question_type,
               Question.content)
        .join(Student, Student.id == StudentAnswer.student_id)
        .join(User, User.id == Student.user_id)
        .join(Question, Question.id == StudentAnswer.question_id)
        .where(StudentAnswer.is_correct.is_(None))
        .order_by(StudentAnswer.submitted_at.desc())
        .limit(50)
    )
    if class_ids is not None:
        stmt = stmt.where(Student.class_id.in_(class_ids))

    rows = db.execute(stmt).all()

    # ---- 每条作答的满分：人工批改的"部分正确"要给分数，前端必须有上限 ----
    #   不能在循环里逐条 _fetch_full_score（每条自己开 session，50 条 = 50 次连接），
    #   按来源表批量取两张映射表再拼。
    full_map = _batch_full_scores(db, [a for a, *_ in rows])

    return [
        {"answer_id": a.id, "student": f"{name}({no or '-'})",
         "question_type": qtype, "question": content[:40], "content": a.content,
         "full_score": full_map.get(a.id, 100.0),
         "submitted_at": str(a.submitted_at)}
        for a, no, name, qtype, content in rows
    ]


def _batch_full_scores(db: Session, answers: list[StudentAnswer]) -> dict[int, float]:
    """批量算一批作答各自的满分，返回 {answer_id: full_score}。

    得到的数据：answers —— student_answers 行列表（可能混着 homework / exam 来源）

    要做的操作：
        t1 按来源分两组（homework → homework_questions，exam → exam_paper_questions）
        t2 每组一次 IN 查询拿 (来源id, 题目id) → 分值
        t3 按作答回填；查不到（历史脏数据）给 100 兜底，和 _fetch_full_score 口径一致

    返回：{261: 8.0, 262: 4.0, ...}

    为什么批量：
        待批改列表一次 50 条，逐条调 _fetch_full_score 会开 50 个独立 session，
        批改页首屏直接慢成几百毫秒。
    """
    from app.models import ExamPaperQuestion, HomeworkQuestion

    hw_pairs = {(a.source_id, a.question_id) for a in answers if a.source_type == "homework"}
    exam_pairs = {(a.source_id, a.question_id) for a in answers if a.source_type == "exam"}

    score_of: dict[tuple[str, int, int], float] = {}

    if hw_pairs:
        rows = db.execute(
            select(HomeworkQuestion.homework_id, HomeworkQuestion.question_id,
                   HomeworkQuestion.score)
            .where(HomeworkQuestion.homework_id.in_({p[0] for p in hw_pairs}))
        ).all()
        for hid, qid, sc in rows:
            score_of[("homework", hid, qid)] = float(sc)

    if exam_pairs:
        rows = db.execute(
            select(ExamPaperQuestion.paper_id, ExamPaperQuestion.question_id,
                   ExamPaperQuestion.score)
            .where(ExamPaperQuestion.paper_id.in_({p[0] for p in exam_pairs}))
        ).all()
        for pid, qid, sc in rows:
            score_of[("exam", pid, qid)] = float(sc)

    return {
        a.id: score_of.get((a.source_type, a.source_id, a.question_id), 100.0)
        for a in answers
    }



@router.get("/answers/{answer_id}")
def answer_detail(answer_id: int, user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(require_detail_channel),
                  db: Session = Depends(get_db)):
    """作答明细（🔴 越权验证口）：只认 detail/self 通道，researcher 调用必 403。

    为什么单独留这个端点：M5 完成判据要求"researcher 调明细接口必须 403"，这就是那个被调的接口。
    
    守卫用 require_detail_channel 而不是 require_any，两通道永不融合（开发计划/架构文档 8.4 的关键禁令）。
    """
    if "admin" not in user.roles:
        if not any(s["type"] == "detail" and
                   _answer_class_id(db, answer_id) in s.get("class_ids", []) for s in scopes):
            # self 通道（学生看自己）也放行：复用明细接口给前端复用
            answer = db.get(StudentAnswer, answer_id)
            profile = db.get(Student, answer.student_id) if answer else None
            if not (any(s["type"] == "self" for s in scopes)
                    and profile is not None and profile.user_id == user.id):
                raise HTTPException(status.HTTP_403_FORBIDDEN,
                                    detail="forbidden: detail scope required")

    answer = db.get(StudentAnswer, answer_id)
    if answer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="作答不存在")
    question = db.get(Question, answer.question_id)
    return {"answer_id": answer.id, "content": answer.content,
            "is_correct": answer.is_correct, "score": answer.score,
            "question_id": question.id, "question_type": question.question_type,
            "standard_answer": question.answer}


# =====================================================================
# T-03 班级错题汇总（detail 通道的聚合：老师可以看自己班的聚合）
# =====================================================================
@router.get("/class/{class_id}/error-summary")
def error_summary(class_id: int, user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """班级错题汇总 + Top3 薄弱知识点（按错因计数，聚合输出无学生明细）。"""

    _ensure_class_in_scope(scopes, user, class_id)

    rows = db.execute(
        select(ErrorLog.error_type, KnowledgePoint.id, KnowledgePoint.name,
               func.count().label("cnt"))
        .join(StudentAnswer, StudentAnswer.id == ErrorLog.answer_id)
        .join(Student, Student.id == StudentAnswer.student_id)
        .join(Question, Question.id == StudentAnswer.question_id)
        .join(KnowledgePoint, KnowledgePoint.id == Question.kp_id)
        .where(Student.class_id == class_id)
        .group_by(ErrorLog.error_type, KnowledgePoint.id, KnowledgePoint.name)
        .order_by(func.count().desc())
    ).all()

    kp_total: dict[str, dict] = {}
    for etype, kp_id, kp_name, cnt in rows:
        b = kp_total.setdefault(kp_id, {"kp_id": kp_id, "name": kp_name, "wrong_count": 0,
                                        "error_types": {}})
        b["wrong_count"] += cnt
        b["error_types"][etype] = cnt
    top3 = sorted(kp_total.values(), key=lambda x: -x["wrong_count"])[:3]
    return {"class_id": class_id, "top_weak_kps": top3,
            "detail": [{"error_type": e, "kp_id": k, "kp_name": n, "count": c}
                       for e, k, n, c in rows]}


@router.get("/class/{class_id}/mastery")
def class_mastery(class_id: int, user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(get_scopes)):
    """班级知识点平均掌握度（老师端雷达图；直接复用 M4 的共享 tool，JSON 字符串转回 dict）。"""
    _ensure_class_in_scope(scopes, user, class_id)
    from app.tools.graph_tools import query_class_mastery

    return json.loads(query_class_mastery.invoke({"class_id": class_id}))



@router.get("/homeworks")
def list_homeworks(page: int = Query(default=1, ge=1),
                   size: int = Query(default=10, ge=1, le=50),
                   user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """我布置的作业清单（时间倒序 + 分页），给新建作业页右侧「已布置作业」列表用。

    得到的数据：
        page（页码，从 1 起）、
        size（每页条数，默认 10）、
        keyword（作业名模糊搜，可空）；
        teacher 只看自己布置的，admin 看全部。

    要做的操作：
        homeworks 左连 classes 取班级名，按 id 倒序分页；
        批量统计每份作业的题数（homework_questions 行数）与已交人数
        （student_answers 中 source_type='homework' 的 distinct student_id 数）。

    返回：
        {"total": 12, "page": 1, "size": 10,
         "items": [{"id": 4, "title": "随堂作业", "class_name": "初二1班",
                    "question_count": 5, "submitted": 20, "created_at": "2026-10-24 14:53:11"}]}

    为什么：老师要一眼看出"布置了但没人交"，所以每行都带题数与已交人数。
    """
    stmt = select(Homework, Class.grade, Class.name).join(Class, Class.id == Homework.class_id)
    count_stmt = select(func.count()).select_from(Homework)
    if "admin" not in user.roles:
        stmt = stmt.where(Homework.teacher_id == user.id)
        count_stmt = count_stmt.where(Homework.teacher_id == user.id)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(stmt.order_by(Homework.id.desc())
                      .offset((page - 1) * size).limit(size)).all()
    ids = [hw.id for hw, _, _ in rows]

    q_counts: dict[int, int] = {}
    sub_counts: dict[int, int] = {}
    if ids:
        q_counts = dict(db.execute(
            select(HomeworkQuestion.homework_id, func.count(HomeworkQuestion.id))
            .where(HomeworkQuestion.homework_id.in_(ids))
            .group_by(HomeworkQuestion.homework_id)).all())
        sub_counts = dict(db.execute(
            # 去重学生数（不是作答条数）：一份作业 5 题 × 20 人 = 100 条作答，
            # 但"已交"说的是几个人交了 → count(distinct student_id)
            select(StudentAnswer.source_id,
                   func.count(func.distinct(StudentAnswer.student_id)))
            .where(StudentAnswer.source_type == "homework",
                   StudentAnswer.source_id.in_(ids))
            .group_by(StudentAnswer.source_id)).all())

    return {"total": total, "page": page, "size": size,
            "items": [{"id": hw.id, "title": hw.title,
                       "class_name": f"{grade}{name}",
                       "question_count": q_counts.get(hw.id, 0),
                       "submitted": sub_counts.get(hw.id, 0),
                       "created_at": str(hw.created_at)} for hw, grade, name in rows]}


@router.get("/knowledge-points")
def list_knowledge_points(db: Session = Depends(get_db)):
    """知识点清单：全量知识点 + 每个知识点在题库里的可抽题量。

    得到的数据：
        PG 的 knowledge_points（全量 63 个）与 questions（题量来源）；
        无入参。

    要做的操作：
        knowledge_points 左连 chapters、右连 questions 聚合，
        用 CASE 条件计数剔除 quality_flag='disabled' 的题（与抽题引擎口径一致）。

    返回：
        [{"id": "kp_crossmult", "name": "十字相乘法",
          "chapter_name": "第21章 一元二次方程", "question_count": 8}, ...]

    为什么：知识点与题库都是 seed 灌进来的数据，前端自己硬一份清单必然和库对不上。
    """
    rows = db.execute(
        select(
            KnowledgePoint.id,
            KnowledgePoint.name,
            KnowledgePoint.chapter_id,
            Chapter.name,
            # 用 CASE 累加而不是 where 过滤：LEFT JOIN 保住"0 题的知识点"（老师要看得到缺口）
            func.coalesce(
                func.sum(case((Question.quality_flag != "disabled", 1), else_=0)), 0
            ),
        )
        .outerjoin(Chapter, Chapter.id == KnowledgePoint.chapter_id)
        .outerjoin(Question, Question.kp_id == KnowledgePoint.id)
        .group_by(KnowledgePoint.id, KnowledgePoint.name, KnowledgePoint.chapter_id,
                  Chapter.name, Chapter.order)
        .order_by(Chapter.order, KnowledgePoint.id)
    ).all()
    return [{"id": kp_id, "name": name, "chapter_id": ch_id, "chapter_name": ch_name,
             "question_count": cnt} for kp_id, name, ch_id, ch_name, cnt in rows]



@router.get("/classes")
def my_classes(user: CurrentUser = Depends(get_current_user),
               scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """我的任教班级（老师端页面首屏下拉框）。admin 返回全部班级。"""
    if "admin" in user.roles:
        rows = db.execute(select(Class)).scalars().all()
    else:
        ids = _detail_class_ids(scopes)
        rows = db.execute(select(Class).where(Class.id.in_(ids))).scalars().all() if ids else []
    return [{"id": c.id, "grade": c.grade, "name": c.name} for c in rows]


# =====================================================================
# M7 考试批改：按试卷 / 按题目分组（主观题）
# =====================================================================
def _visible_class_ids(scopes: list[dict], user: CurrentUser) -> list[int] | None:
    """我可见的班级集合；admin 返回 None 表示不限。

    得到的数据：scopes（双通道权限）

    要做的操作：只取 detail 通道的 class_ids

    返回：{3, 7} 排序后的列表；admin → None

    为什么单独抽：
        卷列表、按题分组两处都要用同一个口径，
        直接在两个函数里各写一遍 _detail_class_ids 迟早写岔。
    """
    if "admin" in user.roles:
        return None
    return sorted(_detail_class_ids(scopes))


@router.get("/exam/papers")
def exam_papers(user: CurrentUser = Depends(get_current_user),
                scopes: list[dict] = Depends(get_scopes),
                subject: str | None = None, db: Session = Depends(get_db)):
    """我教的班里已推送的试卷 + 交卷/待批改统计（考试批改页的左侧列表）。

    得到的数据：当前账号的 detail scope；可选 subject 过滤

    要做的操作：
        t1 取 exam_paper_classes 里属于我任教班的卷子
        t2 每卷统计：推送班名、应交人数、已交人数、主观题待批改条数

    返回：
        [{"paper_id": 3, "title": "初二数学月考卷", "grade": "初二",
          "class_names": ["初二1班"], "question_count": 20,
          "student_count": 39, "submitted_count": 31, "pending_count": 7}]

    为什么老师端要按"卷"而不是按"作答"列：
        待批改页原本是一条条作答流水（谁、哪题、什么时候交），
        老师拿到的是一堆散点，无法回答"这份卷我还剩几题没批"。
        试卷维度才有"批改进度"，题维度才有"同一题横向对比"。
    """
    from app.services.exam_delivery import teacher_papers

    return teacher_papers(db, _visible_class_ids(scopes, user), subject)


@router.get("/exam/{paper_id}/grading")
def exam_grading_groups(paper_id: int,
                        user: CurrentUser = Depends(get_current_user),
                        scopes: list[dict] = Depends(get_scopes),
                        db: Session = Depends(get_db)):
    """按题目分组的主观题作答（老师批改主视图）。

    得到的数据：paper_id

    要做的操作：
        t1 卷子存在性 + 越权守卫（这份卷必须推给了我教的某个班）
        t2 取卷面全部主观题（seq 升序），一题一组
        t3 每题下面挂全班学生的作答：姓名、原文、手写图、判分状态

    返回：见 exam_delivery.group_grading 的 docstring

    为什么按题分组：
        老师批一道题时脑子里只有一条评分标准，连续看 30 个学生对同一题的答案，
        尺度才稳；按学生排会在一份卷里反复切换标准。
        AI 批改同理——同一题的标准答案/采分点固定，逐份跑出来的建议更一致。
    """
    from app.services.exam_delivery import group_grading

    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"试卷 {paper_id} 不存在")

    class_ids = _visible_class_ids(scopes, user)
    if class_ids is not None:
        from app.models import ExamPaperClass

        pushed = set(db.execute(
            select(ExamPaperClass.class_id).where(ExamPaperClass.paper_id == paper.id)
        ).scalars().all())
        if not pushed & set(class_ids):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                detail="forbidden: 这份试卷没有推送到你的班级")

    return group_grading(db, paper, class_ids)


@router.post("/exam/{paper_id}/question/{question_id}/pre-grade")
def exam_pregrade_question(paper_id: int, question_id: int,
                           user: CurrentUser = Depends(get_current_user),
                           scopes: list[dict] = Depends(get_scopes),
                           db: Session = Depends(get_db)):
    """对某一题的全部待批改作答批量跑 AI 批改，返回每条的建议分（老师再逐条裁决）。

    得到的数据：paper_id + question_id

    要做的操作：
        t1 取该题在本卷里所有 is_correct IS NULL 的作答（限我可见的班）
        t2 逐条 invoke 批改图：客观题直接终局，主观题停在 interrupt 返回建议
        t3 汇总每条的建议分 / 评语 / 置信度

    返回：
        {"question_id": 21, "graded": 3, "failed": 0,
         "items": [{"answer_id": 501, "status": "pending_confirm",
                    "suggestion": {"ai_score": 6.0, "full_score": 8.0, ...}}]}

    为什么按题批量而不是按人：
        一条评分标准跑 N 份，模型每次只换了学生作答；
        按人批量时每题都要重新认题干，慢一倍还容易漂。
        失败不整体回滚：某一条 LLM 超时不该让另外 29 条白跑。
    """
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"试卷 {paper_id} 不存在")

    class_ids = _visible_class_ids(scopes, user)

    stmt = (
        select(StudentAnswer.id)
        .join(Student, Student.id == StudentAnswer.student_id)
        .where(StudentAnswer.source_type == "exam")
        .where(StudentAnswer.source_id == paper.id)
        .where(StudentAnswer.question_id == question_id)
        .where(StudentAnswer.is_correct.is_(None))
        .order_by(StudentAnswer.id)
    )
    if class_ids is not None:
        stmt = stmt.where(Student.class_id.in_(class_ids))

    answer_ids = list(db.execute(stmt).scalars().all())
    if not answer_ids:
        return {"question_id": question_id, "graded": 0, "failed": 0, "items": []}

    graph = _get_grading_graph()
    items: list[dict] = []
    failed = 0
    for aid in answer_ids:
        cfg = {"configurable": {"thread_id": f"grading_{aid}"}}
        try:
            out = graph.invoke({"answer_id": aid, "teacher_user_id": user.id}, cfg)
        except Exception as exc:       # 单条失败不拖累整批
            failed += 1
            items.append({"answer_id": aid, "status": "error",
                          "message": f"{type(exc).__name__}: {exc}"})
            continue

        if "__interrupt__" in out:
            items.append({"answer_id": aid, "status": "pending_confirm",
                          "suggestion": out["__interrupt__"][0].value})
        else:
            items.append({"answer_id": aid, "status": out.get("status"),
                          "is_correct": out.get("is_correct"),
                          "final_score": out.get("final_score")})

    return {"question_id": question_id, "graded": len(answer_ids) - failed,
            "failed": failed, "items": items}
