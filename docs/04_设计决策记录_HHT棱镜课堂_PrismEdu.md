# 04 · 设计决策记录（ADR）— HHT 棱镜课堂 PrismEdu

> **定位**：记录项目中"特意的设计"——每个决策写清三件事：**选了什么 / 为什么 / 放弃了什么**。  
> 代码只回答"是这么写的"，本文档回答"为什么不是那么写的"。  
> **维护规则（2026-10-17 老大定稿）**：**问答驱动，不批量预填**——老大逐文件审阅代码，提出"为啥做成 A、相比 B 有何利弊"这类问题，解答后当轮记录进本文档；推翻旧决策时不删原文，在原条目下追加"❌ 已推翻（原因+日期）"。  
> **存量说明**：下方 M1 的 D1~D16 是生成代码时的预填底账，老大审阅时可逐条挑战——答不上来或答错的，修订；老大推翻的，标注已推翻。

---

## M1 · 数据与权限基座（2026-10）

### D1 · 权限采用"双通道 scope"，detail 与 aggregate 永不融合

- **背景**：教研员做学情分析需要年级粒度聚合数据，但**不能**接触任何学生明细（隐私红线，PRD C-02）。
- **决策**：`build_scopes()` 返回的 scope 列表按 `type` 分为 `detail` / `aggregate` / `self` 三种，**两条通道独立返回、互不展开**；接口侧另有守卫 `require_detail_channel` / `require_aggregate_channel`，researcher 调明细接口直接 403。
- **为什么不融合**：若把两通道合并成"一个大权限对象"，任何一处过滤逻辑写歪（比如把 aggregate 的年级范围误用到明细查询上），隐私就漏了。**类型隔离 = 隔离的第一性保证**，比"运行时仔细检查"可靠。
- **放弃的备选**：单一 scope 对象 + 字段级开关（嵌套深、容易写歪）；纯装饰器校验（守卫逻辑散落，漏一个接口就是事故）。
- **验收锚点**：单测 `test_researcher_detail_channel_403`（🔴 关键用例）。

### D2 · 角色不放在 users 表上，用 user_roles 关联表展开

- **背景**：一人可兼任多角色（如教师兼教研员），JWT 签发要拼 `roles: [...]` 数组。
- **决策**：users 表不存 role；`user_roles` 表一行一个角色，`(user_id, role)` 复合唯一。
- **为什么**：单列多值（逗号分隔）没法建索引没法约束；JSON 列没法复合唯一。关联表是关系数据库标准解，查询/约束/扩展（将来角色加有效期）都留了口。
- **代价**：多一次 JOIN。可接受——登录频次远低于查询频次。

### D3 · 角色值用 String + 应用层校验，不用 PG 枚举类型

- **决策**：`role: Mapped[str]`，合法值由应用层 `VALID_ROLES` 集合把关（JWT 签发时校验）。
- **为什么**：PG 的 `ENUM` 加一个角色要 `ALTER TYPE`，在 SQLite（单测库）上行为还不一致；String + 应用层校验跨库可移植，加角色只需改一处常量。
- **取舍**：数据库层失去枚举兜底，脏值理论上可入库——靠签发端校验 + 测试覆盖弥补。

### D4 · 级联删除配两层：relationship 的 cascade + ForeignKey 的 ondelete

- **决策**：删 User 时其 UserRole/Student 行同步删除——Python 侧 `cascade="all, delete-orphan"`，数据库侧 `ondelete="CASCADE"`。
- **为什么**：两层是不同世界的行为——cascade 只在走 SQLAlchemy 的 `session.delete()` 时生效；ondelete 在**任何**途径删父行时生效（包括 psql 手删）。只配一层总有绕过的路径，双保险才稳。
- **额外动机**：SQLite（单测库）对 ondelete 支持不完整，Python 侧 cascade 保证单测里级联也正确。

### D5 · 软删除用户：is_active=False，不物理删行

- **背景**：学生毕业/教师离职后，其历史作答数据仍要参与统计与模型训练。
- **决策**：User 停用只置 `is_active=False`（登录被拒），数据行保留。
- **放弃的备选**：物理删除 + 归档表（M1 阶段过度设计，等数据量大了再评估）。

### D6 · 模型层用 SQLAlchemy 2.0 声明式写法（Mapped / mapped_column）

- **决策**：全部 20 张表用 2.0 写法；类型注解直接当建表信息（`str | None` → 可空列）。
- **为什么**：注解 = 建表类型 + 读出类型 + IDE 提示，一式三用；与 FastAPI/Pydantic 的类型风格一致。
- **为什么不直接写 .sql 脚本**：① 同一份模型在 PG（生产）与 SQLite（单测内存库）上自动切换方言，单测不依赖 Docker；② Alembic 在 M3+ 对"模型 vs 库结构"自动 diff 生成迁移；③ 查询结果是带类型的对象；④ 参数化自动防注入。代价是学习成本前置（复杂分析 SQL 仍降级 `text()` 直写）。

### D7 · Base 登记制：所有模型必须 import 同一个 Base

- **决策**：`class Base(DeclarativeBase)` 是唯一登记处；`app/models/__init__.py` 把全部模型文件串起来 import。
- **为什么**：`Base.metadata` 是建表与迁移的唯一数据源，漏 import 的模型是"幽灵表"——create_all 和 Alembic 都看不见它。
- **由此产生的约定**：新增模型文件**必须**同步加进 `__init__.py`（checklist 项）。

### D8 · Alembic 首版迁移用 create_all 策略 + 空库校验

- **决策**：0001_initial 不手写 20 张表的 DDL，直接 `Base.metadata.create_all()`；执行前检查库中已存在业务表则报错拒绝。
- **为什么**：首版"模型即迁移"，零漂移风险；手写 20 张表 DDL 必然与模型有出入。空库校验防止 create_all 在旧库上"半途合并"出四不像结构。
- **边界**：从 0002 起改用 autogenerate diff，手写只做微调。

### D9 · engine 惰性单例：import 零副作用

- **决策**：`session.py` 不在 import 时建 engine，用 `@lru_cache` 的 `get_engine()` 惰性创建；模块级 `SessionLocal` 通过 `__getattr__` 惰性解析。
- **为什么**：import 即连库（副作用）会让"跑个单测也要装 psycopg、连不上就崩"。惰性化后：导入任何模块都安全，连不上只在真正用库那一刻报错。
- **教训来源**：M1 review 阶段单测因 import 副作用被迫装驱动，当场改掉。

### D10 · 单测用 SQLite 内存库替代真 PG

- **决策**：conftest 用 `sqlite:///:memory:`，每测一建、用完即弃。
- **前提**：模型全部使用跨库可移植类型（JSON 列在 PG 上是 JSONB、SQLite 上是 TEXT，双兼容）。
- **边界**：PG 专有特性（JSONB 索引、ondelete 细节）单测盖不住，由 seed 脚本实跑（容器环境）补充验证。

### D11 · 密码哈希直用 bcrypt 库，不用 passlib

- **决策**：`bcrypt.hashpw/checkpw` + 5 次失败锁定（默认进程内计数，可切 Redis）。
- **为什么**：passlib 1.7.4 已停更且与 bcrypt 5.x 不兼容（`__about__` 被移除，产生告警），2026 年新项目没有理由再引入这层过时包装。
- **已知边界 → 已落地（2026-10-27，见 D11-b）**：失败锁定计数原先只在进程内存里，多 worker 部署时各自计数。现已抽出 `app/core/login_guard.py`，配 `REDIS_URL` 即走 Redis ZSET，多 worker 共享同一份计数；没配或 Redis 挂了自动退回内存。

### D11-b · 登录失败计数：ZSET 滑动窗口 + 降级到内存（2026-10-27）

- **决策**：
  1. 窗口语义**不变**——只数最近 `lockout_minutes`（15 分钟）内的失败，用 Redis **ZSET**（member=失败时刻+uuid，score=时间戳）实现：`ZREMRANGEBYSCORE` 剪枝 + `ZADD` 记账 + `ZCARD` 数数，一条 pipeline 走完。
  2. Redis 不可用时**降级到进程内存**并打 warning，冷却 60 秒后再试；登录接口**永不因 Redis 故障返回 5xx**。
- **为什么不用 INCR + EXPIRE**：那是固定窗口——从第一次失败起算 15 分钟，到点整块清零。用户在第 14 分钟失败 4 次、第 16 分钟又能白拿 4 次机会，防护变松。ZSET 存的是"每次失败发生在什么时候"，剪枝后与原实现逐条等价（`tests/test_login_guard.py` 用 5 组序列对照两个后端，判定完全一致）。
- **为什么降级而不是 fail-closed**：校园场景峰值 3000 人登录，Redis 抖 10 秒就全校进不来，代价远大于"防护退化到单 worker 水准"。退化后仍等于改造前的水平，不是"没有防护"。
- **为什么 member 要带 uuid**：ZSET 成员唯一，同一微秒的两次失败若用时间戳当 member，第二条只会更新 score 而不新增 → 第 5 次被吞、永不锁定。高并发爆破完全可能撞同一微秒。
- **踩过的坑**：`get_guard()` 在持有 `_state_lock` 时调 `_demote()`（内部再抢同一把非重入锁）= 自死锁，单测表现为 pytest 永不返回。已拆成 `_demote_locked()`（调用方持锁）+ `_demote()`（自己拿锁）。
- **仍未做（诚实声明）**：锁定只按 username，攻击者可用错误密码把任意账号锁 15 分钟（DoS）。要治需"按 IP 限流 + 账号锁定只惩罚来源 IP"，属另一需求。

### D12 · 题库正确性：答案公式现算 + 断言自校验，不手抄

- **决策**：`tools/gen_question_bank.py` 所有数值题答案由公式现算，出题参数用 assert 代回验证（Δ 完全平方、扇形面积代回、根代回方程），任一断言炸掉就拒绝生成 CSV。
- **为什么**：手抄答案必错（M1 开发中真实发生过：`(2,-5,2)` 的根 0.5 被 `//` 截断成 0，被断言逮住）。**数据正确性靠生成时保证，不靠人工核对**。
- **原则沉淀**：凡能计算的绝手写，凡能断言的必断言。

### D13 · 跨库写入顺序：PG 先、Milvus 后

- **决策**：seed_question_bank 先在 PG 一个事务写完（chapters/KPs/questions），commit 成功后才向 Milvus 灌向量。
- **为什么**：PG 是事实源（source of truth），事务回滚干净；Milvus 数据可从 PG 全量再生，失败删了重灌即可。反过来（Milvus 先写）会产生"向量库有、事实库无"的孤儿数据，排查困难。

### D14 · Milvus 向量切片 id 带源文件指纹

- **决策**：教材切片 id = `tb_{文件名md5前8位}_{序号}`，而非纯序号。
- **为什么**：序号每次调用从 0 数起，灌第二个 PDF 必然撞主键；Milvus 允许重复主键不报错，后果是检索出重复结果——静默的数据污染。加指纹后天然全局唯一。
- **教训来源**：M1 review 阶段发现，当场修掉。

### D15 · RAG 检索结果强制带溯源（source）

- **决策**：两个 collection 的每条记录都存 `source`（文件名+页码 / 题号），retriever 返回的每条 hit 必带。
- **为什么**：PRD C-02 验收硬指标"答案可溯源"；且题库数据对学生物理不可见，教师侧检索需要连解析一起呈现。
- **对应约束**：`output_fields` 按 collection 分别指定（Milvus 对 schema 外字段直接报错），不搞通用大而全列表。

### D16 · Embedding 维度单一事实源：config.embed_dim

- **决策**：`embed_dim=1024` 只在 config 定义一次；collection 建库、ingest 校验都引用它。
- **为什么**：模型换了维度变了，若两处写死必有一处忘改——Milvus 建库维度不匹配的报错发生在灌数时，离案发现场很远，排查成本高。

---

### D17 · 时间戳列用 server_default，业务默认值用 default（2026-10-18 老大审 user.py 时问出）

- **问答原点**：老大审 `user.py` 问"default vs server_default 的区别是啥"。
- **决策**：`created_at` 等时间戳列一律 `server_default=func.now()`（数据库侧填值，写进 DDL 的 DEFAULT 子句）；`is_active=True` 这类业务默认值用 `default=`（SQLAlchemy 插入时填）。
- **为什么**：server_default 对**任何**插值途径生效（psql 手插、别的语言、运维脚本）；default 只在走 SQLAlchemy 时生效，手插行会得到 NULL。时间戳的完整性要求"谁插都有"，交给数据库最稳。
- **代价/边界**：server_default 的值只能是数据库能算的（now()、常量），做不了"根据 Python 配置动态定"——这类需求回退到 default。

---

### D18 · seed 的 --wipe 用 TRUNCATE CASCADE，不逐表 DELETE（2026-10-18 实跑踩坑）

- **背景**：首次实跑 `seed_question_bank.py --wipe` 报 ForeignKeyViolation——库里已有上次灌的 185 题，且 student_answers 等子表有测试数据引用 questions。
- **决策**：wipe 改为 `TRUNCATE questions, knowledge_points, chapters RESTART IDENTITY CASCADE`。
- **为什么**：引用 questions/kps 的子表多达 6 张（student_answers/error_logs/bkt_state/diagnostic_quizzes/homework_questions/mastery_records），逐表 DELETE 既啰嗦又必漏；CASCADE 让 PG 沿 FK 依赖链自动截断，RESTART IDENTITY 顺便归零自增序列（重灌后 id 与 CSV 干净对齐）。
- **放弃的备选**：给所有子表 FK 加 ondelete="CASCADE"——业务表级联删除是**业务语义**（学生删了作答要不要删？），不能为 seed 方便而放宽。

### D19 · pymilvus 3.x 的 create_index 必须用 IndexParams 对象（2026-10-18 实跑踩坑）

- **背景**：传 dict 报 `expected IndexParams, got dict`（项目装的是 pymilvus 3.0.2）。
- **决策**：统一用 `client.prepare_index_params()` 造对象 + `add_index(field_name=...)` 填参。
- **为什么**：pymilvus 3.x 收紧类型校验；网上大量教程是 2.x 的 dict 写法，照抄即炸。**升级依赖时 API 签名要重验，不能信记忆**。

### D20 · Milvus collection 建库时必须 load，search 只认内存态（2026-10-18 实跑踩坑）

- **背景**：insert + flush 完成后自检检索报 `collection not loaded`。
- **决策**：`ensure_collections()` 里建完索引立即 `load_collection(name)`（幂等）。
- **为什么**：Milvus 数据分"已落盘"与"已加载进内存"两态——insert/flush 只到前者，search 只认后者。这个两态模型是 Milvus 与 PG 最不同的心智差异。
- **代价**：load 占内存（standalone 单机无所谓）；insert 不需要 load。

### D21 · Embedding 双模式工厂 + key 与供应商绑死（2026-10-18 实跑踩坑）

- **背景**：401 InvalidApiKey——.env 的 LLM_API_KEY 是 DeepSeek 官方 key（LLM_BASE_URL=deepseek），兜底逻辑却拿它调阿里百炼。
- **决策**：① embeddings.py 改双模式：EMBED_MODEL 是本地路径 → HuggingFaceEmbeddings（sentence-transformers，MPS 加速）；是云端模型名 → DashScopeEmbeddings（需 DASHSCOPE_API_KEY）。② key 兜底注释明确警告：llm_api_key 只有真是百炼 key 才兜得动。
- **为什么**：本地 BGE-M3 免 key 免网络（Mac M4 跑 185 题向量化约 3 分钟），且与云端 text-embedding-v3 恰好同为 1024 维——collection 建库参数零改动。
- **原则沉淀**：**key 不是通用密码，是供应商绑定凭证**——base_url 指向谁，key 就必须是谁家的。

---

## M2+ · （待里程碑结束时追加）
