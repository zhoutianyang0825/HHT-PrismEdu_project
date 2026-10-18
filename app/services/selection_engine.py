"""组卷引擎骨架（架构文档 5.3）：M2 给 S-10 用，M4 扩展给 T-01/R-08 复用。

核心工程原则（架构文档原话）：**选题是确定性计算，不交给 LLM**。
LLM 只决定"选哪些知识点、各占多少比重"（配额表），真正抽题由本文件的
Python 规则执行——同一份输入必须产出同一套题，老师才敢信。

M2 版实现范围（S-10 需要的最小集）：
  ✓ 难度定档：p_known 反相关映射（PRD 5.4 Step 4）
  ✓ 单知识点 ≤2 题硬截断，配额让给次优先知识点
  ✓ 难度不足时放宽到其他档补齐
  ✓ 题量不足记缺口，宁可少题不乱补（绝不随机凑数）
M4 再加（R-08 需要）：quality_flag 降权、近 7 天去重参数化、题型配比。
"""


def difficulty_for(p_known: float) -> str:
    """掌握度 → 难度档（PRD 5.4：越薄弱越给基础题，反相关映射）。

    得到的数据：该生在该知识点的 BKT 掌握概率，类似 0.33
    返回：easy / medium / hard
      p_known < 0.40  → easy（先补地基）
      0.40 ~ 0.70     → medium
      > 0.70          → hard（少量，验证能不能迁移）
    边界值归属：0.40 归 medium、0.70 归 hard（左闭右开区间，和 PRD 文字一致）
    """
    if p_known < 0.40:
        return "easy"
    if p_known < 0.70:
        return "medium"
    return "hard"


def select_quiz_questions(
    target_points: list[dict],
    candidates_by_kp: dict[str, list[dict]],
    total: int = 8,
    per_kp_cap: int = 2,
    exclude_ids: set[int] | None = None,
) -> tuple[list[dict], list[str]]:
    """按 Top5 目标知识点配题（纯函数：不碰 DB / 不调 LLM，单测直接打这里）。

    得到的数据（参数形态示例）：
      target_points = [                        # rank_targets 的输出，已按优先级排好
          {"kp_id": "kp_crossmult", "name": "十字相乘法", "p_known": 0.33, "reason": "近30天连错3次"},
          {"kp_id": "kp_peifang",   "name": "配方法",     "p_known": 0.52, "reason": "前置薄弱点"},
      ]
      candidates_by_kp = {                     # 题库候选，按知识点预分组
          "kp_crossmult": [{"id": 12, "kp_id": "kp_crossmult", "difficulty": "easy"}, ...],
          "kp_peifang":   [],
      }
      exclude_ids = {31, 47}                   # 近期做过的题，直接排除
    要做的操作：逐知识点（按优先级顺序）→ 定难度档 → 档内优先、他档兜底
               → 单点 ≤2 截断 → 总量 8 封顶 → 不足记缺口
    返回：(选中的题目 dict 列表, 缺口说明列表)
      缺口示例：["kp_peifang: 题库无可用题（目标档 medium，池中 0 题）"]
    """
    exclude_ids = exclude_ids or set()
    picked: list[dict] = []
    gaps: list[str] = []

    for tp in target_points:
        if len(picked) >= total:
            break                            # 总配额用完，后面的知识点不再消费
        kp_id = tp["kp_id"]
        want = difficulty_for(tp["p_known"])
        # 排除近期做过的题
        pool = [c for c in candidates_by_kp.get(kp_id, []) if c["id"] not in exclude_ids]

        # 难度分配策略：目标档优先排前面，不够从其他档兜底
        in_order = [c for c in pool if c["difficulty"] == want] + \
                   [c for c in pool if c["difficulty"] != want]
        take = in_order[: min(per_kp_cap, total - len(picked))]

        if not take:
            gaps.append(f"{kp_id}: 题库无可用题（目标档 {want}，池中 {len(pool)} 题）")
        picked.extend(take)

    return picked, gaps


def fetch_candidates_by_kp(kp_ids: list[str]) -> dict[str, list[dict]]:
    """从 PG 题库按知识点批量取候选题（quiz_graph 的 select 节点调用）。

    得到的数据：知识点 id 列表，类似 ["kp_crossmult", "kp_peifang"]
    要做的操作：查 questions 表，quality_flag='disabled' 直接剔除
    （abnormal 保留——M4 才有 R-04 写回 abnormal，M2 阶段全库都是 normal）
    返回：{kp_id: [{"id": 12, "kp_id": "...", "difficulty": "easy"}, ...], ...}
    注意：这里不做难度过滤——定档是引擎的职责，取数只负责把池子捞全
    """
    from sqlalchemy import select
    from app.db.session import get_session_factory
    from app.models import Question

    session = get_session_factory()()
    try:
        rows = session.execute(
            select(Question.id, Question.kp_id, Question.difficulty)
            .where(Question.kp_id.in_(kp_ids))
            .where(Question.quality_flag != "disabled")
        ).all()
    finally:
        session.close()

    grouped: dict[str, list[dict]] = {kp: [] for kp in kp_ids}
    for qid, kp, diff in rows:
        grouped[kp].append({"id": qid, "kp_id": kp, "difficulty": diff})
    return grouped
