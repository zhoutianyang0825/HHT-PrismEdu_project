"""M2-T2 · interrupt 挂起-恢复最小样例：先脱离业务验证机制，再上业务图。

三种跑法（对应"MemorySaver → PostgresSaver"的验证阶梯）：

  # ① 同进程 MemorySaver：最基础的挂起→恢复
  uv run python scripts/demo_interrupt.py

  # ② 同进程 PostgresSaver：业务同库的持久化（setup() 建内部表）
  uv run python scripts/demo_interrupt.py --pg

  # ③ 跨进程 PostgresSaver（🔴 核心验证：后端重启后挂起的 interrupt 仍可恢复）
  uv run python scripts/demo_interrupt.py --pg --phase1   # 挂起后进程退出
  uv run python scripts/demo_interrupt.py --pg --phase2   # 新进程恢复，继续跑完

预期：phase2 在"全新进程"里把 phase1 挂起的会话续上——这就是 PRD 验收
"后端进程重启后挂起会话仍在"的最小证明。
"""
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.core.config import settings

THREAD = {"configurable": {"thread_id": "demo-1"}}   # demo 单线程，id 固定


class DemoState(TypedDict):
    reply: str


def gate_node(state: DemoState) -> dict:
    """唯一节点：抛出 interrupt 挂起，等 resume；拿到口令后生成交接数据。

    interrupt() 的返回值 = Command(resume=X) 里的 X。
    这里故意用"口令"这种与业务无关的东西，证明机制本身通了。
    """
    secret = interrupt({"question": "挂起了！请输入口令（随便输入什么都行）"})
    return {"reply": f"收到 resume 值：{secret!r}，图继续执行到 END"}


def build_demo_graph(checkpointer):
    builder = StateGraph(DemoState)
    builder.add_node("gate", gate_node)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=checkpointer)


def run_memory_saver() -> None:
    """① MemorySaver：同进程挂起 → 恢复（机制最小区验证）。"""
    graph = build_demo_graph(MemorySaver())
    result = graph.invoke({}, THREAD)
    assert "__interrupt__" in result, "预期第一次 invoke 挂起"
    payload = result["__interrupt__"][0].value
    print(f"[MemorySaver] 挂起成功，payload = {payload}")

    result = graph.invoke(Command(resume="芝麻开门"), THREAD)
    print(f"[MemorySaver] 恢复成功，最终 state = {result}")
    print("[MemorySaver] ① 同进程挂起-恢复 ✓")


def run_postgres(phase: int) -> None:
    """②③ PostgresSaver：与业务表同库的持久化 checkpointer。

    陌生写法 with PostgresSaver.from_conn_string(...)：连接由上下文管理器
    托管，出 with 块自动释放。setup() 建它需要的内部表（幂等，可重复调）——
    忘调 setup 的表现是首次写 checkpoint 报"关系不存在"（架构文档 12 坑）。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(settings.postgres_dsn) as checkpointer:
        checkpointer.setup()
        graph = build_demo_graph(checkpointer)

        if phase == 1:
            result = graph.invoke({}, THREAD)
            assert "__interrupt__" in result
            print(f"[PostgresSaver] phase1 挂起成功：{result['__interrupt__'][0].value}")
            print("[PostgresSaver] 进程即将退出——挂起状态已写进 PG 的 checkpoint 表")
            return

        if phase == 2:
            # 全新进程、全新连接，靠 PG 里存的 checkpoint 续上 phase1 的挂起
            result = graph.invoke(Command(resume="芝麻开门"), THREAD)
            print(f"[PostgresSaver] phase2 跨进程恢复成功：{result['reply']}")
            print("[PostgresSaver] ③ 重启后 resume ✓ —— M2 关键路径打通")
            return

        # phase == 0：同进程完整走一遍
        result = graph.invoke({}, THREAD)
        assert "__interrupt__" in result
        print(f"[PostgresSaver] 挂起成功：{result['__interrupt__'][0].value}")
        result = graph.invoke(Command(resume="芝麻开门"), THREAD)
        print(f"[PostgresSaver] 恢复成功：{result['reply']}")
        print("[PostgresSaver] ② 同进程持久化挂起-恢复 ✓")


if __name__ == "__main__":
    if "--pg" in sys.argv:
        phase = 1 if "--phase1" in sys.argv else (2 if "--phase2" in sys.argv else 0)
        run_postgres(phase)
    else:
        run_memory_saver()
