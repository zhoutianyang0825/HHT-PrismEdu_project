"""
app/api/student.py
M5-T1/T2 · 学生端：SSE 流式引导 + interrupt resume + S-10 诊断测验 + M7 考试模块。

🔴 本文件是 M5 最关键的两个打通之一（另一个在 teacher.py）：
  LangGraph 的 interrupt 挂起 / Command(resume) 恢复，如何跨 HTTP 请求工作——

【SSE 与 interrupt 的配合协议（前端按 type 字段分发）】
  每条 SSE 事件都是一行 "data: {json}\n\n"，json 里必有 type：
    type=node      中间节点完成（前端可显示"正在思考…"进度）
    type=interrupt 图挂起了！payload 里有本次的引导提示，前端渲染后，
                   下一条学生输入必须 POST /api/student/resume
    type=message   图跑完，content 是 Agent 最终回复
    type=error     出错（SSE 生成器 try/except 兜底，不静默断流）
    type=end       本次流结束

【thread_id 与会话续接】
  thread_id = "{student_id}_{question_id}"（M2 定死的约定）。
  checkpointer 是 PostgresSaver：挂起状态落 PG，后端重启后仍可 resume。
  同一道题的所有 HTTP 请求带同样的 student_id+question_id，就续在同一个会话上。

【身份守卫】
  学生只能操作自己（self scope）：body.student_id 必须等于当前 JWT 用户的学生档案 id。
  admin 代演放行。这就是"越权守卫挂载"在数据层的落地。

【M7 考试模块（本文件下半段）】
  教研端定稿 → 推送班级（exam_paper_classes）→ 学生在这四步里完成一场考试：
    GET  /exams               我被推了哪些卷、交没交、多少分
    GET  /exams/{id}          题目（脱敏）+ 我已填的答案
    POST /exams/{id}/submit   交卷：逐题落库 → 客观题立刻判 → 建考试记录
    POST /upload/answer-image 手写解题步骤拍图上传（base64）
"""

from functools import lru_cache
import base64
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.db.session import get_db
from app.models import (
    BKTState,
    Chapter,
    DiagnosticQuiz,
    ExamPaper,
    ExamPaperQuestion,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    Question,
    Student,
    StudentAnswer,
)
from app.schemas.api import (
    ExamSubmitIn,
    ImageUploadIn,
    QuizSubmitIn,
    StudentChatIn,
    StudentResumeIn,
)

router = APIRouter()


# =====================================================================
# 图运行时单例：首次请求时才连 PG 建 checkpointer/store（import 零副作用）
# =====================================================================
@lru_cache
def _get_student_graph():
    """学生主图单例（带 PG checkpointer + PG store）。

    lru_cache：依赖的 checkpointer/store 各持一个psycopg 连接池，必须全进程只有一份，不能每个请求 new。
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
    """把 dict 打包成一条 SSE 格式帧。格式铁律：`data: <json>\n\n`（两个换行结尾，浏览器才认为帧结束）。"""
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

    得到的数据：graph 已编译图 / graph_input invoke 入参（dict 或 Command）/ config 含 thread_id

    要做的操作：graph.stream 逐节点产事件 → 遇 __interrupt__ 发 interrupt 帧 → 图跑完发 message + end 帧

    为什么要 try/except（架构文档坑表"SSE 生成器里的异常被吞"）：
        生成器里抛异常，前端只会看到连接断开、无任何提示——
        必须捕获后 yield 一条 type=error 帧再结束，前端才能弹出错误。
    """
    try:
        # 按 build_student_graph（）编译好的图，执行图的流式输出，且为增量模式：每执行完一个节点，就产出一个 带增量消息的chunk
        for chunk in graph.stream(graph_input, config, stream_mode="updates"):

            # chunk 形如 {"classify_intent": {...增量...}}：一个节点一个 chunk
            if "__interrupt__" in chunk:
                payload = chunk["__interrupt__"][0].value
                yield _sse({"type": "interrupt", "payload": payload})
                continue

            node_name, diff = next(iter(chunk.items()))

            
            #   前2轮引导时，respond 节点都是无增量消息的状态（因为“讲解终稿”是第3轮引导才出的答案），那么 LangGraph的updates 模式下吐出来的 chunk 是 {节点名: None} 
            #   所以 diff 可能是 None，直接 diff.get() 会炸 AttributeError: 'NoneType' object has no attribute 'get'。
            #   所以这里必须先判 None，对None进行处理 → 不发帧，继续下一个 chunk 。
            #   补一句精确的触发点：真正踩坑的是"讲解终稿已产出"那一刻（讲解路径 / 引导第 3 轮出终稿），
            #   此时 respond 判到 messages[-1] 已是 AIMessage 就 return {}；前 2 轮图停在 interrupt 上，respond 根本不执行。
            if node_name == "respond":
                if not isinstance(diff, dict) or not diff.get("messages"):
                    continue

                last = diff["messages"][-1]
                content = last.content if isinstance(last, AIMessage) else str(last)

                # 抛出 sse数据帧
                yield _sse({"type": "message", "content": content})

            else:
                # ★ 讲解路径补丁：explain_concept / guide_scaffold(满3轮) 会把 AIMessage 终稿写进 messages，
                # 该类节点有增量消息，所以 需要yield返回给前端， 否则讲解永远只以 type=node 进度帧出现，前端拿不到正文。
                msgs = diff.get("messages") if isinstance(diff, dict) else None
                if msgs and isinstance(msgs[-1], AIMessage):
                    yield _sse({"type": "message", "content": msgs[-1].content}) # 将新增的AIMessage的消息，包成sse 返回给前端

                else:
                    # 前端可忽略，也可显示"正在分类意图…"
                    yield _sse({"type": "node", "node": node_name})

        yield _sse({"type": "end"})

    except Exception as exc:   #  最后一道网：任何异常都要让前端知道
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
        t1  学生输入"这题答案是多少"
        走引导链路的是"求答案"类输入："这题答案是多少" → ask_answer

        t2  safety_check → classify_intent = ask_answer ✅

        t3  guide_scaffold：LLM 生成第 1 层提示 → interrupt(payload) 挂起
            └ yield interrupt 帧（前端显示提示）→ ⚠️ guide_scaffold函数栈 在interrupt处挂起，此时不会执行respond节点

        t3' 学生 resume 输入作答 → guide_scaffold 续跑记入 HumanMessage ✅

        t4  回 classify_intent 重新分类 → "提交作答" → grade_submit ✅
        
        t4' 归一化后字符串比对 vs 标答 ✅（你理解对：这就是 M2 简版）

        t5  答错 → respond
            输出固定鼓励文案：
            "还没对，别急。可以说「讲解一下」让我带你梳理思路，或者把你的计算过程发我…"
            └ yield message 帧给前端（这句话才是抛出的增量）

        t6  学生自己输入「讲解一下」→ 同一 thread 新一轮 invoke（⚠️ 这是新调用，不是"重放"）
            └ classify_intent = explain → explain_concept（RAG+LLM）
            └ 讲解 AIMessage 增量 → yield message 帧（补丁后能正常发到前端）✅

        t7  引导满 3 轮仍不会 → guide_scaffold 置 force_explain → 路由到 explain_concept
            └ 讲完整解答（写出最终答案，带 RAG 教材出处）
            └ respond 空过（AIMessage 已在 explain_concept 产出）→ END → yield end 帧 ✅

    ★ 五类意图（2026-10-25 新增 guide_scaffold）：
        ask_answer     "这题答案是多少"   → guide_scaffold（不给答案，先引导）
        guide_scaffold "这题考什么/没思路/给点提示" → guide_scaffold（直接进引导）
        submit_answer  提交自己的作答      → grade_submit
        explain        "讲解一下/详细讲讲"  → explain_concept
        off_topic      与本题无关的闲聊     → respond

    ★ homework_id 决定作答落库的"来源"（学生端「我的作业」点进来才有值）：
        有值 → source_type="homework"、source_id=作业id
                → 老师端 T-03「作业提交情况」按这两个字段查全班作答，才能查到
        None → source_type="chat"、source_id=0
                → 只给 S-10 诊断测验供错题数据，老师端看不到（自由练习本就不属于作业）
      两处都传：state 里存一份给同 thread 后续复用，config.configurable 里传一份给 resume 兜底
    """
    # 身份校验
    _own_student_or_admin(db, user, body.student_id)

    graph = _get_student_graph()

    # ★ homework_id 走 config.configurable 而不是只放 state：
    #   挂起后 resume 走 Command(resume=...) 不带 state 更新，
    #   只有 config 里这份才是本次请求的新鲜值（学生可能换了作业再回到同一题）
    config = {
        "configurable": {
            "thread_id": f"{body.student_id}_{body.question_id}",
            "homework_id": body.homework_id,
        }
    }

    # 防御性 resume：图还挂着，这条输入就是"对上一次提示的回应"，不能用新输入 invoke
    # 因为图都挂了checkpointer，所以先查：当前thread上，有没有interrupt挂起
    snap = _graph_state(graph, config)

    # 如果snap.next 非空，包 Command(resume=message)的值，进原先的interrupt
    if snap.next:
        gen = _stream_graph_events(graph, Command(resume=body.message), config)

    else:
        gen = _stream_graph_events(
            graph,
            {"student_id": body.student_id, 
             "question_id": body.question_id,
             "homework_id": body.homework_id,   # 作业上下文：判分落库定来源（homework/chat）
             "messages": [{"role": "user", "content": body.message}]
            },
            config,
        )

    # fastapi的流式响应，gen=生成器函数，流式响应中，自带async for 消费yield抛出来的值
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

 
@router.post("/resume")
def resume(body: StudentResumeIn, user: CurrentUser = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """恢复被 interrupt 挂起的会话：message 包进 Command(resume=...)。

    得到的数据：student_id + question_id 定位 thread；message 是学生的回应；
                homework_id 是作业上下文（自由练习不传）
    要做的操作：守卫 → Command(resume) invoke → SSE 帧
                homework_id 走 config.configurable：resume 不带 state 更新，
                只靠 state 里那份可能是旧的（学生可能先在自由练习答过同一题）
    图没挂起时调用会怎样：LangGraph 对"无挂起却 resume"直接报错，
        在生成器里被 try/except 接住 → 前端收到 type=error，提示刷新重试。
    """
    _own_student_or_admin(db, user, body.student_id)

    graph = _get_student_graph()
    # homework_id 同样走 config：resume 不带 state 更新，只有这里能覆盖旧值
    config = {
        "configurable": {
            "thread_id": f"{body.student_id}_{body.question_id}",
            "homework_id": body.homework_id,
        }
    }
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


@router.get("/homework/{homework_id}/progress")
def homework_progress(homework_id: int, user: CurrentUser = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """当前学生在这份作业上的完成进度（逐题状态 + 已答/总题数）。

    得到的数据：
        homework_id（作业 id）；
        学生档案由当前登录账号解析（self 通道，不看 query 参数）。

    要做的操作：
        1. 校验作业存在、且属于自己班级（admin 放行）
        2. 数这份作业共几题（homework_questions）
        3. 查自己在这些题上的作答（source_type='homework', source_id=作业id）
        4. 按题目 id 拼成逐题状态

    返回：
        {"homework_id": 6, "total": 10, "answered": 7, "correct": 5,
         "items": [{"question_id": 7, "is_correct": true, "score": 10.0}, ...]}

    为什么单开一个接口：
        学生端要在左侧题号旁边标 ✓/✗、顶部显示"已完成 7/10"，
        而 homework/{id}/questions 是脱敏题目列表、不含作答状态；
        完成度又必须从库里读真值——前端自己在内存里记会因刷新页面全部丢失。
    """
    profile = db.execute(select(Student).where(Student.user_id == user.id)).scalar_one_or_none()
    if profile is None and "admin" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="当前账号没有学生档案")

    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="作业不存在")
    if "admin" not in user.roles and hw.class_id != profile.class_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden: 该作业不属于你的班级")

    total = db.execute(
        select(func.count()).select_from(HomeworkQuestion)
        .where(HomeworkQuestion.homework_id == homework_id)
    ).scalar_one()

    rows = db.execute(
        select(StudentAnswer.question_id, StudentAnswer.is_correct, StudentAnswer.score)
        .where(StudentAnswer.student_id == profile.id)
        .where(StudentAnswer.source_type == "homework")
        .where(StudentAnswer.source_id == homework_id)
    ).all()

    items = [
        {"question_id": qid, "is_correct": bool(ok) if ok is not None else None, "score": score}
        for qid, ok, score in rows
    ]

    return {
        "homework_id": homework_id,
        "total": total,
        "answered": len(items),
        "correct": sum(1 for it in items if it["is_correct"] is True),
        "items": items,
    }


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


# =====================================================================
# M7 考试模块：取卷 / 交卷 / 手写图上传
# =====================================================================
UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "data" / "uploads"

# 只收这三种：手机拍照是 jpeg，截图是 png，苹果新机型可能是 webp
ALLOWED_IMAGE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024     # 6MB：1280px JPEG 约 200~400KB，留足余量


def _self_profile(db: Session, user: CurrentUser) -> Student:
    """当前登录账号的学生档案（self 通道唯一入口）。

    得到的数据：user（JWT 解析结果，含 roles）

    要做的操作：students 表按 user_id 查一行

    返回：Student 行

    不满足会怎样：403。没有学生档案的账号（纯老师/教研）调学生端接口一律拦下，
    否则 profile 为 None 会在后面拼出莫名其妙的 AttributeError。
    """
    profile = db.execute(
        select(Student).where(Student.user_id == user.id)
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="当前账号没有学生档案")
    return profile


@router.get("/exams")
def my_exams(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """我这一届的考试列表：被推送的卷 + 我的考试记录。

    得到的数据：当前登录账号 → 学生档案 → class_id

    要做的操作：
        t1 exam_paper_classes 按 class_id 过滤出"我们班被推的卷"
        t2 每卷拼上我的 exam_attempts（没交过就是 null）
        t3 数一下还有几道主观题等老师批

    返回：
        [{"paper_id": 3, "title": "初二数学月考卷", "question_count": 20,
          "full_score": 100.0, "pending_subjective": 0,
          "attempt": {"status": "graded", "total_score": 86.0, ...}}]

    为什么 attempt 为空也要返回：
        学生端要分两个区——「待考试」和「考试记录」。
        待考的卷 attempt=null，记录区的卷 attempt 有值，一个接口够用。
    """
    from app.services.exam_delivery import student_papers

    profile = _self_profile(db, user)
    return student_papers(db, profile)


@router.get("/exams/{paper_id}")
def exam_paper_detail(paper_id: int, user: CurrentUser = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """取一份卷的作答界面数据：题目（脱敏）+ 我已填的答案。

    得到的数据：paper_id

    要做的操作：
        t1 学生档案 + 卷子存在性
        t2 越权守卫：这份卷必须推给了我的班（exam_paper_classes），否则 403
        t3 卷面题目按 seq 顺序出（选择→填空→解答），不带标准答案与解析

    返回：见 exam_delivery.exam_detail 的 docstring

    为什么不能复用 /homework/{id}/questions：
        那份接口按 homework_questions 查题、也不带每题分值与"我的作答"，
        而考试界面必须显示"这题 8 分"和上次填了什么（刷新页面不丢草稿）。
    """
    from app.services.exam_delivery import exam_detail, student_can_take

    profile = _self_profile(db, user)
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"试卷 {paper_id} 不存在")

    if not student_can_take(db, paper, profile):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="forbidden: 这份试卷没有推送到你的班级")

    return exam_detail(db, paper, profile)


@router.post("/exams/{paper_id}/submit")
def exam_submit(paper_id: int, body: ExamSubmitIn,
                user: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """交卷：逐题落库 → 客观题立刻判分 → 建考试记录。

    得到的数据：paper_id + body.answers（逐题 content 与手写图地址）

    要做的操作：
      t1 守卫：自己 + 卷子存在 + 卷子推给了自己的班 + 没交过（重复提交 409）
      t2 save_exam_answers：一个事务写 student_answers（source_type='exam'），
         漏答的客观题补空作答（漏答≠未判分，必须判 0 分）
      t3 客观题逐题跑批改图 → 规则直判 → 写 grading_records + BKT + 错题本
      t4 主观题不跑图：只留 is_correct=NULL，等老师在待批改里按题批
      t5 建 exam_attempts：客观题得分先落，主观题批完由老师端点回写
      t6 返回逐题结果，前端据此在考试记录里显示对错

    返回：
        {"paper_id": 3, "objective_score": 42.5, "full_score": 100.0,
         "pending_subjective": 3,
         "attempt": {"id": 12, "status": "submitted", ...},
         "results": [{"question_id": 7, "is_correct": true, "score": 2.5}, ...]}

    为什么客观题不用前端算：
        答案在服务端，前端拿不到（脱敏），只能后端判。
        也没走"前端提交答案 → 后端比对"的轻量路子，为的是复用批改图里
        已经写好的一套：写 grading_records、更新 BKT、答错落错题本。
    """
    from app.services.exam_delivery import (
        create_attempt,
        paper_total_score,
        save_exam_answers,
        student_can_take,
    )

    profile = _self_profile(db, user)
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"试卷 {paper_id} 不存在")
    if not student_can_take(db, paper, profile):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="forbidden: 这份试卷没有推送到你的班级")

    from app.models import ExamAttempt

    dup = db.execute(
        select(ExamAttempt.id)
        .where(ExamAttempt.paper_id == paper.id)
        .where(ExamAttempt.student_id == profile.id)
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="这份试卷你已经交过了")

    # ---- t2 落作答 ----
    try:
        answer_ids = save_exam_answers(db, paper, profile, body.answers)
        db.commit()
    except Exception:
        db.rollback()
        raise

    if not answer_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="作答与卷面题目对不上，请重新打开试卷")

    # ---- t3 客观题判分（主观题跳过：留给老师）----
    # 数据源用"卷面全部题目"，不是 answer_ids —— 学生漏答的客观题也必须判 0，
    # 否则它会以 is_correct=NULL 躺在库里，学生页显示"待老师批改"、
    # 老师待批改队列里混进一堆选择题（用户实际踩到过）。
    paper_rows = db.execute(
        select(Question.id, Question.question_type)
        .join(ExamPaperQuestion, ExamPaperQuestion.question_id == Question.id)
        .where(ExamPaperQuestion.paper_id == paper.id)
        .order_by(ExamPaperQuestion.seq)
    ).all()

    graph = _get_grading_graph()
    results: list[dict] = []
    objective_score = 0.0
    missed = 0
    for qid, qtype in paper_rows:
        if qtype == "subjective":
            continue

        aid = answer_ids.get(qid)
        if aid is None:
            # 兜底补行理论上不会走：save_exam_answers 会把漏答的客观题补一行空作答。
            # 真走到这里说明那一步出了岔子，跳过但记账，最后由 exam_detail 自愈。
            missed += 1
            continue

        cfg = {"configurable": {"thread_id": f"grading_{aid}"}}
        out = graph.invoke({"answer_id": aid, "teacher_user_id": user.id}, cfg)
        score = float(out.get("final_score") or 0)
        objective_score += score
        results.append({
            "question_id": qid, "answer_id": aid,
            "status": out.get("status", "confirmed"),
            "is_correct": out.get("is_correct"), "score": score,
        })

    # 待批 = 卷面主观题里"学生真的答了"的那几道（没作答的不该进老师队列）
    pending_subjective = sum(
        1 for qid, qtype in paper_rows if qtype == "subjective" and qid in answer_ids
    )

    # ---- t5 建考试记录 ----
    full_score = float(db.execute(
        select(func.coalesce(func.sum(ExamPaperQuestion.score), 0))
        .where(ExamPaperQuestion.paper_id == paper.id)
    ).scalar_one()) or paper_total_score(paper)

    try:
        attempt = create_attempt(db, paper, profile, objective_score, full_score,
                                 pending_subjective)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        db.rollback()
        raise

    return {
        "paper_id": paper.id,
        "objective_score": round(objective_score, 1),
        "full_score": round(full_score, 1),
        "pending_subjective": pending_subjective,
        "attempt": {
            "id": attempt.id, "status": attempt.status,
            "objective_score": attempt.objective_score,
            "subjective_score": attempt.subjective_score,
            "total_score": attempt.total_score,
            "full_score": attempt.full_score,
        },
        "results": results,
    }


@router.post("/upload/answer-image")
def upload_answer_image(body: ImageUploadIn,
                        user: CurrentUser = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """手写解题步骤拍图上传（base64 → 落盘 → 返回可访问 URL）。

    得到的数据：{"filename": "IMG_001.jpg", "data": "data:image/jpeg;base64,/9j/...",
                "purpose": "answer"}

    要做的操作：
        t1 拆 data URL 取 mime → 只认 jpeg/png/webp（别的当 415 拒掉）
        t2 base64 解码 → 体积 > 6MB 直接 413
        t3 文件名用 uuid4（绝不沿用客户端文件名：那张图可能是 ../../etc/passwd）
        t4 落到 data/uploads/{purpose}/{YYYYMM}/{uuid}.jpg
        t5 返回 /uploads/... 相对路径，前端拼 API_BASE 显示

    返回：{"url": "/uploads/answer/202610/ab12cd34.jpg", "bytes": 263145}

    为什么存文件不存库：
        手写图动辄几百 KB，塞进 student_answers 会让整张表扫一次就爆内存；
        关键是它还会进 checkpoint 快照（批改图的 state 带 student_answer 文本），
        二进制进状态带来的是"每个 checkpoint 都背着图"。

    为什么第一行先要学生档案：
        上传口不涉及他人数据，但也不该让任何登录账号往磁盘里塞文件。
        没有学生档案的账号（纯老师/教研）→ 403，最小权限。
    """
    _self_profile(db, user)

    raw = body.data.strip()
    mime = "image/jpeg"
    payload = raw

    if raw.startswith("data:"):
        head, _, payload = raw.partition(",")
        mime = head[5:].split(";")[0].strip().lower() or mime

    if mime not in ALLOWED_IMAGE:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            detail=f"不支持的图片类型 {mime}，请用 JPG/PNG/WEBP")

    try:
        blob = base64.b64decode(payload, validate=False)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="图片数据不是合法 base64")

    if not blob:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="图片是空的")
    if len(blob) > MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB，请压缩后再传")

    # 用途白名单：不接受任意目录名（防目录穿越）
    purpose = body.purpose if body.purpose in {"answer"} else "answer"
    url = _persist_upload(blob, purpose, ALLOWED_IMAGE[mime])
    return {"url": url, "bytes": len(blob)}


def _persist_upload(blob: bytes, purpose: str, suffix: str = ".jpg") -> str:
    """把图片字节写到 data/uploads/{purpose}/{YYYYMM}/，返回可访问的相对 URL。

    得到的数据：blob（已解码的图片字节）、purpose（白名单内的子目录名）、
                suffix（".jpg"/".png"/".webp"，由调用方按 mime 决定）

    要做的操作：
        t1 年月分桶（一个月一个目录，避免单目录堆几十万文件）
        t2 文件名用 uuid4 —— 绝不沿用客户端文件名（那张图的名字可能是 ../../etc/passwd）
        t3 按 suffix 落盘

    返回："/uploads/answer/202610/ab12cd34ef.jpg"

    为什么单独抽一个函数：
        落盘是唯一有副作用的一步。抽出来之后，越权矩阵可以把这一步换成假实现，
        "上传口鉴权对不对"和"文件写哪去了"两件事就能分开测。
    """
    from datetime import datetime

    bucket = datetime.now().strftime("%Y%m")
    target_dir = UPLOAD_ROOT / purpose / bucket
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{suffix}"
    (target_dir / filename).write_bytes(blob)

    return f"/uploads/{purpose}/{bucket}/{filename}"
