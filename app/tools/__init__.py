# app/tools/__init__.py
# M4-T1 · 共享 Tool 层统一出口（架构文档 6.2）。
#
# 【这个包存在的意义】
#   同一件查询三端都要用：学生端推同类题、老师端查某班掌握度、教研端两个 crew 查错题热点。
#   若各写各的，就是三套 SQL / 三套 Cypher，改一个字段要改三处，还容易口径不一致
#   （比如"错题"一处算近 7 天、一处算近 30 天，两个页面数字对不上，老师立刻不信系统）。
#   所以：**用 LangChain 的 @tool 定义一次**，谁要用谁 import：
#     - LangGraph 的学生/批改 Agent：bind_tools([...]) 挂给模型，模型自己决定调哪个
#     - CrewAI 的 analysis_crew / exam_crew：经 app/crews/llm_bridge.to_crewai_tools() 包一层
#     - 纯 Python 流程（exam_crew 的选题器）：直接 .invoke({...}) 当普通函数使
#
# 【返回类型铁律】所有 tool 一律返回 JSON 字符串，不返回 dict/list。
#   卡点：CrewAI 把 tool 返回值原样拼进 LLM 上下文；dict 的 str() 形态是
#        "{'kp_id': 'kp_a', 'avg_p_known': 0.31}"（单引号 + Python 布尔），
#        模型照抄参数时容易把单引号抄进 JSON，解析就炸。JSON 双引号标准风最不容易抄歪。
#

# 【本层 7 个工具一览（名字 → 干什么 → 谁在用）】
#   query_knowledge_graph       某知识点前置链（Neo4j 多跳）→ R-04 专家 / R-08 规划师
#   query_class_mastery         某班各点平均掌握度（聚合无明细）→ R-04 专家 / R-08 规划师
#   query_error_hotspots        年级错题热点 Top N → R-08 月考模式的原料
#   query_paper_stats           某卷逐题作答长表 → R-04 统计层的原始数据
#   query_student_records       某生某点历次作答 → BKT 回放 / 学生 Agent 画像
#   search_question_bank        题库检索 → S-10 配题 / S-05 同类题 / T-01 选题
#   select_questions_by_quota_tool  配额抽题的 tool 壳（底层是纯 Python 引擎，可复现）

from app.tools.bank_tools import search_question_bank, select_questions_by_quota_tool
from app.tools.graph_tools import (
    query_class_mastery,
    query_error_hotspots,
    query_knowledge_graph,
)
from app.tools.stats_tools import query_paper_stats, query_student_records

# ★ __all__ 的作用：声明 `from app.tools import *` 导出哪些名字。
# 写上 __all__ 等于告诉工具链 "这些是给外部用的转出口"。同时 IDE 补全也认这份清单。
__all__ = [
    "query_knowledge_graph",            # 图谱：前置链
    "query_class_mastery",              # 图谱域聚合：班级掌握度
    "query_error_hotspots",             # 图谱域聚合：年级错题热点
    "query_paper_stats",                # 统计：试卷逐题长表
    "query_student_records",            # 统计：学生作答序列
    "search_question_bank",             # 题库：检索
    "select_questions_by_quota_tool",   # 题库：按配额抽题
]
