"""
🔴 LangGraph checkpointer 单例：interrupt 挂起会话的唯一存放地。

 得到的数据：PostgresSaver 实例（全进程只有这一个）

 要做的操作：get_checkpointer() 首次调用时建 psycopg 连接池 + 造 saver + setup() 建内部表

 为什么是单例（和 M1 的 get_engine 完全同一套路）：
   checkpointer 底层是 psycopg 连接池，必须全进程复用。
   如果每个请求 new 一个 → 每个请求一套连接池 → 3000 并发把 PG 连接数直接打爆。

 ★ 三个坑（都是踩过才知道的）：
   1. 懒加载：import 本模块绝不连库。否则单元测试没起 PG 就在 import 阶段炸，
      违反 M1 定下的"import 无副作用"铁律。@lru_cache 天然做到"第一次调用才执行"。
   2. setup() 必须调：它幂等建内部表（checkpoints / writes）。
      漏调的表现是——第一次 interrupt 挂起时抛"关系 checkpoints 不存在"，报错位置
      离病根十万八千里，排查一次要半小时。
   3. 多进程注意：uvicorn --workers N 时每个子进程各有一份 saver，
      但大家都连同一个 PG，所以挂起数据天然共享。
"""
from functools import lru_cache

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from app.core.config import settings

# 连接池大小：M2 CLI 单人使用 5 就够；
# M5 上 3000 并发时要调到 50+（并发请求数 / 每个请求的 checkpoint 操作数）
# ⚠️ min_size 必须显式写：psycopg_pool 的 min_size 默认是 4，
#    不写而把 max_size 设成比它小会直接抛 "max_size must be greater or equal than min_size"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5


@lru_cache
def get_checkpointer() -> PostgresSaver:
    """得到的数据：绑好 PG 连接池的 PostgresSaver，进程级单例。

    要做的操作：
      1. 建 psycopg 连接池（min_size 自动预热，open=True 立即建首批连接）
      2. 把池子交给 PostgresSaver —— LangGraph 接受连接池或单连接，
         给池子它就会按需借还，不会每次操作都新建 TCP 连接
      3. setup() 幂等建内部表

    参数：无（一切从 settings 读）

    纯逻辑单测请用 get_memory_checkpointer()**
    """
    pool = ConnectionPool(
        conninfo=settings.postgres_dsn,   # postgresql://user:pass@host:port/db
        min_size=POOL_MIN_SIZE,           # 显式写，别吃 psycopg_pool 的默认值（默认 4）
        max_size=POOL_MAX_SIZE,
        open=True,                        # 立刻建首批连接，不懒到第一次查询
        # 🔴 autocommit=True 不是可选项，是必须：
        #   setup() 建的内部表要配 CREATE INDEX CONCURRENTLY 索引，
        #   而 CONCURRENTLY 不能在事务块里跑（psycopg 默认自动开事务），
        #   不开 autocommit 会抛 ActiveSqlTransaction。
        kwargs={"autocommit": True},
    )
    saver = PostgresSaver(pool)          # 传池子，不传单连接
    saver.setup()                        # 幂等：内部表已存在就直接跳过
    return saver


def get_memory_checkpointer() -> MemorySaver:
    """给单元测试 / 冒烟脚本用：内存版 checkpointer，进程结束即丢。

    单测之间必须互相独立，共用一个 MemorySaver 会跨用例串状态。
    何时用：跑 tests/、跑冒烟 import 检查时；
    正式演示/服务一律用 get_checkpointer()。
    """
    return MemorySaver()


def close_checkpointer() -> None:
    """进程退出前关连接池（脚本 main() 结尾的 finally 里调）。

    陌生点 get_checkpool.cache_clear()：
    把 @lru_cache 的缓存清掉，下次调用 get_checkpointer() 会重新走一遍建池流程。
    不清的话，池子关了这套 saver 还被缓存着，下次调用就拿到一个连不上的死对象。
    """
    get_checkpointer.cache_clear()
