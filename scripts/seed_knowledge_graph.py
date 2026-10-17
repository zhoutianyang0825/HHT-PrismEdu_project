"""M1-T3 · Neo4j 图谱 seed 脚本：把 data/knowledge_graph/math.json 导入 Neo4j。

 导入内容：
   Chapter 节点            ← chapters[]
   KnowledgePoint 节点      ← chapters[].knowledge_points[]
   BELONGS_TO 关系          ← 知识点 → 章节
   HAS_PREREQUISITE 关系    ← prerequisite_edges：from(前置点) → to(依赖它的点)
 幂等：全部用 MERGE，重复跑不产生重复节点/边。
 --wipe：先删光本图的 Chapter/KnowledgePoint 及其关系再导入（开发期重建用）。

 运行：uv run python scripts/seed_knowledge_graph.py [--wipe]
"""
import json
import sys
from pathlib import Path

# 让脚本在项目根直接可跑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.graph_db.neo4j_client import close_driver, get_driver

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "knowledge_graph" / "math.json"


def wipe(tx) -> None:
    """删光图里的 Chapter / KnowledgePoint（连带关系）。MASTERY 关系挂在 KP 上也会一并删除。

    Cypher 陌生点 DETACH DELETE：DELETE 只删节点（有关系挂着会报错），
    DETACH DELETE 连节点带所有关系一起删——删节点基本永远用带 DETACH 的版本。
    n:Chapter OR n:KnowledgePoint：按标签过滤要删的节点，别把别的数据误伤。
    """
    tx.run("MATCH (n) WHERE n:Chapter OR n:KnowledgePoint DETACH DELETE n")


def import_data(tx, graph: dict) -> tuple[int, int, int]:
    """在一个事务里 MERGE 全部节点与关系，返回 (章节数, 知识点数, 前置边数)。

    Cypher 陌生点 MERGE：'有则匹配、无则创建'的幂等写法（区别于 CREATE 的
    无脑新建）——脚本重复跑 N 次也只会有 N=1 份节点/边，这就是 seed 脚本
    必须用 MERGE 不用 CREATE 的原因。MERGE 后面跟 SET 补充/更新其余属性。
    参数 $id / $name：Cypher 的占位符，值由 tx.run 的 kwargs 传入，
    不要用 f-string 拼 Cypher（注入风险 + 转义地狱）。
    """
    n_ch = n_kp = n_edge = 0
    for ch in graph["chapters"]:
        tx.run(
            """
            MERGE (c:Chapter {id: $id})
            SET c.name = $name, c.subject = $subject, c.grade = $grade, c.order = $order
            """,
            id=ch["id"], name=ch["name"], subject=graph["subject"],
            grade=ch["grade"], order=ch["order"],
        )
        n_ch += 1
        for kp in ch["knowledge_points"]:
            tx.run(
                """
                MERGE (k:KnowledgePoint {id: $id})
                SET k.name = $name, k.chapter_id = $ch, k.difficulty = $diff,
                    k.description = $desc, k.subject = $subject
                WITH k
                MATCH (c:Chapter {id: $ch})
                MERGE (k)-[:BELONGS_TO]->(c)
                """,
                id=kp["id"], name=kp["name"], ch=ch["id"],
                diff=kp["difficulty"], desc=kp.get("description"), subject=graph["subject"],
            )
            n_kp += 1
    for e in graph["prerequisite_edges"]:
        # 方向约定：from(前置点) -[:HAS_PREREQUISITE]-> to(依赖它的点)
        # 这样 MATCH (kp)<-[:HAS_PREREQUISITE*1..2]-(p) 查出的 p 就是 kp 的前置点
        tx.run(
            """
            MATCH (pre:KnowledgePoint {id: $src})
            MATCH (dep:KnowledgePoint {id: $dst})
            MERGE (pre)-[:HAS_PREREQUISITE]->(dep)
            """,
            src=e["from"], dst=e["to"],
        )
        n_edge += 1
    return n_ch, n_kp, n_edge


def main() -> None:
    graph = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    driver = get_driver()

    with driver.session(database="neo4j") as session:
        if "--wipe" in sys.argv:
            session.execute_write(wipe)
            print("[wipe] 已清空旧图谱")
        # 陌生函数 execute_write：开一个托管事务跑写入函数（tx 参数由驱动递进来）。
        #   函数里任何一步抛异常，整个事务自动回滚——不会导一半留个残图。
        #   注意 read 要用对应的 execute_read，读写分开是 Neo4j 集群路由的要求
        n_ch, n_kp, n_edge = session.execute_write(import_data, graph)

    print(f"[OK] Chapter {n_ch} 个 / KnowledgePoint {n_kp} 个 / HAS_PREREQUISITE {n_edge} 条")

    # 自检：跑一条多跳前置链（M1 完成判据里的 Cypher），能查出结果才算成功。
    # 陌生点 <-[:HAS_PREREQUISITE*1..3]- ：反向指关系 + *1..3 变长路径（1 到 3 跳），
    #   即"所有能通过 1~3 条前置边走到 kp_yinshi 的点"，这是"从坏点回溯前置缺口"的核心查询
    records, _, _ = driver.execute_query(
        """
        MATCH p = (a:KnowledgePoint {id: 'kp_yinshi'})<-[:HAS_PREREQUISITE*1..3]-(pre)
        RETURN DISTINCT pre.name AS name, length(p) AS depth ORDER BY depth
        """,
        database_="neo4j",
    )
    chain = [r["name"] for r in records]
    print(f"[自检] 因式分解的前置链（1~3跳）: {chain}")
    if not chain:
        print("[WARN] 前置链为空，请检查 HAS_PREREQUISITE 方向是否导反")
        sys.exit(1)
    close_driver()


if __name__ == "__main__":
    main()
