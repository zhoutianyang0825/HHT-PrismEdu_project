"""M3-T4 · BKT 仓储层：掌握度读写。

 本文件定位：mastery.py 的纯函数只管算，落库全在这里
 get_bkt_state()  读：当前 p_known + 距上次作答天数（update_mastery 的两个输入）
 sync_mastery()   写：bkt_state 覆盖 + mastery_records 追加快照 + Neo4j MASTERY 同步
"""
from datetime import datetime, timezone

from app.db.session import get_session_factory
from app.models import BKTState, MasteryRecord
from app.services.mastery import DEFAULT_PARAMS

# Cypher 集中管理（与 graph_db/cypher.py 的约定一致；M3 只用这一条）
# 语义：给知识点挂 MASTERY 掌握关系，MERGE 保证幂等——重跑不产生重复边
CYPHER_UPSERT_MASTERY = """
MATCH (kp:KnowledgePoint {id: $kp_id})
MERGE (kp)<-[m:MASTERY {student_id: $student_id}]-(s:Student {id: $student_id})
SET m.p_known = $value, m.updated_at = datetime()
"""


def get_bkt_state(student_id: int, kp_id: str) -> tuple[float, float]:
    """读 BKT 当前状态，即 update_mastery 的两个输入。

    得到的数据：
      student_id  学生档案 id（students 表主键，注意不是 users.id）
      kp_id       知识点业务 id，类似 "kp_crossmult"
    要做的操作：查 bkt_state 一行；查不到 = 第一次碰这个知识点，返回先验值
    返回：元组 (p_known, days_since_last)，形如 (0.33, 12.4)
          无记录时返回 (0.10, 0.0)——先验且不衰减
    为什么：BKT 是"状态机"，每次更新必须先拿到上一次状态，不能凭空算
    """
    session = get_session_factory()()
    try:
        row = session.get(BKTState, (student_id, kp_id))  # 复合主键直接 get
        if row is None:
            return DEFAULT_PARAMS["p_init"], 0.0

        # 距上次更新天数：现在 - last_updated，秒差换算成天
        delta = datetime.now(timezone.utc) - row.last_updated
        days = max(0.0, delta.total_seconds() / 86400.0)
        return row.p_known, days
    finally:
        session.close()


def sync_mastery(student_id: int, kp_id: str, new_p: float) -> None:
    """一次 BKT 更新后的三处落库。

    得到的数据：
      student_id  学生档案 id
      kp_id       知识点 id，类似 "kp_crossmult"
      new_p       update_mastery 算出的新掌握概率，类似 0.65
    要做的操作：
      1. bkt_state：有行就覆盖 p_known，没行就插（复合主键保证一人一点一行）
      2. mastery_records：追加一行快照（只增不改）
      3. Neo4j：MERGE MASTERY 边（best-effort）
    返回：None；第 3 步失败只打警告，不抛异常
    为什么：三处各司其职——实时状态给下次 BKT，快照给趋势线，图副本给教研端 Cypher 聚合
    """
    # ---- 1+2：PG 侧，事实源，一个事务写完 ----
    session = get_session_factory()()
    try:
        row = session.get(BKTState, (student_id, kp_id))
        if row is None:
            # 复合主键行不存在 → 插新行（不能 UPDATE 空对象）
            session.add(BKTState(student_id=student_id, kp_id=kp_id, p_known=new_p))
        else:
            row.p_known = new_p       # 覆盖现状；last_updated 靠 onupdate 自动刷

        session.add(MasteryRecord(student_id=student_id, kp_id=kp_id, mastery_value=new_p))
        session.commit()              # 两张表同事务：要么都在要么都不在
    finally:
        session.close()

    # ---- 3：Neo4j 图副本，best-effort ----
    # 数据流向：PG 是事实源，Neo4j 只是副本。图库挂了只让教研端聚合暂缺数据，
    #   绝不能因为图库断连把批改/BKT 主链路一起拖死
    try:
        from app.graph_db.neo4j_client import run_query
        run_query(CYPHER_UPSERT_MASTERY, {
            "student_id": str(student_id),
            "kp_id": kp_id,
            "value": new_p,
        })
    except Exception as exc:
        print(f"[bkt_repository] ⚠ Neo4j MASTERY 同步失败（不影响主链路）: {exc}")
