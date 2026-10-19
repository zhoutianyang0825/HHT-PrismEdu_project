# app/crews/__init__.py
# M4 教研端两个 crew 的包出口。
#
#   analysis_crew → R-04 考后分析（三角色 sequential：数据分析师 → 命题质量专家 → 教研组长）
#   exam_crew     → R-08 考前出卷（命题规划 → 选题执行 → 质量审核，≤2 轮回退）
#   llm_bridge    → 两个 crew 共用的 LLM 实例与 Tool 适配（挡住环境变量与工具协议两个坑）
#   demo_analysis → 演示入口：`uv run python -m app.crews.demo_analysis`（--exam 开出卷模式）
#
# 【为什么不在这里做 `from app.crews.analysis_crew import ...` 的转出口】
#   import app.crews 就会连带 import crewai（analysis_crew/exam_crew 顶部都要用 crewai 的类）。
#   本包被 M5 的 API 层整体 import，也会被单测 import——保持 __init__ 为空壳，
#   意味着"想用哪个 crew 就显式 import 哪个模块"，不会因为引了包名就把 crewai 拖进来。
#   这是 import 成本与依赖隔离的取舍，不是忘了写。
