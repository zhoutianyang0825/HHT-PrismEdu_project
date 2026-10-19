"""M2-T2 · 学生图编排：StateGraph 组装 + 编译出口。

状态图拓扑（架构文档 4.2 的 M2 版，归因/推荐/BKT 三个节点 M3 接入）：

    START → safety_check ──blocked──→ respond → END
                │放行
                ▼
          classify_intent 意图识别
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

from app.graphs.student.nodes import (
    classify_intent,
    explain_concept,
    grade_submit,
    guide_scaffold,
    respond,
    safety_check,
)

from app.graphs.student.state import StudentAgentState

# 路由函数1
def route_after_safety(state: StudentAgentState) -> str:
    """safety_check 的条件边：被拦截 → 直接 respond 兜底，不再进 LLM。"""
    return "respond" if state.get("blocked_reason") else "classify_intent"


# 路由函数2，核心路由表
def route_by_intent(state: StudentAgentState) -> str:
    """意图 → 节点名 的路由表（条件边只认"返回节点名"这一种契约）。"""
    return {
        "ask_answer": "guide_scaffold", # 直接问答案时，不能直接给答案，先引导学生
        "submit_answer": "grade_submit", 
        "explain": "explain_concept",   # 直接说不会做/要讲解时，启动讲解
        "off_topic": "respond",
    }[state["intent"]]


def build_student_graph(
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
):
    """组装并编译学生图。

    参数（两个都是必填，缺任一立刻 raise）：
      checkpointer  短期记忆（按 thread_id）：
                    interrupt 挂起的会话靠它落库，进程重启后还能 resume。
                    正式传 app.db.checkpointer.get_checkpointer()（PG）；
                    单元测试传 get_memory_checkpointer()（内存）。

      store         长期记忆（按 namespace 元组）：
                    学生画像靠它跨题共享，guide_scaffold 的 warm_start 直接往它里面写/读。
                    正式传 app.db.store.get_store()（PG，重启画像还在）；
                    降级通路传 get_memory_store()（内存，本次运行接受画像丢失）。

    返回的数据：CompiledStateGraph（可 invoke 的成品图）
    """
    builder = StateGraph(StudentAgentState) # 初始化一张空图

    builder.add_node("safety_check", safety_check)
    builder.add_node("classify_intent", classify_intent)
    builder.add_node("guide_scaffold", guide_scaffold)
    builder.add_node("explain_concept", explain_concept)
    builder.add_node("grade_submit", grade_submit)
    builder.add_node("respond", respond)


    builder.add_edge(START, "safety_check")
    builder.add_conditional_edges(
        "safety_check", route_after_safety, {"respond": "respond", "classify_intent": "classify_intent"},
    )

    builder.add_conditional_edges(
        "classify_intent", route_by_intent,
        {
            "guide_scaffold": "guide_scaffold",
            "grade_submit": "grade_submit",
            "explain_concept": "explain_concept",
            "respond": "respond",
        },  # 允许的路径声明
    )


    builder.add_edge("guide_scaffold", "classify_intent") # ← 回环：从引导节点再回到分类节点，"提示→再试"多轮引导
    builder.add_edge("explain_concept", "respond")
    builder.add_edge("grade_submit", "respond")
    builder.add_edge("respond", END)


    # checkpointer 在签名里必填（没有默认值）→ 忘传直接 TypeError。
    if checkpointer is None:
        raise ValueError(
            "checkpointer 不能为 None，需调用以下函数获得：\n"
            "  正式服务 → 调用app.db.checkpointer.get_checkpointer()，因为 interrupt 挂起的会话要靠它落pg库，才能重启恢复。\n"
            "  单元测试     → 调用app.db.checkpointer.get_memory_checkpointer()（内存，重启丢失内存数据帧）"
        )

    if store is None:
        raise ValueError(
            "store 不能为 None，学生画像要靠它 跨thread_id 共享（warm_start 依赖它），需调用以下函数获得：\n"
            "       正式演示/服务 → app.db.store.get_store()（PG，重启画像还在）\n"
            "       降级通路     → app.db.store.get_memory_store()（内存，接受本次运行画像丢失）"
        )


    # 两个记忆都是必填（都是"必须功能"），降级由调用方显式选工厂，
    # 调用build_student_graph（）前，需要获得checkpointer和store对象，再传入
    return builder.compile(
        checkpointer=checkpointer,
        store=store,
    )



def make_thread_id(student_id: int, question_id: int) -> str:
    """thread_id 约定：{student_id}_{question_id}（架构文档 4.4）。

    为什么是这个粒度：
        同一道题的多轮引导共享上下文
        换题就是开新 thread，引导轮数attempt_count归零（attempt_count 是"每题引导轮数"）。
    """
    return f"{student_id}_{question_id}"
