"""
app/api/research.py
M5-T1 · 教研端端点组：年级聚合 / R-04 考后分析 / R-08 出卷 / 出卷记录。

【通道纪律】本组全部走聚合通道（require_aggregate_channel）：
  researcher 的 scope 是 {"type": "aggregate", "grade": "初二", "subject": "math"}，
  所有查询按 (grade, subject) 过滤聚合输出 —— 接口层面就不存在"按年级查学生明细"的路。
  teacher 也能调（T-03 同源数据，detail 通道天然放行聚合接口），admin 恒放行。

【R-04 → R-08 闭环在 API 层的落点】
  POST /paper/{id}/analyze ：算指标 → analysis_crew → write_back_quality_flags
  POST /exam/generate      ：exam_crew 内部 fetch_candidates 把 abnormal 沉底
  → "上一份卷子的分析结果，自动变成下一份卷子的组卷依据"

  ⚠ 曾经有个 GET /questions/abnormal 把写回的异常题列出来给人看，
    已删除（2026-10-26）：它只是展示、不能操作，且与 analyze 返回里的
    report.abnormal_items 信息重复。写回与降权两条链路一行未动。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
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
    ExamPaperClass,
    ExamPaperQuestion,
    KnowledgePoint,
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
# R-01 前置：当前账号的教研范围（前端年级选项的唯一数据源）
# =====================================================================
# 年级排序基准：中文年级必须按学段排，不能按字典序
# （字典序会排出 "初一, 初三, 初二" 这种反人类顺序）
GRADE_ORDER = ("初一", "初二", "初三")


def _grade_sort_key(grade: str) -> tuple[int, str]:
    """年级排序键：已知学段按 GRADE_ORDER 排，未知学段排最后再按字面排。"""
    if grade in GRADE_ORDER:
        return (GRADE_ORDER.index(grade), "")
    return (len(GRADE_ORDER), grade)


@router.get("/my-scope")
def my_scope(user: CurrentUser = Depends(get_current_user),
             scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """返回当前账号能看的教研范围（年级 + 学科），供前端渲染年级选项。

    得到的数据：
        scopes —— 双通道权限列表，如
            [{"type": "detail", "class_ids": [3], "subjects": ["math"]},
             {"type": "aggregate", "grade": "初二", "subject": "math"}]
        user.roles —— 如 ["teacher", "researcher"]（双角色两通道并存）

    要做的操作：
        1. 只取 aggregate 通道里的 grade —— 每条任命一个（兼任两年级就是两条）
        2. admin 不受任命限制 → 取 classes 表里真实存在的年级（不硬编码）
        3. 年级按学段顺序排、学科去重排序

    返回：
        {"grades": ["初二"], "subjects": ["math"], "limited": True}
        limited=False 只出现在 admin，表示"不按任命限制"

    为什么：
        前端原来把年级写死成三项，教研组长点进去能看到自己没被任命的年级 ——
        数据是空的，选了还会被 _ensure_grade_in_scope 判 403。
        可见范围该由权限算出来，不能让前端猜。
    """
    grades = _agg_grades(scopes, user)
    limited = grades is not None

    # admin 没有任命记录但权限不限 → 用库里真实存在的年级兜底
    if grades is None:
        grades = set(db.execute(select(Class.grade).distinct()).scalars().all())

    subjects = {s["subject"] for s in scopes if s["type"] == "aggregate"}
    if not subjects:
        from app.models import KnowledgePoint

        subjects = set(db.execute(select(KnowledgePoint.subject).distinct()).scalars().all())

    return {
        "grades": sorted(grades, key=_grade_sort_key),
        "subjects": sorted(subjects) or ["math"],
        "limited": limited,
    }


# =====================================================================
# 课程显示名映射（前端把 kp_id / chapter_id 翻成人话的唯一数据源）
# =====================================================================
@router.get("/naming")
def naming_map(subject: str = "math",
               user: CurrentUser = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """返回课程元数据的 id → 名称映射。

    得到的数据：subject="math"

    要做的操作：查 chapters + knowledge_points，按 subject 过滤

    返回：
        {"chapter": {"ch_quadratic": "第21章 一元二次方程", ...},
         "kp": {"kp_peifang": "配方法", ...}}

    为什么：
        配额表的「章节」列和抽题缺口里的知识点前缀都只有 id。
        名字必须从库里回捞——前端那套静态表在知识图谱重构后已经过期
        （旧 ch_quadratic 之外还有 LLM 自造的 ch_21_quad_eq），硬编码必然对不上。
        这个接口不含学生数据、不含年级聚合，纯课程元数据，所以不挂年级校验。
    """
    from app.services.naming import fetch_naming

    return fetch_naming(subject=subject)


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
          —— 同时按题回读 exam_paper_questions.score 当每题满分（不能用 100 均摊）
      t3  compute_item_metrics 纯 Python 算 P/D（数字不经过 LLM 的手）
      t4  attach_kp_ids 补知识点 → run_paper_analysis 三角色顺序跑
      t5  write_back_quality_flags：异常题 questions.quality_flag=abnormal
      t6  返回 {paper_id, paper_title, metrics, report}；下次 /exam/generate 抽题时 abnormal 自动沉底
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

    # 每题满分取卷面真实分值，不再"100 分均摊到每题"。
    #   定稿时 publish_paper 已把 选3/填4/解8 摊好的分值固化进 exam_paper_questions，
    #   这里直接回读，P/D 才是真比例。
    #   为什么必须改：均摊会把一道 2.5 分的选择题当成 5.56 分，
    #   学生全对 → P=0.45（应≈1）；一道 11 分的解答题全对 → P=1.98（>1）。
    #   结果就是"选择题集体过难、解答题集体过易"，异常题清单一整片是假的。
    score_map = {
        int(qid): float(sc or 0)
        for qid, sc in db.execute(
            select(ExamPaperQuestion.question_id, ExamPaperQuestion.score)
            .where(ExamPaperQuestion.paper_id == paper_id)
        ).all()
    }
    # 兜底：卷面表里没有这道题（正常不会发生——学生只能考已定稿的卷）。
    #   真出现说明库里卷面与作答对不上，退回均摊口径，至少不让 P/D 变成 NaN。
    fallback_full = 100.0 / max(len({r[1] for r in rows}), 1)
    df["full_score"] = [
        score_map.get(int(qid)) or fallback_full for qid in df["q_id"]
    ]

    metrics = compute_item_metrics(df)
    
    metrics = attach_kp_ids(metrics, {int(r[1]): r[3] for r in rows})

    paper = db.get(ExamPaper, paper_id)

    # 三角色Agent，按顺序跑（analysis_crew）
    report = run_paper_analysis(metrics, title=paper.title if paper else "", grade=paper.grade if paper else "")

    written = write_back_quality_flags(metrics)   # 异常题写回，R-08 下次消费

    return {"paper_id": paper_id, "paper_title": paper.title if paper else "",
            "metrics": metrics,
            "report": report.model_dump(), "abnormal_written": written}


# 试卷信息获取
def _paper_grade(db: Session, paper_id: int) -> str:
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"试卷 {paper_id} 不存在")
    
    return paper.grade


def _heal_saved_plan(plan_json: dict | None) -> dict:
    """读历史配额表时，把 chapter_id 拉回课程库里的真值。

    得到的数据：plan_json={"plan": {"items": [{"kp_id": "kp_peifang",
                "chapter_id": "ch_21_quad_eq", ...}]}, "question_ids": [...], "gaps": [...]}

    要做的操作：拿 kp_id 反查 knowledge_points.chapter_id，覆盖掉自造的 chapter_id

    返回：同结构，items[].chapter_id 已换成 "ch_quadratic" 这类真实值

    为什么：
        旧卷子的 chapter_id 是命题规划师自己拼的（ch_21_quad_eq），库里根本没有这个章节，
        前端就算接上「id → 名字」映射也翻不出来。
        kp_id 是真的，而知识点表里本来就存着归属章节 → 按 kp 反查是最稳的补救。
        深拷贝再改：直接改 ORM 上的 JSON 字段会被当成脏数据，可能顺手写回库。
    """
    import copy

    from app.services.naming import fetch_kp_chapter_map

    if not isinstance(plan_json, dict):
        return plan_json or {}

    data = copy.deepcopy(plan_json)
    items = ((data.get("plan") or {}).get("items")) or []
    kp_ids = [it.get("kp_id") for it in items if it.get("kp_id")]
    kp_chapter = fetch_kp_chapter_map(kp_ids)

    for it in items:
        real_chapter = kp_chapter.get(it.get("kp_id"))
        if real_chapter:
            it["chapter_id"] = real_chapter

    return data



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


# =====================================================================
# R-08 出卷记录：清单 + 单卷题目详情
# =====================================================================
# 🔴 路由顺序有讲究：/exam/papers 必须写在 /exam/{paper_id} 形态之前，
#   否则 "papers" 会被当成 paper_id 去转 int → 422。
@router.get("/exam/papers")
def exam_papers(grade: str | None = None, subject: str = "math", limit: int = 30,
                user: CurrentUser = Depends(get_current_user),
                scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """出卷记录清单（教研端「出卷记录」模块的唯一数据源）。

    得到的数据：grade（可空 → 我任命范围内的全部年级）、subject、limit（最多 100）

    要做的操作：
        t1 权限：指定了 grade 就校验它在不在我的任命范围；没指定就按权限集合过滤
        t2 卷子按 id 倒序（最新出的在最上面）
        t3 批量补三件事：推送班级名、卷面题数、卷面总分
           —— 三条 IN 查询取完，绝不按卷循环查（30 份卷 = 30 次往返）

    返回：
        {"total": 2, "items": [
            {"paper_id": 6, "title": "初三数学月考卷", "exam_type": "monthly",
             "grade": "初三", "subject": "math", "status": "published",
             "created_at": "2026-10-26 16:10:03", "published_at": "2026-10-26 17:05:11",
             "class_ids": [4], "class_names": ["初三1班"],
             "question_count": 18, "full_score": 100.0, "has_paper_questions": true}]}

    为什么需要这个接口：
        出卷后页面一刷新就"看不见自己刚生成的卷子"，只能靠记 paper_id。
        组长真正要回答的是三件事——出过几份卷、发给了谁、那份卷里有什么题，
        分别落在 exam_papers / exam_paper_classes / exam_paper_questions 三张表。

    为什么题数要兜底：
        卷面表只在定稿（/assign）时才固化。draft 卷查 exam_paper_questions 是空的，
        这时退回 quota_plan_json 里的 question_ids 长度，至少能告诉组长"这份卷规划了几题"。
    """
    allowed = _agg_grades(scopes, user)
    if grade:
        _ensure_grade_in_scope(scopes, user, grade)

    stmt = select(ExamPaper).order_by(ExamPaper.id.desc()).limit(max(1, min(limit, 100)))
    if grade:
        stmt = stmt.where(ExamPaper.grade == grade)
    elif allowed is not None:
        # 空集合时 in_([]) 恒假 → 直接返回空清单，不能退化成"看全部"
        stmt = stmt.where(ExamPaper.grade.in_(sorted(allowed)))
    if subject:
        # subject 是 M7 才补的列，老卷子为 NULL；只按 subject 硬过滤会把老卷全漏掉
        stmt = stmt.where(or_(ExamPaper.subject == subject, ExamPaper.subject.is_(None)))

    papers = list(db.execute(stmt).scalars().all())
    if not papers:
        return {"total": 0, "items": []}

    ids = [p.id for p in papers]

    class_rows = db.execute(
        select(ExamPaperClass.paper_id, Class.id, Class.grade, Class.name)
        .join(Class, Class.id == ExamPaperClass.class_id)
        .where(ExamPaperClass.paper_id.in_(ids))
        .order_by(Class.grade, Class.name)
    ).all()
    classes: dict[int, list[dict]] = {}
    for pid, cid, cgrade, cname in class_rows:
        classes.setdefault(pid, []).append({"id": cid, "name": f"{cgrade}{cname}"})

    size_rows = db.execute(
        select(ExamPaperQuestion.paper_id,
               func.count(ExamPaperQuestion.id),
               func.coalesce(func.sum(ExamPaperQuestion.score), 0))
        .where(ExamPaperQuestion.paper_id.in_(ids))
        .group_by(ExamPaperQuestion.paper_id)
    ).all()
    sizes = {pid: (cnt, float(total or 0)) for pid, cnt, total in size_rows}

    items = []
    for p in papers:
        cnt, total = sizes.get(p.id, (0, 0.0))
        planned = (p.quota_plan_json or {}).get("question_ids") or []
        cls = classes.get(p.id, [])
        items.append({
            "paper_id": p.id,
            "title": p.title,
            "exam_type": p.exam_type,
            "grade": p.grade,
            "subject": p.subject or "math",
            "status": p.status,
            "created_at": str(p.created_at) if p.created_at else None,
            "published_at": str(p.published_at) if p.published_at else None,
            "class_ids": [c["id"] for c in cls],
            "class_names": [c["name"] for c in cls],
            "question_count": cnt or len(planned),
            "full_score": round(total, 1),
            "has_paper_questions": bool(cnt),
        })

    return {"total": len(items), "items": items}


def _question_item(q: Question, kp: KnowledgePoint | None, seq: int,
                   score: float | None) -> dict:
    """把一道题拼成前端能直接渲染的形状。

    得到的数据：q（题目行）、kp（知识点行，可能为 None）、seq（卷面题号）、score（该题分值）

    要做的操作：抽题目正文/选项/答案/解析 + 题型/难度/知识点名

    返回：{"seq": 1, "question_id": 21, "question_type": "choice", "difficulty": "easy",
           "kp_id": "kp_peifang", "kp_name": "配方法", "score": 3.0,
           "content": "…", "options": "A. …", "answer": "A", "analysis": "…"}

    为什么带标准答案：教研组长出完卷要核对答案与分值，这是他的本职；
                     学生端走的是另一个接口（exam_detail），那边才脱敏。
    """
    return {
        "seq": seq,
        "question_id": q.id,
        "question_type": q.question_type,
        "difficulty": q.difficulty,
        "kp_id": q.kp_id,
        "kp_name": kp.name if kp else None,
        "score": score,
        "content": q.content,
        "options": q.options,
        "answer": q.answer,
        "analysis": q.analysis,
    }


@router.get("/exam/{paper_id}/detail")
def exam_paper_detail(paper_id: int, user: CurrentUser = Depends(get_current_user),
                      scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """单份试卷的题目详情（出卷记录点开看到的那一屏）。

    得到的数据：paper_id

    要做的操作：
        t1 权限：卷子的年级必须在我的任命范围
        t2 优先读 exam_paper_questions（定稿时固化的卷面：题号 + 每题分值，顺序已按 选→填→解 重排）
        t3 卷面为空（draft 未定稿）→ 退回 quota_plan_json.question_ids，
           按规划顺序回捞，分值给 null（前端显示"待定稿"，不编一个假分数）

    返回：
        {"paper": {"paper_id": 6, "title": "…", "grade": "初三", "status": "published",
                   "exam_type": "monthly", "created_at": "…", "published_at": "…",
                   "class_names": ["初三1班"]},
         "drafted": true, "full_score": 100.0, "question_count": 18,
         "questions": [ …见 _question_item… ]}

    为什么 draft 卷也要能看：
        组长生成完卷子会先核对配额表，再决定要不要定稿。这时候题目已经抽好了，
        只是分值还没分配。看不到题就只能"闭眼定稿"。
    """
    _ensure_grade_in_scope(scopes, user, _paper_grade(db, paper_id))
    paper = db.get(ExamPaper, paper_id)

    rows = db.execute(
        select(ExamPaperQuestion, Question, KnowledgePoint)
        .join(Question, Question.id == ExamPaperQuestion.question_id)
        .outerjoin(KnowledgePoint, KnowledgePoint.id == Question.kp_id)
        .where(ExamPaperQuestion.paper_id == paper.id)
        .order_by(ExamPaperQuestion.seq)
    ).all()

    questions: list[dict] = []
    if rows:
        questions = [_question_item(q, kp, seq=pq.seq, score=pq.score) for pq, q, kp in rows]

    else:
        ids = (paper.quota_plan_json or {}).get("question_ids") or []
        if ids:
            qmap = {q.id: q for q in db.execute(
                select(Question).where(Question.id.in_(ids))).scalars().all()}
            kp_ids = {q.kp_id for q in qmap.values() if q.kp_id}
            kps = {k.id: k for k in db.execute(
                select(KnowledgePoint).where(KnowledgePoint.id.in_(kp_ids))).scalars().all()} if kp_ids else {}
            for i, qid in enumerate(ids, start=1):
                q = qmap.get(qid)
                if q is None:
                    continue            # 题库被清过/题被删，跳过而不是整卷 404
                questions.append(_question_item(q, kps.get(q.kp_id), seq=i, score=None))

    cls_rows = db.execute(
        select(Class.id, Class.grade, Class.name)
        .join(ExamPaperClass, ExamPaperClass.class_id == Class.id)
        .where(ExamPaperClass.paper_id == paper.id)
        .order_by(Class.grade, Class.name)
    ).all()

    drafted = bool(rows)
    full_score = round(sum(float(q["score"] or 0) for q in questions), 1) if drafted else None

    return {
        "paper": {
            "paper_id": paper.id,
            "title": paper.title,
            "grade": paper.grade,
            "subject": paper.subject or "math",
            "exam_type": paper.exam_type,
            "status": paper.status,
            "created_at": str(paper.created_at) if paper.created_at else None,
            "published_at": str(paper.published_at) if paper.published_at else None,
            "class_ids": [cid for cid, _, _ in cls_rows],
            "class_names": [f"{g}{n}" for _, g, n in cls_rows],
        },
        "drafted": drafted,
        "full_score": full_score,
        "question_count": len(questions),
        "questions": questions,
    }


# 取出题策略表（题型配额表）quota
@router.get("/exam/{paper_id}/quota")
def exam_quota(paper_id: int, user: CurrentUser = Depends(get_current_user),
               scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """取配额表（组长可人工核对后再 assign 定稿）。"""
    _ensure_grade_in_scope(scopes, user, _paper_grade(db, paper_id))
    paper = db.get(ExamPaper, paper_id)
    return {"paper_id": paper.id, "title": paper.title, "status": paper.status,
            # 读时就地纠正 chapter_id：旧卷子里存的是规划师自造的假章节 id
            "quota_plan": _heal_saved_plan(paper.quota_plan_json), "review": paper.remark}



# 试卷定稿并同步
@router.post("/exam/{paper_id}/assign")
def exam_assign(paper_id: int, body: ExamAssignIn,
                user: CurrentUser = Depends(get_current_user),
                scopes: list[dict] = Depends(get_scopes), db: Session = Depends(get_db)):
    """试卷定稿并推送给目标班级：status → published，同时把"发给谁"真正落库。

    得到的数据：paper_id + body.class_ids（教研组长勾的班，通常同一年级）

    要做的操作：
      t1 校验：卷子在不在 / 年级在不在我的教研范围内 / 班级是否存在
      t2 ensure_paper_questions：把 quota_plan_json 里的题号固化成卷面
         （题号 + 每题分值，全卷分值之和 = 卷面总分）
      t3 写 exam_paper_classes：这份卷给哪几个班
      t4 paper.status='published' + published_at，审计埋点
      t5 数影响面：几个班 / 几个学生 / 几个科任老师

    返回：
        {"paper_id": 3, "status": "published", "class_ids": [1, 2],
         "question_count": 20, "full_score": 100.0,
         "student_count": 78, "teacher_count": 3,
         "classes": [{"id": 1, "name": "1班", "grade": "初二", "students": 39}]}

    为什么这一步要改：
        旧实现把 class_ids 只写进 audit_logs.detail 字符串就返回了 —— 账记了，
        但 exam_paper_classes 空着，学生端查不到自己被推了卷、老师端收不到主观题，
        "定稿"之后链路直接断掉。现在推送范围是查询得出来的真数据。

        作答数据不再依赖 scripts/gen_demo_data.py 合成：
        学生真的在这份卷上作答，R-04 考后分析直接能跑。
    """
    _ensure_grade_in_scope(scopes, user, _paper_grade(db, paper_id))

    from app.services.audit_service import record_audit
    from app.services.exam_delivery import publish_paper

    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"试卷 {paper_id} 不存在")

    try:
        result = publish_paper(db, paper, body.class_ids)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception:
        db.rollback()
        raise

    record_audit(db, user.id, "exam.assign", f"exam_papers:{paper_id}",
                 detail=str({"class_ids": body.class_ids,
                             "question_count": result["question_count"],
                             "students": result["student_count"]}))
    db.commit()

    return result
