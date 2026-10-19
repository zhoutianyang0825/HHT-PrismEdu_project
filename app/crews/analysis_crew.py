# app/crews/analysis_crew.py
# M4-T3 · R-04 考后分析 crew（架构文档 6.1）：三个角色顺序协作。
#
# 【三个角色各自干什么（顺序不是随便排的，每一步都要吃上一步的产出）】
#
#   ① 考试数据分析师  输入：算好的指标 JSON（Python 给的，不是它自己算的）
#                    动作：读懂 P/D，点名异常题、按异常类型归类
#                    不调任何工具 —— 数据已经在手里，调工具纯浪费一轮往返
#
#   ② 命题质量专家    输入：①的异常清单 + 异常题的 kp_id
#                    动作：查图谱前置链（query_knowledge_graph）
#                          查班级平均掌握度（query_class_mastery）
#                    判断：这道题是"题目问题"还是"教学问题"
#                    ★ 判据：前置点平均 p_known 明显偏低 → 教学问题；否则才是题目问题
#
#   ③ 教研组长        输入：①+②的全部产出
#                    动作：汇总成《AnalysisReport》：概况 / 异常题诊断卡 / 可执行建议
#                    ★ output_pydantic=AnalysisReport：格式不达标直接校验失败
#
# 【为什么是 sequential（顺序）而不是 hierarchical/parallel】
#   ② 要吃 ① 的异常清单，③ 要吃 ①+② 的结论 —— 强依赖，没有可并行的分支。
#   CrewAI 的 context=[前一个 task] 就是用来声明"这一步的输入包含上一步输出"。
#
# 【为什么 LLM 只做解读，不做计算】
#   P/D 已经在 app/services/analysis.py 用 Pandas 算好，随任务描述喂进来。
#   LLM 算平均分"十次对九次"，错的那次会让组长当场质疑整个系统；
#   数字不经过模型的手，这个风险归零。模型只负责"为什么/怎么补"这类表达型工作。
from app.crews.llm_bridge import get_crewai_llm, to_crewai_tools
from app.schemas.analysis import AnalysisReport
from app.services.analysis import metrics_context_for_crew
from app.tools.graph_tools import query_class_mastery, query_knowledge_graph


def build_analysis_crew(metrics: list[dict], title: str = "", grade: str = ""):
    """组装 R-04 分析 crew（只组装，不执行）。

    得到的数据：metrics=[{"q_id": 7, "kp_id": "kp_crossmult", "P": 0.245, "D": 0.05,
                          "is_abnormal": True, "issues": ["过难", "区分度不足"]}, ...]
                （必须已 attach_kp_ids，否则专家查图谱只能拿到 unknown）；
                title="初三数学月考试卷"；grade="初三"
    要做的操作：低温 LLM → 包两个图谱工具 → 指标压成瘦身 JSON → 建 3 Agent + 3 Task（context 串链）
    返回：未 kick 的 Crew(agents=[分析师, 专家, 组长], tasks=[分析, 诊断, 建议],
          process=sequential)
    为什么：R-04 要"数字由 Python 算、成因由专家查图谱、建议由组长汇总"，三者有依赖只能串行
    """
    from crewai import Agent, Crew, Process, Task

    # t1：低温。0.3 是"解读类任务"的常用档位：
    #   0 会让措辞过于模板化（读起来像机器填空）；0.7 又容易在建议里编造没数据支撑的结论。
    llm = get_crewai_llm(temperature=0.3)

    # t2：两个共享工具包一层适配。这里必须走 to_crewai_tools，不能直接塞 LangChain tool
    #   （原因见 llm_bridge 文件头的坑②）。
    graph_tools = to_crewai_tools([query_knowledge_graph, query_class_mastery])

    # t3：指标瘦身成 JSON 文本（去掉冗余字段，见 metrics_context_for_crew 的说明）
    stats_text = metrics_context_for_crew(metrics, title=title, grade=grade)

    # ---- t4：三个 Agent ----
    data_analyst = Agent(
        role="考试数据分析师",
        goal="准确解读每题难度系数 P 与区分度 D，标出全部异常题并按异常类型归类",
        # backstory 的作用：给模型"身份+领域知识"。这里把三条阈值写进背景，
        # 是为了即使提示词里的 issues 字段丢了，它也能自己按阈值判断（双重保险）。
        backstory="你是教育测量学专家，熟悉经典测量理论：P 是难度（越大越简单），"
                  "D 是区分度（高低分组得分差占比），P<0.3 过难、P>0.9 过易、D<0.2 区分度不足。",
        tools=[],                                   # 指标已算好，分析师不需要任何工具
        llm=llm,
        verbose=False,                              # False：不打印每步思考，演示输出更干净
    )

    item_quality_expert = Agent(
        role="命题质量专家",
        goal="对每道异常题判断成因：是题目本身的问题，还是班级前置知识薄弱的教学问题",
        # backstory 里把"判据"写死：前置 p_known 低 → 教学问题。不给判据，
        # 模型会凭"感觉"给结论（"这题表述不清"之类），组长没法验证。
        backstory="你长期参与中考命题审题。判断依据：先用工具查该题知识点的"
                  "前置知识链路，再看班级在该知识点上的平均掌握度——"
                  "前置点平均 p_known 明显偏低就是教学问题，否则才是题目问题。",
        tools=graph_tools,                          # 查前置链 + 班级掌握度
        llm=llm,
        verbose=False,
    )

    research_lead = Agent(
        role="教研组长",
        goal="汇总前两位的发现，产出可执行的下一阶段教研建议",
        backstory="你需要给全年级数学组布置下阶段教学重点。建议必须可执行："
                  "写到具体知识点、具体班级、具体动作（补课时/换题/复核）。",
        # 无需工具：它的输入是前两人的产出，输出是一份结构化报告
        llm=llm,
        verbose=False,
    )

    # t5：三个 Task，用 context 串成依赖链
    #   先在本地算出异常题号清单，写进任务描述里点名——
    #   这叫"防漏报"：只给模型一整张指标表，它偶尔会漏掉一两道；
    #   明确告诉它"系统预筛出 q_id=[7,13,19]"，漏报率显著下降。
    abnormal_ids = [r["q_id"] for r in metrics if r.get("is_abnormal")]

    t_analyze = Task(
        description=(
            # 指标 JSON 原样贴进来：这是模型的"事实来源"，它会照着复述而不是自己算
            f"以下是试卷《{title or '未命名'}》（{grade}）每题的统计指标 JSON：\n{stats_text}\n"
            f"请解读：1) 整卷难度概况；2) 点名全部异常题（系统预筛 q_id={abnormal_ids}），"
            "逐题给出异常类型（过难/过易/区分度不足）与数据依据。"
        ),
        # expected_output 不是"随便写写"：它决定模型交付物的形状，
        # 写得含糊（"一段分析"）模型就会自由发挥；写清字段，输出才稳定。
        expected_output="一段结构化分析文字：先整卷概况，再逐异常题列 q_id、P、D、异常类型",
        agent=data_analyst,
    )

    t_diagnose = Task(
        description=(
            "针对上一步列出的每道异常题：用 query_knowledge_graph 工具查它 kp_id 的"
            "前置知识链路，用 query_class_mastery 工具查班级掌握度（class_id 由输入上下文提供，"
            "没有就说明并跳过该工具）。然后逐题判断成因是'题目问题'还是'教学问题'，给出依据。"
        ),
        expected_output="逐异常题的成因结论，每条含：q_id、结论（题目/教学）、依据（引用工具返回的数据）",
        agent=item_quality_expert,
        # ★ context：把 ① 的产出作为本任务的输入上下文。
        #   不写这行，专家看不到异常清单，只能对整张卷子泛泛而谈。
        context=[t_analyze],
    )

    t_advice = Task(
        description=(
            "汇总数据分析师与命题质量专家的结论，输出最终教研报告。"
            "teaching_suggestions 每条一句话、可执行、落到知识点和班级。"
        ),
        expected_output="符合 AnalysisReport schema 的结构化 JSON",
        agent=research_lead,
        # 两个上游都要吃：概况来自 ①，成因来自 ②
        context=[t_analyze, t_diagnose],
        # ★ output_pydantic：末位任务卡格式。
        #   模型返回会先过 Pydantic 校验，字段缺失/类型不符都会失败（由 run_paper_analysis 兜底）。
        output_pydantic=AnalysisReport,
    )

    # t6：组装。process=sequential → 按 tasks 列表顺序执行，后一个能看到前一个的 context。
    return Crew(
        agents=[data_analyst, item_quality_expert, research_lead],
        tasks=[t_analyze, t_diagnose, t_advice],
        process=Process.sequential,
        verbose=False,
    )


def run_paper_analysis(metrics: list[dict], title: str = "", grade: str = "") -> AnalysisReport:
    """跑完整个 R-04 crew 并返回结构化报告（M5 的 API 层直接调）、

    得到的数据：metrics（同上，需已补 kp_id）；title="初三数学月考试卷"；grade="初三"
    要做的操作：组装 crew → kickoff（三角色顺序跑，中途查库 + 调 LLM）→ 取 .pydantic
    返回：AnalysisReport(overview="整卷平均 2.6/4，3 道异常题……",
          abnormal_items=[ItemNote(q_id=7, P=0.245, D=0.05, issue="过难", cause="前置薄弱……")],
          teaching_suggestions=["下周补 1 课时十字相乘专题，2/5 班优先"])
    为什么：结构化输出失败时用本地指标重建报告，保证"能保的事实全保、不能保的表达如实承认"
    """
    crew = build_analysis_crew(metrics, title=title, grade=grade)
    result = crew.kickoff()          # 真正开跑：这里开始花 token + 查库

    if result.pydantic is not None:
        return result.pydantic       # 正常路径：Pydantic 校验通过的干净对象

    # 降级：raw 是模型最后输出的原文（一段可能不合法的 JSON 或纯文本）
    return AnalysisReport(
        overview=f"[结构化输出失败，原始文本如下]\n{result.raw}",
        abnormal_items=[
            {"q_id": r["q_id"], "kp_id": r.get("kp_id", "unknown"),
             "P": r["P"], "D": r["D"], "issue": "、".join(r["issues"]), "cause": "待人工复核"}
            for r in metrics if r.get("is_abnormal")
        ],
        teaching_suggestions=[],
    )
