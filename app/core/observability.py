"""Langfuse 观测层：Agent 链路监控（监控第二层）的唯一入口。

 得到的数据：settings.langfuse_public_key / secret_key / host 三项配置
 要做的操作：
   build_callbacks()          —— 产一个带身份归属的 CallbackHandler，塞进 LangGraph 的 config
   enable_crewai_tracing()    —— 给 CrewAI 底下的 litellm 挂 langfuse 上报钩子（幂等）
 返回：见各函数 docstring

【为什么单独建这个模块，而不是各文件直接 import langfuse】
  ① 降级集中管理：keys 没配 / 包没装 / 上报失败，都必须"观测归观测，业务归业务"——
     绝不能让 Langfuse 挂掉影响学生答题。散在各文件里，每个调用点都得自己 try/except。
  ② 环境变量同步只做一次：LangChain 走 handler 构造参数，但 CrewAI 底下的 litellm
     只认 os.environ 里的 LANGFUSE_* —— pydantic 的 .env 加载不导出到环境变量，
     必须这里手动 setdefault 同步一次，litellm 才读得到。

【内网部署的意义】
  Langfuse 自托管在 docker-compose 里（http://localhost:3100），作答正文、错因、
  评语这些教学数据全程不出校园内网，和云版 Langfuse 的合规差别就在这。
"""
import logging
import os

from app.core.config import settings

logger = logging.getLogger(__name__)

_import_warned = False          # 包没装只警告一次，别每个请求刷一条
_litellm_hooked = False         # litellm 钩子全局只能设一次（重复 append 会双份上报）


def _configured() -> bool:
    """keys 是否已配置。两个 key 必须同时有——只配一个会把请求打到无效项目上。"""
    return bool(settings.langfuse_public_key) and bool(settings.langfuse_secret_key)


def _sync_env() -> None:
    """把 keys 同步进 os.environ（供 litellm 读；pydantic 加载 .env 不导出环境变量）。

    setdefault：环境里已有的优先——容器部署时可能直接在 docker env 里给了
    真实值，.env 里的值不该覆盖它。
    """
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)


def build_callbacks(user_id: str | None = None,
                    session_id: str | None = None,
                    trace_name: str | None = None,
                    metadata: dict | None = None,
                    tags: list[str] | None = None) -> list:
    """产 Langfuse 的 CallbackHandler 列表，直接塞进 LangGraph/LangChain 的 config。

    得到的数据：调用方身份（user_id=登录名）、会话归属（session_id）、本次运行名
    要做的操作：keys 未配返回 []；langfuse 包缺失警告一次后返回 []；
                都齐则构造 CallbackHandler（每次调用新实例，handler 轻量且并发安全）
    返回：[] 或 [handler]——调用方写 config["callbacks"] = build_callbacks(...)，
          空列表和没有这个 key 对 LangChain 完全等价，所以业务零感知

    参数怎么取值（UI 上对应关系）：
        user_id     登录名，如 "R003"      → Traces 页按 User 筛选
        session_id  一个学生的一个场景一条线，
                    如 "student_3"         → Sessions 页把多次 trace 串成一条会话
        trace_name  本次运行的类型名，
                    如 "student_chat"      → Traces 页 Name 列
        metadata    任意 dict（student_id/question_id/homework_id）→ trace 详情页展示
        tags        ["student"] / ["teacher"] / ["research"]      → 按端筛选

    为什么 session_id 用学生档案 id 而不是 thread_id：
        thread_id 是"学生×题目"粒度（一题一条），用作文档里的"会话"会碎成几百条；
        学生档案 id 才是"这个学生和 Agent 的全部交互"粒度，题目维度在 trace 详情里看。
    """
    global _import_warned

    if not _configured():
        return []

    try:
        from langfuse.callback import CallbackHandler   # v2 的内置路径
    except ImportError:
        if not _import_warned:
            _import_warned = True
            logger.warning("langfuse 未安装（uv add langfuse），Agent 链路观测关闭")
        return []

    try:
        handler = CallbackHandler(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            user_id=user_id,
            session_id=session_id,
            trace_name=trace_name,
            metadata=metadata,
            tags=tags,
        )
        return [handler]
    except Exception as exc:                            # 构造失败也不能影响业务请求
        logger.warning("Langfuse handler 构造失败，本次运行不上报：%s: %s",
                       type(exc).__name__, exc)
        return []


def enable_crewai_tracing() -> bool:
    """给 CrewAI 底下的 litellm 挂 langfuse 上报钩子（教研端 crew 专用入口）。

    得到的数据：无（读 settings + os.environ）
    要做的操作：keys 配齐 → 同步环境变量 → litellm.success_callback 挂 "langfuse"；
                未配齐/包缺失 → 什么都不做
    返回：True=挂上了，False=没挂

    为什么走 litellm 钩子而不是 callback handler：
        CrewAI 的 LLM 请求由 litellm 直发（crewai.LLM 包装），不经过 LangChain
        的 callback 体系；litellm 原生认 LANGFUSE_* 环境变量，
        success_callback=["langfuse"] 是它官方的接入点。
    """
    global _litellm_hooked

    if not _configured():
        return False
    if _litellm_hooked:
        return True

    try:
        import litellm
    except ImportError:
        return False

    _sync_env()
    # 覆盖式赋值而不是 append：别处可能已挂别的回调，这里只保证 langfuse 在列
    litellm.success_callback = ["langfuse"]
    _litellm_hooked = True
    logger.info("CrewAI/litellm 的 Langfuse 上报已启用：%s", settings.langfuse_host)
    return True
