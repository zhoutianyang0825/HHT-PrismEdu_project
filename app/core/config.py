# app/core/config.py
# 全局配置：从项目根的 .env 读取，所有默认值与 docker-compose.yml 对齐
# 这样不配 .env 也能连本地容器，只有 LLM_API_KEY 必须手填

# ★ pydantic-settings 是 pydantic v2 拆出来的独立包，所以 pyproject.toml 里要单独装 pydantic-settings
#   pydantic 主包只管"数据校验"（BaseModel），pydantic-settings中的 BaseSettings 具备 "自动读环境"的能力
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """★ BaseSettings vs BaseModel：

    BaseModel    = 一张空表格：字段类型定好后，值必须由你亲手传进去，它只负责校验
    BaseSettings = 会自己翻档案柜的表格：实例化时按 "初始化参数 > 环境变量 > .env 文件 > 默认值"
                   的优先级自动找值，字段名与环境变量名自动匹配（大小写不敏感）

    效果：settings.postgres_host 的值可能来自 .env、系统环境变量、默认值，
          调用方完全不用关心值从哪来。
    """

    # ---- PostgreSQL（业务表 + checkpointer 同库）----
    # ★ 类型注解在这里不只是说明，是校验器：postgres_port: int 声明后，
    #   .env 里的字符串 "5432" 会被自动转成 int；写错成 "abc" 会在启动瞬间报错（fail-fast）
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "prismedu"
    postgres_password: str = "prismedu_dev_2026"
    postgres_db: str = "prismedu"

    # ---- Neo4j 知识点图谱 ----
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "prismedu neo4j 2026"   # 密码含空格，与 compose 保持一致

    # ---- Milvus 向量库 ----
    milvus_uri: str = "http://localhost:19530"
    # 双 collection 物理隔离（PRD C-02）：教材三端可见，题库仅教师/教研/管理员
    collection_textbook: str = "textbook_math"
    collection_question: str = "question_bank"

    # ---- LLM（DashScope 兼容 OpenAI 接口）----
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""                          # 必填项，留空则自检报 FAIL
    llm_model: str = "deepseek-v4-flash"

    # ---- Embedding（M1 入库用，先占位）----
    embed_model: str = "text-embedding-v3"
    embed_dim: int = 1024                          # ★ 必须与 text-embedding-v3 输出维度一致，建 collection 时用
    dashscope_api_key: str = ""                    # embedding 直连百炼用；留空则代码里兜底用 llm_api_key（同一把 DashScope key）

    # ---- JWT / 登录安全（M1-T2）----
    secret_key: str = "change-me-in-prod"          # 生产必须换；demo 用
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720         # 一周版不发 refresh token，12h 长令牌
    password_min_length: int = 8                   # 密码强度策略：最短 8 位且必须含字母+数字
    login_max_failures: int = 5                    # 连续失败 5 次
    lockout_minutes: int = 15                      # 锁定 15 分钟


    # ★ model_config 是 pydantic v2 的配置入口（v1 的 class Config 写法已废弃）：
    #   env_file: 实例化时去读哪个文件。相对路径以"启动命令所在目录"为基准，
    #             必须在项目根 uv run；换目录跑读不到时会静默退回默认值
    #   extra:    .env 里出现类没声明的字段怎么办。BaseSettings 默认 forbid（直接报错，
    #             防止环境变量拼错字静默失效）；这里显式设 ignore，容忍暂时用不上的条目
    model_config = {"env_file": ".env", "extra": "ignore"}


    # ★ @property：把"方法"伪装成"属性"。
    #   调用时写 settings.postgres_dsn（不带括号），看着像在读字段，实际每次访问都现场执行函数体拼一次字符串。
    #   为啥不存成普通字段：dsn 依赖上面 5 个 PG 字段，存成固定值，改密码它就过期了；
    #   property 每次现拼，永远不会和字段不同步。
    @property
    def postgres_dsn(self) -> str:
        """得到的数据：标准 DSN 连接串，格式 协议://用户名:密码@主机:端口/库名
        例：postgresql://prismedu:xxx@localhost:5432/prismedu

        为为啥是这个格式：DSN（Data Source Name）是数据库界的"统一地址写法"，
        psycopg / SQLAlchemy / Alembic 全认它——一处拼好，三处复用。
        """
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url(self) -> str:
        """SQLAlchemy / Alembic 专用连接串：postgresql+psycopg://...

        为啥不是直接用 postgres_dsn：SQLAlchemy 的 postgresql:// 默认找 psycopg2 驱动，
        本项目装的是 psycopg 3，必须显式写成 postgresql+psycopg:// 才会走对驱动。
        """
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


# ★ 全局单例：本模块被 import 的瞬间执行一次 Settings()，之后全项目
#   from app.core.config import settings 拿到的都是同一个实例。
#   好处：.env 只在启动时读一次；改配置要重启进程才生效——这正是我们要的可预期性
settings = Settings()
