"""M3-T3 · BKT 掌握度三步更新：遗忘衰减 → 贝叶斯后验 → 学习效应。

 本文件定位：纯函数，零 DB / 零 LLM 依赖
 
 调用链：grading_graph 的 update_mastery 节点 → update_mastery() → bkt_repository.sync_mastery()
"""
import math

# BKT 四个参数的 领域常用默认值（数据攒够后用 EM 算法拟合，现在统一初值）
# p_init   初始掌握概率：学生第一次见这个知识点的先验
# p_learn  一次练习后由"不会"转"会"的概率
# p_slip   "会但做错"的概率：被错因动态替换，见下面 SLIP_BY_ERROR
# p_guess  "不会但蒙对"的概率：选择题偏高，解答题应调到 0.05（M4 出卷时按题型细分）
DEFAULT_PARAMS = {"p_init": 0.10, "p_learn": 0.30, "p_slip": 0.10, "p_guess": 0.20}


# 错因 → p_slip失误 的映射表（与 error_logs.error_type 的枚举一一对应）
# 例：粗心 0.45 = 有 45% 可能是"会但算错"，所以掌握度惩罚要轻得多
SLIP_BY_ERROR = {
    "概念不清": 0.05,     # 真没掌握，几乎不留余地地压低 P
    "方法不会": 0.10,     # 默认值，走正常"未掌握"路径
    "粗心":    0.45,    # 视为"会但算错"，P 下降幅度明显放缓
    "审题失误": 0.35,    # 不因读错题就重判为完全不会
}


# 遗忘系数：半衰期 7 天 → 7 天不练，掌握度衰减到一半
# 推导：e^(-λ*7) = 0.5  →  λ = ln2 / 7 ≈ 0.099
LAMBDA = math.log(2) / 7


def apply_decay(p_known: float, days_since_last: float) -> float:
    """计算知识点的遗忘衰减度，得出“还会”的概率

    得到的数据：
      p_known          上一次的掌握概率，类似 0.80
      days_since_last  距上次作答的天数，类似 30.0
    要做的操作：p_known * e^(-λ·days)，久不练习自然回落
    返回：衰减后的掌握概率，形如 0.041
    为什么：单独暴露成函数，是为了直接测试 "30 天无作答" 这种无作答情况（无作答不能调 update_mastery 叠加贝叶斯与学习效应）
    """
    return p_known * math.exp(-LAMBDA * days_since_last)


def update_mastery(
    p_known: float,
    is_correct: bool,
    error_type: str | None,
    days_since_last: float,
    params: dict | None = None,
) -> float:
    """一次作答后的 BKT 三步更新。全系统的掌握度只从这里产生

    得到的数据（逐参数）：
      p_known          上一次的掌握概率，类似 0.72；首次作答传 DEFAULT_PARAMS["p_init"]
      is_correct       本次是否答对
      error_type       错因，类似 "粗心"；答对时必须传 None（答对没有错因可言）
                       传表里没有的值不报错，静默退回默认 slip
      days_since_last  距上次作答天数，类似 3.0；当天连续作答传 0 或 1 均可
                       传负数会被 clamp 到 0（否则 e^(正数) 会把掌握度"练"回去）
      params           四个 BKT 参数字典；=None 时用 DEFAULT_PARAMS（推荐）

    要做的操作：利用四参数 BKT公示，先做遗忘衰减 → BKT贝叶斯后验修正 → 叠加BKT学习效应

    返回：新的掌握概率，恒落在 (0, 1) 开区间，形如 0.649
    
    """
    # 四个 BKT 参数字典；=None 时用 DEFAULT_PARAMS（推荐）
    params = params or DEFAULT_PARAMS

    # 答错时按错因取 slip；答对时用默认 slip = 即便真会，也有 10% 概率做错
    # 纪律：错因只替换本次 slip，不改其它参数、不直接加减掌握度
    p_slip = params["p_slip"]

    # 答错且有了错因时，再按错因获取新的p_slip
    if not is_correct and error_type:
        p_slip = SLIP_BY_ERROR.get(error_type, params["p_slip"])  # 兜底：未知错因退回默认


    # 防脏数据：天数不可能为负
    days = max(0.0, days_since_last)

    # ---- 第 1 步：遗忘衰减，久不练习掌握度自然回落，得出“还会”的概率 ----
    p_decayed = apply_decay(p_known, days)


    # ---- 第 2 步：贝叶斯后验。答对抬升 P，答错压低 P，幅度受 slip 与 guess 约束 ----
    # 题目答对了的情况下：
    if is_correct: 
        # 真会且答对
        # P(会)·(1-slip) = 真会 × 没失误 
        num = p_decayed * (1 - p_slip)   

        # 如果不会但蒙对 
        # P(不会)·params["p_guess"] = 不会 × 蒙对了
        # 汇总：答对的总概率
        den = num + (1 - p_decayed) * params["p_guess"]  

    # 题目答错的情况下：
    else:
        # P(会)·slip = 真会 × 失误  -- "会但做错"概率  
        num = p_decayed * p_slip  

        # (1 - p_decayed) * (1 - params["p_guess"]) = 不会 x 没蒙对 -- “不会且没蒙对”概率
        # 汇总：答错的总概率
        den = num + (1 - p_decayed) * (1 - params["p_guess"])  


    # p_guess 取极小值时 den 可能为 0，那么就回退到衰减值
    # 答对之后，学生真会的概率： 真会概率/答对总概率
    # 答错之后，学生真会的概率： 真会概率/答错总概率
    p_posterior = num / den if den > 0 else p_decayed


    # ---- 第 3 步：练习本身带来学习效应，向"会"推进一小步 ----
    # 注意：答错也加学习效应——学生刚练过这道题，总归学到了点东西
    new_p = p_posterior + (1 - p_posterior) * params["p_learn"]


    # 数值卫生：夹在 (0, 1) 开区间内，别让下游拿 0 去当除数
    # 上限 1-1e-6 避免“绝对掌握”
    return min(max(new_p, 1e-6), 1.0 - 1e-6) 
