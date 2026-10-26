"""M4-T4 · R-08 试卷生成：命题规划 → 选题执行 → 质量审核。

 三角色分工：
   ① 命题规划师  唯一调 LLM 的环节，产 QuotaPlan 配额表（output_pydantic 卡格式）
   ② 选题执行器  纯 Python 调 selection_engine，零 LLM（抽题必须可复现）
   ③ 质量审核员  硬指标纯 Python 算，不过关降难度重抽，最多 2 轮
 两种模式的硬指标：月考（错题命中率 ≥70%）/ 摸底（章节覆盖 ≥80%），见 PRD 5.5
"""
import json
from datetime import datetime

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

# 学科/类型的中文名：标题是给人看的，直接把 "math"/"monthly" 拼进去会显示成
# "初三math 月考卷"这种半截英文，组长在列表里还要反应一下
SUBJECT_CN = {"math": "数学", "chinese": "语文", "english": "英语"}
EXAM_TYPE_CN = {"monthly": "月考", "midterm": "期中", "final": "期末"}


def _paper_title(grade: str, subject: str, exam_type: str) -> str:
    """生成卷名，必须带上出卷日期。

    得到的数据：grade="初三"、subject="math"、exam_type="monthly"

    要做的操作：中文名拼装 + 末尾追加 %m-%d

    返回："初三数学月考卷 · 10-26"

    为什么带日期：
        原来标题是固定模板，同一届出两次月考就是两份"初三数学月考卷"。
        教研端列表、学生端考试卡片都靠标题区分卷子 —— 名字一撞，
        18 题的卷和 17 题的卷看着像同一份，会被误判成"推送给 1 班却让 2 班看到了"。
        （真事：卷 5/卷 6 同名，核对了一圈审计记录才发现是两份卷。）

        日期只到天，是因为标题太长会挤掉列表里的题数与满分；
        真要精确到某一份，看列表里的 #paper_id 即可。
    """
    stamp = datetime.now().strftime("%m-%d")
    return f"{grade}{SUBJECT_CN.get(subject, subject)}{EXAM_TYPE_CN.get(exam_type, exam_type)}卷 · {stamp}"


# =====================================================================
# ① 命题规划师：唯一调 LLM 的环节
# =====================================================================
def _catalog_text(catalog: list[dict]) -> str:
    """把课程目录 拼成 给命题规划师看的知识点/章节白名单。

    得到的数据：catalog（fetch_chapter_catalog 输出）

    要做的操作：逐章一行标题 + 其下知识点 id/名，缩进两格

    返回：
        "【章节白名单（chapter_id 只能从这里照抄）】
         - ch_quadratic = 第21章 一元二次方程
             下辖知识点：kp_crossmult=十字相乘法 / kp_peifang=配方法
         - ch_rotation = 第23章 旋转
             （本章暂无知识点记录）"

    为什么：
        LLM 只被告知"第21章 一元二次方程"这个中文名时，会自己造一个 ch_21_quad_eq 当 id，
        而库里叫 ch_quadratic → 配额表章节列显示成谁都不认识的编码，
        章节覆盖率统计也因为集合对不上而失真。给白名单才是治本。
    """
    lines = ["【章节白名单（chapter_id 只能从这里照抄，禁止自造）】"]
    for ch in catalog:
        lines.append(f"- {ch['chapter_id']} = {ch['chapter_name']}")
        if ch["kp"]:
            kp_text = " / ".join(f"{k['kp_id']}={k['name']}" for k in ch["kp"])
            lines.append(f"    下辖知识点：{kp_text}")
        else:
            lines.append("    （本章暂无知识点记录）")
    return "\n".join(lines)


def _plan_quota(
    exam_type: str,
    grade: str,
    subject: str,
    hotspots: list[dict],
    mastery_summary: str = "",
    catalog_text: str = "",
) -> QuotaPlan:
    """跑单 Agent crew，产出配额表（出题策略表）。

    得到的数据：exam_type="monthly"；grade="初三"；subject="math"；
                hotspots=[{"kp_id": "kp_crossmult", "name": "十字相乘法", "wrong_count": 27}, ...]；
                mastery_summary="kp_crossmult 0.31 / kp_peifang 0.52"（摸底型用，可空）；
                catalog_text （章节/知识点白名单文本，可空）

    要做的操作：按考试类型拼规则文本，和热点 JSON、白名单一起写进 Task description，kickoff 出表

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
            + (f"{catalog_text}\n\n" if catalog_text else "")
            + "请产出 QuotaPlan：每个知识点一行（kp_id/chapter_id/question_count/"
              "difficulty/question_type/reason），question_count 总和即整卷题量"
              "（月考默认 20 题左右，摸底型默认 25 题左右）。reason 写清入选依据。\n"
              "★ 硬约束：kp_id 与 chapter_id 必须原样照抄上面白名单里的 id，"
              "不许自己拼（如把「第21章 一元二次方程」写成 ch_21_quad_eq）；"
              "一个知识点只属于它所在的那一章，chapter_id 取该知识点所在行的章节 id。"
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
        covered = {q.get("chapter_id") for q in picked}
        # unknown / None 不算已覆盖：兜底表给 "unknown"，还没回填章节的题给 None
        covered.discard("unknown")
        covered.discard(None)
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
        # ★ 哪些指标"本次不检查"要明说：光给 null，组长的第一反应是"数据丢了"，
        #   而不是"月考本来就不看章节覆盖"。前端据此显示「本次不检查」。
        "not_checked": (
            ["type_dist", "chapter_coverage"] if plan.exam_type == "monthly"
            else ([] if hit_kp_ids else ["hit_rate"])
        ),
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


def _fix_plan_ids(plan: QuotaPlan) -> tuple[QuotaPlan, list[str], dict[str, str]]:
    """拿课程库纠正配额表里的 id（LLM 会自己拼不存在的 id，必须在校验前修掉）。

    得到的数据：plan.items 里可能含 kp_sqrt（库里没有）、chapter_id="ch_21_quad_eq"（库里叫 ch_quadratic）

    要做的操作：
        1. 用 kp_id 反查 knowledge_points：查不到的整行丢弃（抽题时这一行必然全变缺口）
        2. 查得到的，chapter_id 一律按知识点归属重写——章节归属是数据事实，不该问 LLM

    返回：
        (纠正后的 QuotaPlan, 说明清单, {kp_id: 知识点名})
        说明清单形如 ["kp_sqrt: 课程库中不存在该知识点，配额行已丢弃（规划师给出了不存在的 id）"]

    为什么：
        不修的话有两个后遗症：配额表「章节」列显示 ch_21_quad_eq 这种谁都不认识的编码；
        摸底型的章节覆盖率把 covered 集合跟真实章节对不上，指标直接失真。
        只在展示层翻译是治标——库里压根没有这个 id，翻译也翻不出来。
    """
    from app.services.naming import fetch_kp_chapter_map, fetch_kp_names

    kp_ids = [it.kp_id for it in plan.items]
    kp_chapter = fetch_kp_chapter_map(kp_ids)
    kp_names = fetch_kp_names(kp_ids)

    notes: list[str] = []
    kept: list[QuotaItem] = []
    dropped: set[str] = set()
    fixed_chapters = 0

    for it in plan.items:
        # 知识点不存在 → 整行丢弃：留着只会在缺口清单里刷一条"池中仅 0 题"，误导组长
        if it.kp_id not in kp_chapter:
            dropped.add(it.kp_id)
            continue

        d = it.model_dump()
        real_chapter = kp_chapter[it.kp_id] or "unknown"
        if d["chapter_id"] != real_chapter:
            d["chapter_id"] = real_chapter
            fixed_chapters += 1

        kept.append(QuotaItem(**d))

    for kp_id in sorted(dropped):
        notes.append(f"{kp_id}: 课程库中不存在该知识点，配额行已丢弃（规划师给出了不存在的 id）")

    if fixed_chapters:
        notes.append(f"{fixed_chapters} 行章节归属已按课程库纠正（规划师自造的 chapter_id 已替换）")

    return (
        QuotaPlan(exam_type=plan.exam_type, total_score=plan.total_score, items=kept),
        notes,
        kp_names,
    )


def _reviewer_summary(report: ReviewReport, exam_type: str, grade: str) -> str:
    """把硬指标数值组织成 给教研组长看的结论文字。

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
            # ★ 三条口径约束：不写死的话模型会把 null 读成"数据缺失，需补齐后复核"，
            #   组长看了以为要往题库补题——实际那只是"月考不查这一项"
            + "\n\n写作口径（必须遵守）：\n"
              "1. metrics 里值为 null 的指标 = 本次考试类型不检查这一项（如月考不查题型配比/章节覆盖），"
              "一句话带过或直接不提，禁止写成「数据缺失」「需补齐数据后复核」。\n"
              "2. problems 为空即全部达标，不要自己引申风险。\n"
              "3. 需要下一步动作时，只允许推荐这两种："
              "① 调整配额表后重新生成（重跑会重新规划并重抽）；② 题库该难度/题型不足时先补题库再生成。"
              "不要编造系统里没有的功能（如「人工复核确认按钮」）。"
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
    from app.services.naming import fetch_chapter_catalog
    from app.tools.graph_tools import query_error_hotspots

    # t1 热点 + 前置知识链集合：
    hotspots = json.loads(query_error_hotspots.invoke({"grade": grade, "subject": subject, "top_n": 10}))  # .invoke({...}) 是 @tool 函数调用方式，返回 JSON 字符串
    hit_kp_ids = _expand_with_prerequisites({h["kp_id"] for h in hotspots})


    # t2 课程目录白名单（章节 id + 下辖知识点 id）：
    #    不给这份清单，规划师会自己拼 ch_21_quad_eq 这种库里不存在的 id，配额表章节列和覆盖率统计全跟着失真
    catalog = fetch_chapter_catalog(subject, grade)


    # t3 命题规划LLM，出策略表（配额表）Planner； exam_type、total_score 再次赋传参值，防模型抄错
    plan = _plan_quota(exam_type, grade, subject, hotspots, catalog_text=_catalog_text(catalog))
    plan.exam_type = exam_type
    plan.total_score = total_score


    # t4 用课程库纠正 id：不存在的知识点整行丢弃，chapter_id 一律按知识点归属重写
    plan, id_notes, kp_names = _fix_plan_ids(plan)
    if not plan.items:
        # 全被丢弃（规划师几乎全在编 id）→ 用纯规则表兜底，并再纠正一次
        plan = _fallback_plan(exam_type, hotspots)
        plan.exam_type = exam_type
        plan.total_score = total_score
        plan, more_notes, kp_names = _fix_plan_ids(plan)
        id_notes += ["规划表的 id 全部无效，已退回纯规则配额表"] + more_notes


    # t5 抽题 + 四项硬指标校验硬校验，2轮校验不通过，返回缺口清单
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
        #   kp_label：缺口清单直接写"配方法"而不是"kp_peifang"——那是给组长看的
        round_picked, round_gaps = select_questions_by_quota(
            quota_items=[it.model_dump() for it in plan.items],
            candidates_by_kp=candidates,
            kp_label=kp_names,
        )

        picked = round_picked
        # 每一轮都把 id 纠正说明带上：第 2 轮重抽也一样适用
        gaps = id_notes + round_gaps

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
            title=_paper_title(grade, subject, exam_type),
            grade=grade,
            # subject 必须落：定稿推送要按"这个班这个学科的科任老师"过滤，
            #   只存 grade 的话数学老师也会收到英语卷
            subject=subject,
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
