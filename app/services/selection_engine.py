# app/services/selection_engine.py
# 组卷引擎（架构文档 5.3）：M2 给 S-10 用，M4 扩展给 T-01/R-08 复用。
#
# 【本文件的最高原则——先记住这一句，后面全是它的展开】
#   选题是确定性计算，绝不交给 LLM。
#     - LLM 只决定"选哪些知识点、各占多少比重"（配额表 QuotaPlan，在 exam_crew 里出）
#     - 真正抽题由本文件的 Python 规则执行：同一份输入 → 必然同一套题
#   为什么较真到这个程度：老师会把卷子打印出来对着看。今天抽 A 卷、明天重跑抽出 B 卷，
#   他会立刻判定"这系统不可信"。LLM 每次结果不同，所以它一步都不许踏进抽题。
#
# 【函数地图（谁在用谁）】
#   M2 已有，接口已冻结（S-10 的 quiz_graph 与 tests/test_quiz_generation.py 依赖它们）：
#     difficulty_for            掌握度 → 难度档（反相关：越弱越给基础题）
#     select_quiz_questions     S-10 专用：Top5 知识点逐点配题，总量 8、单点 ≤2
#     fetch_candidates_by_kp    从 PG 捞候选池（唯一碰 DB 的函数）
#   M4 新增（R-08 需要，开发计划 M4-T2）：
#     select_questions_by_quota 配额抽题：配额执行 / abnormal 沉底 / 近 7 天去重 /
#                               单点 ≤2 跨配额累计截断 / 难度不足放宽邻档 / 缺口不乱补
#
# 【关于"abnormal 降权 50%"的实现说明（PRD 5.3 的原文，这里落成确定性规则）】
#   PRD 说异常题"降权 50%"。抽题是确定性排序，"50% 概率被选中"这种随机语义没法复现，
#   所以落成等价规则：同难度档内 abnormal 一律排到所有 normal 之后（沉底）。
#   效果：池子够 → normal 先走光，abnormal 一道进不来（等于权重砍到 0 附近）；
#        池子不够 → abnormal 顶上兜底（等于仍保留权重）。
#   好处：完全可复现、可单测（tests/test_selection_engine.py::test_abnormal_sinks_below_normal）。
from sqlalchemy import select


# =====================================================================
# 一、M2 已有：S-10 专用（原样保留，接口一个都不许动）
# =====================================================================
def difficulty_for(p_known: float) -> str:
    """掌握度 → 难度档（越薄弱越给基础题）。

    得到的数据：p_known=0.33（BKT 掌握概率，0~1；传 33 会一律给 hard）
    要做的操作：按 0.40 / 0.70 两刀切三段（左闭右开）
    返回："easy"（<0.40）/ "medium"（0.40~0.70）/ "hard"（≥0.70）
    为什么：S-10 补救测验要"掌握度越低题越基础"，这条映射是反相关的落点
    """
    if p_known < 0.40:
        # 0.33 走这里 → easy：薄弱生先做基础题，做出信心比做难题重要
        return "easy"
    if p_known < 0.70:
        # 0.52 走这里 → medium：会一点但不稳，中档题查漏
        return "medium"
    # 0.85 走这里 → hard：已掌握，用少量难题验证迁移能力（不再重复刷同类题）
    return "hard"


def select_quiz_questions(
    target_points: list[dict],
    candidates_by_kp: dict[str, list[dict]],
    total: int = 8,
    per_kp_cap: int = 2,
    exclude_ids: set[int] | None = None,
) -> tuple[list[dict], list[str]]:
    """S-10 诊断测验配题：按 Top5 知识点逐点抽题。

    得到的数据：target_points=[{"kp_id": "kp_crossmult", "p_known": 0.33, "reason": "近30天连错3次"}, ...]
                （已按优先级排序）；candidates_by_kp={"kp_crossmult": [{"id": 12,
                "difficulty": "easy"}, ...], "kp_peifang": []}；total=8；per_kp_cap=2；
                exclude_ids={31, 47}
    要做的操作：逐点定难度档→过滤近期做过的→目标档优先、他档兜底→单点 ≤2 截断→总量封顶
    返回：([{"id": 12, "kp_id": "kp_crossmult", "difficulty": "easy"}, ...],
           ["kp_peifang: 题库无可用题（目标档 medium，池中 0 题）"])
    为什么：S-10 的核心配题规则，抽题必须可复现，所以做成纯函数便于单测
    """
    exclude_ids = exclude_ids or set()   # None → 空集合：下面统一用 `in set`，不用再判空
    picked: list[dict] = []             # 已选中的题（最终 8 道）
    gaps: list[str] = []                # 缺口说明（诚实记账，不拿来凑数）

    for tp in target_points:
        # 总配额用完就停：不因为"还有知识点没照顾"而超发（8 题就是 8 题）
        if len(picked) >= total:
            break                            # 后面的知识点本轮不再消费
        kp_id = tp["kp_id"]
        # 掌握度 → 难度档：0.33→easy、0.52→medium、0.85→hard（反相关，见 difficulty_for）
        want = difficulty_for(tp["p_known"])

        # 先把近期做过的题剔掉：exclude 发生在"取数之后、排序之前"，
        #   若放到最后再剔，会出现"明明池里够题却只选出 1 道"的假缺口。
        pool = [c for c in candidates_by_kp.get(kp_id, []) if c["id"] not in exclude_ids]

        # 【两段拼接实现"档内优先、他档兜底"】
        #   第一段：难度正好匹配 want 的（如 easy 目标 → easy 题）
        #   第二段：其余难度（medium + hard），保持原顺序
        #   拼起来后从头取 = 先吃目标档，目标档不够再吃别的档。
        #   ⚠ 不要写成 sorted(key=难度==want)：布尔排序在 stable sort 下等价，但读起来费劲，
        #     而且将来若要"邻档优先于远档"，两段拼接更容易改成三段。
        in_order = [c for c in pool if c["difficulty"] == want] + [c for c in pool if c["difficulty"] != want]

        # 取几道：单点上限 与 总量剩余额度 取小。
        #   例：第 4 个点时 picked=6，total=8 → min(2, 2)=2；第 5 个点时 picked=8 → 循环已 break。
        take = in_order[: min(per_kp_cap, total - len(picked))]

        if not take:
            # 池子空（或全被 exclude 掉）：记缺口，继续下一个点。
            #   绝不用"别的知识点的题"或"随机题"来凑——那是把诊断测验变成盲盒。
            gaps.append(f"{kp_id}: 题库无可用题（目标档 {want}，池中 {len(pool)} 题）")

        picked.extend(take)             # list.extend：把 take 的元素逐个并入 picked

    return picked, gaps


def fetch_candidates_by_kp(kp_ids: list[str]) -> dict[str, list[dict]]:
    """从 PG 题库按知识点批量取候选池（本文件唯一碰数据库的函数）。

    得到的数据：kp_ids=["kp_crossmult", "kp_peifang"]（传 [] 会生成非法 IN ()）
    要做的操作：一条 IN 查询捞回所有点的题，剔除 disabled，按 kp_id 分组
    返回：{"kp_crossmult": [{"id": 12, "kp_id": "kp_crossmult", "difficulty": "easy",
                           "question_type": "fill", "quality_flag": "normal"}, ...],
          "kp_peifang": []}
    为什么：取数与策略分离——这里只备料（不排序、不滤难度），定档和排序都归上层引擎
    """
    from app.db.session import get_session_factory
    from app.models import Question

    session = get_session_factory()()
    try:
        rows = session.execute(
            select(
                Question.id, Question.kp_id, Question.difficulty,
                # M4 起多取两列：question_type（R-08 题型配比要用它过滤）
                #                quality_flag（abnormal 降权要用它排序）
                # 对 M2 的调用方无影响——字典多两个键，select_quiz_questions 只读 id/difficulty。
                Question.question_type, Question.quality_flag,
            )
            .where(Question.kp_id.in_(kp_ids))                    # 一次查完，避免 N+1 次查询
            .where(Question.quality_flag != "disabled")           # 判死刑的题永不出池
        ).all()

    finally:
        session.close()

    # 先给每个 kp 建空列表：保证"没题的 kp"也存在于结果里。
    #   若省掉这步，上层 candidates_by_kp.get(kp_id, []) 也能跑，但返回结构就不稳定
    #   （有时有键有时没键），断言和调试都变难。
    grouped: dict[str, list[dict]] = {kp: [] for kp in kp_ids}
    for qid, kp, diff, qtype, flag in rows:
        grouped[kp].append({
            "id": qid, "kp_id": kp, "difficulty": diff,
            "question_type": qtype, "quality_flag": flag,
        })
    return grouped


# =====================================================================
# 二、M4 新增：R-08 配额抽题（纯函数，不碰 DB / 不调 LLM，可单测）
# =====================================================================
# 难度档"相邻放宽"顺序：目标档没题了，先借相邻档，再借远档。
#   easy ↔ medium ↔ hard 是一条链，所以"沿链就近"：
#     easy 的邻居是 medium（hard 隔着两格，最后借）
#     medium 的邻居是 easy 和 hard——这里选择"先 easy 后 hard"：
#       考试卷子缺中档题时，用基础题补比用难题补更安全（难题补进来会把难度分布顶飞）
#     hard 的邻居是 medium，最后才是 easy
#   ⚠ 填错后果：若把这张表写反（easy 优先借 hard），一张本该"补基础"的卷子
#     会被塞进一堆难题，审核环节必然报"难度分布偏出容差"，白跑两轮。
_RELAX_ORDER = {
    "easy":   ["easy", "medium", "hard"],    # 目标 easy → 先 medium 后 hard
    "medium": ["medium", "easy", "hard"],    # 目标 medium → 先 easy（补基础更安全）后 hard
    "hard":   ["hard", "medium", "easy"],    # 目标 hard → 先 medium 后 easy
}


def _pool_sort_key(q: dict) -> tuple[int, int]:
    """池内排序键：normal 在前、abnormal 沉底，同档按 id 升序。

    得到的数据：{"id": 31, "difficulty": "easy", "quality_flag": "abnormal"}
    要做的操作：产出 (权重, id) 二元组给 sorted 用
    返回：(1, 31)；normal 的则是 (0, 12)
    为什么：这就是"abnormal 降权 50%"的确定性实现；id 兜底保证同输入同输出
    """
    return (0 if q.get("quality_flag", "normal") == "normal" else 1, q["id"])


def select_questions_by_quota(
    quota_items: list[dict],
    candidates_by_kp: dict[str, list[dict]],
    exclude_ids: set[int] | None = None,
    per_kp_cap: int = 2,
) -> tuple[list[dict], list[str]]:
    """按配额行逐条抽题（R-08 的选题核心）。

    得到的数据：quota_items=[{"kp_id": "kp_crossmult", "difficulty": "medium",
                              "question_type": "fill", "question_count": 4}, ...]；
                candidates_by_kp（fetch_candidates_by_kp 输出）；exclude_ids={31, 47}；per_kp_cap=2
    要做的操作：单点跨行累计截断 → 过滤近期/已选 → 沿放宽链找题（题型硬约束、abnormal 沉底）
    返回：([{"id": 12, "kp_id": "kp_crossmult", "difficulty": "medium", "quality_flag": "normal"}, ...],
           ["kp_peifang: 配额 4 题超单知识点 2 题上限，按 2 题执行",
            "kp_peifang: 1 题由 medium 档放宽补入", "kp_x: medium/fill 缺 2 题（…）"])
    为什么：R-08 要求"配额可打印、抽题可复现、缺口不硬凑"，这三条都落在这个纯函数里
    """
    exclude_ids = exclude_ids or set()
    picked: list[dict] = []
    picked_ids: set[int] = set()        # 全局去重：同一道题不许在卷里出现两次
    kp_used: dict[str, int] = {}        # 每个 kp 已用掉多少配额（跨配额项累计，红线靠它兜住）
    gaps: list[str] = []

    for item in quota_items:
        kp_id = item["kp_id"]
        want_type = item.get("question_type", "")   # 缺省空串 → 下面 `not want_type` 表示不限
        need = int(item["question_count"])

        # ---- 红线 1：单知识点 ≤ per_kp_cap，且是"跨配额项累计"口径 ----
        #   为什么要累计：同一 kp 可能出现两行配额（如"填空 2 题 + 选择 2 题"），
        #   若只看单行，这个点最终会出 4 题，直接违反 PRD 验收第 4 条。
        remain_cap = per_kp_cap - kp_used.get(kp_id, 0)
        if remain_cap <= 0:
            gaps.append(f"{kp_id}: 超过单知识点 {per_kp_cap} 题上限，配额行被丢弃")
            continue                                  # 整行跳过，接着处理下一行配额
        if need > remain_cap:
            # 透明化：配额行要 4 题、红线只剩 2 题 → 照实记录，别让组长对着结果发懵
            gaps.append(f"{kp_id}: 配额 {need} 题超单知识点 {per_kp_cap} 题上限，按 {remain_cap} 题执行")
            need = remain_cap

        # ---- 过滤：近 7 天做过 + 本轮已选过 ----
        #   两类排除合在一条推导式里，比分两次 filter 少一次遍历，也不容易漏。
        pool = [c for c in candidates_by_kp.get(kp_id, [])
                if c["id"] not in exclude_ids and c["id"] not in picked_ids]

        taken_from_pool: list[dict] = []   # 本行配额最终拿到的题
        lack = need                        # 还差几道（逐档递减，减到 0 收工）
        reasons = []                       # 本行的"放宽记录"（最后统一进 gaps）
        for diff in _RELAX_ORDER[item["difficulty"]]:
            if lack <= 0:
                break                          # 配额已满足，不用再借档
            # 档内排序：normal 优先（abnormal 沉底）、id 升序（可复现）
            tier = sorted((c for c in pool if c["difficulty"] == diff), key=_pool_sort_key)
            # 题型硬约束：只取目标题型。
            #   为什么不像难度那样放宽：R-08 摸底型的 4:3:3 题型配比是审核硬指标，
            #   拿填空题凑选择题的配额，最后一定卡在审核环节（白跑一轮）。
            tier = [c for c in tier if not want_type or c.get("question_type") == want_type]
            take = tier[:lack]
            taken_from_pool.extend(take)
            lack -= len(take)
            if diff != item["difficulty"] and take:
                # 只要"借了别的档"就留痕：审核报告里能看到"这张卷有 1 题是放宽补进来的"
                reasons.append(f"{len(take)} 题由 {diff} 档放宽补入")

        if lack > 0:
            # 缺口：放宽后仍不够 → 照实记账。绝不跨知识点借题、不随机补题。
            gaps.append(
                f"{kp_id}: {item['difficulty']}/{want_type} 缺 {lack} 题"
                f"（放宽难度后仍不足，池中仅 {len(pool)} 题）"
            )

        for q in taken_from_pool:
            picked.append(q)
            picked_ids.add(q["id"])            # 立刻登记，防后续配额行又选中同一道题
        kp_used[kp_id] = kp_used.get(kp_id, 0) + len(taken_from_pool)

        if reasons:
            gaps.append(f"{kp_id}: " + "；".join(reasons))   # 放宽记录进缺口清单，透明可查

    return picked, gaps
