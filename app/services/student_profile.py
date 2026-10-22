"""M2-T4（可选加分项）· InMemoryStore 学生画像 + warm_start 兜底（架构文档 4.4）。

分层定位（必须能讲出来）：
  短期记忆  PostgresSaver   作用域 thread_id        一道题的引导对话、挂起会话
  长期记忆  InMemoryStore   作用域 namespace 元组    学生画像（跨 thread 共享）
  真源      PostgreSQL/Neo4j 主键/节点              掌握度、作答、错因

  
关键坑：InMemoryStore 的"长期"只是跨 thread，不跨进程——进程重启画像清零。
对策：PostgreSQL 当真源、Store 当缓存，进节点前幂等 warm_start 预热。
升级路径：InMemoryStore() → PostgresStore(...) 一行切换，业务代码零改动。
"""


def build_profile(student_id: int) -> dict:
    """从业务库（真源）算画像：薄弱知识点 Top5 + 近 30 天错因习惯。

    得到的数据：学生 id（students 表主键）
    要做的操作：bkt_state 按 p_known 升序取 5 个 + error_logs 按错因计数
    返回：{"weak_kps": [("kp_crossmult", 0.33), ...], "habits": {"粗心": 2, "概念不清": 1}}
    """
    from sqlalchemy import select, func
    from app.db.session import get_session_factory
    from app.models import BKTState, ErrorLog, StudentAnswer

    session = get_session_factory()()
    try:
        weak_rows = session.execute(
            select(BKTState.kp_id, BKTState.p_known)
            .where(BKTState.student_id == student_id)
            .order_by(BKTState.p_known.asc())
            .limit(5)
        ).all()
        habit_rows = session.execute(
            select(ErrorLog.error_type, func.count(ErrorLog.id))
            .join(StudentAnswer, StudentAnswer.id == ErrorLog.answer_id)
            .where(StudentAnswer.student_id == student_id)
            .group_by(ErrorLog.error_type)
        ).all()
    finally:
        session.close()

    return {
        "weak_kps": [(kp, float(p)) for kp, p in weak_rows],
        "habits": {t: int(n) for t, n in habit_rows},
    }


def warm_start(store, student_id: int) -> None:
    """用业务库给 Store 兜底：进程重启后自动重建画像（幂等，成本=一次 PG 查询）。

    得到的数据：LangGraph 注入的 store、学生 id
    要做的操作：画像已存在直接返回；不存在就 build_profile 算一份写进去
    陌生点 store.get/put：Store 的读写 API。
      namespace 必须是元组 ("student", "1001")——写成 "student_1001" 字符串
      是架构文档 12 点名的坑：前缀检索会失效
    """
    ns = ("student", str(student_id))
    if store.get(ns, "profile") is not None:
        return                              # 幂等：已有就别重复查库
    profile = build_profile(student_id)
    store.put(ns, "profile", profile)


def get_profile(store, student_id: int) -> dict | None:
    """读画像（CLI/节点拼 prompt 用）。返回 None 表示画像为空（新学生正常现象）。"""
    item = store.get(("student", str(student_id)), "profile")
    return item.value if item else None
