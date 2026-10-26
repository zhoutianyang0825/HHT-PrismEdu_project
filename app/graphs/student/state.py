"""M2-T1 · 学生图 State 定义（架构文档 4.1 原样落地）。

 得到的数据：一次学生会话里要流转的全部字段

 要做的操作：只声明"状态里有什么"，不写任何行为，行为全在 nodes.py
 
"""
from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages


# 陌生点 Annotated[list, add_messages]：Annotated 是"给类型附元数据"的写法。
#   第二个参数 add_messages 交给 LangGraph 当合并策略：节点返回新消息列表时，
#   不是整体覆盖 state["messages"]，而是按 id 追加合并
class StudentAgentState(TypedDict):

    # ---- 身份与上下文（首次 invoke 时传入，之后 checkpointer 记住）----
    student_id: int
    question_id: int

    # 作业上下文：学生从「我的作业」点进来时带作业 id，自由练习时为 None
    # 作用：判分落库时决定 source_type —— 有作业 → "homework"(老师端按它查全班作答)；
    #      没有 → "chat"(只给诊断测验供错题数据)
    homework_id: int | None

    # ---- 多轮对话 ----
    messages: Annotated[list, add_messages]

    # ---- 路由决策 ----
    # intent 五类路由（开发计划 M2-T1）：求答案 / 要引导提示 / 提交作答 / 要讲解 / 闲聊
    # 五选1
    intent: Literal["ask_answer", "guide_scaffold", "submit_answer", "explain", "off_topic"]

    # ---- 安全过滤（入口层，PRD 2.4）----
    blocked_reason: str          #  "" = 空，放行； "检测到 prompt 注入尝试" = 非空，被安全过滤拦截，直接走兜底回复

    # ---- RAG 检索结果 ----
    retrieved_context: str       # 教材片段，带 [1]xxxx / [2]xxxx 出处

    # ---- 图谱 ----
    current_kp: str              # 本题考察的知识点 id
    weak_prerequisites: list[str]  # 前置薄弱点名称（M3 归因时深度用，M2 先占位）

    # ---- 引导状态 ----
    attempt_count: int           # 已引导轮数，上限 3（第 3 轮给完整讲解）
    hint_level: int              # 提示阶梯：1 方向 → 2 步骤 → 3 关键公式
    force_explain: bool          # True=引导已用尽，要给完整解答（讲解节点据此决定是否留最后一步）

    # ---- 作答与判分（M2 简版规则判分；M3 换 grading.py 完整服务）----
    current_answer: str
    is_correct: bool | None      # None=还没判分、True=对、False=错

    # ↓↓↓ S-10 相关（quiz 子图通过 question_ids 与主图交接）----
    quiz_mode: bool              # True 表示本次请求是 "生成测验" 而非单题作答
    target_points: list[dict]    # Top5 目标知识点 [{kp_id, name, p_known, reason}, ...]
    generated_question_ids: list[str]


class QuizState(TypedDict):
    """S-10 quiz_graph 子图的独立 State（架构文档 4.3）。

    为什么独立：
        - 主图是"单轮单题+多轮引导"的状态机，
        - 子图生成测验是 "一次生成一批题" 的批处理，
    """

    student_id: int
    days: int                    # 往前取多少天的错题，默认 30
    total: int                   # 测验总题数，默认 8
    
    # ---- 中间产物 ----
    error_kp_ids: list[str]      # 错题知识点集合 E
    error_freq: dict[str, int]   # 每个知识点近 N 天错了几次（排序 tie-break 用）
    candidate_kps: list[dict]    # 图谱外扩出的候选集 C = E ∪ 前置点
    target_points: list[dict]    # Top5 目标知识点

    # ---- 最终产物 ----
    question_ids: list[int]      # 选出的题目 PG id 列表
    questions: list[dict]        # 题目详情（给 CLI/前端渲染卡片）
    quiz_id: int | None          # 写入 diagnostic_quizzes 后的记录 id
    generated_reason: str        # 面向学生的一句话解释
