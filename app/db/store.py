"""🔴 LangGraph Store 单例：学生画像的存放地（长期记忆，跨 thread 共享）。

 得到的数据：PostgresStore 实例（全进程只有这一个）
 要做的操作：get_store() 首次调用时建 psycopg 连接池 + PostgresStore + setup() 建内部表

★ 它和 checkpointer 的分工（架构文档 4.4，最容易搞混的一对）：
   checkpointer  短期记忆  key = thread_id      作用域=一道题的引导对话、挂起会话
   store         长期记忆  key = namespace 元组  作用域=跨题、跨会话的学生画像
   PostgreSQL    真源      —— 画像/掌握度/作答的真值都在业务表里，store 只是加速缓存

★ 三个坑：
   1. 懒加载：@lru_cache 保证 import 本模块不连库（与 checkpointer 同套路）。
   2. setup() 必须调：幂等建内部表（checkpoints/store/writes 几张表），
      漏调的表现是首次 store.put() 报"关系不存在"。
   3. namespace 必须是【元组】：("student", "1001")
      写成字符串 "student_1001" 前缀检索会失效——这是官方文档点名的坑。
"""
from functools import lru_cache

from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from psycopg_pool import ConnectionPool

from app.core.config import settings

# 与 checkpointer 共用一个 PG，但连接池分开建：
# 两者读写频率不同（store 是低频画像，checkpoint 是高频挂起）
# 分池避免互相抢占连接。
POOL_MIN_SIZE = 1     # 画像是低频操作，1 条常驻够用，不预热
POOL_MAX_SIZE = 5


@lru_cache
def get_store() -> PostgresStore:
    """
    何时用：正式演示 / M5 服务 —— 画像要跨进程存活（重启后学生画像还在）

    返回的数据：绑好 PG 连接池的 PostgresStore，进程级单例。
    """
    pool = ConnectionPool(
        conninfo=settings.postgres_dsn,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        open=True,
        # 🔴 autocommit=True 不是可选项，是必须：
        #   PostgresStore.setup() 内部要跑 CREATE INDEX CONCURRENTLY，
        #   而 CONCURRENTLY 【不能】在事务块里执行。psycopg 默认自动开事务，
        #   不开 autocommit 就会报 ActiveSqlTransaction。
        kwargs={"autocommit": True},
    )
    store = PostgresStore(pool)      # 传连接池
    store.setup()                    # 幂等建内部表
    return store



def get_memory_store() -> InMemoryStore:
    """给单元测试 / 调试用：内存版 store，每次调用【新建】一个。

    为什么单独给一个而不让 checkpointer 那边自动降级：
      画像是"必须功能"，所以 graph 那边强制必填（漏传就炸）；
      但"内存版"是一条【显式的降级通路】——调用方主动声明"本次运行接受画像丢失"，
      而不是框架偷偷把必填参数填成空壳。和 checkpointer.get_memory_checkpointer() 完全对称。
    """
    return InMemoryStore()
