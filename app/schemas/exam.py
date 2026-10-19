# app/schemas/exam.py
# R-08 试卷生成的结构化契约（架构文档 6.4）。
#
# 【三个模型串起一条链，谁也不许私自加字段】
#   命题规划师 LLM → QuotaPlan（配额表，可打印、可人工改）
#   质量审核员 Python → ReviewReport（硬指标数值 + 结论文字）
#   三个角色之间只传这两个模型；配额表还要打印给教研组长人工改，
#   所以字段名一旦定下就是对外接口，改字段等于改合同。
#
# 【Field 的 description 不是给人看的注释，是给 LLM 看的说明书】
#   CrewAI 把这些 description 一起塞进结构化输出的 schema 里，
#   模型据此决定每个字段该填什么。写得含糊，模型就瞎填；
#   写清"如 '年级错题 Top3'"，它才会填出人话。
from typing import Literal

from pydantic import BaseModel, Field


class QuotaItem(BaseModel):
    """一行配额：某知识点出几道、什么难度、什么题型、为什么选它。

    字段即"打印给教研组长的一页配额纸"，reason 必须是能读懂、能改的人话。
    """

    # kp_id：知识点业务 id（如 kp_crossmult），与 knowledge_points.id、Neo4j 节点 id 同一套。
    #   填错后果：抽题时按 id 查池查不到 → 该行配额全变缺口，卷子题量不足；
    #   填成中文名（"十字相乘法"）不报错但必定查空，是这个字段最阴的错法。
    kp_id: str = Field(description="知识点 id，如 kp_crossmult")

    # chapter_id：所属章节 id（如 ch_quadratic）。
    #   作用只有一个——摸底型的"章节覆盖率 ≥80%"要用它做集合去重统计。
    #   填 "unknown"（兜底表的做法）不会崩，但覆盖率会如实报低，审核结论会写明"章节覆盖不足"。
    chapter_id: str = Field(description="所属章节 id，用于覆盖率校验")

    # question_count：该知识点配几道题。
    #   ge=1：填 0 或负数直接被 Pydantic 拦下（ValidationError），避免"要 0 道题"这种无意义配额。
    #   注意引擎侧还有 per_kp_cap=2 的硬红线（PRD R-08 验收：同一知识点不超过 2 题），
    #   所以填 3 以上不会多给你题，只会在缺口清单里多一条"超上限被截断"的记录。
    question_count: int = Field(ge=1, description="该点配几道题")

    # difficulty：难度档，三选一。
    #   用 Literal 而不是 str：模型若输出"中"（中文）或 "MEDIUM"（大写）会立刻校验失败，
    #   而不是带着非法值一路流到组卷引擎（那里 _RELAX_ORDER[difficulty] 会 KeyError 崩掉）。
    difficulty: Literal["easy", "medium", "hard"] = Field(description="难度档")

    # question_type：题型，三选一，必须与 questions.question_type 的取值域一致。
    #   为什么是硬约束：R-08 摸底型要求"选择:填空:解答 ≈ 4:3:3"，题型在引擎里当过滤条件用，
    #   写一个库里不存在的题型（如 "essay"）→ 该行一道题也抽不到，全进缺口清单。
    question_type: Literal["choice", "fill", "subjective"] = Field(description="题型")

    # reason：入选理由，直接决定教研组长能不能"看懂并改得动"。
    #   好的 reason："年级错题 Top3，近30天错 27 次"；坏的 reason："重要知识点"（无法验证、无法改）。
    reason: str = Field(description="入选理由，给教研组长看，如 '年级错题 Top3'")


class QuotaPlan(BaseModel):
    """整卷配额表：命题规划师的输出，也是 LLM 在本系统里的权限边界。

    LLM 只出到这一层（选点 + 配比），真正抽题由 selection_engine 纯 Python 执行。
    """

    # exam_type：考试类型，决定用哪套硬指标（目标分布/覆盖率/命中率）。
    #   填错后果很直观：传 "monthly" 却按期末出题 → 审核时"章节覆盖率"根本没被检查，
    #   一张月考卷可能只覆盖两章，教研组长拿到手才发现"这不是月考，是章节小测"。
    exam_type: Literal["monthly", "midterm", "final"] = Field(
        description="月考巩固型 / 期中期末摸底型"
    )

    # total_score：卷面总分，默认 100。
    #   它只影响"每题多少分"（总题量 ÷ 总分），不影响抽题数量。
    #   填 150 也行，但前端/统计页的"满分"展示要能跟着变，别在别处写死 100。
    total_score: int = Field(default=100, description="卷面总分，默认 100")

    # items：配额行列表，一题一行知识点。总和 = 整卷题量（月考约 20、摸底约 25）。
    #   空列表是合法的（Pydantic 不拦），但下游会得到一张 0 题卷；
    #   run_exam_generation 里用 _fallback_plan 兜住"LLM 规划失败"这种空表情况。
    items: list[QuotaItem] = Field(description="逐知识点配额行")


class ReviewReport(BaseModel):
    """质量审核结果：硬指标数值（Python 算）+ 结论文字（LLM 组织）。

    passed=False 时 problems 就是缺口清单，原样交教研组长——绝不乱补题。
    """

    # passed：所有硬指标是否全过。
    #   True 才会走"可布置"流程；False 时 M5 的接口应把它标成"待人工复核"，不许直接发给学生。
    passed: bool = Field(description="硬指标是否全部通过")

    # rounds_used：实际用了几轮抽题（1 或 2）。
    #   用来解释"为什么这张卷子难度偏低"——第 2 轮是降过难度的，
    #   组长看到 rounds_used=2 就知道系统已经让步过，可以决定要不要人工补难题。
    rounds_used: int = Field(description="实际用了几轮抽题（上限 2）")

    # metrics：硬指标原始数值，示例
    #   {"hit_rate": 0.75, "diff_dist": {"easy": 0.4, "medium": 0.45, "hard": 0.15},
    #    "type_dist": null, "chapter_coverage": null, "per_kp_max": 2, "total_questions": 20}
    #   用 dict 而不是逐字段建模：这些是"审核证据"，字段随指标演进会变，
    #   钉死成模型反而每次加指标都要改契约。前端按 key 取值展示即可。
    metrics: dict = Field(description="硬指标数值，如 {'hit_rate': 0.75, 'diff_dist': {...}}")

    # problems：未通过项的中文说明，逐条可读。
    #   默认空列表（default_factory=list）：用可变默认值必须给工厂函数，
    #   直接写 = [] 会让所有实例共享同一个 list（Pydantic 会拦，但要知道为什么）。
    problems: list[str] = Field(default_factory=list, description="未通过的指标与缺口说明")

    # summary：给教研组长看的结论文字（LLM 组织；LLM 挂了降级为规则模板拼的文本）。
    #   为什么默认空串而不是必填：先算指标、再补文字，两步分开；
    #   summary 生成失败不能拖累前面已经算好的硬指标。
    summary: str = Field(default="", description="给教研组长看的结论文字（LLM 组织，失败降级为规则文本）")
