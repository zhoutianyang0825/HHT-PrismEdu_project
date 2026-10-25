"""Embedding 双模式工厂：
    本地模型路径 → HuggingFace；
    云端模型名 → DashScope。

全项目共用一个 LangChain Embeddings实例 （lru_cache 单例），别在循环里反复 new（有连接/加载开销）

维度：两种模式的向量模型，输出都是 config.embed_dim=1024 维 （BGE-M3 与 text-embedding-v3 同维）
"""
from functools import lru_cache
from pathlib import Path

from app.core.config import settings


def _is_local_model(model: str) -> bool:
    """判断 EMBED_MODEL 是本地路径还是云端模型名。

    以 / ./ ~/ 开头 或 磁盘上真实存在的 → 本地路径；否则按云端模型名处理。
    不填/填错会怎样：路径当模型名传给 DashScope 会 404，模型名当路径会 FileNotFoundError
    """
    return model.startswith(("/", "./", "~/")) or Path(model).exists()


@lru_cache
def get_embeddings():
    """按 EMBED_MODEL 的形态自动选择 embedding 后端（结果缓存，全程只建一次，单例模式）。

    得到的数据：settings.embed_model（本地模型目录 或 云端模型名）、settings.dashscope_api_key（只有云端模式用）

    要做的操作：
        路径形态 → HuggingFaceEmbeddings（本地 BGE-M3）；
        模型名形态 → DashScopeEmbeddings（云端）；
        两种形态都要求 EMBED_MODEL 非空，缺配置直接报错。

    返回：LangChain Embeddings 实例（可 embed_documents / embed_query）。

    为什么空配置要报错而不是兜底：换向量模型就是换向量空间——
    Milvus 里存的是旧模型的向量，拿新模型去查会"不报错但结果全乱"，比直接报错难查得多。
    同理，云端模式必须用 DASHSCOPE_API_KEY：LLM_API_KEY 是 DeepSeek 的 key，喂给百炼只会拿到 401。

    陌生写法 @lru_cache：Python 内置装饰器（functools），给函数装记忆。
      第一次调用真执行函数体，之后所有的调用不执行函数体，直接返回上次的结果。
      这里用它实现单例：本地模型加载要几秒、云端客户端有连接开销，全程只建一次。
    """
    model = (settings.embed_model or "").strip()
    if not model:
        raise RuntimeError(
            "缺 EMBED_MODEL：请在 .env 里配它——"
            "本地模型填模型目录（如 /path/to/bge-m3），云端模型填模型名（如 text-embedding-v3）。"
            "这里刻意不给默认值：换向量模型就是换向量空间，静默用错模型会让检索结果悄悄变乱。"
        )

    if _is_local_model(model):  # ---- 为True，则本地模式：BGE-M3

        # 陌生类 HuggingFaceEmbeddings：langchain_community 对 transformers 模型的封装，
        from langchain_community.embeddings import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(
            model_name=model,   # model_name = 本地向量模型目录
            model_kwargs={"device": "mps"},
        )

    # ---- 云端模式：text-embedding-v3 → 走 DashScope API ----
    # DashScopeEmbeddings = 阿里百炼 embedding 服务的客户端封装
    from langchain_community.embeddings import DashScopeEmbeddings

    api_key = (settings.dashscope_api_key or "").strip()
    if not api_key:
        raise RuntimeError(
            "走云端 embedding 需要 DASHSCOPE_API_KEY："
            "不要用 LLM_API_KEY 兜底——那是 DeepSeek 的 key，喂给百炼只会拿到 401。"
        )

    return DashScopeEmbeddings(
        model=model,
        dashscope_api_key=api_key,
    )
