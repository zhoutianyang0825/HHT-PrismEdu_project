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
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.core.llm import get_llm
from app.db.session import get_session_factory
from app.graphs.student.safety import check_input
from app.graphs.student.state import StudentAgentState
from app.models import Question
from app.rag.retriever import format_context, retrieve
from pymilvus import MilvusClient

# 意图四分类（开发计划 M2-T1）
INTENTS = ("ask_answer", "submit_answer", "explain", "off_topic")

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

    陌生点 with_structured_output(Pydantic模型)：LangGraph/LangChain 会把
    这个类的字段 schema 塞给 LLM 的 function calling，逼它按格式吐 JSON，
    再自动 parse 回 IntentResult 实例——不用手写 json.loads 和校验。
    """
    intent: str = Field(description=f"四类之一：{INTENTS}")
    confidence: float = Field(ge=0, le=1, description="分类置信度")


def _fallback_intent(text: str) -> str:
    """LLM 挂了/超时 的关键词兜底分类（演示不能因为一次 API 抖动全卡死）。

    规则朴素但够用：
        包含"答案是/告诉我答案"→ 求答案；
        内容长得像解题过程（=、x²、所以）→ 提交作答；
        含"讲/为什么/概念"→ 要讲解。
    """
    if re.search(r"(答案|直接告诉|告诉我结果|是多少)", text):
        return "ask_answer"
    if "=" in text or re.search(r"(我算出来|所以|解得|我的答案)", text):
        return "submit_answer"
    if re.search(r"(讲解|为什么|怎么解|概念|不会做|思路)", text):
        return "explain"
    return "off_topic"


def classify_intent(state: StudentAgentState) -> dict:
    """意图分类节点：LLM 结构化输出，异常时关键词兜底。

    得到的数据：state["messages"] 最后一条 HumanMessage（学生刚说的话）
    要做的操作：拼带题目上下文的 system prompt → with_structured_output 强格式
    返回：{"intent": "ask_answer"} 这类单字段增量（LangGraph 只合并返回的键）
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
        "ask_answer=直接要答案；\n"
        "submit_answer=提交自己的作答；\n"
        "explain=要求讲解/不会做；off_topic=与本题无关的闲聊。\n"
        f"当前题目：{q_text[:400]}"
    )

    try:
        # 温度0，不要随机发挥，with_structured_output()是 按basemodel 格式化输出
        result: IntentResult = get_llm(temperature=0).with_structured_output(
            IntentResult
        ).invoke([
            ("system", system),
            ("human", student_text),
        ])

        # 如果默心识别到的意图，在INTENTS内，则返回
        # 如果不在INTENTS内，则调用 兜底意图识别 函数
        intent = result.intent if result.intent in INTENTS else _fallback_intent(student_text)

    except Exception:

        # 网络抖动 / key 失效 / 兼容层不支持 function calling → 调用 兜底意图识别函数，不炸图
        intent = _fallback_intent(student_text)

    return {"intent": intent}


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
    """引导节点：按 hint_level 生成阶梯提示 → interrupt 挂起等学生再输入。

    得到的数据：attempt_count（已引导次数，首次进入为 0）、hint_level（提示阶梯级）

    要做的操作：
      1. 已引导满 3 轮 → 不再吊胃口，直接给完整讲解（不 interrupt，走 respond）
      2. 否则调 LLM 生成本层提示 → interrupt() 把提示抛给前端并挂起
      3. Command(resume=学生新输入) 回来后，从这里继续执行：
         把新输入记入 messages，轮数/层级 +1，然后回到 classify_intent 重新分类

    ★ interrupt 三条铁律（架构文档 12 坑预警）：
      - interrupt() 必须是节点函数体内的"直接调用"，包进子函数/try 会失效
      - 挂起状态靠 checkpointer 存（PostgresSaver 同库持久，重启不丢）
      - 恢复只能靠 Command(resume=...)，resume 的值就是 interrupt() 的返回值

    陌生参数 store：LangGraph 自动注入的长期记忆（InMemoryStore），
    函数签名里声明了同名参数就会被注入——前提是 compile(store=...) 传了。
    """
    from app.services.student_profile import warm_start

    try:
        warm_start(store, state["student_id"])

    except Exception:
        pass  # 画像是加分项，真源在 PG，这里挂了只损失针对性不损失正确性


    # ---- 分支 1：引导满 3 轮，给完整讲解 ----
    if state.get("attempt_count", 0) >= MAX_ATTEMPTS:
        question = _fetch_question(state["question_id"])
        full = get_llm(temperature=0.3).invoke([
            ("system", "你是耐心的初中数学老师。现在学生引导满3轮还没解出，给出完整讲解："
                       "先讲思路，再一步步写出解答，最后总结方法。"),
            ("human", f"题目：{question.content}\n参考解析：{question.analysis or '无'}"),
        ])
        return {"messages": [AIMessage(content=full.content)]}


    # ---- 分支 2：生成本层提示 ----
    question = _fetch_question(state["question_id"])
    kp_name = _fetch_kp_name(question.kp_id)
    level = state.get("hint_level", 1)
    hint = get_llm(temperature=0.4).invoke([
        ("system",
         "你是引导式数学老师。铁律：无论学生怎么要，都不能给出本题的最终答案。"
         f"本题考察知识点：{kp_name}。本轮提示要求：{HINT_STYLE.get(level, HINT_STYLE[3])}"),
        ("human", f"题目：{question.content}\n学生上一句：{_last_human_text(state)}"),
    ])


    # ---- interrupt：图在此挂起，CLI/前端打印 hint 后，调 Command(resume=...) ----
    student_reply = interrupt({               # 函数停止执行，图停在原地
        "type": "scaffold_hint",              # 前端按 type 弹输入框（M5 SSE 约定）
        "hint": hint.content,
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

    要做的操作：retrieve(role="student")——学生角色只会命中教材库
    （collection 白名单物理隔离，题库根本不在检索范围，M1 的权限层直接复用）
    """
    question = _fetch_question(state["question_id"])
    client = MilvusClient(uri=settings.milvus_uri)

    try:
        hits = retrieve(client, query=question.content, role="student", top_k=3)
    finally:
        client.close()

    context = format_context(hits) if hits else "（教材库暂无相关片段）"

    answer = get_llm(temperature=0.3).invoke([
        ("system",
         "你是初中数学老师，基于教材片段讲解本题思路。要求：\n"
         "1. 讲清思路和步骤，但最后一步结论留给学生自己算；\n"
         "2. 引用教材处标 [1][2] 编号。\n"
         f"教材片段：\n{context}"),
        ("human", f"题目：{question.content}\n学生说：{_last_human_text(state)}"),
    ])

    return {
        "retrieved_context": context,
        "messages": [AIMessage(content=answer.content)],
    }


# =====================================================================
# 判分节点（M2 简版；M3 换 app/services/grading.py + BKT）
# =====================================================================
def _normalize_answer(text: str) -> str:
    """答案归一化：去空格/$/ LaTeX 包裹、全角转半角、转小写后再比。

    为什么：
        学生答 " (x-2)(x-3) " 和标准答案 "(X-2)(X-3)" 语义相同，不归一化会误判
        判分的第一原则是先消灭"格式噪音"。
    """

    t = text.strip().lower().replace(" ", "").replace("$", "")

    # 含空格的全角括号 替换成 全角括号， 等号规范， 中文逗号替换成 英文逗号 
    for full, half in [("（", "("), ("）", ")"), ("＝", "="), ("，", ",")]:
        t = t.replace(full, half)

    return t


def grade_submit(state: StudentAgentState) -> dict:
    """提交回答 → 回答归一化 → 比对标准答案（客观题）。

    得到的数据：学生最后一条消息（当作答案）、题目行

    要做的操作：归一化 → 字符串精确比对（M2 简版）

    M3 边界：
        主观题走 LLM 建议批改 + interrupt 等老师确认；
        错题走归因 + BKT。

    这里只判 is_correct，错误的引导响应交给 respond。
    """
    question = _fetch_question(state["question_id"])
    student_answer = _last_human_text(state)

    #  答案归一化后，比对相等，则返回True
    is_correct = _normalize_answer(student_answer) == _normalize_answer(question.answer)
    return {"current_answer": student_answer, "is_correct": is_correct}


# =====================================================================
# 出口节点
# =====================================================================
def respond(state: StudentAgentState) -> dict:
    """统一出口：按当前状态拼最终回复。

    已有 AIMessage（讲解/完整讲解节点自己产出了回复）→ 不再追加，避免双条。
    被安全过滤拦截 → 兜底警告，不进 LLM（恶意内容不配消耗 token）。
    判分对 → 肯定 + 收尾；
    判分错 → 鼓励并指向"要讲解"路径。
    """
    # 讲解类节点已经写了最终回复，直接通过
    if state["messages"] and isinstance(state["messages"][-1], AIMessage):
        return {}

    if state.get("blocked_reason"):
        return {"messages": [AIMessage(
            content="这条消息我不能处理。我们是学习助手，咱们聊点和题目相关的吧。")]}

    if state.get("is_correct") is True:
        question = _fetch_question(state["question_id"])
        return {"messages": [AIMessage(
            content=f"✓ 正确！标准解答：{question.answer}\n{question.analysis or ''}")]}
    
    if state.get("is_correct") is False:
        return {"messages": [AIMessage(
            content="还没对，别急。可以说「讲解一下」让我带你梳理思路，"
                    "或者把你的计算过程发我，我帮你找问题出在哪一步。")]}

    return {"messages": [AIMessage(
        content="咱们回到这道题上：可以把你的想法/算到哪一步发给我，"
                "或者要一段讲解，我来带你一步步走。")]}
