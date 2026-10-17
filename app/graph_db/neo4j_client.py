"""Neo4j driver 单例：全项目唯一的图谱连接出口。

 得到的数据：neo4j.GraphDatabase.driver 实例
 要做的操作：get_driver() 惰性创建；run_query() 包一层 execute_query 方便脚本用
 坑：Neo4j 5.x 推荐execute_query，旧的 session.run 裸用容易忘关 session 泄连接
 陌生库 neo4j（官方 Python 驱动）：连接 Neo4j 图数据库（ bolt:// 协议）。
   driver = 管连接池的长连接对象（创建开销大，全局一个就够，所以惰性单例）；
   session = driver 上开出的"一次对话"（用完必须关）；
   访问图数据不用 SQL，用 Cypher 语言——MATCH 查、MERGE 建、DETACH DELETE 删。
"""
from neo4j import GraphDatabase

from app.core.config import settings

_driver = None


def get_driver():
    """惰性单例：第一次调用才建连接，脚本和 FastAPI 生命周期都能用。

    GraphDatabase.driver 本身不建 TCP 连接（和 SQLAlchemy 的 create_engine 一样
    是"配置对象"），第一次真正发查询时才连——所以失败也会推迟到查询时才暴露。
    auth=(用户名, 密码)：元组形式，对应 compose 里的 NEO4J_AUTH。
    """
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


def run_query(cypher: str, params: dict | None = None) -> list[dict]:
    """执行只读/写入 Cypher 并把记录转成 dict 列表。

    陌生函数 execute_query（Neo4j 5.x 推荐 API）：driver 自带的托管执行——
    它自动从连接池借 session、自动重试瞬时故障、自动关 session，三件事全包，
    比手动 with driver.session() 少踩"忘关连接"的坑。
    返回三元组 (records, summary, keys)，这里只用 records，所以解包时后两个用 _ 丢弃。
    """
    driver = get_driver()
    records, _, _ = driver.execute_query(cypher, params or {}, database_="neo4j")
    return [dict(r) for r in records]


def close_driver() -> None:
    """进程退出前调用；脚本结尾 finally 里关掉。"""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
