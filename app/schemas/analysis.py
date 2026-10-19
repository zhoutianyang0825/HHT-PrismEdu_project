# app/schemas/analysis.py
# R-04 试卷分析的结构化输出契约（架构文档 6.1 / 开发计划 M4-T3）。
#
# 【为什么末位 Task 必须用 output_pydantic 卡格式（开发计划称 "output_json 卡格式"）】
#   CrewAI 默认输出是一段自然语言。对演示很友好，对系统很致命：
#     - 前端没法渲染（"建议第 2 条是什么"没法按字段取）
#     - PG 没法存（存一段散文，以后想统计"哪类异常最多"只能再让 LLM 读一遍）
#     - 下游没法校验（组长想按 q_id 排序？文本里没有结构化 id）
#   用 Pydantic 模型卡住 schema 后，模型的返回经 structured output 校验，
#   字段缺一个、类型错一个都过不去——格式不靠模型自觉，靠契约。
#
# 【为什么会失败，以及我们怎么兜】
#   模型偶尔会把 P 写成 "0.28（偏难）" 这种带说明的字符串，校验就挂。
#   analysis_crew.run_paper_analysis 里对此有降级分支：把原始文本塞进 overview，
#   异常题清单从本地指标重建（数字本来就是我们算的），保证链路不白跑。
from pydantic import BaseModel, Field


class ItemNote(BaseModel):
    """单道异常题的"病例卡"：异常类型 + 成因 + 依据。

    例：Q07 P=0.28 过难，因"需同时调十字相乘、配方，班级前置 p_known=0.31"判定为教学问题。
    """

    # q_id：题目 id（questions.id）。填错后果：组长按 id 去题库复核时找不到题，
    #   整条诊断作废；所以这个字段的数值来自 Python 的指标行，不让 LLM 自己编。
    q_id: int = Field(description="题目 id")

    # kp_id：该题考察的知识点 id。它是"查图谱前置链"的钥匙——
    #   填 unknown（demo 里 CSV 映射缺失时会这样）时，专家只能给泛泛结论。
    kp_id: str = Field(description="考察的知识点 id")

    # P：难度系数（平均得分/满分，越大越简单）。
    #   ge=0, le=1：值域锁死。模型若输出 28（忘了除满分）会被校验拦下，
    #   而不是让前端画出一根突破天际的柱子。
    P: float = Field(ge=0, le=1, description="难度系数（越大越简单）")

    # D：区分度（前27%均分 - 后27%均分，再除满分）。
    #   没有值域限制：D 可以是负数（低分组反而考得比高分组好），
    #   负值恰恰是"这题有问题"的强信号，不该被校验拦掉。
    D: float = Field(description="区分度，可为负（低分组比高分组考得好）")

    # issue：异常类型，取 "过难" / "过易" / "区分度不足"。
    #   为什么用自由字符串不写 Literal：Python 侧可能给一道题同时打两个标签
    #   （如"过难、区分度不足"），用 join 拼成一条；写死 Literal 会把这种情况卡掉。
    issue: str = Field(description="异常类型：过难 / 过易 / 区分度不足")

    # cause：成因判断 + 依据。必须带数据（引用工具返回的 p_known、前置点名字），
    #   只有结论没有依据的诊断卡等同于废话——组长无法验证，也就无法据此排课。
    cause: str = Field(description="成因判断：题目问题还是教学问题，须给依据")


class AnalysisReport(BaseModel):
    """R-04 最终交付物：教研组长汇总后的结构化报告。

    流向：crew 末位 Task → API JSON → Streamlit 教研页；建议直接可用于排课。
    """

    # overview：整卷概况（平均分、异常题总数、整体难度评价）。
    #   一段话，不拆字段：这些数字前端已能自己从 metrics 算，
    #   这里保留的是"人话总结"，让组长一眼看懂这张卷子考成什么样。
    overview: str = Field(description="整卷概况：平均分、异常题总数、整体难度评价")

    # abnormal_items：逐题诊断卡列表。
    #   空列表合法（整卷指标全正常），前端要能渲染"本次无异常题"的空态。
    abnormal_items: list[ItemNote] = Field(description="逐道异常题的诊断卡")

    # teaching_suggestions：可执行的教研建议，一条一句话。
    #   "可执行"的标准写在 Field 描述里，也在 Task 描述里强调了三遍：
    #   要落到"知识点 + 班级 + 动作"（如"下周补 1 课时十字相乘专题，2/5 班优先"）。
    #   写成"加强基础训练"这种，等于没说——组长没法排进课表。
    teaching_suggestions: list[str] = Field(
        description="可执行的教研建议，每条一句话，如 '下周补 1 课时十字相乘专题，2/5 班优先'"
    )
