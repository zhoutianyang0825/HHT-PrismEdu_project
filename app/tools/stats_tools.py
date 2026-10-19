# app/tools/stats_tools.py
# M4-T1 · 统计侧共享 Tool：试卷逐题长表 + 学生作答序列。
#
#   谁在用：
#     query_paper_stats     → R-04 的数据分析师（拿它当 analysis.compute_item_metrics 的原料）
#                             M5 的 /api/research/papers/{id}/analysis 接口也会直接调
#     query_student_records → BKT 掌握度服务回放作答序列；学生 Agent 查"我这题以前错过没"
#
#   两个工具的共性：都是"把库里的事实捞出来，不做任何判断"。
#   判断（P 是多少、算不算异常）全在 app/services/analysis.py，工具层保持哑管道，
#   这样才能对"数字从哪来"负责——数据错看工具，算法错看服务，边界清楚。
import json

from langchain_core.tools import tool


@tool
def query_paper_stats(paper_id: int) -> str:
    """取某张试卷的全部作答长表（每行 = 一个学生 × 一道题）。

    得到的数据：paper_id=3（exam_papers.id，不存在时返回 {"error": ...} 而非抛错）
    要做的操作：读试卷拿总分与题量算每题满分 → 查该卷已判分作答 → 拼 records 长表
    返回：{"paper_id": 3, "title": "初三数学月考试卷", "full_score_per_q": 4.0,
          "records": [{"student_id": 101, "q_id": 7, "score": 3.0, "full_score": 4.0}, ...]}
    为什么：R-04 要按题算 P/D，长表直接 groupby("q_id")，不用先 melt
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models import ExamPaper, StudentAnswer

    session = get_session_factory()()
    try:
        # session.get(模型, 主键)：按主键取一行，比 query().filter(id==).first() 更直接
        paper = session.get(ExamPaper, int(paper_id))
        if paper is None:
            # 早返回也要走 finally 关 session（下面是 try/finally，return 也会触发 finally）
            return json.dumps({"error": f"paper_id={paper_id} 不存在"}, ensure_ascii=False)

        quota = paper.quota_plan_json or {}
        # 【兼容两种历史存法】
        #   现行写法（exam_crew 落库）：{"plan": {...}, "question_ids": [...], "gaps": [...]}
        #   早期/手工写入：直接把 QuotaPlan 铺在顶层（顶层就有 items）
        #   两种都认，避免"老数据读不出来"的兼容性事故。
        plan = quota.get("plan", quota if "items" in quota else {})
        q_ids = quota.get("question_ids", [])
        # max(1, len(q_ids))：题量为 0 时防 ZeroDivisionError（除零在 Python 里是硬异常）
        full_per_q = round(plan.get("total_score", 100) / max(1, len(q_ids)), 2) if q_ids else 0.0

        rows = session.execute(
            select(
                StudentAnswer.student_id,
                StudentAnswer.question_id,
                StudentAnswer.score,
            )
            .where(StudentAnswer.source_type == "exam")     # 只统计考试作答（作业/测验不算）
            .where(StudentAnswer.source_id == int(paper_id))
            # score IS NOT NULL 过滤未判分作答：
            #   student_answers.is_correct 是三态（NULL=未判分/True/False），
            #   主观题老师没点确认前 score 也是 NULL。把这些行算进去，
            #   平均分会被人为拉低，P 值直接失真——统计前必须清掉。
            .where(StudentAnswer.score.isnot(None))
        ).all()
    finally:
        session.close()

    records = [
        {"student_id": sid, "q_id": qid, "score": float(score), "full_score": full_per_q}
        for sid, qid, score in rows
    ]
    return json.dumps(
        {"paper_id": int(paper_id), "title": paper.title,
         "full_score_per_q": full_per_q, "records": records},
        ensure_ascii=False,
    )


@tool
def query_student_records(student_id: int, kp_id: str) -> str:
    """查某学生在某知识点上的历次作答记录。

    得到的数据：student_id=101（students.id，**不是** users.id）；kp_id="kp_crossmult"
    要做的操作：student_answers JOIN questions 按知识点过滤，提交时间升序
    返回：[{"question_id": 12, "is_correct": false, "score": 0.0,
           "source_type": "homework", "submitted_at": "2026-10-01T12:00:00+00:00"}, ...]
    为什么：BKT 更新要按时间顺序回放（顺序错了遗忘衰减 Δt 会变负数）
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models import Question, StudentAnswer

    session = get_session_factory()()
    try:
        rows = session.execute(
            select(
                StudentAnswer.question_id,
                StudentAnswer.is_correct,
                StudentAnswer.score,
                StudentAnswer.source_type,      # homework / quiz / exam，帮调用方区分场景
                StudentAnswer.submitted_at,
            )
            .join(Question, Question.id == StudentAnswer.question_id)   # 要题目上的 kp_id
            .where(StudentAnswer.student_id == int(student_id))
            .where(Question.kp_id == str(kp_id))
            .order_by(StudentAnswer.submitted_at.asc())                # ★ 时间升序，见上
        ).all()
    finally:
        session.close()

    out = [
        {
            "question_id": qid,
            # 三态原样保留：is_correct 为 None 表示"还没判分"，
            # 不能 bool(None) 变成 False（那等于把未判分当错题，BKT 会误判为不会）
            "is_correct": bool(c) if c is not None else None,
            "score": float(s) if s is not None else None,
            "source_type": st,
            # isoformat()：把 datetime 转成 "2026-10-01T12:00:00+00:00" 字符串
            # 直接 json.dumps(datetime) 会抛 TypeError（datetime 不是 JSON 原生类型）
            "submitted_at": sa.isoformat() if sa else None,
        }
        for qid, c, s, st, sa in rows
    ]
    return json.dumps(out, ensure_ascii=False)
