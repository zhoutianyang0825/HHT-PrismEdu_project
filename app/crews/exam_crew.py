"""M4-T4 · R-08 试卷生成：命题规划 → 选题执行 → 质量审核。

 三角色分工：
   ① 命题规划师  唯一调 LLM 的环节，产 QuotaPlan 配额表（output_pydantic 卡格式）
   ② 选题执行器  纯 Python 调 selection_engine，零 LLM（抽题必须可复现）
   ③ 质量审核员  硬指标纯 Python 算，不过关降难度重抽，最多 2 轮
 两种模式的硬指标：月考（错题命中率 ≥70%）/ 摸底（章节覆盖 ≥80%），见 PRD 5.5
"""
import json

from app.crews.llm_bridge import get_crewai_llm, to_crewai_tools
from app.schemas.exam import QuotaItem, QuotaPlan, ReviewReport
from app.services.selection_engine import fetch_candidates_by_kp, select_questions_by_quota

# 月考（巩固型）目标分布：易 40 / 中 45 / 难 15
MONTHLY_DIFF_TARGET = {"easy": 0.40, "medium": 0.45, "hard": 0.15}

# 期中/期末（摸底型）目标分布：易 70 / 中 20 / 难 10
MOOD_DIFF_TARGET = {"easy": 0.70, "medium": 0.20, "hard": 0.10}

# 摸底型题型配比：选 : 填 : 解 ≈ 4 : 3 : 3
MOOD_TYPE_TARGET = {"choice": 0.4, "fill": 0.3, "subjective": 0.3}

TOLERANCE = 0.10          # 分布容差 ±10 个百分点
HIT_RATE_MIN = 0.70       # 月考错题命中率下限
CHAPTER_COVER_MIN = 0.80  # 摸底型章节覆盖率下限
MAX_ROUNDS = 2            # 审核回退上限（PRD 5.5）


# =====================================================================
# ① 命题规划师：唯一调 LLM 的环节
# =====================================================================
def _plan_quota(
    exam_type: str,
    grade: str,
    subject: str,
    hotspots: list[dict],
    mastery_summary: str = "",
) -> QuotaPlan:
    """跑单 Agent crew，产出配额表（出题策略表）。

    得到的数据：exam_type="monthly"；grade="初三"；subject="math"；
                hotspots=[{"kp_id": "kp_crossmult", "name": "十字相乘法", "wrong_count": 27}, ...]；
                mastery_summary="kp_crossmult 0.31 / kp_peifang 0.52"（摸底型用，可空）
    要做的操作：按考试类型拼规则文本，和热点 JSON 一起写进 Task description，kickoff 出表
    返回：QuotaPlan(exam_type="monthly", total_score=100, items=[QuotaItem(...), ...])；
          LLM 输出不合规则退 _fallback_plan 的纯规则表
    为什么：配额是策略不是计算——LLM 只决定"选哪些点、各占多少"，抽题一律交给 Python
    """
    from crewai import Agent, Crew, Process, Task   # 陌生库 crewai：Agent/Task/Crew 见 llm_bridge 文件头

    # 温度 0.2：配额表要稳定，同一份热点跑两次应得到相近的表
    llm = get_crewai_llm(temperature=0.2)

    # 规划师只需要"查前置链"：热点/掌握度数字 Python 已算好塞进提示词，不必再让它查库
    from app.tools.graph_tools import query_knowledge_graph
    planner_tools = to_crewai_tools([query_knowledge_graph])

    if exam_type == "monthly":
        rule_text = (
            "月考是巩固型：配额的知识点必须 ≥70% 来自下面给出的错题热点及其前置链"
            "（可用 query_knowledge_graph 工具查热点的前置点，前置点也要适当配额）。"
            f"难度目标：易 40% / 中 45% / 难 15%。避开历史 P<0.3 的偏难题。"
        )
    else:
        rule_text = (
            "期中/期末是摸底型：知识点要按章节均匀覆盖（章节覆盖率目标 ≥80%）。"
            f"难度目标：易 70% / 中 20% / 难 10%。题型配比：选择:填空:解答 ≈ 4:3:3。"
            "保留历史区分度好的题，剔除历史 D<0.2 的无效题对应的知识点低难度配额。"
        )

    hotspots_text = json.dumps(hotspots, ensure_ascii=False)

    planner = Agent(
        role="命题规划师",
        goal=f"为{grade}{subject}制定一份{'月考（巩固型）' if exam_type == 'monthly' else '摸底型'}"
             "的知识点×难度×题型配额表",
        # backstory 里写明"不出题"：否则模型会在 reason 里顺手编题，污染配额表
        backstory="你熟悉初中数学命题规范，知道月考该纠偏、期末该覆盖。"
                  "你只做规划，不出题、不抽题。",
        tools=planner_tools,
        llm=llm,
        max_iter=4,             # Agent 最多思考-行动轮次：防它反复查同一个前置点
        verbose=False,
    )

    t_plan = Task(
        description=(
            f"考试类型：{exam_type}；年级：{grade}；学科：{subject}。\n"
            f"{rule_text}\n\n"
            f"年级错题热点（近30天，wrong_count 降序）：\n{hotspots_text}\n\n"
            + (f"班级掌握度摘要（avg_p_known 升序，最薄弱在前）：\n{mastery_summary}\n\n" if mastery_summary else "")
            + "请产出 QuotaPlan：每个知识点一行（kp_id/chapter_id/question_count/"
              "difficulty/question_type/reason），question_count 总和即整卷题量"
              "（月考默认 20 题左右，摸底型默认 25 题左右）。reason 写清入选依据。"
        ),
        expected_output="符合 QuotaPlan schema 的结构化 JSON 配额表",
        agent=planner,
        # 陌生参数 output_pydantic：让模型按该 Pydantic 模型吐结构化结果，
        # 字段缺失/枚举非法直接被拦下，拿到的要么合法要么 None（走兜底）
        output_pydantic=QuotaPlan,
    )

    crew = Crew(
        agents=[planner],
        tasks=[t_plan],
        # 陌生类 Process：Crew 的执行方式，sequential=按 tasks 顺序串行
        process=Process.sequential,
        verbose=False,
    )

    # 陌生方法 kickoff：真正开跑，返回 CrewOutput；结构化结果在 .pydantic
    result = crew.kickoff()
    if result.pydantic is not None:
        return result.pydantic

    return _fallback_plan(exam_type, hotspots)


def _fallback_plan(exam_type: str, hotspots: list[dict]) -> QuotaPlan:
    """LLM 规划失败时的纯规则配额表。

    得到的数据：exam_type="monthly"；hotspots=[{"kp_id": "kp_a", "wrong_count": 27}, ...]
    要做的操作：按 wrong_count 占比把 20（月考）/25（摸底）题分给各热点，每点至少 1 题
    返回：QuotaPlan，难度全 medium、题型全 choice、chapter_id 全 "unknown"
    为什么：出卷是硬业务，不能因一次 LLM 抖动就断链；故意不完美的兜底表会让审核如实报偏差
    """
    total = 20 if exam_type == "monthly" else 25
    wrong_sum = sum(h["wrong_count"] for h in hotspots) or 1   # or 1 防除零
    items = []
    for h in hotspots:
        # max(1, ...)：小占比点至少 1 题，否则四舍五入成 0 等于没进配额
        count = max(1, round(total * h["wrong_count"] / wrong_sum))
        items.append({
            "kp_id": h["kp_id"],
            "chapter_id": "unknown",     # 不冒充已覆盖：覆盖率统计会 discard 掉
            "question_count": count,
            "difficulty": "medium",      # 全中等 → 审核会如实报难度偏差
            "question_type": "choice",
            "reason": f"错题热点兜底配额（近30天错 {h['wrong_count']} 次）",
        })
    return QuotaPlan(exam_type=exam_type, total_score=100, items=items)


# =====================================================================
# ③ 质量审核员：硬指标纯 Python（可单测）
# =====================================================================
def check_paper_hard_metrics(
    picked: list[dict],
    plan: QuotaPlan,
    hit_kp_ids: set[str],
    total_chapters: int,
) -> tuple[bool, dict, list[str]]:
    """对抽出来的卷子跑四项硬指标，零 LLM。

    得到的数据：picked=[{"id": 1, "kp_id": "kp_a", "difficulty": "easy",
                          "question_type": "fill", "chapter_id": "ch_1"}, ...]；
                plan=本轮 QuotaPlan；hit_kp_ids={"kp_a", "kp_prereq_x"}（热点∪前置链）；total_chapters=5
    要做的操作：空卷检查 → 命中率 → 难度分布 → 题型配比（仅摸底）→ 章节覆盖（仅摸底）→ 单点 ≤2
    返回：(False, {"hit_rate": 0.5, "diff_dist": {"easy": 0.5, "medium": 0.5, "hard": 0.0},
           "per_kp_max": 5, "total_questions": 10},
           ["月考错题命中率 50% < 70%", "难度分布偏出容差：hard 实际 0%，目标 15%（±10%）"])
    为什么：三项指标必须确定可复现、可手算单测；LLM 只负责解读（_reviewer_summary）
    """
    problems: list[str] = []
    total_n = len(picked)
    if total_n == 0:
        return False, {}, ["整卷 0 题：题池不足，请先补题库或放宽难度"]

    # ---- 指标1：月考错题命中率 ----
    hit_n = sum(1 for q in picked if q["kp_id"] in hit_kp_ids)
    hit_rate = hit_n / total_n
    # hit_kp_ids 非空才检查：空集合是"不检查"，不是"命中 0%"
    if hit_kp_ids and plan.exam_type == "monthly" and hit_rate < HIT_RATE_MIN:
        problems.append(f"月考错题命中率 {hit_rate:.0%} < {HIT_RATE_MIN:.0%}")

    # ---- 指标2：难度分布 ----
    target = MONTHLY_DIFF_TARGET if plan.exam_type == "monthly" else MOOD_DIFF_TARGET
    # 先建好三档键，某档没题时才能算出 0.0% 而不是 KeyError
    diff_dist = {d: 0 for d in ("easy", "medium", "hard")}
    for q in picked:
        diff_dist[q["difficulty"]] += 1
    diff_dist = {d: round(c / total_n, 3) for d, c in diff_dist.items()}
    for d, want in target.items():
        if abs(diff_dist[d] - want) > TOLERANCE:
            problems.append(
                f"难度分布偏出容差：{d} 实际 {diff_dist[d]:.0%}，目标 {want:.0%}（±{TOLERANCE:.0%}）"
            )

    # ---- 指标2b：摸底型题型配比 ----
    type_dist = {}
    if plan.exam_type in ("midterm", "final"):
        type_dist = {t: 0 for t in ("choice", "fill", "subjective")}
        for q in picked:
            # .get(..., 0)+1：碰到枚举外题型不崩，记成额外键，分母仍算它 → 比例自然偏低
            type_dist[q.get("question_type", "")] = type_dist.get(q.get("question_type", ""), 0) + 1
        type_dist = {t: round(c / total_n, 3) for t, c in type_dist.items()}
        for t, want in MOOD_TYPE_TARGET.items():
            if abs(type_dist.get(t, 0) - want) > TOLERANCE:
                problems.append(f"题型配比偏出容差：{t} 实际 {type_dist.get(t, 0):.0%}，目标 {want:.0%}")

    # ---- 指标3：章节覆盖率（仅摸底型）----
    chapter_coverage = None            # None = 本次不检查
    if plan.exam_type in ("midterm", "final") and total_chapters > 0:
        covered = {q.get("chapter_id", "unknown") for q in picked}
        covered.discard("unknown")     # 兜底表的 unknown 不算已覆盖
        chapter_coverage = len(covered) / total_chapters
        if chapter_coverage < CHAPTER_COVER_MIN:
            problems.append(f"章节覆盖率 {chapter_coverage:.0%} < {CHAPTER_COVER_MIN:.0%}")

    # ---- 指标4：单知识点 ≤2 题（硬红线）----
    per_kp: dict[str, int] = {}
    for q in picked:
        per_kp[q["kp_id"]] = per_kp.get(q["kp_id"], 0) + 1
    over = {kp: c for kp, c in per_kp.items() if c > 2}
    if over:
        problems.append(f"单知识点超 2 题红线：{over}")

    metrics = {
        "hit_rate": round(hit_rate, 3) if hit_kp_ids else None,
        "diff_dist": diff_dist,
        "type_dist": type_dist or None,
        "chapter_coverage": round(chapter_coverage, 3) if chapter_coverage is not None else None,
        "per_kp_max": max(per_kp.values()) if per_kp else 0,
        "total_questions": total_n,
    }
    return (len(problems) == 0), metrics, problems


def _relax_plan(plan: QuotaPlan) -> QuotaPlan:
    """审核不过后，每个配额项难度降一档，产出新的策略表。

    得到的数据：plan.items=[QuotaItem(kp_id="kp_a", difficulty="hard", ...), ...]

    要做的操作：hard → medium → easy（easy 不动），reason 末尾追加回退标记，重建 QuotaPlan

    返回：新 QuotaPlan，items 里 difficulty 已降档、reason 带 "[审核回退] 难度降档重抽"

    为什么：审核报"过难/分布偏差"多是难题抢不到题（题库少 + abnormal 沉底），降档最有希望兑现
    """
    downgrade = {"hard": "medium", "medium": "easy", "easy": "easy"}
    items = []
    for it in plan.items:
        d = it.model_dump()                                   # Pydantic → dict
        d["difficulty"] = downgrade[d["difficulty"]]
        # 追加而非覆盖：原始入选依据要留着给组长看
        d["reason"] = d["reason"] + "；[审核回退] 难度降档重抽"
        items.append(QuotaItem(**d))
    return QuotaPlan(exam_type=plan.exam_type, total_score=plan.total_score, items=items)


def _reviewer_summary(report: ReviewReport, exam_type: str, grade: str) -> str:
    """把硬指标数值组织成给教研组长看的结论文字。

    得到的数据：report=ReviewReport(passed=False, rounds_used=2, metrics={...},
                problems=["难度分布偏出容差：hard ..."], summary="")；exam_type="monthly"；grade="初三"
    要做的操作：硬指标 JSON 丢给 LLM 写 150 字内结论；任何异常退回模板拼装
    返回：一段中文结论，如 "本次月考未通过：难题占比 0%，低于 15% 目标，建议补 1~2 道难题后重抽。"
    为什么：数值已由 Python 算好，这一步只买"文笔"——失败也只丢文笔，不丢信息
    """
    fallback = (
        f"{'通过' if report.passed else '未通过'}审核（{exam_type}，{grade}，"
        f"第 {report.rounds_used} 轮）。"
        + ("；".join(report.problems) if report.problems else "全部硬指标达标。")
    )
    try:
        from app.core.llm import get_llm

        prompt = (
            "你是试卷质量审核员。把下面的硬指标 JSON 组织成一段给教研组长看的"
            "结论文字（150 字内，直说结论与问题，不要客套）：\n"
            + json.dumps({"exam_type": exam_type, "grade": grade,
                          "passed": report.passed, "metrics": report.metrics,
                          "problems": report.problems}, ensure_ascii=False)
        )
        return get_llm(temperature=0.2).invoke(prompt).content.strip()
    except Exception:
        return fallback


# =====================================================================
# 对外主入口：规划 → 抽题 → 审核 ≤2 轮 → 落盘持久化
# =====================================================================
def run_exam_generation(
    exam_type: str,
    grade: str,
    subject: str = "math",
    total_score: int = 100,
    created_by: int | None = None,
) -> dict:
    
    """R-08 主流程：一张卷子从配额表（策略表）到产出试卷，再到落库（draft）。

    得到的数据：exam_type="monthly"；grade="初三"；subject="math"；total_score=100；created_by=3（可空）

    要做的操作：查热点∪前置 → Planner 出配额表 → 循环 ≤2 轮抽题+硬校验（不过降难度）→ 出结论 → 落库

    返回：{"paper_id": 3, "plan": {...}, "question_ids": [7, 12, 20, ...],
          "gaps": ["kp_peifang: 配额 4 题超单知识点 2 题上限，按 2 题执行", ...],
          "review": {"passed": false, "rounds_used": 2, "metrics": {...},
                     "problems": [...], "summary": "..."}}

    为什么：R-08 的唯一入口，把"LLM 出策略 / Python 出结果 / 审核可回溯"串成一次调用
    """
    from app.db.session import get_session_factory
    from app.models import Chapter, ExamPaper
    from app.tools.graph_tools import query_error_hotspots

    # t1 热点 + 前置知识链集合：
    hotspots = json.loads(query_error_hotspots.invoke({"grade": grade, "subject": subject, "top_n": 10}))  # .invoke({...}) 是 @tool 函数调用方式，返回 JSON 字符串
    hit_kp_ids = _expand_with_prerequisites({h["kp_id"] for h in hotspots})

    # t2 命题规划LLM，出策略表（配额表）Planner； exam_type、total_score 再次赋传参值，防模型抄错
    plan = _plan_quota(exam_type, grade, subject, hotspots)
    plan.exam_type = exam_type
    plan.total_score = total_score

    # t3 抽题 + 四项硬指标校验硬校验，2轮校验不通过，返回缺口清单
    all_kp_ids = list({it.kp_id for it in plan.items})
    picked: list[dict] = []
    gaps: list[str] = []
    passed, metrics, problems = False, {}, ["尚未抽题"]
    rounds_used = 0

    # 抽2轮题目
    for round_no in range(1, MAX_ROUNDS + 1):
        rounds_used = round_no
        # 捞题库
        candidates = fetch_candidates_by_kp(all_kp_ids)

        # 根据出题策略表/配额表，抽题
        picked, gaps = select_questions_by_quota(
            quota_items=[it.model_dump() for it in plan.items],
            candidates_by_kp=candidates,
        )

        # 题目的章节覆盖率
        total_chapters = _count_chapters(grade, subject)

        # 抽出来的题，进行四项硬指标校验
        passed, metrics, problems = check_paper_hard_metrics(picked, plan, hit_kp_ids, total_chapters)

        if passed:
            break

        if round_no < MAX_ROUNDS:
            plan = _relax_plan(plan)               # 降难度下一轮重抽

        # 第 2 轮仍不过：不再重试，缺口清单原样交组长（宁可少题不错题）

    report = ReviewReport(
        passed=passed, rounds_used=rounds_used,
        metrics=metrics, problems=problems, summary="",
    )

    # LLM 把校验数字结论，组织成人话（挂了用模板）
    report.summary = _reviewer_summary(report, exam_type, grade)


    # t5 落库（draft）：quota_plan_json 一次存 plan + 选题 + 缺口
    session = get_session_factory()()
    try:
        paper = ExamPaper(
            title=f"{grade}{subject} {'月考' if exam_type == 'monthly' else '期中' if exam_type == 'midterm' else '期末'}卷",
            grade=grade,
            exam_type=exam_type,
            quota_plan_json={
                "plan": plan.model_dump(),
                "question_ids": [q["id"] for q in picked],
                "gaps": gaps,
            },
            status="draft",
            created_by=created_by,
            remark=report.model_dump_json(),       # 审核报告全文留痕
        )
        session.add(paper)
        session.flush()                            # flush 后才有自增 id（commit 前 DB 已发 INSERT）
        paper_id = paper.id
        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()

    return {
        "paper_id": paper_id,
        "plan": plan.model_dump(),
        "question_ids": [q["id"] for q in picked],
        "gaps": gaps,
        "review": report.model_dump(),
    }


# =====================================================================
# 小工具：Neo4j 前置链外扩 / 章节计数
# =====================================================================
def _expand_with_prerequisites(kp_ids: set[str], depth: int = 2) -> set[str]:
    """把错题热点扩成"热点 ∪ 前置知识点链"。

    得到的数据：kp_ids={"kp_crossmult", "kp_peifang"}； depth=2

    要做的操作：Neo4j 沿 HAS_PREREQUISITE 反向边查 1~2 跳，得到前置知识点链，并入原集合，

    返回：集合{"kp_crossmult", "kp_peifang", "kp_poly_mult", "kp_power_ops"}

    为什么：月考要求 ≥70% 命中"错题 Top10 及其前置"，所以需要热点错题，及其前置知识链
    """
    try:
        from app.graph_db.neo4j_client import run_query

        # 反向箭头 <-[:...*1..depth]-：seed 约定边从"前置"指向"依赖它的点"，
        # 查前置必须逆着走；写成正方向拿到的是"后继点"，语义就反了
        rows = run_query(
            """
            MATCH (e:KnowledgePoint)<-[:HAS_PREREQUISITE*1..$depth]-(p:KnowledgePoint)
            WHERE e.id IN $ids
            RETURN DISTINCT p.id AS kp_id
            """,
            {"ids": sorted(kp_ids), "depth": depth},
        )

        # 两个集合取并集
        return kp_ids | {r["kp_id"] for r in rows} 
    
    except Exception:
        # Neo4j 挂了不炸主流程：命中率少算前置，审核会如实反映
        return set(kp_ids)


def _count_chapters(grade: str, subject: str) -> int:
    """数章节总数，做章节覆盖率的分母。

    得到的数据：grade="初三"；subject="math"
    要做的操作：SELECT count(*) FROM chapters WHERE grade=? AND subject=?
    返回：5（没有匹配行时为 0）
    为什么：摸底型要求章节覆盖率 ≥80%，没有分母这项指标就算不出来
    """
    from sqlalchemy import func, select

    from app.db.session import get_session_factory
    from app.models import Chapter

    session = get_session_factory()()
    try:
        return int(session.execute(
            select(func.count())
            .select_from(Chapter)
            .where(Chapter.grade == grade)
            .where(Chapter.subject == subject)
        ).scalar() or 0)          # scalar() 可能返回 None，or 0 兜底
    finally:
        session.close()
