"""M2-T5 · 命令行全流程测试：扮演学生求答案 → 阶梯提示 → 再试 → 批改。

运行：
  uv run python -m app.graphs.student.cli_demo            # 默认学生1 + 题库里第一题
  uv run python -m app.graphs.student.cli_demo --question-id 3

CLI 命令（学生输入里混着用）：
  讲解一下 / 我的答案是xxx / 这题答案是多少   → 走学生图（意图自动分类）
  生成测验                                   → 触发 S-10 quiz 子图
  退出                                       → 结束

预期交互（开发计划 M2 完成判据的模拟）：
  学生> 这题答案是多少？
  Agent> 先观察常数项：哪两个数相乘等于 6，相加又等于 -5？   ← interrupt 挂起
  学生> 直接告诉我答案嘛
  Agent> 提示再进一步：试 (x-2)(x-?)                        ← 仍不给答案，引导递进
  学生> (x-2)(x-3)=0
  Agent> ✓ 正确！x₁=2, x₂=3
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from sqlalchemy import select

from app.core.config import settings
from app.core.llm import get_llm
from app.db.session import get_session_factory
from app.graphs.student.graph import build_student_graph, make_thread_id
from app.graphs.student.quiz_graph import build_quiz_graph
from app.models import Question


def pick_question(question_id: int | None) -> Question:
    """取演示用题目：指定 id 或题库第一题。库里没题先跑 seed_question_bank.py。"""
    session = get_session_factory()()
    try:
        stmt = select(Question).order_by(Question.id)
        if question_id:
            stmt = stmt.where(Question.id == question_id)
        return session.scalars(stmt).first()
    finally:
        session.close()


def print_quiz(result: dict) -> None:
    """打印 S-10 产出：目标知识点 + 题目 + 一句话理由（验收看三样东西）。"""
    print("\n===== S-10 诊断测验 =====")
    print(f"理由：{result.get('generated_reason', '')}（quiz_id={result.get('quiz_id')}）")
    print("目标知识点（按优先级）：")
    for tp in result.get("target_points", []):
        print(f"  - {tp['kp_id']:<16} p_known={tp['p_known']:.2f}  {tp['reason']}")
    print("题目：")
    for i, q in enumerate(result.get("questions", []), start=1):
        print(f"  {i}. [{q['difficulty']}] {q['kp_id']} -> 题目id={q['id']}")
    print("=========================\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="M2 学生 Agent CLI demo")
    parser.add_argument("--student-id", type=int, default=1)
    parser.add_argument("--question-id", type=int, default=None)
    args = parser.parse_args()

    question = pick_question(args.question_id)
    if question is None:
        sys.exit("题库里没有题目：先跑 uv run python scripts/seed_question_bank.py")

    # PostgresSaver：与业务表同库（架构 v1.6 定稿）。setup() 幂等建内部表。
    # 陌生点：PostgresSaver 必须用 with（上下文管理器）拿实例，它要托管连接
    with PostgresSaver.from_conn_string(settings.postgres_dsn) as checkpointer:
        checkpointer.setup()
        graph = build_student_graph(checkpointer=checkpointer, store=InMemoryStore())
        quiz_graph = build_quiz_graph()   # 子图无 interrupt，不需要 checkpointer

        tid = make_thread_id(args.student_id, question.id)
        config = {"configurable": {"thread_id": tid}}

        print(f"=== HHT棱镜课堂 · 学生端 CLI（student={args.student_id} thread={tid}）===")
        print(f"题目[{question.id}]：{question.content}\n（标准答案对 学生 隐藏）\n")

        pending_resume = False   # True = 图正挂在 interrupt 上，下一条输入用 Command(resume=)
        while True:
            try:
                text = input("学生> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text == "退出":
                break

            if text == "生成测验":
                quiz = quiz_graph.invoke(
                    {"student_id": args.student_id, "days": 30, "total": 8},
                    config={"configurable": {"thread_id": f"quiz_{args.student_id}"}},
                )
                print_quiz(quiz)
                continue

            if pending_resume:
                result = graph.invoke(Command(resume=text), config)
                pending_resume = False
            else:
                result = graph.invoke(
                    {"student_id": args.student_id, "question_id": question.id,
                     "messages": [HumanMessage(content=text)]},
                    config,
                )

            # interrupt 挂起：打印 hint，标记下一条输入是 resume
            if "__interrupt__" in result:
                payload = result["__interrupt__"][0].value
                print(f"Agent> {payload['hint']}")
                print(f"        （第 {payload['attempt_count']} 次引导，还想再要答案可以继续输入）")
                pending_resume = True
                continue

            print(f"Agent> {result['messages'][-1].content}\n")

    print("再见！")


if __name__ == "__main__":
    main()
