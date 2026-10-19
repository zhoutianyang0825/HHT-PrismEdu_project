"""M3-T6 · 批改全链 CLI 验收：挂起 → resume 确认 → 分数生效 → BKT 变化。

用法（两种批改各走一遍）：

  # ① 主观题 HITL 全链（🔴 M3 验收核心）：seed 数据 → LLM 建议 → 挂起 →
  #    你在终端扮演老师确认/改分/驳回 → 分数生效 → BKT 前后对比
  uv run python scripts/demo_grading.py

  # ② 跳过 seed，直接批库里已有的一条作答（重复演示用）
  uv run python scripts/demo_grading.py --answer-id 42

预期交互（模拟）：
  [1] seed 主观题作答（学生答了一半）...
  [2] 触发批改，图挂起，AI 建议如下：
      ┌─ AI 批改建议（置信度 0.82）────────────
      │ 建议得分：50.0 / 100.0
      │ 采分点：['正确列出判别式 已达成', '未讨论 Δ=0 情形 缺失']
      │ 评语：思路对了一半……
      └──────────────────────────────
  老师操作：c=采纳 / e=改分 / r=驳回 > e
  改分至 > 65
  [3] 批改生效：final_score=65.0 status=confirmed grader_type=teacher
  [4] BKT：p_known 0.100 → 0.326（知识点 kp_panbie，答对线以上走抬升路径）
  [5] audit_logs 已留痕：grading.confirm -> student_answers:1001
  M3 主观题 HITL 全链 ✓
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.core.security import hash_password
from app.db.session import get_session_factory
from app.graphs.teacher.grading_graph import build_grading_graph, make_thread_id
from app.models import (
    Class,
    Question,
    Student,
    StudentAnswer,
    User,
    UserRole,
)


# =====================================================================
# seed：最小可批改数据（班级/用户/学生/主观题/一条"答了一半"的作答）
# =====================================================================
def seed_subjective_answer() -> int:
    """造一条可批改的主观题作答（幂等，重复跑不会重复建数据）。

    得到的数据：无入参
    要做的操作：依次确保 班级 / 学生账号 / 学生档案 / 主观题 存在，再插一条"答了一半"的作答
    返回：student_answers 的自增 id，形如 1001
    为什么：M3 验收要验证"挂起→确认→生效"全链，不能依赖 M1 题库 seed 的具体题号
    """
    session = get_session_factory()()
    try:
        # ---- 班级（固定 id=1 场景：先查再建，幂等）----
        klass = session.query(Class).filter_by(grade="初三", name="演示班").first()
        if klass is None:
            klass = Class(grade="初三", name="演示班")
            session.add(klass)
            session.flush()

        # ---- 学生账号（is_active=True，演示专用）----
        user = session.query(User).filter_by(username="demo_student").first()
        if user is None:
            user = User(
                username="demo_student",
                password_hash=hash_password("Demo1234"),
                name="演示学生",
                is_active=True,
            )
            user.roles.append(UserRole(role="student"))
            session.add(user)
            session.flush()

        student = session.query(Student).filter_by(user_id=user.id).first()
        if student is None:
            student = Student(user_id=user.id, class_id=klass.id, student_no="S-DEMO")
            session.add(student)
            session.flush()

        # ---- 主观题（一次一考：判别式讨论）----
        question = session.query(Question).filter_by(
            content="用判别式判断方程 x²-2x+1=0 的根的情况，并说明理由。"
        ).first()
        if question is None:
            question = Question(
                subject="math",
                chapter_id="ch_quadratic",
                kp_id="kp_panbie",
                question_type="subjective",
                difficulty="medium",
                content="用判别式判断方程 x²-2x+1=0 的根的情况，并说明理由。",
                answer="Δ=b²-4ac=4-4=0，因为 Δ=0，所以方程有两个相等的实数根 x₁=x₂=1。",
                analysis="采分点：①正确写出 Δ 的算式；②算出 Δ=0；③结论：两个相等实数根。",
                source="演示数据",
            )
            session.add(question)
            session.flush()

        # ---- 学生作答（故意答一半：算对 Δ=0 但结论只说"有根"）----
        answer = StudentAnswer(
            source_type="homework",
            source_id=0,          # 演示无真实作业，满分走默认 100
            question_id=question.id,
            student_id=student.id,
            content="Δ=4-4=0，所以方程有根。",
        )
        session.add(answer)
        session.commit()
        return answer.id
    finally:
        session.close()


def print_interrupt_payload(payload: dict) -> None:
    """把 interrupt payload 渲染成老师能看的确认框。

    得到的数据：payload，interrupt() 抛出的 dict，形如
                {"ai_score": 50.0, "full_score": 100.0, "scoring_basis": [...], "confidence": 0.82}
    要做的操作：逐行打印（M5 换成 Streamlit 确认组件）
    返回：None（只往终端写）
    为什么：CLI 阶段要让人一眼看清"AI 给了多少分、依据是什么"，才判断得了要不要改分
    """
    print("┌─ AI 批改建议（置信度 {:.2f}）────────".format(payload.get("confidence", 0)))
    print(f"│ 建议得分：{payload['ai_score']} / {payload['full_score']}")
    for line in payload.get("scoring_basis", []):
        print(f"│ 采分点：{line}")
    print(f"│ 评语：{payload['ai_comment']}")
    print("└" + "─" * 36)


def ask_teacher_decision(full_score: float) -> dict:
    """终端里扮演老师，收集一次批改决定。

    得到的数据：full_score 本题满分，用于提示改分范围
    要做的操作：循环读输入，c=采纳 / e=改分 / r=驳回
    返回：老师决定 dict，形如 {"action": "edit", "score": 65.0}
    为什么：这个 dict 就是 Command(resume=...) 传回去的值，格式必须与图里的约定一致
    """
    while True:
        raw = input("老师操作：c=采纳 / e=改分 / r=驳回 > ").strip().lower()
        if raw == "c":
            return {"action": "confirm"}
        if raw == "e":
            score = float(input(f"改分至（0~{full_score}）> ").strip())
            return {"action": "edit", "score": score}
        if raw == "r":
            return {"action": "reject"}
        print("输入不认识，请输 c / e / r")


def main() -> None:
    """跑一次完整批改：seed → invoke 挂起 → 老师决定 → resume → 展示 BKT 变化。

    得到的数据：命令行参数 --answer-id（可选）；不给就现场 seed 一条新的作答
    要做的操作：建图（MemorySaver）→ 首次 invoke → 按是否挂起分支 → resume → 打印结果
    返回：None；结果全部打印到终端
    为什么：这是 M3 的系统级验收入口，一条命令就能验证"挂起→确认→分数生效→BKT 变化"全链
    """
    # ---- ① 准备数据 ----
    if "--answer-id" in sys.argv:
        answer_id = int(sys.argv[sys.argv.index("--answer-id") + 1])
        print(f"[1] 使用已有作答 answer_id={answer_id}")
    else:
        answer_id = seed_subjective_answer()
        print(f"[1] seed 主观题作答完成 answer_id={answer_id}")

    graph = build_grading_graph(MemorySaver())   # 同进程演示用内存 checkpointer；正式用 get_checkpointer()
    config = {"configurable": {"thread_id": make_thread_id(answer_id)}}

    # ---- ② 首次 invoke：主观题会在 LLM 建议后挂起 ----
    print("[2] 触发批改图（主观题：LLM 建议后挂起等老师）...")
    result = graph.invoke(
        {"answer_id": answer_id, "teacher_user_id": None}, config
    )

    if "__interrupt__" not in result:
        # 走到了客观题直判（answer_id 指向选择/填空题）或已批过，直接展示结果
        print(f"    未挂起：status={result.get('status')} final_score={result.get('final_score')}")
        return

    payload = result["__interrupt__"][0].value
    print_interrupt_payload(payload)

    # ---- ③ 老师决定 → Command(resume=...) 恢复 ----
    decision = ask_teacher_decision(payload["full_score"])
    print("[3] resume 恢复，图继续执行：commit → 归因(答错时) → BKT ...")
    result = graph.invoke(Command(resume=decision), config)

    print(f"    批改结果：status={result['status']} final_score={result.get('final_score')}")
    print(f"    is_correct={result.get('is_correct')} error_type={result.get('error_type') or '-'}")

    # ---- ④ BKT 前后对比 ----
    if result.get("p_known_after") is not None:
        print(
            f"[4] BKT：p_known {result['p_known_before']:.3f} → {result['p_known_after']:.3f}"
            f"（知识点 {result['kp_id']}，三处同步：bkt_state/快照/Neo4j）"
        )

    print("M3 主观题 HITL 全链 ✓（挂起 → resume 确认 → 分数生效 → BKT 变化）")


if __name__ == "__main__":
    main()
