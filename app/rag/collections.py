"""M1-T4/T5 · Milvus 入库与检索的 collection 管理层。

 两个 collection（PRD C-02 双库隔离）：
   textbook_math   教材切片，role_visible = "all"（三端可见）
   question_bank   题库条目，role_visible = "teacher_only"（教师/教研可见）

 Schema 设计（pymilvus MilvusClient）：
   id            VARCHAR(64)  主键（题库用 q_<PG id>，教材用 tb_<seq>）
   text          VARCHAR      原文
   embedding     FLOAT_VECTOR(EMBED_DIM=1024)
   kp_id         VARCHAR      题库关联知识点；教材为空串
   chapter_id    VARCHAR      章节来源
   role_visible  VARCHAR      "all" / "teacher_only" —— 检索时 expr 过滤的关键字段
   source        VARCHAR      溯源展示：教材"文件名-p页" / 题库"题号"

 ★ 坑预警（架构文档 12）：Milvus expr 是类 SQL 字符串，字符串值带双引号、数字不带；
   expr='role_visible != "teacher_only"'，写成字典语法会静默查不到东西。
"""
# 陌生库 pymilvus：Milvus 向量数据库的官方 Python 客户端。三个建库核心类：
#   FieldSchema  定义一列（名字/类型/参数）——类比 SQLAlchemy 的 mapped_column
#   DataType     列类型枚举（VARCHAR/INT64/FLOAT_VECTOR...），向量列 FLOAT_VECTOR
#   CollectionSchema  一堆 FieldSchema 打包成"表结构"——类比 Base.metadata 里的一张表
# 概念对齐：Milvus 的 collection ≈ PG 的 table；区别是它专门为"向量列 + 相似度检索"而生
from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient

from app.core.config import settings

# 检索权限过滤 expr：按角色给出白名单条件
# student → 只能看 all；teacher/researcher/admin → all + teacher_only
ROLE_EXPR = {
    "student": 'role_visible == "all"',
    "teacher": 'role_visible in ["all", "teacher_only"]',
    "researcher": 'role_visible in ["all", "teacher_only"]',
    "admin": 'role_visible in ["all", "teacher_only"]',
}


def visible_collections(role: str) -> list[str]:
    """角色 → 可见 collection 白名单（物理隔离层：student 连题库 collection 都不进）。"""
    if role in ("teacher", "researcher", "admin"):
        return [settings.collection_textbook, settings.collection_question]
    return [settings.collection_textbook]


def _schema_question() -> CollectionSchema:
    fields = [
        FieldSchema("id", DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema("text", DataType.VARCHAR, max_length=8192),
        FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=settings.embed_dim),
        FieldSchema("kp_id", DataType.VARCHAR, max_length=64),
        FieldSchema("chapter_id", DataType.VARCHAR, max_length=64),
        FieldSchema("difficulty", DataType.VARCHAR, max_length=16),
        FieldSchema("role_visible", DataType.VARCHAR, max_length=32),
        FieldSchema("source", DataType.VARCHAR, max_length=256),
    ]
    return CollectionSchema(fields, description="PrismEdu question bank")


def _schema_textbook() -> CollectionSchema:
    fields = [
        FieldSchema("id", DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema("text", DataType.VARCHAR, max_length=8192),
        FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=settings.embed_dim),
        FieldSchema("chapter_id", DataType.VARCHAR, max_length=64),
        FieldSchema("page", DataType.INT64),
        FieldSchema("role_visible", DataType.VARCHAR, max_length=32),
        FieldSchema("source", DataType.VARCHAR, max_length=256),
    ]
    return CollectionSchema(fields, description="PrismEdu textbook chunks")


def ensure_collections(client: MilvusClient) -> None:
    """建库前准备：两个 collection 不存在就创建并建向量索引（幂等，可重复调用）。"""
    for name, schema in [
        (settings.collection_question, _schema_question()),
        (settings.collection_textbook, _schema_textbook()),
    ]:
        if client.has_collection(name):
            continue
        client.create_collection(collection_name=name, schema=schema)
        # 向量索引：普通列有 B-tree 就够，向量列必须建专用索引否则检索是全量暴力扫描。
        #   index_type=AUTOINDEX：Milvus 自动挑内部索引结构（standalone 默认推荐）；
        #   metric_type=COSINE：相似度算法用余弦夹角（两个向量方向越近分数越高，
        #   1.0=完全同向）。选 COSINE 是因为语义向量的"意思相近"就体现在方向上，
        #   不在长度上。坑：这里的 metric 必须和检索时 search_params 的一致，不一致报错
        client.create_index(
            collection_name=name,
            field_name="embedding",
            index_params={"index_type": "AUTOINDEX", "metric_type": "COSINE"},
        )


def drop_collections(client: MilvusClient) -> None:
    """重建用：删两个 collection（seed 脚本 --wipe 时调用）。"""
    for name in [settings.collection_question, settings.collection_textbook]:
        if client.has_collection(name):
            client.drop_collection(name)
