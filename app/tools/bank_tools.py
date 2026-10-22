# app/tools/bank_tools.py
# M4-T1 · 题库侧共享 Tool：题库检索 + 配额抽题的 tool 壳。
#
# 【壳与芯的分工，这条边界是本文件最要紧的设计】
#   select_questions_by_quota_tool（工具，返回 JSON 字符串）
#        ↑ 只做三件事：解析入参、调引擎、把结果转 JSON 字符串
#   select_questions_by_quota      （引擎，纯函数，在 app/services/selection_engine.py）
#        ↑ 真正决定"选哪几道题"的规则全在这儿，同一输入必然同一输出
#
#   为什么非要拆开：
#     ① 芯是纯函数 → 单测直接打，不需要 DB / LLM / CrewAI（tests/test_selection_engine.py 就是这么测的）；
#     ② 壳只是适配层 → 换框架只改壳，规则一行不动；
#     ③ exam_crew 的选题执行器直接调芯（跳过壳），少一次 JSON 往返，也少一次 LLM 插手的机会。
import json

from langchain_core.tools import tool


@tool
def search_question_bank(
    kp_id: str,
    difficulty: str = "",
    question_type: str = "",
    exclude_ids: list[int] | None = None,
    limit: int = 5,
) -> str:
    """ 按知识点筛题，可叠加过滤条件： 难度/题型/排除近做过的题

    得到的数据：
            kp_id="kp_crossmult"；
            difficulty="easy"（空=不过滤）；
            question_type="fill"（空=不过滤）；
            exclude_ids=[31, 47]（近 7 天做过的）；
            limit=5

    要做的操作：按 kp 筛 → 叠加难度/题型 → 剔除 exclude → 截 limit 条

    返回：[
            {"id": 12, "kp_id": "kp_crossmult", "difficulty": "easy", "question_type": "fill",
             "content": "计算：x²-5x+6=0 的解是……", "quality_flag": "normal"
            }, ...
        ]

    为什么：S-10 配题、S-05 推同类题、T-01 选题三处都要"按知识点捞题"，定义一次复用
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models import Question

    session = get_session_factory()()
    try:
        # 【增量拼 WHERE】先建基础 stmt，满足一个条件就 where 一次。
        #   若不这么写，难度×题型×排除 三个可选项要写 8 个分支；链式追加天然支持任意组合。
        stmt = (
            select(
                Question.id, Question.kp_id, Question.difficulty,
                Question.question_type, Question.content, Question.quality_flag,
            )
            .where(Question.kp_id == str(kp_id))
            .where(Question.quality_flag != "disabled")      # ★ 硬过滤，见 docstring
        )

        # 分别叠加过滤条件： 难度/题型/排除近做过的题
        if difficulty:
            stmt = stmt.where(Question.difficulty == str(difficulty))
        if question_type:
            stmt = stmt.where(Question.question_type == str(question_type))
        if exclude_ids:
            # notin_ 的坑：传空列表会生成 "NOT IN ()"，部分库直接语法错误，
            # 所以上面用 `if exclude_ids` 挡住 None / [] /False。
            stmt = stmt.where(Question.id.notin_([int(i) for i in exclude_ids]))

        rows = session.execute(stmt.limit(max(1, int(limit)))).all()

    finally:
        session.close()

    out = [
        {"id": i, "kp_id": kp, "difficulty": d, "question_type": t,
         # content 截 80 字：tool 返回值会进 LLM 上下文，整套题干几万字，
         # 模型反而抓不住重点。要看全文让它按 id 再查一次。
         "content": c[:80], 
         "quality_flag": q
        }
        for i, kp, d, t, c, q in rows
    ]
    return json.dumps(out, ensure_ascii=False)


@tool
def select_questions_by_quota_tool(plan_json: str, exclude_ids: list[int] | None = None) -> str:
    """按 QuotaPlan 配额表 执行抽题（壳函数，逻辑在 selection_engine）。

    得到的数据：
            plan_json='{"exam_type": "monthly", "total_score": 100, "items": [...]}' ；   
            exclude_ids=[31, 47]；

    要做的操作：json.loads 解析 → 按知识点捞池 → 调纯函数引擎抽题 → 结果转 JSON

    返回：{"picked": [{"id": 12, "kp_id": "...", "difficulty": "medium",
                      "question_type": "fill", "quality_flag": "normal"}],
          "gaps": ["kp_peifang: medium/fill 缺 2 题（放宽难度后仍不足，池中仅 1 题）"]}

    为什么：给 CrewAI/LangGraph 一个"能调用的门面"，而抽题规则仍留在可单测的纯函数里
    """
    from app.services.selection_engine import fetch_candidates_by_kp, select_questions_by_quota

    # 解析配额表：items = 配额行列表（每行：知识点 + 题量 + 难度 + 题型）
    plan = json.loads(plan_json)
    items = plan.get("items", [])

    # 知识点去重后一次性捞池 → {kp_id: [候选题目...]}。
    #   先 set 再 list：同一 kp 可能出现两行（不同题型），不去重会查两遍、池里出现重复候选。
    kp_ids = list({it["kp_id"] for it in items})
    candidates = fetch_candidates_by_kp(kp_ids)
    picked, gaps = select_questions_by_quota(
        quota_items=items,
        candidates_by_kp=candidates,
        # 外部传进来的 list[int] → set[int]：引擎内部做成员判断用 in set（O(1)），
        exclude_ids=set(int(i) for i in (exclude_ids or [])),
    )
    
    return json.dumps({"picked": picked, "gaps": gaps}, ensure_ascii=False)
