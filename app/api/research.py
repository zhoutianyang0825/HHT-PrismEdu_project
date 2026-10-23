"""
app/api/research.py
M5-T1 · 教研端端点组：年级聚合 / R-04 考后分析 / R-08 出卷 / 异常题清单。

【通道纪律】本组全部走聚合通道（require_aggregate_channel）：
  researcher 的 scope 是 {"type": "aggregate", "grade": "初二", "subject": "math"}，
  所有查询按 (grade, subject) 过滤聚合输出 —— 接口层面就不存在"按年级查学生明细"的路。
  teacher 也能调（T-03 同源数据，detail 通道天然放行聚合接口），admin 恒放行。

【R-04 → R-08 闭环在 API 层的落点】
  POST /paper/{id}/analyze ：算指标 → analysis_crew → write_back_quality_flags
  GET  /questions/abnormal ：读 quality_flag == "abnormal" 的题
  POST /exam/generate      ：exam_crew 内部 fetch_candidates 把 abnormal 沉底
  → "上一份卷子的分析结果，自动变成下一份卷子的组卷依据"
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_scopes,
    require_aggregate_channel,
)
from app.db.session import get_db
from app.models import (
    BKTState,
    Class,
    ExamPaper,
    Question,
    Student,
    StudentAnswer,
)
from app.schemas.api import ExamAssignIn, ExamGenerateIn

# 接口级角色校验，走require_aggregate_channel（）函数，通过判断scope中的type的值 是否属于  {aggregate, detail}
router = APIRouter(dependencies=[Depends(require_aggregate_channel)])


def _agg_grades(scopes: list[dict], user: CurrentUser) -> set[str] | None:
    """从 aggregate scope聚合通道权限明细中，抽所有年级集合。admin 返回 None = 不限。"""
    if "admin" in user.roles:
        return None
    
    return {s["grade"] for s in scopes if s["type"] == "aggregate"}


def _ensure_grade_in_scope(scopes: list[dict], user: CurrentUser, grade: str) -> None:
    allowed = _agg_grades(scopes, user)

    # 不是admin角色，并且查询的年级 不属于 aggregate scope内的值，raise 403
    if allowed is not None and grade not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail=f"forbidden: 你只负责 {'、'.join(sorted(allowed))}，不含 {grade}")


# =====================================================================
# R-01 年级掌握度（聚合，无明细）
# =====================================================================
@router.get("/grade/{grade}/mastery")
def grade_mastery(grade: str, subject: str = "math",
                  user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """年级掌握度概览：按知识点求全年级平均 p_known（升序=最薄弱在前）。"""

    _ensure_grade_in_scope(scopes, user, grade)

    from sqlalchemy import func

    from app.models import KnowledgePoint

    rows = db.execute(
        select(BKTState.kp_id, KnowledgePoint.name, func.avg(BKTState.p_known))
        .join(Student, Student.id == BKTState.student_id)
        .join(Class, Class.id == Student.class_id)
        .join(KnowledgePoint, KnowledgePoint.id == BKTState.kp_id)
        .where(Class.grade == grade)
        .where(KnowledgePoint.subject == subject)
        .group_by(BKTState.kp_id, KnowledgePoint.name)
        .order_by(func.avg(BKTState.p_known).asc())
    ).all()
    return {"grade": grade, "subject": subject,
            "items": [{"kp_id": k, "name": n, "avg_p_known": round(float(p), 3)}
                      for k, n, p in rows]}



# 横向对比
@router.get("/mastery/compare")
def mastery_compare(grade: str, subject: str = "math",
                    user: CurrentUser = Depends(get_current_user),
                    scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """班级横向对比：年级内每个班的平均掌握度（图表数据源）。"""

    _ensure_grade_in_scope(scopes, user, grade)

    from sqlalchemy import func

    rows = db.execute(
        select(Class.id, Class.name, func.avg(BKTState.p_known),
               func.count(func.distinct(BKTState.student_id)))
        .join(Student, Student.class_id == Class.id)
        .join(BKTState, BKTState.student_id == Student.id)
        .where(Class.grade == grade)
        .group_by(Class.id, Class.name)
        .order_by(func.avg(BKTState.p_known).asc())
    ).all()
    return {"grade": grade,
            "items": [{"class_id": cid, "class_name": name,
                       "avg_p_known": round(float(p), 3), "student_count": n}
                      for cid, name, p, n in rows]}


# =====================================================================
# R-04 考后分析：指标(Python) → crew(LLM 解读) → quality_flag 写回
# =====================================================================
@router.post("/paper/{paper_id}/analyze")
def paper_analyze(paper_id: int, user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """触发 analysis_crew 跑 R-04，返回结构化报告并把异常题写回 quality_flag。

    完整时间线：
      t1  校验：卷子存在 + 年级在 scope 内
      t2  取该卷全部作答（source_type=exam, source_id=paper_id）拼 DataFrame
          —— 没有作答数据 → 409，提示先跑演示脚本合成作答（M6-T4 的 gen_demo_data）
      t3  compute_item_metrics 纯 Python 算 P/D（数字不经过 LLM 的手）
      t4  attach_kp_ids 补知识点 → run_paper_analysis 三角色顺序跑
      t5  write_back_quality_flags：异常题 questions.quality_flag=abnormal
      t6  返回 {metrics, report}；下次 /exam/generate 抽题时 abnormal 自动沉底
    """
    _ensure_grade_in_scope(scopes, user, _paper_grade(db, paper_id))

    import pandas as pd

    from app.services.analysis import attach_kp_ids, compute_item_metrics, write_back_quality_flags
    from app.crews.analysis_crew import run_paper_analysis

    # 取出考试作答
    rows = db.execute(
        select(StudentAnswer.student_id, StudentAnswer.question_id,
               StudentAnswer.score, Question.kp_id)
        .join(Question, Question.id == StudentAnswer.question_id)
        .where(StudentAnswer.source_type == "exam")
        .where(StudentAnswer.source_id == paper_id)
        .where(StudentAnswer.is_correct.isnot(None))   # 未判分（主观题待确认）不进统计
    ).all()
    if not rows:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="该试卷还没有已判分的作答数据，先布置作答（演示用 scripts/gen_demo_data.py）")

    # 压成一张dataframe （rows ，4） 
    df = pd.DataFrame(rows, columns=["student_id", "q_id", "score", "kp_id"])

    # 加一行 full_score
    df["full_score"] = 100.0 / max(len({r[1] for r in rows}), 1)   # 卷面 100 均摊到每题

    metrics = compute_item_metrics(df)
    
    metrics = attach_kp_ids(metrics, {int(r[1]): r[3] for r in rows})

    paper = db.get(ExamPaper, paper_id)

    # 三角色Agent，按顺序跑（analysis_crew）
    report = run_paper_analysis(metrics, title=paper.title if paper else "", grade=paper.grade if paper else "")

    written = write_back_quality_flags(metrics)   # 异常题写回，R-08 下次消费

    return {"paper_id": paper_id, "metrics": metrics,
            "report": report.model_dump(), "abnormal_written": written}


# 试卷信息获取
def _paper_grade(db: Session, paper_id: int) -> str:
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"试卷 {paper_id} 不存在")
    
    return paper.grade



# =====================================================================
# R-08 出卷三件套
# =====================================================================

# 试卷生成
@router.post("/exam/generate")
def exam_generate(body: ExamGenerateIn, user: CurrentUser = Depends(get_current_user),
                  scopes: list[dict] = Depends(get_scopes)):
    """R-08 生成试卷：exam_crew（规划→抽题→审核 ≤2 轮）→ 落库 draft。

    返回 paper_id + QuotaPlan 配额表 + 缺口清单 + 审核报告。
    审核 passed=False 时状态仍是 draft，/assign 定稿前组长可人工看配额表。
    """
    _ensure_grade_in_scope(scopes, user, body.grade)

    from app.crews.exam_crew import run_exam_generation

    result = run_exam_generation(
        exam_type=body.exam_type, 
        grade=body.grade, 
        subject=body.subject,
        total_score=body.total_score, 
        created_by=user.id,
    )
    return result


# 取出题策略表（题型配额表）quota
@router.get("/exam/{paper_id}/quota")
def exam_quota(paper_id: int, user: CurrentUser = Depends(get_current_user),
               scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """取配额表（组长可人工核对后再 assign 定稿）。"""
    _ensure_grade_in_scope(scopes, user, _paper_grade(db, paper_id))
    paper = db.get(ExamPaper, paper_id)
    return {"paper_id": paper.id, "title": paper.title, "status": paper.status,
            "quota_plan": paper.quota_plan_json, "review": paper.remark}



# 试卷定稿并同步
@router.post("/exam/{paper_id}/assign")
def exam_assign(paper_id: int, body: ExamAssignIn,
                user: CurrentUser = Depends(get_current_user),
                scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """试卷定稿并布置到目标班级（status → published）。

    作答数据由 scripts/gen_demo_data.py 合成（M6-T4）——演示闭环第二线：
    出卷 → 虚拟班级作答 → analyze → 异常题写回 → 下次出卷降权生效。
    """
    _ensure_grade_in_scope(scopes, user, _paper_grade(db, paper_id))

    from app.services.audit_service import record_audit

    paper = db.get(ExamPaper, paper_id)
    paper.status = "published"
    record_audit(db, user.id, "exam.assign", f"exam_papers:{paper_id}",
                 detail=str({"class_ids": body.class_ids}))
    db.commit()

    return {"paper_id": paper_id, "status": "published", "class_ids": body.class_ids}


@router.get("/questions/abnormal")
def abnormal_questions(subject: str = "math",
                       user: CurrentUser = Depends(get_current_user),
                       scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """历史异常题清单（R-04 产出、R-08 消费的接口）：quality_flag=abnormal 的题。"""

    rows = db.execute(
        select(Question).where(Question.quality_flag == "abnormal")
        .where(Question.subject == subject).order_by(Question.id)
    ).scalars().all()

    return [{"id": q.id, "kp_id": q.kp_id, "difficulty": q.difficulty,
             "content": q.content[:60], "quality_flag": q.quality_flag}
            for q in rows]


