# app/crews/llm_bridge.py
# CrewAI 与项目既有配置之间的"桥"：LLM 实例 与 Tool 适配。
#
# 【为什么需要这座桥——两个必踩的坑（架构文档坑表原文）】
#
# 坑① 两个 crew 的 OpenAI 环境变量互相打架
#   CrewAI 底层用 litellm 发请求，默认读 OPENAI_API_KEY / OPENAI_BASE_URL 环境变量。
#   本项目走 DashScope 兼容端点（deepseek-v4-flash）。若不显式传 base_url + api_key：
#     → litellm 找 OPENAI_API_KEY，找不到直接抛 AuthenticationError；
#     → 或环境里恰好有别的项目的 OPENAI_API_KEY，请求打到 OpenAI 官方，模型名对不上，404。
#   解法：get_crewai_llm() 把 settings 三件套显式塞进 crewai.LLM。
#        所有 Agent 的 llm= 参数都必须从这里拿，一个都不许在自己文件里裸建 LLM()。
#
# 坑② LangChain tool 不能直接塞给 CrewAI Agent
#   本项目依赖清单里只有 crewai，没装 crewai-tools（1.x 已把它拆成独立包，
#   Tool.from_langchain() 那条便捷路不存在）。
#   解法：to_crewai_tools() 手工把 LangChain @tool 适配成 crewai.tools.BaseTool。
#        共享 Tool 层"定义一次、三端复用"的承诺，全靠这 30 行兑现。
#
# 【CrewAI 的几个核心概念】
#   LLM     = 模型配置对象（模型名/地址/key/温度），一次建好给所有 Agent 复用
#   Agent   = 一个有角色设定的"AI 员工"：role（头衔）/ goal（目标）/ backstory（背景知识）
#             + tools（能用什么工具）+ llm（用哪个模型）
#   Task    = 一张"工单"：description（要干什么）+ expected_output（交付物长什么样）
#             + agent（谁做）+ output_pydantic（★ 强制交付物符合某模型）
#   Crew    = 把若干 Agent + 若干 Task 组起来跑：process=sequential 就是按 tasks 顺序逐个跑
#   kickoff = 开跑，返回 CrewOutput；结构化结果在 result.pydantic
from functools import lru_cache

from app.core.config import settings
from app.core.observability import enable_crewai_tracing


# ★ @lru_cache：给函数装"记忆"。第一次调用真建 LLM 实例，之后同样参数直接返回上次的实例。
#   为什么：litellm/httpx 背后是连接池，每个 crew、每个 Agent 都 new 一个 LLM 会重复建池；
#   而且 30 行代码里要建 4 个 Agent，共用同一个实例能省一次初始化、配置也只有一个源头。
#   ⚠ 注意：lru_cache 的 key 是"调用参数"，所以 temperature 不同就是不同实例（这是对的）。
@lru_cache
def get_crewai_llm(temperature: float = 0.7):
    """CrewAI 专用 LLM 实例（惰性单例），配置与 app/core/llm.py 同源。

    得到的数据：temperature=0.2（配额表）/0.3（解读建议）/0.7（默认，闲聊讲解）

    要做的操作：把 settings 的 model/base_url/api_key 包成 crewai.LLM（底层 litellm）

    返回：LLM(model="openai/deepseek-v4-flash", base_url="https://dashscope...", temperature=0.2)

    为什么：显式传三件套，避免 litellm 去读 OPENAI_API_KEY 打到 OpenAI 官方（见文件头坑①）
    """
    from crewai import LLM

    # CrewAI 的 LLM 请求走 litellm 直发，不经过 LangChain callback 体系——
    # 观测走 litellm 的 langfuse 钩子（keys 未配时内部直接返回 False，零开销）
    enable_crewai_tracing()

    return LLM(
        model=f"openai/{settings.llm_model}",   # litellm 路由前缀，见 docstring
        base_url=settings.llm_base_url,         # 真正决定"打到哪"
        api_key=settings.llm_api_key,
        temperature=temperature,
    )


# 工具注册表：存{工具名: 原 LangChain tool 实例}，供 Adapter._run 按 self.name 反查
#   为什么用模块级注册表而不是把实例塞进 pydantic 私有属性（PrivateAttr）：
#     ① Adapter 继承 crewai 的 BaseTool，而 BaseTool 是 pydantic 模型，
#        动态塞私有属性要额外声明 PrivateAttr，多一层心智负担；
#     ② 注册表还能当调试入口：想知道某个工具挂在哪个实现上，print 一下即可。

_LC_REGISTRY: dict = {}


def to_crewai_tools(lc_tools: list):
    """把 LangChain @tool 列表适配成 CrewAI BaseTool 列表。

    得到的数据：[query_knowledge_graph, query_class_mastery]（langchain的 StructuredTool 实例）

    要做的操作：原 tool 存进注册表 → 用 name/description/args_schema 建 Adapter 实例

    返回：[<CrewAI_tool query_knowledge_graph>, <CrewAI_tool query_class_mastery>]，可直接塞 Agent(tools=[...])

    为什么：StructuredTool 实例，不能作为CrewAI_tool，给 crewai的agent使用，只能写 30 行适配器
    """
    from crewai.tools import BaseTool

    class _LangChainAdapter(BaseTool):
        """CrewAI 工具的标准形态：三个元信息字段 + 一个 _run 实现。

        name/description/args_schema 三行不是普通类属性，而是 pydantic 的"字段声明"，实例化时用关键字参数赋值。
            name         → 工具名（LLM 调用时按这个名）
            description  → 工具说明（LLM 判断"该不该调"的唯一依据）
            args_schema  → 参数模型（LLM 按它填参；CrewAI 按它校验）
        """

        name: str
        description: str
        args_schema: type          # 直接复用 LangChain tool 的 pydantic 参数模型

        def _run(self, **kwargs):
            """CrewAI 的实际执行入口（框架内部调用，我们不直接调）。

            **kwargs 是 CrewAI 按 args_schema 解析好、校验过的参数，
            例如 {"kp_id": "kp_crossmult", "depth": 2}。
            转发给原 tool 的 .invoke()：LangChain 的工具必须用 invoke 调（不能直接 f()）。
            """
            return _LC_REGISTRY[self.name].invoke(kwargs)

    adapters = []

    for lc in lc_tools:
        _LC_REGISTRY[lc.name] = lc          # t2：登记转发目标
        adapters.append(_LangChainAdapter(
            name=lc.name, description=lc.description, args_schema=lc.args_schema,
        ))
        
    return adapters
