"""Embedding 双模式工厂：本地模型路径 → HuggingFace； 云端模型名 → DashScope。

 得到的数据：LangChain Embeddings 实例（两种模式对外接口完全一致）
 要做的操作：全项目共用一个实例（lru_cache 单例），别在循环里反复 new（有连接/加载开销）

 为什么双模式：
   ① 本地 BGE-M3（.env 的 EMBED_MODEL 是路径）——免 API key、免网络，Mac 上即可跑
   ② 云端 text-embedding-v3（.env 的 EMBED_MODEL 是模型名）——要百炼 DASHSCOPE_API_KEY

 维度：两种模式输出都是 1024 维（BGE-M3 与 text-embedding-v3 恰好同维），
   Milvus collection 的 dim 必须与之一致，详见 config.embed_dim，两处只能改一处

 陌生概念 Embedding（向量化）：把一段文字压成一个 1024 个小数组成的数组（向量），
   文字意思越相近，向量方向越接近——"鸡吃了虫子"和"虫子被鸡吃了"方向几乎一样。
   有了向量才能做语义检索：拿用户问题的向量去库里找方向最近的片段（余弦相似度），
   这就是 RAG 检索的底层原理；关键词重合反而不重要。
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

    陌生写法 @lru_cache：Python 内置装饰器（functools），给函数装记忆。
      第一次调用真执行函数体，之后所有调用不执行函数体，直接返回上次的结果。
      这里用它实现单例：本地模型加载要几秒、云端客户端有连接开销，全程只建一次。
    """

    
    if _is_local_model(settings.embed_model):  # ---- 为True，则本地模式：BGE-M3
        
        # 陌生类 HuggingFaceEmbeddings：langchain_community 对 transformers 模型的封装，
        from langchain_community.embeddings import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(
            model_name=settings.embed_model,   # model_name = 本地向量模型目录
            model_kwargs={"device": "mps"},   
        )

    # ---- 云端模式：text-embedding-v3 → 走 DashScope API ----
    # DashScopeEmbeddings = 阿里百炼 embedding 服务的客户端封装
    from langchain_community.embeddings import DashScopeEmbeddings

   
    api_key = settings.dashscope_api_key or settings.llm_api_key
    if not api_key:
        raise RuntimeError("缺 API key：请在 .env 配 DASHSCOPE_API_KEY")
    return DashScopeEmbeddings(
        model=settings.embed_model,
        dashscope_api_key=api_key,
    )
