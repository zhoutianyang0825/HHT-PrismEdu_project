# app/api/student.py
# M5-T1/T2 · 学生端端点组：SSE 流式引导 + interrupt resume + S-10 诊断测验。
#
# 🔴 本文件是 M5 最关键的两个打通之一（另一个在 teacher.py）：
#   LangGraph 的 interrupt 挂起 / Command(resume) 恢复，如何跨 HTTP 请求工作——
#
# 【SSE 与 interrupt 的配合协议（前端按 type 字段分发）】
#   每条 SSE 事件都是一行 "data: {json}\n\n"，json 里必有 type：
#     type=node      中间节点完成（前端可显示"正在思考…"进度）
#     type=interrupt 图挂起了！payload 里有本次的引导提示，前端渲染后，
#                    下一条学生输入必须 POST /api/student/resume
#     type=message   图跑完，content 是 Agent 最终回复
#     type=error     出错（SSE 生成器 try/except 兜底，不静默断流）
#     type=end       本次流结束
#
# 【thread_id 与会话续接】
#   thread_id = "{student_id}_{question_id}"（M2 定死的约定）。
#   checkpointer 是 PostgresSaver：挂起状态落 PG，后端重启后仍可 resume。
#   同一道题的所有 HTTP 请求带同样的 student_id+question_id，就续在同一个会话上。
#
# 【身份守卫】
#   学生只能操作自己（self scope）：body.student_id 必须等于当前 JWT 用户的学生档案 id。
#   admin 代演放行。这就是"越权守卫挂载"在数据层的落地。
from functools import lru_cache
import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.db.session import get_db
from app.models import (
    BKTState,
    Chapter,
    DiagnosticQuiz,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
)
from app.schemas.api import QuizSubmitIn, StudentChatIn, StudentResumeIn

router = APIRouter()


# =====================================================================
# 图运行时单例：首次请求时才连 PG 建 checkpointer/store（import 零副作用）
# =====================================================================
@lru_cache
def _get_student_graph():
    """学生主图单例（带 PG checkpointer + PG store）。

    为什么 lru_cache：build 本身很轻，但它依赖的 checkpointer/store 各持一个
    psycopg 连接池——必须全进程只有一份，不能每个请求 new。
    """
    from app.db.checkpointer import get_checkpointer
    from app.db.store import get_store
    from app.graphs.student.graph import build_student_graph

    return build_student_graph(checkpointer=get_checkpointer(), store=get_store())


@lru_cache
def _get_grading_graph():
    """批改图单例（quiz submit 逐题触发批改要用）。"""
    from app.db.checkpointer import get_checkpointer
    from app.graphs.teacher.grading_graph import build_grading_graph

    return build_grading_graph(checkpointer=get_checkpointer())


@lru_cache
def _get_quiz_graph():
    """S-10 子图单例（无 interrupt，不需要 checkpointer）。"""
    from app.graphs.student.quiz_graph import build_quiz_graph

    return build_quiz_graph()


# =====================================================================
# 守卫与工具
# =====================================================================
def _own_student_or_admin(db: Session, user: CurrentUser, student_id: int) -> Student:
    """self 通道守卫：当前用户必须是这个 student_id 档案的主人（或 admin）。

    得到的数据：body 里的 student_id（students.id）
    要做的操作：查档案 → 比对 user_id
    不满足会怎样：403。学生 A 拿自己的 token 查学生 B 的掌握度，在这里被拦下。
    """
    profile = db.get(Student, student_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"学生档案 {student_id} 不存在")
    if "admin" not in user.roles and profile.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden: 只能访问自己的数据")
    return profile


def _sse(event: dict) -> str:
    """把 dict 打包成一条 SSE 帧。格式铁律：`data: <json>\n\n`（两个换行结尾，浏览器才认为帧结束）。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _questions_without_answers(rows: list[Question]) -> list[dict]:
    """题目出题端脱敏：题干/选项/难度可以给，标准答案与解析绝不给。

    为什么单独一个函数：quiz 详情、作业题目两处共用，漏一处就是泄题。
    """
    return [
        {
            "id": q.id,
            "question_type": q.question_type,
            "difficulty": q.difficulty,
            "content": q.content,
            "options": json.loads(q.options) if q.options else None,
            "kp_id": q.kp_id,
        }
        for q in rows
    ]


# =====================================================================
# SSE 流式：学生输入 → 图跑（含 interrupt 挂起检测）
# =====================================================================
def _stream_graph_events(graph, graph_input, config):
    """把一次图的执行包成 SSE 事件生成器（学生端与 resume 共用）。

    得到的数据：graph 已编译图 / graph_input invoke 入参（dict 或 Command）/
                config 含 thread_id
    要做的操作：graph.stream 逐节点产事件 → 遇 __interrupt__ 发 interrupt 帧 →
                图跑完发 message + end 帧
    为什么要 try/except（架构文档坑表"SSE 生成器里的异常被吞"）：
        生成器里抛异常，前端只会看到连接断开、无任何提示——
        必须捕获后 yield 一条 type=error 帧再结束，前端才能弹出错误。
    """
    try:
        for chunk in graph.stream(graph_input, config, stream_mode="updates"):
            # chunk 形如 {"classify_intent": {...增量...}}：一个节点一个 chunk
            if "__interrupt__" in chunk:
                payload = chunk["__interrupt__"][0].value
                yield _sse({"type": "interrupt", "payload": payload})
                continue

            node_name, diff = next(iter(chunk.items()))
            # respond 是统一出口节点：diff.messages 最后一条就是给学生的最终回复
            if node_name == "respond" and diff.get("messages"):
                last = diff["messages"][-1]
                content = last.content if isinstance(last, AIMessage) else str(last)
                yield _sse({"type": "message", "content": content})
            else:
                # 中间节点进度（前端可忽略，也可显示"正在分类意图…"）
                yield _sse({"type": "node", "node": node_name})
        yield _sse({"type": "end"})
    except Exception as exc:   # noqa: BLE001 最后一道网：任何异常都要让前端知道
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})


def _graph_state(graph, config):
    """读 thread 当前状态（判断是否正挂在 interrupt 上）。"""
    return graph.get_state(config)


@router.post("/answer/stream")
def answer_stream(
    body: StudentChatIn,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """学生输入（SSE 流式返回）。核心接口：引导/讲解/判分全走这里。

    完整时间线（一次"求答案"交互）：
      t1  POST /api/student/answer/stream {student_id, question_id, message}
      t2  守卫：profile.user_id == user.id（或 admin）
      t3  读 thread 状态：若上一轮还挂在 interrupt（学生刷新了页面再输入），
          防御性转 resume——比让前端死记状态更稳
      t4  graph.stream(...)：safety_check → classify_intent → … 逐节点产 SSE 帧
      t5  guide_scaffold 里 interrupt 挂起 → 流里出现 type=interrupt 帧 → 连接结束
      t6  学生回答后前端改调 /resume，同一 thread 续跑
    """
    _own_student_or_admin(db, user, body.student_id)

    graph = _get_student_graph()
    config = {"configurable": {"thread_id": f"{body.student_id}_{body.question_id}"}}

    # 防御性 resume：图还挂着，这条输入就是"对上一次提示的回应"，不能用新输入 invoke
    snap = _graph_state(graph, config)
    if snap.next:
        gen = _stream_graph_events(graph, Command(resume=body.message), config)
    else:
        gen = _stream_graph_events(
            graph,
            {"student_id": body.student_id, "question_id": body.question_id,
             "messages": [{"role": "user", "content": body.message}]},
            config,
        )

    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/resume")
def resume(body: StudentResumeIn, user: CurrentUser = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """恢复被 interrupt 挂起的会话：message 包进 Command(resume=...)。

    得到的数据：student_id + question_id 定位 thread；message 是学生的回应
    要做的操作：守卫 → Command(resume) invoke → SSE 帧
    图没挂起时调用会怎样：LangGraph 对"无挂起却 resume"直接报错，
        在生成器里被 try/except 接住 → 前端收到 type=error，提示刷新重试。
    """
    _own_student_or_admin(db, user, body.student_id)

    graph = _get_student_graph()
    config = {"configurable": {"thread_id": f"{body.student_id}_{body.question_id}"}}
    gen = _stream_graph_events(graph, Command(resume=body.message), config)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# =====================================================================
# 作业与掌握度
# =====================================================================
@router.get("/homework")
def my_homework(user: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """当前学生的作业列表（self 通道）。"""
    profile = db.execute(
        select(Student).where(Student.user_id == user.id)
    ).scalar_one_or_none()
    if profile is None and "admin" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="当前账号没有学生档案")

    rows = db.execute(
        select(Homework).where(Homework.class_id == profile.class_id)
        .order_by(Homework.created_at.desc())
    ).scalars().all()
    return [
        {"id": h.id, "title": h.title, "kp_ids": h.kp_ids, "created_at": str(h.created_at)}
        for h in rows
    ]


@router.get("/homework/{homework_id}/questions")
def homework_questions(homework_id: int, user: CurrentUser = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """作业题目列表（不带答案——_questions_without_answers 脱敏）。"""
    profile = db.execute(select(Student).where(Student.user_id == user.id)).scalar_one_or_none()
    if profile is None and "admin" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="当前账号没有学生档案")

    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="作业不存在")
    # 越权：别的班的作业不能看（self 通道只认自己班级）
    if "admin" not in user.roles and hw.class_id != profile.class_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden: 该作业不属于你的班级")

    rows = db.execute(
        select(Question).join(HomeworkQuestion, HomeworkQuestion.question_id == Question.id)
        .where(HomeworkQuestion.homework_id == homework_id)
        .order_by(Question.id)
    ).scalars().all()
    return {"homework_id": homework_id, "title": hw.title,
            "questions": _questions_without_answers(list(rows))}


@router.get("/mastery/{student_id}")
def mastery(student_id: int, granularity: str = "kp",
            user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """掌握度数据，granularity=kp 按知识点 / chapter 按章节聚合（雷达图数据源）。"""
    _own_student_or_admin(db, user, student_id)

    if granularity == "kp":
        rows = db.execute(
            select(BKTState.kp_id, KnowledgePoint.name, BKTState.p_known)
            .join(KnowledgePoint, KnowledgePoint.id == BKTState.kp_id)
            .where(BKTState.student_id == student_id)
            .order_by(BKTState.p_known.asc())
        ).all()
        return {"granularity": "kp",
                "items": [{"kp_id": k, "name": n, "p_known": round(float(p), 3)} for k, n, p in rows]}

    # chapter 粒度：知识点 → 章节两层聚合求平均
    rows = db.execute(
        select(Chapter.id, Chapter.name, BKTState.p_known)
        .join(KnowledgePoint, KnowledgePoint.chapter_id == Chapter.id)
        .join(BKTState, BKTState.kp_id == KnowledgePoint.id)
        .where(BKTState.student_id == student_id)
    ).all()
    acc: dict[str, dict] = {}
    for ch_id, ch_name, p in rows:
        bucket = acc.setdefault(ch_id, {"name": ch_name, "sum": 0.0, "n": 0})
        bucket["sum"] += float(p)
        bucket["n"] += 1
    return {"granularity": "chapter",
            "items": [{"chapter_id": cid, "name": b["name"],
                       "p_known": round(b["sum"] / b["n"], 3)} for cid, b in acc.items()]}


# =====================================================================
# S-10 诊断测验三件套
# =====================================================================
@router.post("/quiz/generate")
def quiz_generate(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
                  days: int = 30, total: int = 8):
    """S-10 生成诊断测验：跑 quiz_graph 子图（同步接口，架构文档 8.1 定的）。

    得到的数据：当前登录学生的档案 id
    要做的操作：quiz_graph.invoke（错题知识点 → 图谱外扩 → BKT Top5 → 配题 → 落库）
    返回：quiz_id + target_points（可解释性留痕）+ 题目列表（脱敏无答案）
    """
    profile = db.execute(select(Student).where(Student.user_id == user.id)).scalar_one_or_none()
    if profile is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="当前账号没有学生档案")

    result = _get_quiz_graph().invoke(
        {"student_id": profile.id, "days": days, "total": total},
        config={"configurable": {"thread_id": f"quiz_{profile.id}"}},
    )
    if result.get("quiz_id") is None:
        return {"quiz_id": None, "generated_reason": result.get("generated_reason", ""),
                "target_points": result.get("target_points", []), "questions": []}

    # 题目脱敏后返回（子图里 questions 带答案字段，绝不能原样出 API）
    rows = db.execute(
        select(Question).where(Question.id.in_(result["question_ids"])).order_by(Question.id)
    ).scalars().all()
    return {
        "quiz_id": result["quiz_id"],
        "generated_reason": result["generated_reason"],
        "target_points": result["target_points"],
        "questions": _questions_without_answers(list(rows)),
    }


@router.get("/quiz/{quiz_id}")
def quiz_detail(quiz_id: int, user: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """取测验详情与题目（不含答案，作答走 submit）。"""
    quiz = db.get(DiagnosticQuiz, quiz_id)
    if quiz is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="测验不存在")
    _own_student_or_admin(db, user, quiz.student_id)   # 只能看自己的测验

    rows = db.execute(
        select(Question).where(Question.id.in_(quiz.question_ids)).order_by(Question.id)
    ).scalars().all()
    return {"quiz_id": quiz.id, "generated_reason": quiz.generated_reason,
            "target_points": quiz.target_points_json,
            "questions": _questions_without_answers(list(rows))}


@router.post("/quiz/{quiz_id}/submit")
def quiz_submit(quiz_id: int, body: QuizSubmitIn,
                user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """提交整套测验作答：逐题建 student_answers（source_type=quiz）并触发批改。

    完整时间线（8 题混合卷）：
      t1  守卫：quiz 归属校验（_own_student_or_admin）
      t2  逐题 INSERT student_answers（source_type="quiz", source_id=quiz_id）
      t3  逐题调批改图：
          客观题 → 规则直判 confirmed，响应里立刻有 score/is_correct；
          主观题 → LLM 建议后 interrupt 挂起 → 响应标 "pending_confirm"，
                  等老师在老师端 /teacher/grade/{answer_id}/confirm 裁决
      t4  客观题批改图内部已完成 BKT 回写；主观题等确认后才回写
    返回：逐题结果列表 + 汇总（confirmed_n / pending_n）
    """
    quiz = db.get(DiagnosticQuiz, quiz_id)
    if quiz is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="测验不存在")
    profile = _own_student_or_admin(db, user, quiz.student_id)

    # ---- t2 落作答行（一个事务全写，失败整体回滚）----
    answer_ids: dict[int, int] = {}   # question_id -> answer_id
    try:
        for item in body.answers:
            row = StudentAnswer(
                source_type="quiz", source_id=quiz.id,
                question_id=item.question_id, student_id=profile.id,
                content=item.content,
            )
            db.add(row)
            db.flush()                     # 拿自增 id，供批改图用
            answer_ids[item.question_id] = row.id
        db.commit()
    except Exception:
        db.rollback()
        raise

    # ---- t3 逐题批改 ----
    graph = _get_grading_graph()
    results, confirmed_n, pending_n = [], 0, 0
    for item in body.answers:
        aid = answer_ids[item.question_id]
        cfg = {"configurable": {"thread_id": f"grading_{aid}"}}
        out = graph.invoke({"answer_id": aid, "teacher_user_id": user.id}, cfg)

        if "__interrupt__" in out:
            # 主观题挂起等老师：score 先不给，页面显示"待老师确认"
            pending_n += 1
            results.append({"answer_id": aid, "question_id": item.question_id,
                            "status": "pending_confirm"})
        else:
            confirmed_n += 1
            results.append({"answer_id": aid, "question_id": item.question_id,
                            "status": out.get("status", "confirmed"),
                            "is_correct": out.get("is_correct"),
                            "score": out.get("final_score"),
                            "p_known_after": out.get("p_known_after")})

    return {"quiz_id": quiz.id, "results": results,
            "confirmed_n": confirmed_n, "pending_n": pending_n}
