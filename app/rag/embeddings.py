"""Embedding 单例：DashScope text-embedding-v3。

 得到的数据：LangChain Embeddings 实例
 要做的操作：全项目共用一个实例，别在循环里反复 new（有连接开销）
 维度：text-embedding-v3 输出 1024 维——Milvus collection 的 dim 必须与之一致，
      详见 config.embed_dim，两处只能改一处

 陌生概念 Embedding（向量化）：把一段文字压成一个 1024 个小数组成的数组（向量），
   文字意思越相近，向量方向越接近——"鸡吃了虫子"和"虫子被鸡吃了"方向几乎一样。
   有了向量才能做语义检索：拿用户问题的向量去库里找方向最近的片段（余弦相似度），
   这就是 RAG 检索的底层原理；关键词重合反而不重要。
"""
from functools import lru_cache

# 陌生库 langchain_community：LangChain 的"第三方集成大包"，各家模型/向量服务
#   的封装类都在这。DashScopeEmbeddings = 阿里百炼 embedding 服务的客户端封装，
#   统一成 LangChain 的 Embeddings 接口：embed_documents(批量文本→向量)、
#   embed_query(单条问题→向量)。好处：以后换 embedding 供应商只改这一处
from langchain_community.embeddings import DashScopeEmbeddings

from app.core.config import settings


@lru_cache
def get_embeddings() -> DashScopeEmbeddings:
    # key 优先级：DASHSCOPE_API_KEY 专门配了就用它；没配就兜底用 LLM 那把
    # DashScope key（.env 里的 LLM_API_KEY 就是百炼的 key，embedding/LLM 通用）
    api_key = settings.dashscope_api_key or settings.llm_api_key
    if not api_key:
        raise RuntimeError("缺 API key：请在 .env 配 LLM_API_KEY 或 DASHSCOPE_API_KEY")
    return DashScopeEmbeddings(
        model=settings.embed_model,
        dashscope_api_key=api_key,
    )
