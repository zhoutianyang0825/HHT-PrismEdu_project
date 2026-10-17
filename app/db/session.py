"""数据库会话管理：engine 单例 + SessionLocal 工厂 + get_db 依赖。

 得到的数据：engine（连接池）、SessionLocal（会话工厂）
 要做的操作：get_db 作为 FastAPI 依赖逐请求借出/归还 session；脚本里直接 SessionLocal()
 坑：pool_pre_ping=True 防止容器闲置后连接被掐、下一个请求拿到死连接报错
 设计：engine 惰性创建——import 本模块绝不能有副作用（连不上 PG / 没装驱动
       都不该在 import 时炸），第一次真正用时才建。
"""
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


# 陌生点 @lru_cache：Python 内置装饰器（functools），"记不住才怪"缓存——
#   第一次调用真执行函数体，之后所有调用直接返回上次的结果（不再执行）。
#   这里用它实现单例：全进程永远只有这一个 engine，反复 new 连接池很贵
@lru_cache
def get_engine() -> Engine:
    """engine 单例。惰性创建，参数只在第一次调用时求值。

    create_engine 本身不连库（只建配置对象），真正建立 TCP 连接是第一次
    执行 SQL 时——所以它放在函数里也很安全。pool_pre_ping=True：每次借连接
    先发个 ping，连接已被容器/防火墙掐掉就自动换新的，防止"隔夜第一个请求报错"。
    future=True：显式启用 2.0 风格 API（老 1.x 用法会直接报错，防止新旧混用）。
    """
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


@lru_cache
def get_session_factory() -> sessionmaker:
    """SessionLocal 工厂单例，绑定惰性 engine。

    陌生点 sessionmaker：会话"工厂"——调用它一次 SessionLocal() 就产出一个
    全新 session（一次数据库对话：N 条语句 + 1 个事务）。为啥要工厂：
    session 不是线程安全/请求安全的，每个请求必须各开各的。
      autoflush=False：commit 前不自动把未保存改动刷成 SQL（flush），避免
        "还没想好就先写了一半"的隐式行为，要写库自己 flush/commit，更可控
      expire_on_commit=False：commit 后不把对象属性标记"过期"——默认行为是
        commit 后再访问对象属性会重新查库，接口层序列化时容易踩
        DetachedInstanceError；关掉后 commit 完对象照常用
    """
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


# 惯例导出：脚本/依赖里直接 from app.db.session import SessionLocal
# 陌生点 模块级 __getattr__（PEP 562）：定义后，任何 `from app.db.session import X`
#   若 X 不是本模块普通变量，Python 会调 __getattr__("X") 问一次——
#   这里借此把 engine/SessionLocal 做成"第一次 import 时才真建"的假全局变量。
#   直接写 SessionLocal = sessionmaker(...) 的话，import 瞬间就建 engine，
#   单测没配 PG 也会炸——这就是当初 review 逮到的 import 副作用问题
def __getattr__(name: str):
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_session_factory()
    raise AttributeError(name)


def get_db():
    """FastAPI 依赖：yield 出一个 session，请求结束（含异常）必还。

    陌生点 yield 依赖：FastAPI 里带 yield 的函数是"带善后逻辑的依赖"——
    yield 前是准备（建 session），yield 出去的值注入给接口参数，
    yield 后是善后（关 session），请求抛异常也会走 finally，连接不泄漏。
    """
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
