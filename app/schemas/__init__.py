# app/schemas/__init__.py
# M4 结构化 Schema 统一出口：R-04 报告与 R-08 配额表的"交接契约"都从这里发。
#
# 【为什么单独建 schemas 包，而不是把模型塞在 crew 文件里】
#   这些模型有四个消费方，谁都不能私自改字段：
#     ① CrewAI 的 Task(output_pydantic=...)  —— 用来卡住 LLM 输出格式
#     ② exam_papers.quota_plan_json / remark —— 直接 model_dump() 进 PG 的 JSON 列
#     ③ M5 的 FastAPI 响应模型                —— 前端拿到的 JSON 就是它的字段
#     ④ 单测                                  —— 造数据/断言都按这套字段
#   四处共用一个模型类，"字段名改了但前端没改"这种事故从结构上就不容易发生。
#
# 【Pydantic 在这里充当什么角色（陌生点补课）】
#   一句话定义：把"数据长什么样"写成代码，运行时自动校验，不合规立刻报错并指出哪个字段。
#   类比：报关单模板——栏目（字段名）和填写要求（类型/取值范围）都印死在纸上，
#        填错了窗口直接退回，不会让错数据流到下一环。
#   代码形态：
#       class QuotaItem(BaseModel):
#           question_count: int = Field(ge=1)      # ge=1 → 填 0 或 -1 直接 ValidationError
#   为什么这里是关键：LLM 的输出是"概率产物"，偶尔会漏字段、把题量写成 0、把难度写成"中"。
#   有 Pydantic 这一层，错输出在校验处就被挡住（我们走兜底分支），而不是流到组卷引擎里
#   变成一个诡异的空卷或死循环。
from app.schemas.analysis import AnalysisReport, ItemNote
from app.schemas.exam import QuotaItem, QuotaPlan, ReviewReport

# __all__：声明外部可用的名字（见 app/tools/__init__.py 里对 __all__ 的详细解释）。
__all__ = [
    "QuotaItem", "QuotaPlan", "ReviewReport",   # R-08 出卷三件套
    "ItemNote", "AnalysisReport",               # R-04 分析两件套
]
