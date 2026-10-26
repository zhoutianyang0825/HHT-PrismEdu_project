"""app/services/exam_delivery.py
考试派发域：定稿推送 → 学生取卷 → 交卷落库 → 老师按题分组批改。

【这张文件补的是哪一段断链】
  R-08 出卷的终点原本是 exam_papers.status='published'，
  /assign 只把 class_ids 写进 audit_logs 的 detail 字符串 —— 记了一笔账，
  但没有任何一张表回答"哪个班该考这份卷、这题值几分、谁交过卷"。
  于是：学生端看不到卷、老师端收不到主观题、R-04 拿不到作答数据。
  本文件把这三问变成三张表（exam_paper_questions / exam_paper_classes / exam_attempts）。

【一图看懂调用顺序】
  教研组长定稿
      └─ publish_paper()     写推送班级 + 固化卷面题目与分值
  学生进考试模块
      └─ student_exam_list() 查"我们班被推了哪些卷 + 我交没交"
      └─ exam_detail()       取题目（脱敏）+ 我已填的答案（回填草稿）
  学生点提交
      └─ submit_exam()       逐题落 student_answers + 建 exam_attempts
                             客观题立刻判分（调用方跑批改图），主观题留 NULL 等老师
  老师进待批改
      └─ teacher_exam_list() 我教的班里已推送的卷 + 待批改条数
      └─ group_grading()     按题分组：第 1 题 → 张三/李四/王五的作答
  老师裁决完一题
      └─ recalc_attempt()    重算该生总分，主观题全判完 → status=graded
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Class,
    ExamAttempt,
    ExamPaper,
    ExamPaperClass,
    ExamPaperQuestion,
    Question,
    Student,
    StudentAnswer,
)

# 题型权重：只用来把「卷面总分」摊到每题上。
#   选择 3 / 填空 4 / 解答 8 近似初中数学常规配分（100 分卷）。
#   为什么不用"总分 ÷ 题数"均摊：一道解答题和一道选择题同分，
#   老师批改时会认为分值与题量不符，也不符合真实卷面。
TYPE_WEIGHT = {"choice": 3.0, "fill": 4.0, "subjective": 8.0}

# 卷面顺序：选择 → 填空 → 解答（抽题结果是按知识点分组的，直接排会乱序）
TYPE_ORDER = {"choice": 0, "fill": 1, "subjective": 2}


# =====================================================================
# 分值分账
# =====================================================================
def allocate_scores(types: list[str], total_score: float = 100.0) -> list[float]:
    """把卷面总分按题型权重摊到每题，保证求和 == total_score（0.5 分精度）。

    得到的数据：
        types = ["choice", "choice", "fill", "subjective"]（按卷面顺序）
        total_score = 100

    要做的操作：
        t1 每题理论分 = 总分 × 本题权重 / 权重之和
        t2 四舍五入到 0.5（中学卷不给 0.37 分这种），最低 1 分
        t3 差额逐题 0.5 分地吸收（往当前分值最大的题上补/扣）

    返回：[2.5, 2.5, 4.0, 91.0] 这类列表，len 与 types 一致，sum == total_score

    为什么：
        没有这一步，exam_paper_questions.score 只能填 0，
        学生端"客观题得分"和 R-04 的得分率就全是 0。
    """
    n = len(types)
    if n == 0:
        return []

    weights = [TYPE_WEIGHT.get(t, 4.0) for t in types]
    w_sum = sum(weights) or 1.0

    scores = [max(1.0, round(total_score * w / w_sum * 2) / 2) for w in weights]

    # 差额吸收：round 是逐题做的，累加必然对不上整卷总分
    diff = round(total_score - sum(scores), 1)
    guard = 0
    while abs(diff) >= 0.5 and guard < 1000:
        idx = max(range(n), key=lambda i: scores[i])
        step = 0.5 if diff > 0 else -0.5
        if scores[idx] + step < 0.5:
            break
        scores[idx] = round(scores[idx] + step, 1)
        diff = round(diff - step, 1)
        guard += 1

    return scores


# =====================================================================
# 定稿推送
# =====================================================================
def paper_question_ids(paper: ExamPaper) -> list[int]:
    """从配额表 JSON 里取抽题结果（exam_crew 落库时就存在这里）。"""
    data = paper.quota_plan_json or {}
    return [int(q) for q in (data.get("question_ids") or [])]


def paper_total_score(paper: ExamPaper) -> float:
    """卷面满分：优先取配额表里的 total_score，缺省 100。"""
    data = paper.quota_plan_json or {}
    plan = data.get("plan") or {}
    try:
        return float(plan.get("total_score") or 100)
    except (TypeError, ValueError):
        return 100.0


def ensure_paper_questions(db: Session, paper: ExamPaper) -> list[ExamPaperQuestion]:
    """确保卷面题目行存在（幂等），返回按题号排好的行。

    得到的数据：paper（exam_papers 一行，quota_plan_json 里有 question_ids）

    要做的操作：
        t1 已有卷面行 → 直接返回（重复定稿不能重新分一次分，那会把老师已经看过的分值改掉）
        t2 否则按「选择→填空→解答、同题型按 id」排好顺序
        t3 按题型权重分摊总分，逐题写 exam_paper_questions(seq, score)
        t4 题目 id 在题库里已不存在的（被删/被禁用）跳过，不报错

    返回：[ExamPaperQuestion(seq=1, score=2.5), ...]，可能为空列表（题目全被删）

    为什么：
        旧卷子（改动前定稿的）没有这张表，学生端一取卷就是 0 题。
        走这个函数即"读时就地补齐"，不用单独写数据修复脚本。
    """
    existing = db.execute(
        select(ExamPaperQuestion)
        .where(ExamPaperQuestion.paper_id == paper.id)
        .order_by(ExamPaperQuestion.seq)
    ).scalars().all()
    if existing:
        return list(existing)

    ids = paper_question_ids(paper)
    if not ids:
        return []

    rows = db.execute(select(Question).where(Question.id.in_(ids))).scalars().all()
    by_id = {q.id: q for q in rows}

    # 按题号顺序（配额表里的抽题顺序会被知识点分组打乱，这里重排）
    ordered = sorted(
        (by_id[qid] for qid in ids if qid in by_id),
        key=lambda q: (TYPE_ORDER.get(q.question_type, 9), q.id),
    )
    if not ordered:
        return []

    scores = allocate_scores([q.question_type for q in ordered], paper_total_score(paper))

    made: list[ExamPaperQuestion] = []
    for seq, (q, score) in enumerate(zip(ordered, scores), start=1):
        row = ExamPaperQuestion(paper_id=paper.id, question_id=q.id, seq=seq, score=score)
        db.add(row)
        made.append(row)

    db.flush()
    return made


def publish_paper(db: Session, paper: ExamPaper, class_ids: list[int]) -> dict:
    """定稿并推送给目标班级（幂等：重复推送只补新增的班）。

    得到的数据：paper（必须是 draft/published）、class_ids（教研组长勾的班）

    要做的操作：
        t1 校验班级存在（不存在的直接 400，别静默丢掉）
        t2 卷面题目固化（ensure_paper_questions）
        t3 推送记录只增不减：新增的 class_id 插入 exam_paper_classes
        t4 paper.status='published' + published_at
        t5 数一下影响面：几个班 / 几个学生 / 几个科任老师

    返回：
        {"paper_id": 3, "status": "published", "class_ids": [1, 2],
         "question_count": 20, "full_score": 100.0,
         "student_count": 78, "teacher_count": 3,
         "classes": [{"id": 1, "name": "1班", "grade": "初二", "students": 39}]}

    为什么返回影响面：
        教研组长点"定稿布置"后，最想知道的是"到底发给谁了"。
        只回 {"status": "published"} 等于什么都没说。
    """
    from sqlalchemy import func as sa_func

    classes = db.execute(select(Class).where(Class.id.in_(class_ids))).scalars().all()
    found = {c.id for c in classes}
    missing = [cid for cid in class_ids if cid not in found]
    if missing:
        raise ValueError(f"班级不存在：{missing}")

    questions = ensure_paper_questions(db, paper)

    already = set(db.execute(
        select(ExamPaperClass.class_id).where(ExamPaperClass.paper_id == paper.id)
    ).scalars().all())

    for cid in class_ids:
        if cid not in already:
            db.add(ExamPaperClass(paper_id=paper.id, class_id=cid))

    paper.status = "published"
    if paper.published_at is None:
        paper.published_at = datetime.now(timezone.utc)

    # 影响面：学生数按班统计，老师数按授课关系（同一老师教两个班只算一次）
    student_rows = db.execute(
        select(Student.class_id, sa_func.count())
        .where(Student.class_id.in_(class_ids))
        .group_by(Student.class_id)
    ).all()
    per_class = {cid: n for cid, n in student_rows}

    from app.models import TeachingAssignment

    teacher_ids = set(db.execute(
        select(TeachingAssignment.teacher_id)
        .where(TeachingAssignment.class_id.in_(class_ids))
        .where(TeachingAssignment.subject == paper.subject)
    ).scalars().all())

    db.flush()

    return {
        "paper_id": paper.id,
        "status": paper.status,
        "class_ids": list(class_ids),
        "question_count": len(questions),
        "full_score": round(sum(q.score for q in questions), 1),
        "student_count": sum(per_class.values()),
        "teacher_count": len(teacher_ids),
        "classes": [
            {"id": c.id, "name": c.name, "grade": c.grade,
             "students": per_class.get(c.id, 0)}
            for c in sorted(classes, key=lambda c: (c.grade, c.name))
        ],
    }


# =====================================================================
# 学生端：取卷 / 交卷
# =====================================================================
def student_papers(db: Session, student: Student) -> list[dict]:
    """当前学生被推送到的试卷列表 + 我的考试状态。

    得到的数据：student（含 class_id）

    要做的操作：exam_paper_classes 按 class_id 过滤 → 拼 attempt（可能没有）

    返回：
        [{"paper_id": 3, "title": "初二数学月考卷", "exam_type": "monthly",
          "grade": "初二", "subject": "math", "published_at": "...",
          "question_count": 20, "full_score": 100.0,
          "attempt": null | {"status": "submitted", "total_score": 62.0, ...},
          "pending_subjective": 2}]

    为什么 pending_subjective 单独给：
        学生交卷后最常问"我多少分"，但主观题没批完时总分是不完整的。
        与其显示一个会跳变的分数，不如明说"还有 2 题等老师批"。
    """
    paper_rows = db.execute(
        select(ExamPaper)
        .join(ExamPaperClass, ExamPaperClass.paper_id == ExamPaper.id)
        .where(ExamPaperClass.class_id == student.class_id)
        .where(ExamPaper.status == "published")
        .order_by(ExamPaper.published_at.desc(), ExamPaper.id.desc())
    ).scalars().all()

    if not paper_rows:
        return []

    paper_ids = [p.id for p in paper_rows]

    # 卷面题量/满分一次查完，别在循环里查
    qcount: dict[int, int] = {}
    qfull: dict[int, float] = {}
    for pid, n, s in db.execute(
        select(ExamPaperQuestion.paper_id, func.count(), func.sum(ExamPaperQuestion.score))
        .where(ExamPaperQuestion.paper_id.in_(paper_ids))
        .group_by(ExamPaperQuestion.paper_id)
    ).all():
        qcount[pid] = int(n)
        qfull[pid] = float(s or 0)

    attempts = {
        a.paper_id: a
        for a in db.execute(
            select(ExamAttempt)
            .where(ExamAttempt.student_id == student.id)
            .where(ExamAttempt.paper_id.in_(paper_ids))
        ).scalars().all()
    }

    pending_rows = db.execute(
        select(StudentAnswer.source_id, func.count())
        .where(StudentAnswer.student_id == student.id)
        .where(StudentAnswer.source_type == "exam")
        .where(StudentAnswer.source_id.in_(paper_ids))
        .where(StudentAnswer.is_correct.is_(None))
        .group_by(StudentAnswer.source_id)
    ).all()
    pending = {sid: int(n) for sid, n in pending_rows}

    out = []
    for p in paper_rows:
        a = attempts.get(p.id)
        out.append({
            "paper_id": p.id,
            "title": p.title,
            "exam_type": p.exam_type,
            "grade": p.grade,
            "subject": p.subject,
            "published_at": str(p.published_at) if p.published_at else None,
            "question_count": qcount.get(p.id, 0),
            "full_score": round(qfull.get(p.id, 0), 1),
            "pending_subjective": pending.get(p.id, 0),
            "attempt": None if a is None else {
                "id": a.id,
                "status": a.status,
                "objective_score": a.objective_score,
                "subjective_score": a.subjective_score,
                "total_score": a.total_score,
                "full_score": a.full_score,
                "submitted_at": str(a.submitted_at),
                "graded_at": str(a.graded_at) if a.graded_at else None,
            },
        })
    return out


def exam_detail(db: Session, paper: ExamPaper, student: Student) -> dict:
    """取一份卷的作答视图：题目（脱敏）+ 我已填的答案 + 我的考试记录。

    得到的数据：paper / student

    要做的操作：
        t1 卷面题目按 seq 取，题干预题干、选项从 questions 取
        t2 **不带 answer / analysis**（泄题）
        t3 我在这份卷上的历史作答按 question_id 拼回填（草稿/已交卷都能回显）

    返回：
        {"paper_id": 3, "title": "...", "full_score": 100.0, "exam_type": "monthly",
         "attempt": null | {...},
         "questions": [{"id": 7, "seq": 1, "question_type": "choice",
                        "content": "...", "options": ["A. ...", ...], "score": 2.5,
                        "my_answer": {"content": "A", "attachments": ["/uploads/..."]}}]}
    """
    import json as _json

    rows = db.execute(
        select(ExamPaperQuestion, Question)
        .join(Question, Question.id == ExamPaperQuestion.question_id)
        .where(ExamPaperQuestion.paper_id == paper.id)
        .order_by(ExamPaperQuestion.seq)
    ).all()

    mine = {
        a.question_id: a
        for a in db.execute(
            select(StudentAnswer)
            .where(StudentAnswer.source_type == "exam")
            .where(StudentAnswer.source_id == paper.id)
            .where(StudentAnswer.student_id == student.id)
            # 按 id 升序取：万一还有历史重复行，稳定取到最早那一条（通常是判过分的），
            # 而不是随机取到后插入的未判行
            .order_by(StudentAnswer.id)
        ).scalars().all()
    }

    questions = []
    healed = False
    from app.services.grading import grade_objective

    for pq, q in rows:
        ans = mine.get(q.id)

        # 脏数据自愈：客观题在交卷那一刻就该判完（规则判分，不存在"待批"），
        # 走到这里说明那次交卷中途断过（重复提交插了未判的新行 / 判分抛异常）。
        # 这里按规则补判并落库 —— 否则页面上会出现"选择题待老师批改"，
        # 老师端待批改队列里也会混进一堆选择题。
        if ans is not None and ans.is_correct is None and q.question_type != "subjective":
            res = grade_objective(q.question_type, ans.content or "",
                                  q.answer or "", float(pq.score or 0))
            ans.is_correct = res["is_correct"]
            ans.score = res["score"]
            healed = True

        questions.append({
            "id": q.id,
            "seq": pq.seq,
            "question_type": q.question_type,
            "difficulty": q.difficulty,
            "content": q.content,
            "options": _json.loads(q.options) if q.options else None,
            "kp_id": q.kp_id,
            "score": pq.score,
            "my_answer": None if ans is None else {
                "content": ans.content,
                "attachments": ans.attachment_json or [],
                "is_correct": ans.is_correct,
                "score": ans.score,
            },
        })

    attempt = db.execute(
        select(ExamAttempt)
        .where(ExamAttempt.paper_id == paper.id)
        .where(ExamAttempt.student_id == student.id)
    ).scalar_one_or_none()

    if healed:
        # 补判出来的分不能只落在明细里：考试记录的总分要跟着重算，
        # 否则学生看到"这题答对了"，总分却没变。
        if attempt is not None:
            recalc_attempt(db, paper.id, student.id)
            db.refresh(attempt)
        db.commit()

    return {
        "paper_id": paper.id,
        "title": paper.title,
        "grade": paper.grade,
        "subject": paper.subject,
        "exam_type": paper.exam_type,
        "full_score": round(sum(q["score"] for q in questions), 1),
        "attempt": None if attempt is None else {
            "id": attempt.id,
            "status": attempt.status,
            "objective_score": attempt.objective_score,
            "subjective_score": attempt.subjective_score,
            "total_score": attempt.total_score,
            "full_score": attempt.full_score,
            "submitted_at": str(attempt.submitted_at),
            "graded_at": str(attempt.graded_at) if attempt.graded_at else None,
        },
        "questions": questions,
    }


def student_can_take(db: Session, paper: ExamPaper, student: Student) -> bool:
    """这份卷是否推给了这个学生的班。"""
    if paper.status != "published":
        return False
    return db.execute(
        select(ExamPaperClass.id)
        .where(ExamPaperClass.paper_id == paper.id)
        .where(ExamPaperClass.class_id == student.class_id)
    ).scalar_one_or_none() is not None


def save_exam_answers(
    db: Session,
    paper: ExamPaper,
    student: Student,
    answers: list,
    fill_missing_objective: bool = True,
) -> dict[int, int]:
    """交卷第一步：把逐题作答写进 student_answers（一个事务），返回 question_id → answer_id。

    得到的数据：answers = [{"question_id": 7, "content": "A", "attachments": [...]}, ...]
                fill_missing_objective=True 时，学生漏答的客观题补一行空作答

    要做的操作：
        t1 同一 (学生, 卷, 题) 已有作答就复用行并覆盖内容（改答案）
        t2 新的就 INSERT
        t3 不在卷面上的 question_id 直接丢掉（防伪造）
        t4 漏答的客观题补空行 —— 不补的话"没做"和"做错"就分不开，
           总分也少算这几题的应扣分

    返回：{7: 101, 8: 102}

    为什么允许覆盖：
        学生可能在提交前反复改，前端如果每次点"暂存"都插一行，
        老师端待批改就会出现 5 条张三对同一题的作答。
    """
    allowed = {
        qid: qtype for qid, qtype in db.execute(
            select(ExamPaperQuestion.question_id, Question.question_type)
            .join(Question, Question.id == ExamPaperQuestion.question_id)
            .where(ExamPaperQuestion.paper_id == paper.id)
        ).all()
    }

    existing: dict[int, StudentAnswer] = {}
    for a in db.execute(
        select(StudentAnswer)
        .where(StudentAnswer.source_type == "exam")
        .where(StudentAnswer.source_id == paper.id)
        .where(StudentAnswer.student_id == student.id)
        .order_by(StudentAnswer.id)
    ).scalars().all():
        keep = existing.get(a.question_id)
        if keep is None:
            existing[a.question_id] = a
            continue

        # 同一题出现两行 = 之前重复交卷留下的脏数据（一次交卷中途失败、学生又点了一次，
        # 每次 save 都插了新行）。这里就地合并：留 id 最小的一条，其余删掉。
        #
        # 为什么非合不可：exam_detail 只能用 {question_id: answer} 结构回显，
        # 多行时 dict 会随机取到后插入的那条 —— 那条很可能还没判分（is_correct=NULL），
        # 于是选择题在页面上显示成"待老师批改"。用户就是这么踩到的。
        db.delete(a)

    ids: dict[int, int] = {}
    for item in answers:
        qid = int(item.question_id)
        if qid not in allowed:
            continue
        content = (item.content or "").strip()
        attachments = list(item.attachments or [])
        row = existing.get(qid)
        if row is None:
            row = StudentAnswer(
                source_type="exam", source_id=paper.id,
                question_id=qid, student_id=student.id,
                content=content, attachment_json=attachments or None,
            )
            db.add(row)
            db.flush()
        else:
            row.content = content
            row.attachment_json = attachments or None
        ids[qid] = row.id

    # 漏答的客观题：补空行 → 规则判分自然判错、扣分，也进错题本
    # 主观题不补：空作答占着老师待批改队列没意义
    if fill_missing_objective:
        for qid, qtype in allowed.items():
            if qtype == "subjective" or qid in ids:
                continue
            row = StudentAnswer(
                source_type="exam", source_id=paper.id,
                question_id=qid, student_id=student.id, content="",
            )
            db.add(row)
            db.flush()
            ids[qid] = row.id

    db.flush()
    return ids


def create_attempt(db: Session, paper: ExamPaper, student: Student,
                   objective_score: float, full_score: float,
                   subjective_pending: int) -> ExamAttempt:
    """交卷第二步：建考试记录（重复交卷 → ValueError）。

    得到的数据：客观题已判分、主观题待批改的题数

    要做的操作：
        t1 有旧记录 → ValueError（一考一卷，重考该由老师重置而不是盖掉记录）
        t2 status：还有主观题待批 → submitted；全判完（纯客观卷）→ graded

    返回：ExamAttempt 行（已 flush，有 id）
    """
    dup = db.execute(
        select(ExamAttempt)
        .where(ExamAttempt.paper_id == paper.id)
        .where(ExamAttempt.student_id == student.id)
    ).scalar_one_or_none()
    if dup is not None:
        raise ValueError("这份试卷你已经交过了")

    row = ExamAttempt(
        paper_id=paper.id, student_id=student.id,
        status="submitted" if subjective_pending else "graded",
        objective_score=round(objective_score, 1),
        subjective_score=0.0,
        total_score=round(objective_score, 1),
        full_score=round(full_score, 1),
    )
    db.add(row)
    db.flush()
    return row


def recalc_attempt(db: Session, paper_id: int, student_id: int) -> ExamAttempt | None:
    """重算某个学生这份卷的得分与状态（老师每次裁决主观题后调用）。

    得到的数据：paper_id / student_id

    要做的操作：
        t1 卷面满分 = sum(exam_paper_questions.score)
        t2 客观题得分 = sum(score) where 题型 != subjective 且 is_correct is not null
        t3 主观题得分 = sum(score) where 题型 == subjective 且 is_correct is not null
        t4 剩余未判分题数 == 0 → status='graded' + graded_at

    返回：更新后的 ExamAttempt；没有考试记录（学生没交卷）返回 None

    为什么按 student_answers 重算而不是累加：
        老师可能改分、可能驳回（is_correct 变 None），累加会算错；
        重算永远和库里现状一致。
    """
    attempt = db.execute(
        select(ExamAttempt)
        .where(ExamAttempt.paper_id == paper_id)
        .where(ExamAttempt.student_id == student_id)
    ).scalar_one_or_none()
    if attempt is None:
        return None

    rows = db.execute(
        select(Question.question_type, StudentAnswer.score, StudentAnswer.is_correct)
        .join(Question, Question.id == StudentAnswer.question_id)
        .where(StudentAnswer.source_type == "exam")
        .where(StudentAnswer.source_id == paper_id)
        .where(StudentAnswer.student_id == student_id)
    ).all()

    objective = 0.0
    subjective = 0.0
    pending = 0
    for qtype, got, ok in rows:
        if ok is None:
            pending += 1
            continue
        if qtype == "subjective":
            subjective += float(got or 0)
        else:
            objective += float(got or 0)

    # 满分取卷面（不是"已作答题目之和"）——学生漏答的题也是这份卷的一部分，
    # 满分若跟着作答走，漏答越多分母越小、得分率反而虚高。
    full = float(db.execute(
        select(func.coalesce(func.sum(ExamPaperQuestion.score), 0))
        .where(ExamPaperQuestion.paper_id == paper_id)
    ).scalar_one())

    attempt.objective_score = round(objective, 1)
    attempt.subjective_score = round(subjective, 1)
    attempt.total_score = round(objective + subjective, 1)
    attempt.full_score = round(full, 1)
    if pending == 0:
        attempt.status = "graded"
        attempt.graded_at = func.now()
    else:
        attempt.status = "submitted"
        attempt.graded_at = None

    db.flush()
    return attempt


# =====================================================================
# 老师端：卷列表 / 按题分组批改
# =====================================================================
def teacher_papers(db: Session, class_ids: list[int] | None, subject: str | None = None) -> list[dict]:
    """我教的班里已推送的试卷 + 交卷/待批改统计。

    得到的数据：class_ids（detail 通道班级；None = admin 不限）、subject（可选过滤）

    要做的操作：
        t1 exam_paper_classes 按班级过滤 → 卷子
        t2 每卷：推送班数、应交人数、已交人数、主观题待批改条数

    返回：
        [{"paper_id": 3, "title": "初二数学月考卷", "grade": "初二",
          "exam_type": "monthly", "published_at": "...",
          "class_names": ["初二1班"], "question_count": 20,
          "student_count": 39, "submitted_count": 31, "pending_count": 7}]
    """
    stmt = select(ExamPaper).where(ExamPaper.status == "published")

    # 用 EXISTS 而不是 join + distinct：
    #   exam_papers 有 quota_plan_json（PG 的 json 类型没有相等运算符），
    #   SELECT DISTINCT exam_papers.* 会直接报
    #   "could not identify an equality operator for type json"。
    #   sqlite 沙盒不会报，所以这条坑只有连真 PG 才炸得出来。
    pushed = select(ExamPaperClass.id).where(ExamPaperClass.paper_id == ExamPaper.id)
    if class_ids is not None:
        pushed = pushed.where(ExamPaperClass.class_id.in_(class_ids))
    stmt = stmt.where(pushed.exists())

    if subject:
        stmt = stmt.where(ExamPaper.subject == subject)

    papers = db.execute(stmt.order_by(ExamPaper.id.desc())).scalars().all()
    if not papers:
        return []

    paper_ids = [p.id for p in papers]

    # 本次可见范围内被推送的班级（admin 不限 = 全部推送班）
    pc_rows = db.execute(
        select(ExamPaperClass.paper_id, Class.id, Class.grade, Class.name)
        .join(Class, Class.id == ExamPaperClass.class_id)
        .where(ExamPaperClass.paper_id.in_(paper_ids))
    ).all()
    classes_of: dict[int, list[str]] = {}
    class_ids_of: dict[int, list[int]] = {}
    for pid, cid, grade, name in pc_rows:
        classes_of.setdefault(pid, []).append(f"{grade}{name}")
        class_ids_of.setdefault(pid, []).append(cid)

    qcount = {
        pid: int(n) for pid, n in db.execute(
            select(ExamPaperQuestion.paper_id, func.count())
            .where(ExamPaperQuestion.paper_id.in_(paper_ids))
            .group_by(ExamPaperQuestion.paper_id)
        ).all()
    }

    submitted = {
        pid: int(n) for pid, n in db.execute(
            select(ExamAttempt.paper_id, func.count())
            .where(ExamAttempt.paper_id.in_(paper_ids))
            .group_by(ExamAttempt.paper_id)
        ).all()
    }

    pending_rows = db.execute(
        select(StudentAnswer.source_id, func.count())
        .join(Question, Question.id == StudentAnswer.question_id)
        .where(StudentAnswer.source_type == "exam")
        .where(StudentAnswer.source_id.in_(paper_ids))
        .where(StudentAnswer.is_correct.is_(None))
        .where(Question.question_type == "subjective")
        .group_by(StudentAnswer.source_id)
    ).all()
    pending = {pid: int(n) for pid, n in pending_rows}

    student_count: dict[int, int] = {}
    for pid, cids in class_ids_of.items():
        student_count[pid] = int(db.execute(
            select(func.count()).select_from(Student).where(Student.class_id.in_(cids))
        ).scalar_one())

    return [{
        "paper_id": p.id,
        "title": p.title,
        "grade": p.grade,
        "subject": p.subject,
        "exam_type": p.exam_type,
        "published_at": str(p.published_at) if p.published_at else None,
        "class_names": classes_of.get(p.id, []),
        "question_count": qcount.get(p.id, 0),
        "student_count": student_count.get(p.id, 0),
        "submitted_count": submitted.get(p.id, 0),
        "pending_count": pending.get(p.id, 0),
    } for p in papers]


def group_grading(db: Session, paper: ExamPaper,
                  class_ids: list[int] | None = None) -> dict:
    """按题目分组的主观题作答（老师批改页的主视图）。

    得到的数据：paper、class_ids（老师可见班；None = 不限）

    要做的操作：
        t1 取卷面全部主观题（seq 升序）—— 一题一组
        t2 取这些题的全班作答（exam 来源 + 该卷），按题归堆
        t3 每条作答带学生名/学号、作答原文、手写图、当前判分状态

    返回：
        {"paper_id": 3, "title": "...", "full_score": 100.0,
         "summary": {"subjective_total": 6, "pending": 12, "graded": 30},
         "groups": [{"question_id": 21, "seq": 12, "content": "…", "score": 8.0,
                     "kp_id": "kp_peifang", "pending": 3, "graded": 8,
                     "answers": [{"answer_id": 501, "student": "张三(20260001)",
                                  "student_no": "20260001", "content": "…",
                                  "attachments": ["/uploads/…"],
                                  "is_correct": null, "score": null,
                                  "submitted_at": "…"}]}]}

    为什么按题分组而不是按学生：
        老师批一道题时脑子里只有一条评分标准，连续看 30 个学生对同一题的答案，
        判分尺度才稳。按学生排会在一份卷里反复切换标准。
    """
    qrows = db.execute(
        select(ExamPaperQuestion, Question)
        .join(Question, Question.id == ExamPaperQuestion.question_id)
        .where(ExamPaperQuestion.paper_id == paper.id)
        .where(Question.question_type == "subjective")
        .order_by(ExamPaperQuestion.seq)
    ).all()

    stmt = (
        select(StudentAnswer, Student.student_no, Class.name, Class.grade)
        .join(Student, Student.id == StudentAnswer.student_id)
        .join(Class, Class.id == Student.class_id)
        .where(StudentAnswer.source_type == "exam")
        .where(StudentAnswer.source_id == paper.id)
    )
    if class_ids is not None:
        stmt = stmt.where(Student.class_id.in_(class_ids))
    answers = db.execute(stmt.order_by(StudentAnswer.submitted_at)).all()

    # 学生姓名在 users 表，单独查一次（避免 join 三个表让 stmt 变复杂）
    from app.models import User

    names = {}
    profs = db.execute(
        select(Student.id, User.name)
        .join(User, User.id == Student.user_id)
        .where(Student.id.in_({a.student_id for a, *_ in answers} or {0}))
    ).all()
    names = {sid: name for sid, name in profs}

    by_q: dict[int, list[dict]] = {}
    for a, no, cname, cgrade in answers:
        by_q.setdefault(a.question_id, []).append({
            "answer_id": a.id,
            "student": f"{names.get(a.student_id, '学生')}({no or '-'})",
            "student_no": no,
            "student_id": a.student_id,
            "class_name": f"{cgrade}{cname}",
            "content": a.content,
            "attachments": a.attachment_json or [],
            "is_correct": a.is_correct,
            "score": a.score,
            "submitted_at": str(a.submitted_at),
        })

    groups = []
    total_pending = 0
    total_graded = 0
    for pq, q in qrows:
        items = by_q.get(q.id, [])
        pending = sum(1 for it in items if it["is_correct"] is None)
        graded = len(items) - pending
        total_pending += pending
        total_graded += graded
        groups.append({
            "question_id": q.id,
            "seq": pq.seq,
            "content": q.content,
            "answer": q.answer,
            "analysis": q.analysis,
            "kp_id": q.kp_id,
            "score": pq.score,
            "pending": pending,
            "graded": graded,
            "answers": items,
        })

    return {
        "paper_id": paper.id,
        "title": paper.title,
        "grade": paper.grade,
        "full_score": round(sum(pq.score for pq, _ in qrows), 1),
        "summary": {
            "subjective_total": len(qrows),
            "pending": total_pending,
            "graded": total_graded,
        },
        "groups": groups,
    }
