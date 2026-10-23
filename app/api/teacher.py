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

from fastapi import APIRouter, Depends, HTTPException, status
from langgraph.types import Command
from sqlalchemy import func, select
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
    Class,
    ErrorLog,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
    User,
)
from app.schemas.api import GradeConfirmIn, HomeworkCreateIn

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

    得到的数据：class_id / title / kp_ids / per_kp / score
    要做的操作：
      t1 越权校验：这个班必须在我任教范围内
      t2 selection_engine.fetch_candidates_by_kp 备料 → select_quiz_questions 抽题
         （target_points 伪装成 p_known=0.5 → 全 medium 档；作业不是诊断测验，取中档）
      t3 一个事务写 homeworks + homework_questions，审计埋点
    为什么复用 S-10 的抽题函数：同一套"单点 ≤2、缺口不乱补"的确定性规则，
    老师布置的作业也能做到"重跑同题"，家长问起来说得清。
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


@router.get("/homework/{homework_id}/submissions")
def homework_submissions(homework_id: int, user: CurrentUser = Depends(get_current_user),
                         scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """该作业全班作答情况（老师只看自己班）。"""
    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="作业不存在")
    _ensure_class_in_scope(scopes, user, hw.class_id)

    rows = db.execute(
        select(StudentAnswer, Student.student_no, User.name, Question.content)
        .join(Student, Student.id == StudentAnswer.student_id)
        .join(User, User.id == Student.user_id)
        .join(Question, Question.id == StudentAnswer.question_id)
        .where(StudentAnswer.source_type == "homework")
        .where(StudentAnswer.source_id == homework_id)
        .order_by(StudentAnswer.student_id, StudentAnswer.question_id)
    ).all()
    return [
        {"answer_id": a.id, "student": f"{name}({no or '-'})", "question": content[:40],
         "content": a.content, "is_correct": a.is_correct, "score": a.score,
         "submitted_at": str(a.submitted_at)}
        for a, no, name, content in rows
    ]


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
    return {"answer_id": answer_id, "status": out.get("status"),
            "final_score": out.get("final_score"),
            "p_known_before": out.get("p_known_before"),
            "p_known_after": out.get("p_known_after")}


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

    return [
        {"answer_id": a.id, "student": f"{name}({no or '-'})",
         "question_type": qtype, "question": content[:40], "content": a.content,
         "submitted_at": str(a.submitted_at)}
        for a, no, name, qtype, content in rows
    ]



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
