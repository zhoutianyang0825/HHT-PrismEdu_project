# app/services/analysis.py
# M4-T5 · R-04 的统计计算层（纯 Pandas，一句 LLM 调用都没有）。
#
# 【工程原则：指标用 Python 算，LLM 只负责解读】
#   这是整个项目反复出现的设计哲学（和组卷引擎同源）：
#     - 该确定的部分（P 是多少、算不算异常）锁死在代码里，可复现、可手算校验
#     - 该表达的部分（为什么会这样、该怎么补）才交给 LLM
#   为什么较真：LLM 算平均分是"十次里对九次"，但漏的那一次会让组长当场发现
#   "这数字不对"，然后整套系统的可信度就没了。数字从不经过模型的手，这个风险就归零。
#
# 【指标口径（PRD 5.6）】
#   得分率 R = 该题平均得分 / 该题满分
#   难度系数 P = 平均得分 / 满分           异常：P < 0.3 过难，P > 0.9 过易
#   区分度 D = (前27%高分组均分 - 后27%低分组均分) / 该题满分      异常：D < 0.2
#
#   一句话理解 D：这道题能不能把"学得好的"和"学得差的"分开。
#     D 高（如 0.6）：高分组几乎全对、低分组几乎全错 → 筛人很灵
#     D 低（如 0.05）：两组分数差不多 → 这题谁也不筛，考了等于没考
#     D 为负：低分组反而考得比高分组好 → 极可能是题目本身有问题（歧义/超纲/答案错）
#
# 【为什么用 27% 分组而不是高低各一半】
#   教育测量学（Kelley 的经典结论）里，取前/后 27% 时"分组差异的稳定性"最好：
#   取 50% 会把中间那批模糊的学生也拉进来，区分度被稀释；取 10% 样本又太少、噪声大。
#   27% 是这套方法流传近百年的经验值，写进常量，不在代码里硬编码魔数。
import json

# 异常阈值写成模块级常量：写回逻辑、单测、crew 提示词三处共用，改一处全局生效。
#   ⚠ 这三个数别凭感觉动：PRD 明确写了 0.3/0.9/0.2，动了等于改验收标准。
P_TOO_HARD = 0.3     # P < 0.3 → 过难
P_TOO_EASY = 0.9     # P > 0.9 → 过易
D_TOO_LOW = 0.2      # D < 0.2 → 区分度不足

HIGH_LOW_GROUP_RATIO = 0.27   # 经典前/后 27% 分组法（教育测量学惯例，见文件头）


def compute_item_metrics(df) -> list[dict]:
    """算每题的难度系数 P 与区分度 D，并标注异常题。

    得到的数据：df=[{"student_id": 101, "q_id": 7, "score": 3.0, "full_score": 4.0}, ...]
                （长表：一行 = 一个学生一道题；list 或 DataFrame 都收）
    要做的操作：算卷面总分→取前后 27% 高低分组→逐题算 P/D→按阈值打标签
    返回：[{"q_id": 7, "P": 0.245, "D": 0.05, "score_rate": 0.245,
           "is_abnormal": True, "issues": ["过难", "区分度不足"]}, ...]（按 q_id 升序）
    为什么：R-04 的指标必须确定可复现、能手算校验；LLM 只负责解读，不碰数字
    """
    import pandas as pd

    # 【入参双兼容】API 层从 tool 拿到的是 records list；单测/demo 也常直接给 list。
    #   若不做这层，调用方必须记得 pd.DataFrame(records) 一下，忘了就是
    #   AttributeError: 'list' object has no attribute 'groupby'（本机冒烟实测踩过）。
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)

    results = []

    # ---- t2：卷面总分（降序）----
    #   groupby("student_id")["score"].sum() → Series，索引是学生 id，值是总分
    #   排序降序：head(n) 拿高分组、tail(n) 拿低分组，所以必须从高到低。
    #   示例：{101: 78.0, 102: 61.0, 103: 55.0, ...}
    totals = df.groupby("student_id")["score"].sum().sort_values(ascending=False)

    # ---- t3：前/后 27% 分组 ----
    #   len=40 → int(40×0.27)=10 人一组
    #   len=4  → int(1.08)=1 人一组
    #   len=3  → int(0.81)=0 人一组 → 空分组算均分是 NaN，D 变 NaN，后面全废。
    #   所以 max(1, ...)：小样本至少取 1 人（3 人卷子就是"最高分 vs 最低分"）。
    n = max(1, int(len(totals) * HIGH_LOW_GROUP_RATIO))
    high_ids = set(totals.head(n).index)     # 高分组学生 id 集合，如 {101, 105, ...}
    low_ids = set(totals.tail(n).index)      # 低分组学生 id 集合

    for q_id, grp in df.groupby("q_id"):
        # 该题满分：取第一行的 full_score。
        #   假设"同一题所有行满分相同"（R-08 均匀分值成立）；
        #   将来若改成按题设分，这里要确保同一题的各行 full_score 一致，否则指标会飘。
        full = grp["full_score"].iloc[0]
        # P = 该题平均得分 / 满分。示例：均分 0.98 / 满分 4.0 = 0.245 → 过难
        p = grp["score"].mean() / full

        # D：只在高低分组里取该题得分再求均分
        #   grp[...]["score"] 是布尔索引：先筛出属于高分组的行，再取 score 列
        #   示例：高组均分 3.8、低组均分 3.6，满分 4 → D = 0.05（谁也不筛）
        high_mean = grp[grp["student_id"].isin(high_ids)]["score"].mean()
        low_mean = grp[grp["student_id"].isin(low_ids)]["score"].mean()
        d = (high_mean - low_mean) / full         # 可为负：低分组考得更好 → 题目可疑

        # 异常标签：一道题可能同时命中多条，全列出（crew 解读时信息更全）
        #   例：Q07 P=0.245 且 D=0.05 → ["过难", "区分度不足"]
        #   这不是重复报警：过难说明"大家都不会"，区分度不足说明"连好学生也不会"，
        #   两条合起来才是完整诊断——这题大概率超纲，而不是学生没学好。
        issues = []
        if p < P_TOO_HARD:
            issues.append("过难")
        if p > P_TOO_EASY:
            issues.append("过易")
        if d < D_TOO_LOW:
            issues.append("区分度不足")

        results.append({
            "q_id": int(q_id),                    # numpy int → Python int，JSON 才序列化得动
            "P": round(float(p), 3),              # 保留 3 位：0.245 够看出过难，太多位是噪声
            "D": round(float(d), 3),
            "score_rate": round(float(grp["score"].mean() / full), 3),   # 均匀分值下与 P 相同
            "is_abnormal": bool(issues),          # 有任一标签即异常，供 pick_abnormal 直接筛
            "issues": issues,
        })

    # pandas 3.0 下 groupby 的键可能是 numpy 类型，统一转 int 后按 q_id 排序：
    #   排序保证"同一份数据跑两次，输出顺序一致"（截图对比、断言都好写）。
    return sorted(results, key=lambda r: r["q_id"])


def attach_kp_ids(metrics: list[dict], q_kp_map: dict[int, str]) -> list[dict]:
    """给指标行补上 kp_id（命题质量专家查图谱要用它）。

    得到的数据：metrics=[{"q_id": 7, "P": 0.245, ...}, ...]；q_kp_map={7: "kp_crossmult", ...}
    要做的操作：逐行按 q_id 查映射填入；原地修改并返回同一 list
    返回：[{"q_id": 7, "P": 0.245, ..., "kp_id": "kp_crossmult"}, ...]（缺映射时填 "unknown"）
    为什么：指标本身只有题号，而图谱接口认知识点 id，中间必须有一层映射
    """
    for row in metrics:
        row["kp_id"] = q_kp_map.get(row["q_id"], "unknown")
    return metrics


def pick_abnormal(metrics: list[dict]) -> list[dict]:
    """挑出异常题（is_abnormal=True）供写回与 crew 使用。

    得到的数据：compute_item_metrics 的输出
    要做的操作：列表推导筛 is_abnormal
    返回：[{"q_id": 7, "P": 0.245, "issues": ["过难"]}, {"q_id": 13, ...}, ...]
    为什么：写回和提示词都只要异常题，独立成函数避免两处各写一遍漏判断
    """
    return [r for r in metrics if r["is_abnormal"]]


def write_back_quality_flags(metrics: list[dict], session=None) -> int:
    """把异常题的 quality_flag 从 normal 写成 abnormal（R-04 → R-08 的回流）。

    得到的数据：metrics（含 q_id/is_abnormal）；session=None（不传则自建 PG 会话）
    要做的操作：收集异常 q_id → IN 查回 → 只把 normal 改 abnormal → commit
    返回：实际改动行数，如 2（disabled 与已 abnormal 都不覆盖、不计数）
    为什么：R-08 抽题时 abnormal 沉底降权，"考后分析→考前改进"就落在这一个字段上
    """
    from app.models import Question

    # t1：异常题号集合（去重 + 排序）
    #   set 去重：同一题在长表里有 N 行，不去重会重复处理 N 次
    #   sorted：让 SQL 的 IN 参数顺序稳定，慢查询日志/审计里好对账
    abnormal_ids = sorted({r["q_id"] for r in metrics if r["is_abnormal"]})
    if not abnormal_ids:
        return 0

    own_session = session is None           # 记住"这个 session 是不是我开的"，决定最后要不要关
    if own_session:
        from app.db.session import get_session_factory
        session = get_session_factory()()

    try:
        # t3：一次捞回目标行（in_ 批量查，避免 N 次单条查询）
        rows = session.query(Question).filter(Question.id.in_(abnormal_ids)).all()
        changed = 0
        for q in rows:
            # t4：只降权 normal；disabled（人工判死刑）和已 abnormal 都不动。
            #   若这里写成无条件赋值，就会出现"人工拉黑的题被系统改回 abnormal、
            #   下次出卷又被抽进来"的事故——disabled 是终态，谁都不许翻。
            if q.quality_flag == "normal":
                q.quality_flag = "abnormal"
                changed += 1
        session.commit()
        return changed
    except Exception:
        # 半路失败整笔回滚：不留"改了一半"的脏状态
        session.rollback()
        raise                               # raise 原异常（不吞），上层才知道写回失败
    finally:
        if own_session:                     # 只关自己开的 session，见 docstring
            session.close()


def metrics_context_for_crew(metrics: list[dict], title: str = "", grade: str = "") -> str:
    """把指标压成 给 crew 提示词用的紧凑 JSON 文本。

    得到的数据：
        metrics（指标行）；
        title="初三数学月考试卷"；
        grade="初三"

    要做的操作：只留 q_id/kp_id/P/D/issues 五个字段，json.dumps

    返回：'{"paper_title": "初三数学月考试卷", "grade": "初三",
           "items": [{"q_id": 7, "kp_id": "kp_crossmult", "P": 0.245, "D": 0.05,
                      "issues": ["过难"]}, ...]}'

    为什么：score_rate 与 P 在均匀分值下重复、is_abnormal 可由阈值推出，省 token 且少干扰
    """
    slim = [
        # 只保留：题号、知识点、两个指标、异常标签
        {"q_id": r["q_id"], "kp_id": r.get("kp_id", "unknown"), "P": r["P"], "D": r["D"],
         "issues": r["issues"]}
        for r in metrics
    ]
    # ensure_ascii=False：中文原样输出（"过难"而不是 \u8fc7\u96be），模型复述时不易走样
    return json.dumps({"paper_title": title, "grade": grade, "items": slim},
                      ensure_ascii=False)
