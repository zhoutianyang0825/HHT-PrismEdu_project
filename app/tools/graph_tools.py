# app/tools/graph_tools.py
# M4-T1 · 图谱侧共享 Tool：Neo4j 前置链路 + 两个"知识点维度"的 PG 聚合。
#
# 【为什么两个 SQL 聚合也放这里】query_class_mastery / query_error_hotspots 查的虽然是 PG，
#   但消费方都是"按知识点思考"的角色（命题规划师、命题质量专家），它们会把
#   "某班 p_known=0.31" 和 "该点的前置是整式乘法" 配在一起做判断。
#   两个数放两个文件，改代码时要来回跳；放一个文件，一次读完就知道口径。
#
# 【返回格式铁律】所有 tool 返回 json.dumps(...) 字符串，不返回 dict。
#   理由：CrewAI 把 tool 输出原样拼进 LLM 上下文，dict 的 str() 形态是单引号 Python 风，
#   模型抄参数时容易抄错；JSON 是双引号标准风，模型见过几十亿次，最不容易抄歪。
#
# 【一个共同的坑：tool 内部才 import 项目模块】
#   本文件顶部只 import 了 json 和 langchain 的 @tool，DB/Neo4j 相关 import 全写在函数体内。
#   为什么：这个包会被 Crew 组装、被单测、被 M5 的 API 层 import，只要有一次
#   "import 时就建连接池 / 就找驱动"，任何缺容器、缺驱动的环境都会在 import 阶段炸掉。
#   延迟 import = import 阶段零副作用，只有真正调用工具时才碰库。
import json

from langchain_core.tools import tool



@tool
def query_knowledge_graph(kp_id: str, depth: int = 2) -> str:
    """查某知识点的前置知识链路（Neo4j 多跳）。

    得到的数据：kp_id="kp_crossmult"（知识点 id，填名称会静默返回空）；
                depth=2（向前追几跳，函数内夹到 1~5，防 0/99）

    要做的操作：沿 <-[:HAS_PREREQUISITE*1..2]- 反向边做 1~depth 跳变长匹配

    返回：[{"kp_id": "kp_poly_mult", "name": "整式乘法", "depth": 1}, ...]，按 depth 升序；
          无前置时 []

    为什么：R-04 判"异常题是否前置薄弱"、R-08 月考把热点前置纳入配额，都要这条链
    """
    from app.graph_db.neo4j_client import run_query

    # 【Cypher 的箭头方向为什么是反的】
    #   seed 脚本约定：HAS_PREREQUISITE 从"前置点"指出去，指向"依赖它的点"。
    #   现在要查：某知识点的前置知识点，就得逆着箭头走：<-[:HAS_PREREQUISITE*1..2]-
    #   *1..2 是"变长关系"语法（1 到 2 跳都算），写成 *1..$depth 用参数控制跳数。

    rows = run_query(
        """
        MATCH path = (kp:KnowledgePoint {id: $kp_id})<-[:HAS_PREREQUISITE*1..$depth]-(pre)
        RETURN pre.id AS kp_id, pre.name AS name, length(path) AS depth
        ORDER BY depth
        """,
        # Neo4j 参数化查询防注入
        # max(1, min(int(depth), 5))：把 LLM 可能给的 0/99/-1 夹到 1~5，见 docstring。
        {"kp_id": kp_id, "depth": max(1, min(int(depth), 5))},
    )

    # ensure_ascii=False：中文按原样输出（"整式乘法"），否则会是 \u6574\u5f0f\u4e58\u6cd5，
    return json.dumps(rows, ensure_ascii=False)


@tool
def query_class_mastery(class_id: int) -> str:
    """查某班级在各知识点上的平均 BKT 掌握度（聚合，无学生明细）。

    得到的数据：class_id=2（classes.id，传班名会类型校验失败）
    要做的操作：bkt_state JOIN students，按 kp_id 求 avg(p_known) 与去重人数，掌握度升序
    返回：[{"kp_id": "kp_crossmult", "avg_p_known": 0.31, "student_count": 20}, ...]
    为什么：教研端要知道"哪块地基最松"，同时不能给聚合角色任何学生明细（双通道隔离）
    """
    from sqlalchemy import func, select

    from app.db.session import get_session_factory
    from app.models import BKTState, Student

    session = get_session_factory()()      # 工厂()() 两级：先取工厂，再开一个新 session
    try:
        rows = session.execute(
            select(
                BKTState.kp_id,
                # p_known的平均值
                func.avg(BKTState.p_known).label("avg_p_known"),
                # count(distinct student_id)：这个知识点上有多少学生有记录。
                func.count(func.distinct(BKTState.student_id)).label("student_count"),
            )
            .join(Student, Student.id == BKTState.student_id)   # 靠学生表才能拿到 class_id
            .where(Student.class_id == int(class_id))           # 只算这个班
            .group_by(BKTState.kp_id)                           # 按知识点分桶
            .order_by(func.avg(BKTState.p_known).asc())         # 最薄弱优先
        ).all()

    # finally 关 session：查询成功失败都归还连接，防连接池被慢慢耗干
    finally:
        session.close()

    # round(float(p), 3)：SQL 的 avg 在不同库可能返回 Decimal，统一转 float 再保留 3 位，
    # 保证 JSON 里是 0.31 这种干净数字（而不是 0.3120456789）。
    out = [{"kp_id": kp, "avg_p_known": round(float(p), 3), "student_count": n}
           for kp, p, n in rows]
    return json.dumps(out, ensure_ascii=False)


@tool
def query_error_hotspots(grade: str, subject: str = "math", top_n: int = 10) -> str:
    """查某年级某学科的错题热点 Top N（R-08 月考模式的命题输入）。

    得到的数据：grade="初三"（必须与 classes.grade 字面量一致，否则静默返回空）；
                subject="math"；top_n=10
    要做的操作：近 30 天 is_correct=False 的作答，按 questions.kp_id 聚合计次，降序取前 N
    返回：[{"kp_id": "kp_crossmult", "name": "十字相乘法", "wrong_count": 27}, ...]
    为什么：月考要"纠偏"，先得知道这个月全年级在哪儿错得最多
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.db.session import get_session_factory
    from app.models import Class, KnowledgePoint, Question, Student, StudentAnswer

    # 时间窗口下界：当前时间往前推 30 天。
    #   用 timezone-aware 的 UTC 时间 —— submitted_at 列是 DateTime(timezone=True)，
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    session = get_session_factory()()
    try:
        rows = session.execute(
            select(
                Question.kp_id,
                KnowledgePoint.name,        # 顺手带出中文名，省得 crew 再查一次库
                func.count().label("wrong_count"),
            )
            # 【五表联查的路径，按"错误从哪来"顺着走】
            #   student_answers(一条错答) → students(是哪个学生) → classes(他哪个班/哪个年级)
            #   student_answers → questions(答的哪道题) → knowledge_points(题考哪个点)
            #   联结写错一个 on 条件（比如 Question.id == Student.id）不会报错，
            #   但会生成笛卡尔积式的错误聚合，数字看着"有值"却完全不对——这是联查最阴的坑。
            .join(StudentAnswer, StudentAnswer.question_id == Question.id)
            .join(Student, Student.id == StudentAnswer.student_id)
            .join(Class, Class.id == Student.class_id)              # 年级过滤走班级表
            .join(KnowledgePoint, KnowledgePoint.id == Question.kp_id)
            .where(Class.grade == str(grade))                       # 年级：初二/初三……
            .where(Question.subject == str(subject))                # 学科：math……
            .where(StudentAnswer.is_correct == False)               # 回答错误的，未判分的不算错
            .where(StudentAnswer.submitted_at >= cutoff)            # 最近30天内的
            .group_by(Question.kp_id, KnowledgePoint.name)          # 点 + 名字一起分桶
            .order_by(func.count().desc())                          # 错得多的在前
            .limit(int(top_n))
        ).all()

    finally:
        session.close()

    out = [{"kp_id": kp, "name": name, "wrong_count": int(n)} for kp, name, n in rows]
    return json.dumps(out, ensure_ascii=False)
