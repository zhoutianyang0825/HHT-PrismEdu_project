"""M1-T5 · RAG Retriever：双 collection + role 过滤 + 溯源返回。

 检索流程（架构文档 7.2）：
   角色 → 可见 collection 白名单（物理隔离）
        → query 向量化（DashScope text-embedding-v3，1024 维）
        → Milvus search(expr=角色过滤)     ← expr 是类 SQL 字符串，不是字典！
        → 多 collection 合并 → 按相似度排序 → 取 top k，带 source 出处

 权限双层（PRD 2.3）：
   第 1 层  collection 白名单：student 连题库 collection 都不会发起检索
   第 2 层  字段级 expr：题库内 role_visible 标记，双保险
"""
from dataclasses import dataclass, field

from pymilvus import MilvusClient

from app.core.config import settings
from app.rag.collections import ROLE_EXPR, visible_collections
from app.rag.embeddings import get_embeddings


# 陌生写法 @dataclass：Python 标准库的"数据类"装饰器——只声明字段类型，
#   自动生成 __init__ / __repr__ / __eq__，省掉手写样板代码。专装纯数据、没业务方法
@dataclass
class RagHit:
    """单条检索结果：text 是内容，source 是溯源（C-02 验收硬指标）。"""
    text: str
    source: str
    collection: str
    score: float                                # COSINE 相似度，越大越相关
    # 陌生写法 field(default_factory=dict)：可变默认值的正确姿势。
    #   直接写 meta: dict = {} 是 Python 著名大坑——所有实例共享同一个 dict；
    #   default_factory=dict 表示"每次创建实例时现场调 dict() 造一个新空字典"
    meta: dict = field(default_factory=dict)    # kp_id / page / difficulty 等


# 每个 collection 的 schema 字段不同，output_fields 必须按库给——
# 向教材库要 kp_id / 向题库要 page 都会因字段不存在直接报错（review 修复项）
COLLECTION_OUTPUT_FIELDS = {
    settings.collection_textbook: ["text", "source", "chapter_id", "page"],
    settings.collection_question: ["text", "source", "kp_id", "chapter_id", "difficulty"],
}


def retrieve(client: MilvusClient, query: str, role: str,
             top_k: int = 5, kp_id: str | None = None,
             difficulty: str | None = None) -> list[RagHit]:
    """按角色检索，返回带出处的片段列表。

    得到的数据：query 用户问题原文；role JWT 里的角色（student/teacher/researcher/admin）
    要做的操作：query 向量化 → 白名单 collection 逐个带 expr 检索 → 合并排序截断
    参数：
      kp_id       只对题库生效——S-05/S-10 按知识点搜同类题时用
      difficulty  只对题库生效——S-10 按掌握度定档抽题时用
      role 传未知值 → 按最严的 student 处理（fail-closed：宁紧勿松）
    """
    # embed_query(单条) vs embed_documents(批量)：Embeddings 接口的两个入口，
    #   检索问题用前者、批量入库用后者。★ 铁律：query 和文档必须用同一个
    #   embedding 模型向量化——不同模型出的向量不在一个空间里，余弦相似度无意义
    query_vector = get_embeddings().embed_query(query)

    # 陌生点 dict.get(k, 默认值)：键不存在时返回默认值而不是 KeyError——
    #   这里 role 传了个未知值就退化成 student 的最严 expr（fail-closed 设计）
    expr = ROLE_EXPR.get(role, ROLE_EXPR["student"])
    # kp_id / difficulty 是字符串等值过滤，值必须带双引号（Milvus expr 语法坑）
    extra = []
    if kp_id:
        extra.append(f'kp_id == "{kp_id}"')
    if difficulty:
        extra.append(f'difficulty == "{difficulty}"')
    if extra:
        expr += " and " + " and ".join(extra)

    hits: list[RagHit] = []
    for coll in visible_collections(role):
        out_fields = COLLECTION_OUTPUT_FIELDS[coll]
        results = client.search(
            collection_name=coll,
            data=[query_vector],        # 要拿去比对的向量；外面套 list 是因为支持一次传多条 query
            anns_field="embedding",     # anns = ANN(Approximate Nearest Neighbor 近似最近邻)，指明用哪个向量列比
            limit=top_k,
            filter=expr,                # 标量过滤：先按 expr 圈行，再在圈里做向量检索
            output_fields=out_fields,   # 检索结果要顺带带回哪些标量列（向量列不回）
            # metric 必须和建索引时一致（都是 COSINE），不一致直接报错
            search_params={"metric_type": "COSINE"},
        )
        # 返回结构是两层嵌套：results[每条query][每条命中]，单 query 所以只有 results[0]
        for res in results:
            for r in res:
                ent = r["entity"]           # entity：命中的那行的标量字段们（output_fields 指定的）
                hits.append(RagHit(
                    text=ent.get("text", ""),
                    source=ent.get("source", ""),
                    collection=coll,
                    score=float(r["distance"]),   # COSINE 相似度分数，越大越像
                    # 字典推导式白名单过滤：entity 里可能有本库没有的空字段，只挑有用的装 meta
                    meta={k: v for k, v in ent.items()
                          if k in ("kp_id", "chapter_id", "page", "difficulty")},
                ))
    # 两个库的结果混在一个列表，按分数统一排序（key=lambda 取每项的 score 字段做排序键）
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


def format_context(hits: list[RagHit]) -> str:
    """检索结果 → 拼进 Prompt 的上下文，带 [1][2] 引用编号（架构文档 7.3）。"""
    lines = []
    for i, h in enumerate(hits, start=1):
        lines.append(f"[{i}] （{h.source}）{h.text}")
    return "\n\n".join(lines)
