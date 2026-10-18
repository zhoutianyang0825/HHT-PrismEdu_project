"""C-03 · 统一 LLM 调用入口：三端 Agent 共用同一个模型实例。

 得到的数据：settings 里的 llm_base_url / llm_api_key / llm_model 三项配置
 要做的操作：get_llm() 惰性单例,第一次调用才创建实例，之后全项目复用同一个

 为什么单例：ChatOpenAI 底层是 httpx 连接池，每处都 new 一个会重复建连接池；且改模型只需改 .env 一处，不用散落各文件

"""
from functools import lru_cache

from langchain.chat_models import init_chat_model

from app.core.config import settings


# @lru_cache 单例： 第一次调用真执行函数体建实例，之后所有调用直接返回上次的结果
@lru_cache
def get_llm(temperature: float = 0.7, streaming: bool = False):
    """得到的数据：绑定好 base_url/key/model 的 ChatModel 实例。

    要做的操作：init_chat_model 按 provider 字符串实例化对应封装类。
    参数：
      temperature 采样温度：0=几乎固定输出（判分类、抽题用），越高越发散（讲解、提示用 0.3~0.7）
      streaming    是否流式输出；M5 SSE 接口时传 True，CLI 阶段用 False 最简单
    """

    llm = init_chat_model(
        settings.llm_model,             # 模型名，如 deepseek-v4-flash
        model_provider="openai",        # 走 OpenAI 协议（DashScope 兼容层）
        base_url=settings.llm_base_url, # https://dashscope.aliyuncs.com/compatible-mode/v1
        api_key=settings.llm_api_key,   # .env 里的 LLM_API_KEY
        temperature=temperature,
        streaming=streaming,
    )

    return llm
