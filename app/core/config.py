# 全局配置：从项目根的 .env 读取，所有默认值与 docker-compose.yml 对齐
# 这样不配 .env 也能连本地容器，只有 LLM_API_KEY 必须手填


# ★ pydantic-settings 是 pydantic v2 拆出来的独立包，pydantic-settings中的 BaseSettings 具备 "自动读环境"的能力
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """ BaseSettings vs BaseModel：

    BaseModel    = 一张空表格：字段类型定好后，值必须由你亲手传进去，它只负责校验
    BaseSettings = 会自己读 系统环境变量 / .env文件：实例化时按 "初始化参数 > 系统环境变量 > .env 文件 > 默认值"的优先级自动找值，字段名与环境变量名自动匹配（大小写不敏感）

    效果：settings.postgres_host 的值可能来自 .env、系统环境变量、默认值，

    """
    # ★ model_config 是 pydantic v2 的配置入口：声明去哪读
        #   env_file:  指定实例化时 去读.env文件。 字段名与环境变量名自动匹配（大小写不敏感），比如：EMBED_MODEL的值 自动落到 embed_model 上 

        #   extra:    .env 里出现类没声明的字段怎么办。BaseSettings 默认 forbid 直接报错，这里显式设 ignore
    model_config = {"env_file": ".env", "extra": "ignore"}


    
    # ---- PostgreSQL（业务表 + checkpointer 同库）----
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



    # ---- LLM（DeepSeek 官方 API 直连，OpenAI 兼容协议）----
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""                             # 必填项：只在 .env 里配，代码里不写死；留空时 check_env 报 FAIL
    llm_model: str = "deepseek-v4-flash"



    # ---- Embedding（入库时用：本地模型目录 或 云端模型名，二选一）----
    embed_model: str = ""                             # 必填项：本地填模型目录（如 .../bge-m3），云端填模型名（如 text-embedding-v3）；空则入库直接报错
    embed_dim: int = 1024                             # 建 collection 的维度，必须与 embed_model 的输出维度一致（BGE-M3 与 text-embedding-v3 同为 1024）
    dashscope_api_key: str = ""                       # 只有走云端 embedding 才需要；DeepSeek 的 key 不能喂给百炼



    # ---- JWT / 登录安全（M1-T2）----
    secret_key: str = "change-me-in-prod"          # 生产必须换；demo 用
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720         # 一周版不发 refresh token，12h 长令牌
    password_min_length: int = 8                   # 密码强度策略：最短 8 位且必须含字母+数字
    login_max_failures: int = 5                    # 连续失败 5 次
    lockout_minutes: int = 15                      # 锁定 15 分钟

    # ---- 登录失败计数的存储后端 ----
    #   redis_url 留空 = 只用进程内存（单 worker 够用，多 worker 各记各的）
    #   填了就走 Redis，多个 worker 共享同一份计数 —— 这是挪 Redis 的唯一目的
    redis_url: str = ""                            # 形如 redis://localhost:6380/0
    login_guard_backend: str = "auto"              # auto（有 url 就用）/ redis（强制）/ memory（强制内存）

    # ---- Langfuse 观测层（监控第二层：Agent 链路） ----
    #   两个 key 留空 = 观测关闭，所有 build_callbacks() 返回空列表，零开销零报错。
    #   keys 从 http://localhost:3100 的 UI 里建项目后拿（Settings → API Keys）。
    langfuse_public_key: str = ""                  # 形如 pk-lf-...
    langfuse_secret_key: str = ""                  # 形如 sk-lf-...
    langfuse_host: str = "http://localhost:3100"   # 搬内网后改成 http://<服务器IP>:3100

    # ---- 使用周报（监控第一层的定时推送，APScheduler 内置）----
    #   四项 SMTP 任一留空 = 周报定时任务整体不启动（插销式，与观测层同一原则）
    #   授权码与 Kuma 通知里配的是同一套（邮箱后台开 SMTP 领的 16 位码）
    report_email_host: str = ""                    # 如 smtp.qq.com / smtp.163.com
    report_email_port: int = 465                   # 465=SSL（QQ/163 默认）；587=STARTTLS
    report_email_user: str = ""                    # 发件邮箱，如 xxxx@qq.com
    report_email_password: str = ""                # SMTP 授权码，不是邮箱登录密码
    report_email_to: str = ""                      # 收件邮箱，多个用逗号分隔
    report_day_of_week: str = "mon"                # cron：周几（mon/tue/... 或 0-6）
    report_hour: int = 8                           # 几点发（本地时区 Asia/Shanghai）


    

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




# ★ 全局单例：本模块被 import 的瞬间执行一次 Settings()，之后全项目 from app.core.config import settings 拿到的都是同一个实例。
settings = Settings()
