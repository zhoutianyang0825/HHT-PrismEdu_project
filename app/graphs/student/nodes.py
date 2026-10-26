"""M2-T1/T2 · 学生 Agent 的节点实现。

节点清单（与架构文档 4.2 状态图一一对应）：
  safety_check      入口安全过滤（内容安全简版，PRD 2.4）
  classify_intent   LLM 结构化输出识别意图（四类路由）
  guide_scaffold    🔴 引导节点：给阶梯提示后 interrupt 挂起，等学生再输入
  explain_concept   要讲解 → RAG 检索教材 + 带出处讲解
  grade_submit      提交作答 → M2 简版规则判分（M3 换 grading.py 完整服务）
  respond           统一出口：把最终回复写进 messages

工程边界说明：架构规定"Agent 层不直接碰数据库"——完整 Tool 层在 M4-T1 建。

"""
import re

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.core.llm import get_llm
from app.core.structured import invoke_structured
from app.core.textfmt import OUTPUT_FORMAT_RULES, clean_llm_text
from app.db.session import get_session_factory
from app.graphs.student.safety import check_input
from app.graphs.student.state import StudentAgentState
from app.models import Question
from app.rag.retriever import format_context, retrieve
from app.services.grading import grade_objective   # M3-T1 判分服务（纯规则，与老师批改图同源）
from app.services.wrong_record import record_answer, record_answer_full_score
from pymilvus import MilvusClient

# 意图四分类（开发计划 M2-T1）
INTENTS = ("ask_answer","guide_scaffold","submit_answer", "explain", "off_topic")

# 引导轮数上限：第 3 轮不再吊胃口，直接给完整讲解（S-04 验收标准）
MAX_ATTEMPTS = 3


# =====================================================================
# DB 过渡期助手（M4 搬进 tool 层）
# =====================================================================
def _fetch_question(question_id: int) -> Question | None:
    """
    按 主键id 取题目行数据。节点里多处要用，收口成一个函数。

    陌生点 get_session_factory()()：两级调用，得到 session。
    """
    session = get_session_factory()()
    try: 
        return session.get(Question, question_id) # 在Question表中，按主键查询，主键=question_id
    finally:
        session.close()     # 异常自动 close 



def _fetch_kp_name(kp_id: str) -> str:
    """取知识点中文名，拼 prompt 用（提示里说"十字相乘法"比说 kp_crossmult 自然）。"""
    from app.models import KnowledgePoint
    session = get_session_factory()()
    try:
        kp = session.get(KnowledgePoint, kp_id) #  在KnowledgePoint表中，按主键查询，主键=kp_id
        return kp.name if kp else kp_id
    
    finally:
        session.close()


# =====================================================================
# 入口安全节点
# =====================================================================
def safety_check(state: StudentAgentState) -> dict:
    """入口安全过滤节点（内容安全简版，PRD 2.4：system prompt 硬约束之外的关键词兜底）。

    得到的数据：学生最新一条输入
    要做的操作：safety.check_input 正则+黑名单两层检查
    返回：{"blocked_reason": ""或原因}；条件边看到非空就直接路由到 respond，恶意输入根本进不了 LLM 节点
    """
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            ok, reason = check_input(m.content)
            return {"blocked_reason": "" if ok else reason}  # 如果 ok，就给空串； 否则给 reason
        
    return {"blocked_reason": ""}   # thread 里暂时没有人类消息（理论上到不了）


# =====================================================================
# 意图分类
# =====================================================================
class IntentResult(BaseModel):
    """LLM 结构化输出的意图结果。

    陌生点 structured.invoke_structured(Pydantic模型)：把字段 schema 变成
    JSON 约束，让 LLM 按格式吐结果，再自动校验回 IntentResult 实例
    """
    intent: str = Field(description=f"五类之一：{INTENTS}")
    confidence: float = Field(ge=0, le=1, description="分类置信度")



def _fallback_intent(text: str) -> str:
    """LLM 挂了/超时 的关键词兜底分类。

    规则朴素但够用，判定顺序有讲究（先具体的，后笼统的）：
        包含"答案是/告诉我答案"         → ask_answer；
        长得像解题过程（=、我算出来、所以）→ submit_answer；
        "考什么/没思路/给点提示/不会做"  → guide_scaffold（要方向，先给阶梯提示）；
        包含"讲解/为什么/解析"           → explain（要完整讲解）；
        都不像                           → off_topic。

    ★ 为什么 guide_scaffold 排在 explain 前面：
        "我没有思路" 同时含"思路"和"没有"，若先判 explain 会被当成要完整讲解，
        直接跳过引导给答案——正好和"先触发引导"的产品意图相反。
    """
    if re.search(r"(答案|直接告诉|告诉我结果|是多少)", text):
        return "ask_answer"
    if "=" in text or re.search(r"(我算出来|所以|解得|我的答案)", text):
        return "submit_answer"
    if re.search(r"(考什么|什么考点|没思路|没有思路|无思路|没头绪|没想法|想不出|"
                 r"提示|思路|从哪|从哪里下手|不会做|做不出)", text):
        return "guide_scaffold"
    if re.search(r"(不会|讲解|讲讲|讲一下|为什么|怎么解|概念|解析|详细讲|什么意思)", text):
        return "explain"
    return "off_topic"


def classify_intent(state: StudentAgentState) -> dict:
    """意图分类节点：LLM 结构化输出，异常时关键词兜底。

    得到的数据：
        state["messages"] 最后一条 HumanMessage（学生刚说的话）；
        state["question_id"]（拼进 prompt 当上下文）

    要做的操作：
        拼带题目上下文的 system prompt → invoke_structured 强格式
        → 结果不在 INTENTS 里就走关键词兜底

    返回：
        {"intent": "ask_answer", "is_correct": None, "force_explain": False}

    为什么顺手返回 is_correct / force_explain：
        这两个都是"上一轮的一次性结果"，不是长期状态。
        is_correct 不清：学生上一轮答错、这一轮只闲聊，respond 仍读到 False，
                        回一句"还没对，别急"——学生问"你是哪里人"却被告知答案没对。
        force_explain 不清：引导满 3 轮后学生再问别的，讲解节点还会塞完整答案。
        attempt_count 不能清：那是整题累计的引导轮数，清了永远满不了 3 轮。
    """
    question = _fetch_question(state["question_id"])    # 取的是题目行 数据
    q_text = question.content if question else "（题目加载失败）"   # 从题目行数据中 取出content = 题目


    # 取最后一条人类消息
    # reversed（）是反向遍历，m的类型是人类消息就保留
    # next（）只取第一个符合的值
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), 
        None,  # 给none，保证图不崩，这就是"fail-soft"（优雅降级）
    )
    student_text = last_human.content if last_human else ""


    system = (
        "你是初中数学答疑助手的意图分类器。结合题目内容判断学生这句话的意图：\n"
        "ask_answer=直接要答案（这题答案是多少/告诉我答案/做出来给我看下）；\n"
        "guide_scaffold=学生自己还没思路、要个方向或提示（这题考什么/考啥/我没思路/给点提示/不会/不会写/怎么做/还是不会）；\n"
        "submit_answer=提交自己的作答（纯数字/算式/x=2或3 这类答案内容）；\n"
        "explain=要求完整讲解或解析（讲解一下/详细讲讲/解析一下/答案看不懂/我完全没有思路你跟我讲解下/查看完整解析）；\n"
        "off_topic=与本题无关的闲聊。\n"
        "★ guide_scaffold 和 explain 的区别（最容易混，看这四条）：\n"
        "  - 学生只是没思路、想要一点提示或方向 → guide_scaffold\n"
        "  - 学生明确要求完整讲解/详细讲解/解析，或说看不懂 → explain\n"
        "  - 学生说「不要提示，直接讲解」→ explain（明确否定了提示）\n"
        "  - 学生说「还是不会」→ guide_scaffold（仍在要提示）\n"
        "★ 纯答案内容（数字、算式、x=2或3）→ submit_answer，"
        "哪怕只有一个数字也是提交作答，不是 ask_answer。\n"
        f"当前题目：{q_text[:400]}"
    )

    try:
        # 走结构化输出适配层（provider 不支持 json_schema 时自动降级到手写 JSON 约束）
        result: IntentResult = invoke_structured(
            get_llm(temperature=0),
            IntentResult,
            [
                ("system", system),
                ("human", student_text),
            ],
        )

        # 如果识别到的意图，在INTENTS内，则返回
        # 如果不在INTENTS内，则调用 兜底意图识别 函数
        intent = result.intent if result.intent in INTENTS else _fallback_intent(student_text)

    except Exception:

        # 网络抖动 / key 失效 / 模型输出解析失败 → 调用 兜底意图识别函数，不炸图
        # ★ 这条 except 是最后一道保险：LLM 挂了整条链路不能断，
        #   退化到 52.9% 的关键词匹配也比直接报错强。
        intent = _fallback_intent(student_text)

    # ★ 顺手清掉上一轮的两个"一次性"标记：
    #   is_correct    —— 不刷新的话，学生上一轮答错、这一轮只闲聊，
    #                    respond 仍会读到 False，回一句"还没对，别急"（截图里的 bug）
    #   force_explain —— 不刷新的话，引导满 3 轮后学生再问别的，讲解节点还会给完整答案
    # 注意 attempt_count 不能清：那是整题累计的引导轮数，清了就永远满不了 3 轮
    return {"intent": intent, "is_correct": None, "force_explain": False}


# =====================================================================
# 引导节点（🔴 M2 关键路径：interrupt 挂起-恢复）
# =====================================================================

# 3次引导的阶梯提示
HINT_STYLE = {
    1: "只给思考方向，不许出现任何具体数字或步骤，一两句话。",
    2: "给出解题步骤框架（第一步做什么、第二步做什么），但每步留空让学生自己填。",
    3: "点出关键公式/方法名，给一个类似的例子，仍然不写出本题答案。",
}


def guide_scaffold(state: StudentAgentState, *, config, store) -> dict:
    """解题引导节点：按 hint_level 生成阶梯提示 → interrupt 挂起等学生再输入。

    得到的数据：attempt_count（已引导次数，首次进入为 0）、hint_level（提示阶梯级）

    要做的操作：
      1. 如果已引导 3 轮 → 不在这里自己写讲解，只置 force_explain 标记，
         由 route_after_guide 把控制权交给 explain_concept 节点
         （这样满轮讲解也能带 RAG 教材出处，和"直接要讲解"走同一条路）
      2. 否则调 LLM 生成本层提示 → interrupt() 把提示抛给前端并挂起
      3. Command(resume=学生新输入) 回来后，从这里继续执行，把新输入记入 messages，轮数/层级 +1，然后回到 classify_intent 重新分类

    ★ interrupt 三条铁律（架构文档 12 坑预警）：
      - interrupt() 必须是节点函数体内的"直接调用"，包进子函数/try 会失效
      - 挂起状态靠 checkpointer 存（PostgresSaver 同库持久，重启不丢）
      - 恢复只能靠 Command(resume=...)，resume 的值就是 interrupt() 的返回值
      - 为了保证"重启后会话还能 resume"，只能从新再走一遍函数（重放），
        执行到interrupt（payload）时，langgraph检测内部的队列中是否有待消费的 resume 值，如果有，返回给interrupt，并消费resume值，剩余代码执行完毕

    陌生参数 store：LangGraph 自动注入的长期记忆（InMemoryStore），图compile(store=...) 指定。
    """
    from app.services.student_profile import warm_start

    try:
        warm_start(store, state["student_id"])

    except Exception:
        pass  # 画像是加分项，真源在 PG，这里挂了只损失针对性不损失正确性


    # ---- 分支 1：引导满 3 轮 → 交给讲解节点给完整解答 ----
    # 只置标记，不自己调 LLM 写讲解：
    #   讲解走 explain_concept 才能带 RAG 教材出处（[1][2] 引用），
    #   和学生直接说"讲解一下"命中的是同一条路径，输出一致。
    if state.get("attempt_count", 0) >= MAX_ATTEMPTS:
        return {"force_explain": True}


    # ---- 分支 2：生成本层提示 ----
    question = _fetch_question(state["question_id"])
    kp_name = _fetch_kp_name(question.kp_id)
    level = state.get("hint_level", 1)
    hint = get_llm(temperature=0.4).invoke([
        ("system",
         "你是引导式数学老师。铁律：负责给学生提供解题的思路提示，不能直接给出本题的最终答案。"
         f"本题考察知识点：{kp_name}。本轮提示要求：{HINT_STYLE.get(level, HINT_STYLE[3])}\n\n"
         + OUTPUT_FORMAT_RULES),
        ("human", f"题目：{question.content}\n学生上一句：{_last_human_text(state)}"),
    ])


    # interrupt 所在节点函数 要"薄"，只放无副作用的代码（LLM 调用、只读查询可以；insert、发通知不行）
    # ---- interrupt（payload）：图在此挂起，CLI/前端打印 hint 后，调 Command(resume=...) ----
    student_reply = interrupt({               # 函数停止执行，图停在原地
        "type": "scaffold_hint",              # 前端按 type 弹输入框（M5 SSE 约定）
        "hint": clean_llm_text(hint.content),
        "attempt_count": state.get("attempt_count", 0) + 1,
        "hint_level": level,
    })


    # resume 回来从这里继续：学生的回答进消息历史，轮数/层级递增
    return {
        "messages": [HumanMessage(content=str(student_reply))],
        "attempt_count": state.get("attempt_count", 0) + 1,  # +1 代表是即将开始下一轮
        "hint_level": min(level + 1, 3),      # 层级封顶 3，取小
    }


def _last_human_text(state: StudentAgentState) -> str:
    """
    取学生最近一句话拼 prompt 用（找不到给占位符，不抛 KeyError）。
    """

    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            return m.content
        
    return "（学生刚拿到题目）"


# =====================================================================
# 讲解节点（RAG 带出处）
# =====================================================================
def explain_concept(state: StudentAgentState) -> dict:
    """要讲解 → RAG 检索教材片段 → LLM 带引用编号讲解。

    得到的数据：retrieved_context（拼好 [1][2] 编号的教材上下文）
              force_explain（True=引导已满 3 轮，要给完整解答）

    要做的操作：retrieve(role="student")——学生角色只会命中教材库
    （collection 白名单物理隔离，题库根本不在检索范围，M1 的权限层直接复用）

    ★ force_explain 的两种讲法：
        False（学生主动说"讲解一下"）→ 最后一步结论留给学生自己算，保持引导式
        True （引导满 3 轮还没解出）  → 给完整解答（写出最终答案），否则学生永远卡在这
    """
    question = _fetch_question(state["question_id"])
    force = state.get("force_explain", False)

    # ---- 检索教材片段（Milvus 挂了不能堵死讲解链路，降级成"无教材上下文"继续讲） ----
    context = ""
    try:
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            hits = retrieve(client, query=question.content, role="student", top_k=3)
        finally:
            client.close()
        context = format_context(hits) if hits else "（教材库暂无相关片段）"
    except Exception:
        context = "（教材库检索暂不可用，请依据题目自身讲解）"

    if force:
        system = (
            "你是耐心的初中数学老师。学生已经连续提示 3 轮仍没解出，"
            "现在要给出完整解答：先讲思路，再一步步写出完整解答过程，然后写出最终答案，"
            "最后总结这类题的方法。\n\n"
            + OUTPUT_FORMAT_RULES
        )
    else:
        system = (
            "你是初中数学老师，基于教材片段讲解本题思路。要求：\n"
            "1. 讲清思路和步骤，但最后一步结论留给学生自己算；\n"
            "2. 学生算不出来或者完全不会时，直接输出答案给学生；\n"
            "3. 引用教材处标 [1][2] 编号。\n\n"
            + OUTPUT_FORMAT_RULES
        )

    answer = get_llm(temperature=0.3).invoke([
        ("system", system + f"\n教材片段：\n{context}"),
        ("human", f"题目：{question.content}\n学生说：{_last_human_text(state)}"),
    ])

    return {
        "retrieved_context": context,
        "messages": [AIMessage(content=clean_llm_text(answer.content))],
    }


# =====================================================================
# 判分节点（M3-T1 判分服务接入版：分题型调不同批改方法）
# =====================================================================
def grade_submit(state: StudentAgentState, config: RunnableConfig | None = None) -> dict:
    """提交回答 → 按题型分发批改（客观题规则判分 / 主观题待老师批改）。

    得到的数据：
        state["messages"] 末条 HumanMessage（学生提交的答案）；
        state["question_id"]（题目主键）；
        state["student_id"]（学生档案 id）；
        state["attempt_count"]（已引导轮数，首轮可能缺失 → 按 0 处理）

    要做的操作：
        1. 按 question_type 分流：
           客观题（choice/fill）→ grade_objective 四层规则判分
             （归一化 → 等价变体 → 表达式等价 → 多值比对，与老师批改图同源）；
           主观题（subjective）→ 对话场景没有老师在场，规则判不了，
             直接回"已提交，等老师批改"（AIMessage 终稿，respond 空过）

        2. ★ 客观题判错时落错题（student_answers + error_logs + BKT）：
           同一道题只记一次，引导后再错不重复记，避免诊断测验的错题频次虚高

    返回：
        客观题 {"current_answer": "x=2或x=3", "is_correct": False}；
        主观题 {"current_answer": "...", "is_correct": None, "messages": [AIMessage(...)]}

    为什么：
        S-10 诊断测验的错题取数是 error_logs JOIN student_answers JOIN questions，
        而聊天链路原来一张表都不写 —— 学生在学习助手里做错多少题都不进库，
        诊断测验永远"没有错题记录"。落库是这条链唯一缺的那一环。
    """
    question = _fetch_question(state["question_id"])
    student_answer = _last_human_text(state)

    # 作业上下文：学生从「我的作业」点进来才有；自由练习是 None
    # 两处都读，config 优先：挂起后 resume 走 Command(resume=...) 不带 state 更新，
    # 此时只有 config.configurable 里这份是新鲜的；首次 invoke 则两者一致
    homework_id = (config or {}).get("configurable", {}).get("homework_id")
    if homework_id is None:
        homework_id = state.get("homework_id")

    # ---- 主观题：对话场景不判分，回执式回复（等老师批改）----
    if question.question_type == "subjective":
        # 落库但不判分（is_correct=None）：老师端「作业提交情况」要看到"这题学生交了、待批改"，
        # 否则主观题作业在老师端永远显示为"没交"
        try:
            record_answer(
                student_id=state["student_id"],
                question=question,
                student_answer=student_answer,
                is_correct=None,
                score=None,
                homework_id=homework_id,
                attempt_count=state.get("attempt_count", 0) or 0,
            )

        except Exception as exc:
            print(f"[grade_submit] ⚠ 主观题作答落库失败（不影响回执）: {exc}")

        return {
            "current_answer": student_answer,
            "is_correct": None,      # 三态保留 NULL：没判分，不是"判错"
            "messages": [AIMessage(
                content="已提交，等老师批改。主观题会先由 AI 给出建议分，"
                        "老师确认后分数和掌握度才会更新。"
            )],
        }


    # ---- 客观题（choice/fill）：调 M3-T1 判分服务 ----
    # 满分取真值：作业题每题 10 分就读 homework_questions.score，
    # 自由练习没有作业语境才按百分制 —— 否则老师端"得分"列会显示 100 而作业配置是 10
    full_score = record_answer_full_score(homework_id, question.id)

    result = grade_objective(
        question_type=question.question_type,
        student_answer=student_answer,
        standard_answer=question.answer,
        full_score=full_score,
    )

    # ---- 判完就落库（对错都落）----
    # 两个消费方同时依赖这张表：
    #   ① 老师端 T-03「作业提交情况」按 (source_type='homework', source_id=作业id) 查全班作答
    #   ② S-10 诊断测验按 error_logs JOIN student_answers WHERE is_correct=false 取错题
    # 之前只在答错时记 chat 来源，所以老师端永远"暂无作答记录"
    try:
        record_answer(
            student_id=state["student_id"],
            question=question,
            student_answer=student_answer,
            is_correct=result["is_correct"],
            score=result["score"],
            homework_id=homework_id,
            attempt_count=state.get("attempt_count", 0) or 0,
        )

    except Exception as exc:
        print(f"[grade_submit] ⚠ 作答落库失败（不影响判分反馈）: {exc}")

    return {"current_answer": student_answer, "is_correct": result["is_correct"]}


# =====================================================================
# 出口节点
# =====================================================================
def _small_talk_reply(state: StudentAgentState) -> str:
    """学生跑题时的一句话回应：接住话头 + 引回当前题目。

    得到的数据：
        state["messages"] 末条 HumanMessage（学生这句闲聊，类似 "你是哪里人"）；
        state["question_id"]（当前题目，用来把话题拉回来）

    要做的操作：
        调 LLM 生成不超过 2 句的回应；异常时用固定文案兜底

    返回：
        回复正文（已过 clean_llm_text，公式/标点收口过）

    为什么不用固定模板：
        "你是哪里人" 回一句"咱们回到题目"太机械，学生会觉得对面是个假人；
        但也不能让它自由发挥——prompt 里把字数、敏感话题、身份暴露都限死了。
    """
    question = _fetch_question(state["question_id"])
    stem = (question.content or "")[:200] if question else ""

    try:
        reply = get_llm(temperature=0.6).invoke([
            ("system",
             "你是初中数学学习助手，正在陪学生做当前这道题。"
             "学生这句话和题目无关（闲聊或跑题）。要求：\n"
             "1. 先用一句自然、口语化的话接住他，不要机械，不要自称语言模型；\n"
             "2. 再用一句话把话题引回这道题；\n"
             "3. 全程不超过 60 字，不要列表、不要标题、不要输出 LaTeX 记号；\n"
             "4. 政治、宗教、暴力、成人、医疗建议等话题礼貌拒绝，只谈学习。\n\n"
             + OUTPUT_FORMAT_RULES),
            ("human", f"当前题目：{stem}\n学生说：{_last_human_text(state)}"),
        ])
        return clean_llm_text(reply.content)

    except Exception:
        # 闲聊不是主链路，LLM 挂了也要给个回复，别把整轮对话卡死
        return "我在的。不过咱们先把这道题往前推一步——说说你想到哪了，或者要一句提示？"


def respond(state: StudentAgentState) -> dict:
    """统一出口节点：按当前状态拼最终回复。

    已有 AIMessage（讲解/完整讲解节点自己产出了回复）→ 不再追加，避免双条。
    被安全过滤拦截 → 兜底警告，不调 LLM（恶意内容不配消耗 token）。
    闲聊跑题 → 一句自然回应 + 引回题目（不能让 AI 装死，也不能陪着跑偏）。
    判分对 → 肯定 + 收尾；
    判分错 → 鼓励并指向"要讲解"路径。

    ★ 顺序有讲究：off_topic 必须排在 is_correct 判断前面。
      因为 is_correct 描述的是"这道题上一次判得对不对"，
      而学生这句话可能只是闲聊，两件事没关系——
      排在后面就会出现"学生问'你是哪里人'，AI 回'还没对，别急'"。
    """
    # 讲解类节点（有增量消息的节点）已经写了最终回复，response节点无增量消息，直接返回{}通过
    if state["messages"] and isinstance(state["messages"][-1], AIMessage):
        return {}

    if state.get("blocked_reason"):
        return {"messages": [AIMessage(
            content="这条消息我不能处理。我们是学习助手，咱们聊点和题目相关的吧。")]}

    # ---- 先判断是否属于闲聊/跑题：先顺着接一句，再自然拉回题目 ----
    if state.get("intent") == "off_topic":
        return {"messages": [AIMessage(content=_small_talk_reply(state))]}

    # 再用判断作答是否正确
    if state.get("is_correct") is True:
        question = _fetch_question(state["question_id"])
        return {"messages": [AIMessage(
            content=clean_llm_text(
                f"✓ 正确！标准解答：{question.answer}\n{question.analysis or ''}"
            ))]}
    
    if state.get("is_correct") is False:
        return {"messages": [AIMessage(
            content="还没对，别急。可以说「讲解一下」让我带你梳理思路，"
                    "或者把你的计算过程发我，我帮你找问题出在哪一步。")]}


    return {"messages": [AIMessage(
        content="咱们回到这道题上：可以把你的想法/算到哪一步发给我，"
                "或者要一段讲解，我来带你一步步走。")]}
