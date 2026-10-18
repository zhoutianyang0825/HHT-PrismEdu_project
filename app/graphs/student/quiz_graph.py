"""M2-T3 · S-10 诊断测验生成子图（架构文档 4.3，独立 StateGraph）。

四步算法（PRD 5.4）：
  fetch_error_kps        近 30 天 error_logs → 去重得错题知识点集合 E + 频次
  expand_prerequisites   Neo4j 1~2 跳 HAS_PREREQUISITE 外扩 → 候选集 C = E ∪ P
  rank_by_bkt            BKT p_known 升序 + 错题频次降序 → Top5 目标知识点
  select_quiz_questions  调组卷引擎逐点配题，总量 8、单点 ≤2（纯规则，不调 LLM）
  persist_quiz           写 diagnostic_quizzes 表（target_points_json 可解释性留痕）

为什么独立子图：主图是"单轮单题+多轮引导"，测验是"一次生成一批题"的批处理，
attempt_count / messages 语义完全不同，通过 question_ids 交接最干净。
"""
from collections import Counter
from datetime import timedelta

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.db.session import get_session_factory
from app.graphs.student.state import QuizState
from app.models import BKTState, DiagnosticQuiz, ErrorLog, Question, StudentAnswer
from app.services.selection_engine import fetch_candidates_by_kp, select_quiz_questions

BKT_DEFAULT_P = 0.10   # bkt_state 没有记录的知识点用 p_init 默认值（PRD 5.1）


# =====================================================================
# 纯函数（tests/test_quiz_generation.py 直接打这里，不碰 DB）
# =====================================================================
def rank_targets(
    error_kp_ids: list[str],
    error_freq: dict[str, int],
    p_known_map: dict[str, float],
    top_n: int = 5,
) -> list[dict]:
    """掌握度升序取 Top5 目标知识点（PRD 5.4 Step 3 的纯逻辑部分）。

    得到的数据：
      error_kp_ids = ["kp_crossmult", "kp_peifang"]      # E ∪ 前置外扩后的候选集
      error_freq   = {"kp_crossmult": 3}                 # 每点近 N 天错几次
      p_known_map  = {"kp_crossmult": 0.33, "kp_peifang": 0.52}
    要做的操作：排序（p_known 升序 → 错题频次降序 → 名字稳定排序）→ 截 Top5
      "同掌握度时按错题频次降序"——保证"又弱又常错"的优先
    返回：[{"kp_id", "p_known", "reason"}, ...]；reason 是给学生的可解释文案
    """
    ranked = sorted(
        error_kp_ids,
        key=lambda kp: (p_known_map.get(kp, BKT_DEFAULT_P),
                        -error_freq.get(kp, 0),
                        kp),
    )[:top_n]

    targets = []
    for kp in ranked:
        freq = error_freq.get(kp, 0)
        reason = (f"近30天连错{freq}次" if freq > 0
                  else "前置薄弱点（错题的先修知识）")
        targets.append({
            "kp_id": kp,
            "p_known": p_known_map.get(kp, BKT_DEFAULT_P),
            "reason": reason,
        })
    return targets


# =====================================================================
# 图节点（含 DB / Neo4j 访问，M4 建 tool 层后收口搬走）
# =====================================================================
def fetch_error_kps(state: QuizState) -> dict:
    """Step 1 · 近 N 天错题 → 错题知识点集合 E + 频次。

    三表联查路径：error_logs(错因) ← student_answers(作答) → questions(kp_id)
    只统计 is_correct == False 的作答——对的不叫错题。
    """
    cutoff = _now() - timedelta(days=state.get("days", 30))
    session = get_session_factory()()
    try:
        rows = session.execute(
            select(Question.kp_id)
            .join(StudentAnswer, StudentAnswer.question_id == Question.id)
            .join(ErrorLog, ErrorLog.answer_id == StudentAnswer.id)
            .where(StudentAnswer.student_id == state["student_id"])
            .where(StudentAnswer.is_correct == False)          # noqa: E712 故意 == False：SQL 语义
            .where(StudentAnswer.submitted_at >= cutoff)
        ).all()
    finally:
        session.close()

    kp_ids = [r[0] for r in rows]
    freq = dict(Counter(kp_ids))       # Counter：计数器容器，.most_common() 常配套用
    return {"error_kp_ids": sorted(set(kp_ids)), "error_freq": freq}


def _now():
    """当前 UTC 时间。抽成函数：单测可以 monkeypatch 它控制"30 天"边界。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def expand_prerequisites(state: QuizState) -> dict:
    """Step 2 · 图谱外扩：错题知识点走 1~2 跳前置边，得候选集 C = E ∪ P。

    Cypher 与架构文档 3.2 #5 一致。方向注意：seed 脚本约定
    HAS_PREREQUISITE 从"前置点"指向"依赖它的点"，
    所以查某点的前置要用反向 <-[:HAS_PREREQUISITE*1..2]- 。
    """
    from app.graph_db.neo4j_client import run_query

    rows = run_query(
        """
        MATCH (e:KnowledgePoint)<-[:HAS_PREREQUISITE*1..2]-(p:KnowledgePoint)
        WHERE e.id IN $ids
        RETURN DISTINCT p.id AS kp_id, p.name AS name
        """,
        {"ids": state["error_kp_ids"]},
    )
    # 候选集 = 错题点本身 ∪ 它们的前置点（DISTINCT 已在 Cypher 去重，union 再兜一次）
    prereq_ids = [r["kp_id"] for r in rows]
    candidate_ids = sorted(set(state["error_kp_ids"]) | set(prereq_ids))
    return {"candidate_kps": [{"kp_id": k} for k in candidate_ids]}


def rank_by_bkt(state: QuizState) -> dict:
    """Step 3 · 读 bkt_state，给候选集每个知识点配掌握度 → rank_targets 排序。"""
    kp_ids = [c["kp_id"] for c in state["candidate_kps"]]
    session = get_session_factory()()
    try:
        rows = session.execute(
            select(BKTState.kp_id, BKTState.p_known)
            .where(BKTState.student_id == state["student_id"])
            .where(BKTState.kp_id.in_(kp_ids))
        ).all()
    finally:
        session.close()

    p_known_map = {kp: float(p) for kp, p in rows}
    targets = rank_targets(
        error_kp_ids=kp_ids,
        error_freq=state["error_freq"],
        p_known_map=p_known_map,
        top_n=5,
    )
    return {"target_points": targets}


def select_quiz_questions_node(state: QuizState) -> dict:
    """Step 4 · 配题：查题库候选 → 组卷引擎抽题（纯规则，可复现）。

    为什么不放 LLM：同一份掌握度必须产出同一套题，LLM 每次结果不同，
    老师就无法信任这个测验（架构文档 4.3 原注释）。
    """
    kp_ids = [tp["kp_id"] for tp in state["target_points"]]
    candidates = fetch_candidates_by_kp(kp_ids)
    # exclude：该生近期做过的题（近 7 天 student_answers，M2 简版全量排除）
    exclude = _recent_answered_ids(state["student_id"])

    picked, gaps = select_quiz_questions(
        target_points=state["target_points"],
        candidates_by_kp=candidates,
        total=state.get("total", 8),
        per_kp_cap=2,
        exclude_ids=exclude,
    )
    if gaps:
        # 宁可少题不乱补：缺口照实打印，演示时这就是"引擎诚实"的证明点
        print(f"[S-10 配题缺口] {gaps}")
    return {
        "question_ids": [p["id"] for p in picked],
        "questions": picked,
    }


def _recent_answered_ids(student_id: int, days: int = 7) -> set[int]:
    """近 7 天该生做过的题目 id（去重用）。M2 简版：全部作答都算。"""
    cutoff = _now() - timedelta(days=days)
    session = get_session_factory()()
    try:
        rows = session.execute(
            select(StudentAnswer.question_id)
            .where(StudentAnswer.student_id == student_id)
            .where(StudentAnswer.submitted_at >= cutoff)
        ).all()
    finally:
        session.close()
    return {r[0] for r in rows}


def persist_quiz(state: QuizState) -> dict:
    """Step 5 · 写 diagnostic_quizzes 表：target_points_json 留痕可解释性。

    generated_reason 面向学生的一句话："你最近在 xxx 上连错了 3 次"。
    陌生点 session.flush()：insert 后立刻拿数据库生成的自增 id
    （不 flush 的话 quiz.id 是 None，commit 前数据库还没发 INSERT）。
    """
    if not state["question_ids"]:
        return {"quiz_id": None,
                "generated_reason": "最近没有错题记录，暂不需要诊断测验"}

    top = state["target_points"][0] if state["target_points"] else None
    reason = (f"你最近在「{top['reason']}」对应的知识点上掌握度偏低"
              f"（{top['p_known']:.2f}），这套 8 题小测帮你逐个补齐"
              if top else "针对性诊断测验")

    session = get_session_factory()()
    try:
        quiz = DiagnosticQuiz(
            student_id=state["student_id"],
            target_points_json=state["target_points"],
            question_ids=state["question_ids"],
            generated_reason=reason,
        )
        session.add(quiz)
        session.flush()               # 拿自增 id
        quiz_id = quiz.id
        session.commit()
    except Exception:
        session.rollback()            # 半路失败整笔回滚，不留脏行
        raise
    finally:
        session.close()

    return {"quiz_id": quiz_id, "generated_reason": reason}


def build_quiz_graph():
    """组装 S-10 子图：纯顺序链路，无 interrupt，不需要 checkpointer。"""
    builder = StateGraph(QuizState)
    builder.add_node("fetch_error_kps", fetch_error_kps)
    builder.add_node("expand_prerequisites", expand_prerequisites)
    builder.add_node("rank_by_bkt", rank_by_bkt)
    builder.add_node("select_quiz_questions_node", select_quiz_questions_node)
    builder.add_node("persist_quiz", persist_quiz)

    builder.add_edge(START, "fetch_error_kps")
    builder.add_edge("fetch_error_kps", "expand_prerequisites")
    builder.add_edge("expand_prerequisites", "rank_by_bkt")
    builder.add_edge("rank_by_bkt", "select_quiz_questions_node")
    builder.add_edge("select_quiz_questions_node", "persist_quiz")
    builder.add_edge("persist_quiz", END)
    return builder.compile()
