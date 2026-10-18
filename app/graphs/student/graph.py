"""M2-T2 · 学生图编排：StateGraph 组装 + 编译出口。

状态图拓扑（架构文档 4.2 的 M2 版，归因/推荐/BKT 三个节点 M3 接入）：

    START → safety_check ──blocked──→ respond → END
                │放行
                ▼
          classify_intent
                │ (条件边 route_by_intent)
    ask_answer  │  submit_answer        explain        off_topic
                ▼        ▼                 ▼               ▼
        guide_scaffold  grade_submit   explain_concept  respond
                │             │              │
                │ (回到分类)   └──────┬───────┘
                ▼                    ▼
          classify_intent          respond → END

🔴 interrupt 链路：guide_scaffold 内部挂起 → Command(resume=学生新输入)
   → 节点从挂起点继续 → 固定边回到 classify_intent 重新分类
   （学生拿到提示后可能"再要提示"，也可能直接"提交作答"，必须重新路由）
"""
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.graphs.student.nodes import (
    classify_intent,
    explain_concept,
    grade_submit,
    guide_scaffold,
    respond,
    safety_check,
)
from app.graphs.student.state import StudentAgentState


def route_after_safety(state: StudentAgentState) -> str:
    """safety_check 的条件边：被拦截 → 直接 respond 兜底，不再进 LLM。"""
    return "respond" if state.get("blocked_reason") else "classify_intent"


def route_by_intent(state: StudentAgentState) -> str:
    """意图 → 节点名 的路由表（条件边只认"返回节点名"这一种契约）。"""
    return {
        "ask_answer": "guide_scaffold",
        "submit_answer": "grade_submit",
        "explain": "explain_concept",
        "off_topic": "respond",
    }[state["intent"]]


def build_student_graph(
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
):
    """组装并编译学生图。

    得到的数据：CompiledStateGraph（可 invoke 的成品图）
    参数：
      checkpointer  短期记忆——CLI/M5 API 都传 PostgresSaver（同库持久，
                    重启不丢挂起会话）；单测可传 MemorySaver 或不传
      store         长期记忆（学生画像）——不传就给 InMemoryStore 兜底，
                    因为 guide_scaffold 签名声明了 store 参数，编译时必须给
    """
    builder = StateGraph(StudentAgentState)

    builder.add_node("safety_check", safety_check)
    builder.add_node("classify_intent", classify_intent)
    builder.add_node("guide_scaffold", guide_scaffold)
    builder.add_node("explain_concept", explain_concept)
    builder.add_node("grade_submit", grade_submit)
    builder.add_node("respond", respond)

    builder.add_edge(START, "safety_check")
    builder.add_conditional_edges(
        "safety_check", route_after_safety,
        {"respond": "respond", "classify_intent": "classify_intent"},
    )
    builder.add_conditional_edges(
        "classify_intent", route_by_intent,
        {
            "guide_scaffold": "guide_scaffold",
            "grade_submit": "grade_submit",
            "explain_concept": "explain_concept",
            "respond": "respond",
        },
    )
    # 引导节点 resume 完成后，学生的"新输入"意图未知 → 回到分类重新路由。
    # 这条"回到分类"的环就是"提示→再试"多轮引导的实现本体
    builder.add_edge("guide_scaffold", "classify_intent")
    builder.add_edge("explain_concept", "respond")
    builder.add_edge("grade_submit", "respond")
    builder.add_edge("respond", END)

    # 陌生点：InMemoryStore() 不传 checkpointer 也能编译——interrupt 场景
    # 才强制要求 checkpointer（挂起状态没地方存就报错）。演示/单测不传时
    # 走不了引导多轮，只能走讲解/判分单轮分支
    return builder.compile(
        checkpointer=checkpointer,
        store=store or InMemoryStore(),
    )


def make_thread_id(student_id: int, question_id: int) -> str:
    """thread_id 约定：{student_id}_{question_id}（架构文档 4.4）。

    为什么是这个粒度：同一道题的多轮引导共享上下文（换题就是新 thread，
    引导轮数归零——attempt_count 是"每题"概念不是"每生"概念）。
    """
    return f"{student_id}_{question_id}"
