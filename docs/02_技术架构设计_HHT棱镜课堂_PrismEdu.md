# HHT棱镜课堂 HHT-PrismEdu · 技术架构设计文档

> 名字寓意：三端共用同一份掌握度数据，棱镜把同一束光折射成学生、老师、教研三个切面。

| 项    | 内容                                                                                                                                                                                          |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 项目名  | HHT棱镜课堂 / HHT-PrismEdu                                                                                                                                                                          |
| 文档版本 | v1.6（v1.6 变更：SQLite → **PostgreSQL**，checkpointer → **PostgresSaver**（与业务表同库）；新增安全基线 4 项现在做（PRD 2.4）；备份决策：一周版不做、README 声明边界，生产化前补（见 C-04））                                                                                       |
| 创建日期 | 2026-10-11（v1.1 变更：掌握度算法由加权公式换为 BKT；v1.2 变更：新增 S-10 测验生成、R-08 试卷生成；**v1.3 变更：向量库 Chroma → Milvus standalone，checkpointer SqliteSaver → MemorySaver，新增 4.4 长期记忆 InMemoryStore 列为 D3 可选加分项**） |
| 配套文档 | `01_PRD_HHT棱镜课堂_PrismEdu.md`                                                                                                                                                                |
| 交付周期 | 2026-10-13 ~ 10-23（工作日晚 3h × 4 + 周末 5h × 2 + 全天 × 5，合计约 62h）                                                                                                                                                         |
| 项目路径 | ~/zty-Developer/HHT-PrismEdu_project                                                                                                                                                            |

---

## 一、技术选型总览

| 层次         | 选型                              | 版本建议                      | 选型理由                                                                           |
| ---------- | ------------------------------- | ------------------------- | ------------------------------------------------------------------------------ |
| Agent 编排   | **LangGraph**                   | langgraph 最新稳定版           | 三端主干框架。状态机天然适合引导式教学和人机协同批改，`interrupt` 机制直接对应业务里的"等学生再试一次"和"等老师确认分数"           |
| 多 Agent 协作 | **CrewAI**                      | crewai 最新稳定版              | 教研端两个 crew：`analysis_crew`（R-04 考后分析三角色）与 `exam_crew`（R-08 出卷三角色），体现多 Agent 分工 |
| LLM        | **DeepSeek**                    | deepseek-v4-flash         | 经 DashScope 兼容 OpenAI 接口调用，与既有调用方式一致                                           |
| Embedding  | **DashScope text-embedding-v3** | text-embedding-v3         | 中文效果好，已有使用经验                                                                   |
| 向量库        | **Milvus standalone**           | pymilvus + langchain-milvus | Docker 单机版自带 etcd + MinIO；已有使用经验，一周内零运维成本                                          |
| 知识图谱       | **Neo4j**                       | 5.26 社区版（OrbStack Docker） | 知识点前置关系是多跳查询，图数据库比邻接表优雅得多                                                      |
| 关系存储       | **PostgreSQL 16**               | Docker 容器                 | 生产级同构，代码生成前切换成本≈0；Alembic 从第一张表管迁移；checkpointer 与业务表同库（一个备份方案覆盖全部状态） |
| API 框架     | **FastAPI**                     | fastapi + uvicorn         | 异步支持 SSE 流式，自带 OpenAPI 文档便于调试                                                  |
| 流式输出       | **SSE**                         | sse-starlette             | Agent 引导过程需要打字机效果                                                              |
| 结构化输出      | **Pydantic v2**                 | pydantic 2.x              | 错因归因、批改建议的 JSON schema 约束                                                      |
| 短期记忆（checkpointer） | **PostgresSaver**               | langgraph-checkpoint-postgres | 多轮引导与 interrupt 挂起状态**跨进程持久**（重启不丢），与业务表同库，零额外运维（详见 4.3）              |
| 长期记忆（可选）       | **InMemoryStore**               | langgraph 内置                 | 学生画像跨 thread 共享，由 PostgreSQL 兜底预热（详见 4.4，D3 尾巴可选；升级路径 PostgresStore 一行）           |
| 数据处理       | **Pandas + NumPy**              | pandas / numpy            | 试卷分析统计计算                                                                       |
| 前端         | **原生 ES Modules（`web_app/`）** | 零构建、零 npm；自写 `serve.py` 静态服务 | 四端 SPA + Hash 路由 + 毛玻璃视觉；**不占 Python 依赖**（`streamlit` / `plotly` 已随 M5-T4 演示端于 2026-10-26 一并移除） |
| 包管理        | **uv**                          | uv                        | 依赖安装与环境隔离                                                                      |

---

## 二、系统分层架构

```
┌──────────────────────────────────────────────────────────┐
│  接入层  web_app（原生 ES Modules，四端：学生 / 老师 / 教研 / 管理）│
│           + FastAPI OpenAPI 文档（调试用）                  │
└───────────────────────────┬──────────────────────────────┘
                            │ HTTP REST + SSE
┌───────────────────────────▼──────────────────────────────┐
│  API 层  FastAPI                                          │
│    /api/student  ·  /api/teacher  ·  /api/research         │
│    JWT 鉴权 + 角色过滤                                     │
└───────────────────────────┬──────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────┐
│  Agent 编排层                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌────────────┐ │
│  │ 学生 Agent      │  │ 批改 Agent      │  │ 教研团队    │ │
│  │ LangGraph 主图   │  │ LangGraph       │  │ CrewAI ×2  │ │
│  │ (interrupt 引导) │  │ (interrupt 确认) │  │ analysis + │ │
│  │ + 测验生成子图   │  │                 │  │ exam       │ │
│  └────────┬────────┘  └────────┬────────┘  └─────┬──────┘ │
│           └──────────┬─────────┴─────────────────┘       │
│                 共享 Tool 层（三端复用）                    │
│                 query_knowledge_graph / query_student_    │
│                 records / search_question_bank / ...      │
└───────────────────────────┬──────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────┐
│  服务层  mastery(BKT 掌握度) / grading(判分) /              │
│          selection_engine(组卷，三处共用) / analysis(统计)  │
│          rag(检索) / auth(鉴权)                            │
└───────────────────────────┬──────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────┐
│  数据层  PostgreSQL(业务表+checkpoint)  │  Neo4j(知识点图谱)  │  Milvus(向量) │
└──────────────────────────────────────────────────────────┘
```

**分层约束**：Agent 层不直接碰数据库，一律通过 Tool 层或服务层访问。这样同一个 Tool 既能被 LangGraph 的 `bind_tools` 调用，也能被 CrewAI 的 `BaseTool` 包装复用，避免三套重复实现。

---

## 三、数据模型设计

### 3.1 PostgreSQL 业务表

| 表名                     | 核心字段                                                                                                           | 说明                                                                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| users                  | id, username, password_hash, name, is_active                                                                   | **不存 role 字段**（v1.5：角色移到 user_roles 多对多）；is_active=False 即停用，保留历史作答数据 |
| **user_roles**         | id, user_id, role                                                                                              | **多角色多对多**，role ∈ {student, teacher, researcher, admin}；JWT 签发 `roles: [...]` 数组 |
| classes                | id, grade, name, head_teacher_id                                                                               | 班级                                                                                                              |
| students               | id, user_id, class_id, student_no                                                                              | 学生档案                                                                                                            |
| **teaching_assignments** | id, teacher_id, class_id, subject                                                                            | **teacher 角色的明细 scope 数据源**（任教关系）；换班/兼课改这张表，权限即时生效                                                             |
| **research_leads**     | id, user_id, grade, subject                                                                                    | **researcher 角色的任命表**；"教研组 = 年级 × 学科"不建实体组表，组长由这张表任命产生。勾 researcher 角色必须先有任命，否则空 scope 登录处处 403 |
| teachers               | id, user_id, title, joined_at                                                                                  | 教师档案（职称等静态信息）；**班级与学科关系一律看 teaching_assignments**                                                              |
| questions              | id, subject, chapter_id, kp_id, question_type, difficulty, content, answer, analysis, source, **quality_flag** | 题库。question_type ∈ {choice, fill, subjective}；**quality_flag ∈ {normal, abnormal, disabled}，由 R-04 写回、R-08 消费** |
| homeworks              | id, teacher_id, class_id, title, kp_ids, created_at                                                            | 作业主表                                                                                                            |
| homework_questions     | id, homework_id, question_id, score                                                                            | 作业题目关联与分值                                                                                                       |
| student_answers        | id, source_type, source_id, question_id, student_id, content, is_correct, score, attempt_count, submitted_at   | 作答记录。**source_type ∈ {homework, quiz, exam}**，让三处作答共用一张表                                                        |
| grading_records        | id, answer_id, grader_type, ai_suggested_score, ai_comment, final_score, final_comment, status                 | status ∈ {pending, confirmed, rejected}                                                                         |
| **bkt_state**          | student_id, kp_id, p_known, last_updated                                                                       | **BKT 当前状态**，每个学生每个知识点一行，实时更新最重要的表                                                                              |
| mastery_records        | id, student_id, kp_id, mastery_value, updated_at                                                               | 掌握度时序快照，每次更新追加一行，画趋势线用                                                                                          |
| error_logs             | id, answer_id, error_type, confidence, evidence, explanation_strategy                                          | 错因归因结果                                                                                                          |
| **diagnostic_quizzes** | id, student_id, target_points_json, question_ids, generated_reason, created_at                                 | **S-10 诊断测验**。target_points_json 存 Top5 知识点及其 p_known 与入选原因（可解释性留痕）                                             |
| exam_papers            | id, title, grade, **exam_type, quota_plan_json, status, created_by**, created_at                               | 试卷。exam_type ∈ {monthly, midterm, final}；quota_plan_json 存 R-08 生成的配额表（可打印、可人工改）                                |
| audit_logs             | id, user_id, action, target, created_at                                                                        | 简化合规日志                                                                                                          |

### 3.2 Neo4j 图模型

**节点**：

| 标签             | 属性                                     | 示例            |
| -------------- | -------------------------------------- | ------------- |
| Chapter        | id, name, subject, order               | 一元二次方程        |
| KnowledgePoint | id, name, chapter_id, difficulty       | 因式分解法、配方法、判别式 |
| Question       | id, question_id（关联 PG）, difficulty | 题目索引，便于图上做推荐  |


**关系**：

| 类型               | 起点 → 终点                         | 说明                            |
| ---------------- | ------------------------------- | ----------------------------- |
| BELONGS_TO       | KnowledgePoint → Chapter        | 知识点归属章节                       |
| HAS_PREREQUISITE | KnowledgePoint → KnowledgePoint | 前置依赖（多跳查询的核心）                 |
| TESTS            | Question → KnowledgePoint       | 题目考察的知识点                      |
| MASTERY          | Student → KnowledgePoint        | BKT 掌握度，属性 p_known、updated_at |

**关键 Cypher 示例**：

```cypher
// 1. 追溯某知识点的全部前置链路（最多 5 跳）
MATCH path = (kp:KnowledgePoint {id: $kp_id})<-[:HAS_PREREQUISITE*1..5]-(pre)
RETURN [n IN nodes(path) | n.name] AS chain, length(path) AS depth
ORDER BY depth

// 2. 查某章所有知识点及其前置数
MATCH (c:Chapter {id: 'ch_quadratic'})<-[:BELONGS_TO]-(kp)
OPTIONAL MATCH (kp)-[:HAS_PREREQUISITE]->(pre)
RETURN kp.name, count(pre) AS prereq_count

// 3. 写回 BKT 掌握度（MERGE 保证幂等）
MATCH (kp:KnowledgePoint {id: $kp_id})
MERGE (kp)<-[m:MASTERY {student_id: $student_id}]-(s:Student {id: $student_id})
SET m.p_known = $value, m.updated_at = datetime()

// 4. 按知识点聚合作答情况（T-03 班级错题汇总）
MATCH (q:Question)-[:TESTS]->(kp:KnowledgePoint)
WHERE q.question_id IN $question_ids
RETURN kp.id, kp.name, count(*) AS wrong_count
ORDER BY wrong_count DESC LIMIT 3

// 5. 【S-10】错题知识点 1~2 跳前置外扩，得到诊断候选知识点集
MATCH (e:KnowledgePoint)<-[:HAS_PREREQUISITE*1..2]-(p:KnowledgePoint)
WHERE e.id IN $error_kp_ids
RETURN DISTINCT p.id AS kp_id, p.name AS name

// 6. 【R-08】按章节算知识点覆盖率（摸底型出卷的硬指标）
MATCH (c:Chapter {subject: 'math'})<-[:BELONGS_TO]-(kp:KnowledgePoint)
WITH count(DISTINCT c) AS total_chapter,
     count(DISTINCT kp) AS total_kp
MATCH (c2:Chapter)<-[:BELONGS_TO]-(kp2:KnowledgePoint)
WHERE kp2.id IN $selected_kp_ids
RETURN total_chapter, total_kp,
       count(DISTINCT kp2) * 1.0 / total_kp AS kp_coverage,
       count(DISTINCT c2) * 1.0 / total_chapter AS chapter_coverage
```

> 一周版建议：掌握度**三处同步**。PostgreSQL `bkt_state` 存当前状态，`mastery_records` 存时序快照（画趋势线用），Neo4j `MASTERY` 关系做图分析侧副本。

### 3.3 掌握度状态模型（BKT）

**选型理由**：每个知识点只有 4 个参数，是 K12 智能辅导系统的经典标配模型。相比自研加权公式，它可解释、经得起追问，且不需要预先训练数据。

**四个参数**（先用领域默认值，数据攒够后再用 EM 算法拟合）：

| 参数      | 含义             | 默认值  | 调节建议               |
| ------- | -------------- | ---- | ------------------ |
| p_init  | 初始掌握概率（先验）     | 0.10 | 统一初值               |
| p_learn | 一次练习后由不会转为会的概率 | 0.30 | 难点调低，容易的一次就会的知识点调高 |
| p_slip  | 会但做错（失误）       | 0.10 | 由错因动态替换，见下表        |
| p_guess | 不会但蒙对          | 0.20 | 选择题偏高，解答题调到 0.05   |

**更新三步走**：遗忘衰减 → 贝叶斯后验修正 → 叠加学习效应。

```python
import math

# BKT 四个参数的领域常用默认值
# p_init 初始掌握概率 / p_learn 一次练习学会的概率
# p_slip 会但做错的概率 / p_guess 不会但蒙对的概率
DEFAULT_PARAMS = {"p_init": 0.10, "p_learn": 0.30, "p_slip": 0.10, "p_guess": 0.20}

# 错因到 slip 的映射：粗心类错误不该被当成"不会"
# 例：{"粗心": 0.45} 表示这次答错有 45% 概率属于发挥失误而非没掌握
SLIP_BY_ERROR = {"概念不清": 0.05, "方法不会": 0.10, "粗心": 0.45, "审题失误": 0.35}

LAMBDA = math.log(2) / 7   # 遗忘系数，半衰期 7 天


def update_mastery(p_known, is_correct, error_type, days_since_last,
                   params=DEFAULT_PARAMS):
    """一次作答后的 BKT 三步更新。这是整个系统的心脏。

    得到的数据：
      p_known          上一次的掌握概率，类似 0.72
      is_correct       本次是否答对，True / False
      error_type       错因，类似 "粗心"；答对时传 None
      days_since_last  距上次作答天数，类似 3.0
      params           四个 BKT 参数，默认 DEFAULT_PARAMS

    要做的操作：遗忘衰减 → 贝叶斯后验修正 → 叠加学习效应
    返回：新的掌握概率，落在 0~1 之间
    """
    # 答错时按错因取 slip；答对时用默认 slip
    p_slip = params["p_slip"]
    if not is_correct and error_type:
        p_slip = SLIP_BY_ERROR.get(error_type, params["p_slip"])

    # 第一步：遗忘衰减，久不练习掌握度自然回落
    p_decayed = p_known * math.exp(-LAMBDA * days_since_last)

    # 第二步：贝叶斯后验。答对抬升 P，答错压低 P，幅度受 slip 与 guess 约束
    if is_correct:
        num = p_decayed * (1 - p_slip)
        den = num + (1 - p_decayed) * params["p_guess"]
    else:
        num = p_decayed * p_slip
        den = num + (1 - p_decayed) * (1 - params["p_guess"])
    p_posterior = num / den if den > 0 else p_decayed   # 防除零

    # 第三步：练习本身带来学习效应，向"会"推进一小步
    return p_posterior + (1 - p_posterior) * params["p_learn"]
```

**错因与 BKT 的联动**：错因不直接改写掌握度数值，只替换本次的 p_slip 参与贝叶斯更新，避免同一个数字被两套规则同时修改。

| 错因   | 本次 p_slip | 含义                  |
| ---- | --------- | ------------------- |
| 概念不清 | 0.05      | 确实没掌握，几乎不留余地地压低 P   |
| 方法不会 | 0.10      | 默认值，走正常未掌握路径        |
| 粗心   | 0.45      | 视为"会但算错"，P 下降幅度明显放缓 |
| 审题失误 | 0.35      | 不因读错题就重判为完全不会       |

**必须写成单测的验证序列**（BKT 出问题全链路数字都不可信）：

| 场景        | 起始 P | 序列         | 期望结果             |
| --------- | ---- | ---------- | ---------------- |
| 连答对       | 0.10 | 对 ×3（每天一题） | 单调上升，三次后到 0.6 以上 |
| 连答错（概念不清） | 0.70 | 错 ×3       | 明显下降             |
| 答错但错因粗心   | 0.70 | 错 ×1（粗心）   | 下降幅度显著小于"概念不清"   |
| 长期不练      | 0.80 | 30 天无作答    | 衰减到 0.1 以下       |

第三行是整个联动设计的验证点：同样答错一道题，"粗心"比"概念不清"的掌握度惩罚要轻得多。

---

## 四、LangGraph 学生 Agent 设计

### 4.1 State 定义

```python
class StudentAgentState(TypedDict):
    # 身份与上下文
    student_id: str
    question_id: str
    # 多轮对话
    messages: Annotated[list, add_messages]   # LangGraph 消息累加器
    # 路由决策
    intent: str                                # ask_answer / ask_hint / submit_answer / ask_concept / off_topic
    # RAG 检索结果
    retrieved_context: str                     # 教材片段，带出处
    # 图谱
    current_kp: str                            # 本题考察的知识点 id
    weak_prerequisites: list[str]              # 追溯出的前置薄弱点名称
    # 引导状态
    attempt_count: int                         # 已引导轮数，上限 3
    hint_level: int                            # 提示阶梯：1 方向 → 2 步骤 → 3 关键公式
    # 作答与判分
    current_answer: str
    is_correct: bool
    # 归因结果
    error_attribution: dict
    similar_question_id: str
    # ↓↓↓ v1.2 新增：S-10 诊断测验生成
    quiz_mode: bool                            # True 表示本次请求是"生成测验"而非单题作答
    target_points: list[dict]                  # Top5 目标知识点 [{kp_id, name, p_known, reason}, ...]
    generated_question_ids: list[str]          # 最终选出的题目 id 列表
```

### 4.2 状态图结构

```
           ┌─────────────┐
           │   START     │
           └──────┬──────┘
                  ▼
        ┌────────────────────┐
        │  classify_intent   │  LLM 结构化输出识别学生意图
        └─────────┬──────────┘
                  ▼
        ┌────────────────────┐
        │ route_by_intent    │  条件边（add_conditional_edges）
        └─────┬─────┬────┬───┘
    ask_answer│     │submit_answer   └─ ask_concept
              ▼     ▼                   ▼
   ┌───────────────┐ ┌──────────────┐ ┌──────────────────┐
   │guide_scaffold │ │ grade_answer │ │ retrieve_context │
   │ attempt_count │ │  客观题规则判分│ │   RAG 检索教材    │
   │ +1, hint_level│ └──────┬───────┘ └────────┬─────────┘
   │ 按阶梯给提示    │        │                  ▼
   └───────┬───────┘        │         ┌──────────────────┐
           ▼                │         │  explain_concept │
   ┌────────────────┐       │         └────────┬─────────┘
   │  INTERRUPT     │       │                  │
   │ 挂起等学生再输入  │       │                  │
   └───────┬────────┘       │                  │
      Command(resume=...)   │                  │
           └────────────────┤                  │
                            ▼                  │
                  ┌───────────────────┐        │
                  │ is_correct?       │        │
                  └───┬───────────┬───┘        │
               False  │           │ True       │
                      ▼           │            │
          ┌─────────────────────┐ │            │
          │attribute_error      │ │            │
          │ Pydantic structured │ │            │
          └──────────┬──────────┘ │            │
                     ▼            │            │
          ┌─────────────────────┐ │            │
          │trace_prerequisite   │ │            │
          │ Neo4j 多跳查前置      │ │            │
          └──────────┬──────────┘ │            │
                     ▼            │            │
          ┌─────────────────────┐ │            │
          │recommend_similar    │ │            │
          │ 按 kp 从题库推同类题   │ │            │
          └──────────┬──────────┘ │            │
                     ▼            ▼            ▼
              ┌──────────────────────────────────┐
              │        update_mastery            │
              │  BKT 三步更新，错因替换 slip      │
              │  三处同步：bkt_state + 快照 + 图  │
              └──────────────┬───────────────────┘
                             ▼
                     ┌───────────────┐
                     │    respond    │
                     └───────┬───────┘
                             ▼
                           END
```

### 4.3 S-10 测验生成子图（独立 StateGraph，从主图旁路调用）

测验生成不嵌在单题作答主图里，单独建一个 `quiz_graph`，避免把主图状态撑爆。入口由 API 直接触发，生成完成后把题目列表交给主图进入作答循环。

```
┌──────────────────┐
│ fetch_error_kps  │  查 error_logs 近 30 天错题 → 去重得错题知识点 E
└────────┬─────────┘
         ▼
┌──────────────────────┐
│ expand_prerequisites │  Cypher 1~2 跳 HAS_PREREQUISITE 外扩得候选集 C
└────────┬─────────────┘
         ▼
┌──────────────────────┐
│ rank_by_bkt          │  读 bkt_state，按 p_known 升序 + 错题频次降序，取 Top5
└────────┬─────────────┘
         ▼
┌──────────────────────┐
│ select_quiz_questions│  调 search_question_bank 逐点配题（每点 1~2 题，共 8 题）
│ (纯规则，不调 LLM)    │  难度按掌握度反相关映射：p<0.4 easy / 0.4~0.7 medium / >0.7 hard
└────────┬─────────────┘
         ▼
┌──────────────────────┐
│ persist_quiz         │  写 diagnostic_quizzes 表，返回 quizzes_id + 题目列表
└────────┬─────────────┘
         ▼
        END → 前端渲染测验卡片 → 学生逐题作答 → 回到主图 update_mastery
```

```python
def select_quiz_questions(state: QuizState):
    """按 Top5 目标知识点配题，难度与该学生掌握度反相关

    得到的数据：state['target_points'] = [{'kp_id': 'kp_crossmult', 'p_known': 0.33, ...}, ...]
    要做的操作：逐知识点调题库 tool 抽题 → 超额截断 → 写回 question_ids
    为什么不放 LLM：抽题要可复现，同一份掌握度必须产出同一套题，
                   LLM 每次结果不同会让老师无法信任这个测验
    """
    question_ids, reason_map = [], {}
    for tp in state["target_points"]:
        difficulty = "easy" if tp["p_known"] < 0.40 else ("medium" if tp["p_known"] < 0.70 else "hard")
        picked = search_question_bank.invoke({
            "kp_id": tp["kp_id"],
            "difficulty": difficulty,
            "exclude_ids": state["recent_question_ids"],   # 近 7 天做过的排除掉
            "limit": 2,
        })
        # 单知识点不超过 2 题，多余的配额自动让位给下一个知识点
        question_ids.extend([q["id"] for q in picked[:2]])
        reason_map[tp["kp_id"]] = tp["reason"]
    return {"generated_question_ids": question_ids[:8]}
```


**为什么用独立子图**：主图是"单轮单题 + 多轮引导"的状态机，测验是"一次生成一批题"的批处理，两者的循环语义完全不同。硬塞进主图会让 `attempt_count`、`messages` 这些字段语义打架。子图各自维护 State，通过 `question_ids` 交接，是最干净的接法。

### 4.4 interrupt 用法要点（本项目的第一个 HITL 场景）

```python
from langgraph.types import interrupt, Command

def guide_scaffold(state: StudentAgentState):
    """引导节点：给阶梯提示，然后interrupt挂起等学生再输入

    得到的数据：state['attempt_count']（已引导次数）、state['hint_level']（提示层级）
    要做的操作：调 LLM 生成本层提示 → interrupt 把提示交给前端 → 挂起
    关键：interrupt 抛出后图在此暂停，前端用 SSE 把提示推给学生，
         学生回答后前端调 /api/student/resume 传 Command(resume=学生的新输入) 继续
    """
    hint = llm.invoke(build_hint_prompt(state))
    student_reply = interrupt({          # ← 图在此挂起，等待 Command(resume=...)
        "type": "scaffold_hint",
        "hint": hint,
        "attempt_count": state["attempt_count"],
    })
    # Command(resume=...) 回来之后，从这里继续执行
    return {
        "messages": [HumanMessage(content=student_reply)],
        "attempt_count": state["attempt_count"] + 1,
        "hint_level": state["hint_level"] + 1,
    }
```

**会话续接机制**：用 `PostgresSaver` 做 checkpointer（v1.6 起，与业务表同库），`thread_id` 建议用 `{student_id}_{question_id}`，保证同一道题的引导上下文跨 HTTP 请求保持，**且跨进程持久——后端重启后挂起的 interrupt 仍可 resume**。接入时：

```python
from langgraph.checkpoint.postgres import PostgresSaver

# ★ 首次启动调一次 setup()，由它创建 checkpoint 内部表（幂等，可重复调用）
with PostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
    checkpointer.setup()
    graph = builder.compile(checkpointer=checkpointer)

# 恢复被 interrupt 挂起的会话
graph.invoke(Command(resume=student_new_input), config={"configurable": {"thread_id": tid}})
```

> Redis 方案已评估并否决：多养一个服务还要配 RDB/AOF 持久化，挂起状态丢了会砸穿 HITL；PG 一库两用（业务数据 + checkpoint），备份方案天然全覆盖。`uvicorn --reload` 仍建议演示时关掉——不是丢会话（PG 持久），而是避免多 worker 抢连接池与重复 setup。

---

### 4.4 长期记忆：InMemoryStore 学生画像

**先澄清一个概念**：本项目的"长期记忆"其实已由 PostgreSQL + Neo4j 承担——`bkt_state` 的 `p_known`、`student_answers` 的作答历史、`error_logs` 的错因分布，都是跨会话、跨学期、重启不丢的数据。它们不放进 Store，是因为要被老师端查询、被教研端用 Pandas / Cypher 聚合统计。**Store 只适合装"只给 Agent 自己读的非结构化软事实"。**

| 层              | 实现                    | 作用域          | 本项目装什么                              |
| -------------- | --------------------- | ------------ | ----------------------------------- |
| 短期记忆（checkpointer） | `PostgresSaver`       | `thread_id`  | 一道题的引导对话、主观题批改的 interrupt 挂起点      |
| 长期记忆（可选）       | `InMemoryStore`       | `namespace` 元组 | 学生画像：薄弱知识点 Top5、近期错因习惯             |
| 业务数据（真·长期记忆）   | PostgreSQL + Neo4j    | 主键 / 节点      | 掌握度、作答、错因、题库、知识点图谱                 |

**唯一值得用 Store 的场景**：学生换一道题就是新 thread，Store 若不预热则读不到画像；若引导 Agent 能读到"这个学生最近老在十字相乘上卡住、错因集中在审题失误"，给出的提示就更有针对性。

**关键坑**：`InMemoryStore` 的"长期"只是**跨 thread**，**不跨进程**，进程重启照样清零（真持久要 `PostgresStore`，v1.6 起项目已有 PG，升级只改一行，但作为可选加分项仍按轻量方案做）。因此采用**业务库兜底**策略——PostgreSQL 当真源，Store 当缓存，进节点前做一次幂等预热：

```python
# app/services/student_profile.py
def warm_start(store, student_id: int) -> None:
    """用业务库给 Store 兜底：进程重启后自动重建画像，等于把持久化交给 PostgreSQL。
    幂等——已存在就直接返回，每次成本仅为一次 PG 查询。"""
    ns = ("student", str(student_id))
    if store.get(ns, "profile"):
        return
    weak   = query_weak_kps(student_id, top_n=5)     # 从 bkt_state 取最薄弱的 5 个知识点
    habits = query_recent_error_types(student_id)    # 从 error_logs 取近 30 天错因分布
    store.put(ns, "profile", {"weak_kps": weak, "habits": habits})


# 节点里：store 与 config 都是 LangGraph 自动注入的关键字参数，不用自己传
def guide_scaffold(state: GuideState, *, store, config):
    sid = config["configurable"]["student_id"]
    warm_start(store, sid)                           # 先兜底预热，保证重启不失忆
    profile = store.get(("student", str(sid)), "profile")
    # profile.value["weak_kps"] 拼进 prompt，让阶梯提示对症下药
```

**取舍要能讲出来**：需要精确计算与统计的数据（掌握度、错因分布）放数据库，只给 Agent 读的软事实（学生画像）放 Store——这个判断比"用过 Store"本身更值钱。若 D5 前时间紧张，直接砍掉，闭环一点不受影响。

**升级路径**：短期记忆已是持久化的 `PostgresSaver`，无需再升级；`InMemoryStore()` → `PostgresStore(...)` 一行切换。`config`、`thread_id`、`namespace` 的写法一个字都不用改。

---

## 五、LangGraph 批改 Agent 设计（第二个 HITL 场景）

### 5.1 State 定义

```python
class GradingState(TypedDict):
    answer_id: str
    question_type: str            # objective / subjective
    student_answer: str
    standard_answer: str
    reference_analysis: str       # 题目解析，喂给 LLM 做批改依据
    rule_result: dict             # 客观题规则判分结果
    ai_score: float               # 主观题 LLM 建议分
    ai_comment: str               # 主观题 LLM 评语
    teacher_decision: dict        # 老师确认结果：confirm / edit / reject
    final_score: float
    status: str                   # pending → confirmed / rejected
```

### 5.2 状态图结构

```
        START
          ▼
   ┌────────────────┐
   │ fetch_answer   │  取作答记录 + 题目 + 标准答案
   └────────┬───────┘
            ▼
   ┌──────────────────────┐
   │ route_by_type        │  条件边，按题型分流
   └────┬────────────┬────┘
objective│            │subjective
        ▼            ▼
┌───────────────┐  ┌────────────────────┐
│grade_objective│  │llm_grade_subjective│
│ 字符串/表达式  │  │ Pydantic 结构化输出 │
│ 归一化后比对   │  │ ai_score+ai_comment│
└───────┬───────┘  └─────────┬──────────┘
        │                    ▼
        │          ┌──────────────────────┐
        │          │   INTERRUPT          │
        │          │ 挂起，等老师确认/修改  │
        │          │ ai_suggest 展示给前端 │
        │          └─────────┬────────────┘
        │              Command(resume=老师决定)
        │                    ▼
        │          ┌──────────────────────┐
        │          │ apply_teacher_decision│
        │          │ 采纳/修改/驳回       │
        │          └─────────┬────────────┘
        ▼                    ▼
   ┌──────────────────────────────────┐
   │        commit_grade              │
   │  写 grading_records + student_   │
   │  answers,is_correct/score        │
   └──────────────┬───────────────────┘
                  ▼
          ┌───────────────┐
          │ update_mastery│  与 S-05 共用同一个 BKT 更新工具
          └───────┬───────┘
                  ▼
                 END
```

### 5.3 主观题结构化输出 Schema

```python
class SubjectiveGrade(BaseModel):
    ai_score: float = Field(ge=0, le=100, description="建议得分，0~100")
    ai_comment: str = Field(description="评语，指出答对了哪几步、缺了哪一步")
    scoring_basis: list[str] = Field(description="采分点逐条对照，例如 ['正确列出判别式', '未讨论 Δ=0 情形']")
    confidence: float = Field(ge=0, le=1, description="评分置信度")
```

### 5.4 客观题判分规则

| 题型     | 判分规则                                                         |                            |
| ------ | ------------------------------------------------------------ | -------------------------- |
| 选择题    | 精确匹配，忽略大小写与前后空格                                              |                            |
| 填空题    | 多空按序比对；支持等价答卷（\`{答案1                                         | 答案2}\` 语法）；去除 LaTeX 包裹符后再比 |
| 计算题表达式 | 用 `sympy` 做表达式等价判断（`sympify + simplify`），避免 `x+1` 与 `1+x` 判错 |                            |

---

## 六、CrewAI 教研端设计

> v1.2 说明：教研端现在有**两个 crew**——`analysis_crew`（R-04 考后分析）与 `exam_crew`（R-08 考前出卷）。两者共用同一套 Tool 层与 LLM 配置，只换 Agent 角色与 Task 定义。它们串起来才是完整闭环：**exam_crew 出题 → 考试作答 → analysis_crew 分析 → 异常题写回 questions.quality_flag → 下次 exam_crew 自动降权**。

### 6.1 R-04 考后分析 crew（analysis_crew）

```python
# 三个 Agent 共享同一批 Tool，Tool 层同时被 LangGraph 三端复用
data_analyst = Agent(
    role="考试数据分析师",
    goal="准确计算每题得分率、难度系数与区分度，标出异常题",
    backstory="你是教育测量学专家，熟悉难度与区分度的经典算法",
    tools=[query_paper_stats, query_student_scores],
    verbose=True,
)

item_quality_expert = Agent(
    role="命题质量专家",
    goal="解读异常题的成因，判断是题目问题还是教学问题",
    backstory="你长期参与中考命题审题，能从数据中看出题目的病灶",
    tools=[query_knowledge_graph, query_class_mastery],
    verbose=True,
)

research_lead = Agent(
    role="教研组长",
    goal="汇总前两人的发现，产出可执行的下一阶段教研建议",
    backstory="你需要给全年级数学组布置下阶段教学重点",
    verbose=True,
)

crew = Crew(
    agents=[data_analyst, item_quality_expert, research_lead],
    tasks=[analyze_stats_task, diagnose_reason_task, write_advice_task],
    process=Process.sequential,     # 顺序执行，前后有依赖
    verbose=True,
)
```

### 6.2 共享 Tool 层（三端复用的关键设计）

| Tool 名                  | 输入                             | 输出               | 被谁调用                           |
| ----------------------- | ------------------------------ | ---------------- | ------------------------------ |
| `query_paper_stats`     | paper_id                       | 每题的得分率 DataFrame | CrewAI 数据分析师、教研界面              |
| `query_knowledge_graph` | kp_id, depth                   | 前置知识点链路          | CrewAI 命题专家、LangGraph 学生 Agent |
| `query_student_records` | student_id, kp_id              | 作答记录列表           | 掌握度服务、学生 Agent                 |
| `query_class_mastery`   | class_id                       | 班级知识点掌握度矩阵       | CrewAI、老师端汇总                   |
| `search_question_bank`  | kp_id, difficulty, exclude_ids | 候选题目列表           | 组卷、同类题推荐                       |
| `retrieve_textbook`     | query, role                    | 带出处的教材片段         | RAG 讲解、归因                      |

**实现方式**：用 LangChain 的 `@tool` 装饰器定义一次，CrewAI 侧用 `Tool.from_langchain()` 或直接继承 `BaseTool` 包装。这样三端 Agent 共享一套业务逻辑。

### 6.3 统计计算（纯 Python，不交给 LLM）

```python
def compute_item_metrics(df, full_score_col="full_score", score_col="score"):
    """计算每题的难度系数 P 与区分度 D

    得到的数据：df 为长表，每行一个学生一道题，含 student_id/q_id/score/full_score
    要做的操作：算得分率→按优先级排序取前后27%分组→求分组平均分之差除以满分
    返回：每题 {"q_id": ..., "score_rate": ..., "P": ..., "D": ..., "is_abnormal": ...}
    """
    results = []
    for q_id, grp in df.groupby("q_id"):
        full = grp[full_score_col].iloc[0]
        P = grp[score_col].mean() / full                      # 难度系数，越大越简单

        # 按学生总分排序，取前后 27% 高低分组
        totals = df.groupby("student_id")[score_col].sum().sort_values(ascending=False)
        n = max(1, int(len(totals) * 0.27))
        high_ids, low_ids = set(totals.head(n).index), set(totals.tail(n).index)
        high_mean = grp[grp.student_id.isin(high_ids)][score_col].mean()
        low_mean  = grp[grp.student_id.isin(low_ids)][score_col].mean()
        D = (high_mean - low_mean) / full                     # 区分度

        results.append({
            "q_id": q_id,
            "score_rate": round(grp[score_col].mean() / full, 3),
            "P": round(P, 3),
            "D": round(D, 3),
            "is_abnormal": P < 0.3 or P > 0.9 or D < 0.2,
        })
    return results
```

> 关键点：**指标用 Python 算，LLM 只负责解读**。这样数字可复现，也避免了 LLM 算错数。这是本设计的一处刻意分工，面试时值得讲。

### 6.4 R-08 试卷生成 crew（exam_crew）

```python
# ① 命题规划师：唯一调 LLM 的出策略环节，输出结构化配额表
exam_planner = Agent(
    role="命题规划师",
    goal="根据考试类型（月考巩固型 / 期中期末摸底型）制定知识点×难度配额表",
    backstory="你熟悉初中数学命题规范，知道月考该纠偏、期末该覆盖",
    tools=[query_class_mastery, query_knowledge_graph],
    verbose=True,
)

# ② 选题执行器：把"抽题"这件事锁死在 tool 里，Agent 只做搬运
#    ★ 坑：CrewAI 的 Agent 并不接受"没有 LLM"，llm=None 会回落到环境默认 provider（通常是 OpenAI），
#      没有 key 就报错。所以正确做法是给一个低温小模型 + 极严 prompt，真正的抽题逻辑全在 tool 里。
exam_selector = Agent(
    role="选题执行器",
    goal="严格按配额表调用出题工具抽题，不得自行编写或修改任何题目",
    backstory="你不产出内容，只负责把规划师定好的配额翻译成工具调用",
    tools=[select_questions_by_quota],     # 抽题逻辑在这个 tool 里，纯 Python 规则
    llm=_cheap_llm(temperature=0),         # 低温模型，prompt 里写明"禁止自行出题"
    allow_delegation=False,
    max_iter=2,                            # 抽两次不到位就交给 Reviewer 判不合格
    verbose=True,
)

# ③ 质量审核员：硬指标自己算，只让 LLM 组织修正意见
exam_reviewer = Agent(
    role="试卷审核员",
    goal="校验覆盖率、难度分布、重复率，不达标则退回重选",
    backstory="你把关命题质量，宁可缺题也不让偏题进卷",
    tools=[query_knowledge_graph],
    verbose=True,
)
```

**配额表 Schema**（三者之间的交接契约，可打印给教研组长人工改）：

```python
class QuotaItem(BaseModel):
    kp_id: str
    chapter_id: str
    question_count: int
    difficulty: Literal["easy", "medium", "hard"]
    question_type: Literal["choice", "fill", "subjective"]
    reason: str            # 为什么选这个点进卷，如"年级错题 Top3，错因集中在方法不会"

class QuotaPlan(BaseModel):
    exam_type: Literal["monthly", "midterm", "final"]
    total_score: int       # 默认 100
    items: list[QuotaItem]
```

**两种模式的生成路线差异**：

| exam_type            | 规划师的输入重点                             | 审核员的硬指标                                        |
| -------------------- | ------------------------------------ | ---------------------------------------------- |
| monthly（月考，巩固型）      | 年级错题 Top10 知识点 + 其前置链路，占比 ≥70%；难度偏中低 | 错题知识点命中率 ≥70%；难度分布 易40/中45/难15 容差 ±10%         |
| midterm / final（摸底型） | 全章节均匀覆盖，章节覆盖率 ≥80%；保留历史 D≥0.2 的好题    | 章节覆盖 ≥80%；题型配比 选4/填3/解答3 容差 ±10%；剔除历史 D<0.2 的题 |

**审核不通过时的回退**： Reviewer 输出修正意见 → Selector 重抽一次 → 仍不过关则二轮整改；**最多 2 轮**，超过就把缺口清单原样返回给教研组长人工处理，绝不为了避免报错而乱补题目（宁可少题不错题）。

> 与 R-04 完全一致的工程原则：**该确定的部分用 Python**（覆盖率、难度分布、重复率统计），**该表达的部分才给 LLM**（选点理由、修正意见）。两个 crew 共用同一条原则，讲起来是完整的方法论而不是零散技巧。

---

## 七、RAG 设计

### 7.1 双 Collection 隔离

| Collection         | 内容           | 可见角色    | Chunk 策略                                    |
| ------------------ | ------------ | ------- | ------------------------------------------- |
| `textbook_math`    | 教材 PDF，按章节切片 | 三端可见    | 按小节切 500 字，overlap 50，metadata 带 chapter_id |
| `question_bank`    | 题库条目 + 解析    | 教师/教研可见 | 一题一个 doc，metadata 带 kp_id、difficulty        |
| `teaching_ref`（可选） | 教参、教案        | 教师/教研可见 | 按段落切 400 字                                  |


### 7.2 检索流程

```
用户提问
   ↓
角色判定(JWT role) → 决定可见 collection 白名单
   ↓
DashScopeEmbeddings 向量化 query
   ↓
Milvus similarity_search(k=5, expr='role_visible != "teacher_only"')   # expr 是类 SQL 字符串，不是字典
   ↓
(可选) bge-reranker 重排 → 取 top 3
   ↓
拼进 Prompt，要求 LLM 标注引用编号 [1][2]
   ↓
返回答案 + sources 列表（文件名 + 页码/题号）
```

### 7.3 溯源要求的 Prompt 约束

所有答案必须在句末标注 `[1]` 形式的引用，并在返回体里给出 `sources` 数组。这是 C-02 验收标准"检索有出处可溯源"的实现方式。

---

## 八、API 设计

### 8.1 公共与学生端

| 方法       | 路径                                       | 说明                                                | 备注                                              |      |
| -------- | ---------------------------------------- | ------------------------------------------------- | ----------------------------------------------- | ---- |
| POST     | `/api/auth/login`                        | 登录返回 JWT                                          | **含 `roles: [...]` 数组**（v1.5 多角色），scope 按角色集合解析 |
| GET      | `/api/student/homework`                  | 学生作业列表                                            | 按 student_id 过滤                                 |      |
| GET      | `/api/student/homework/{id}/questions`   | 作业题目                                              | 不带答案                                            |      |
| POST     | `/api/student/answer/stream`             | 提交输入，SSE 流式返回 Agent 输出                            | **核心接口**，返回引导/讲解流                               |      |
| POST     | `/api/student/resume`                    | 携带 `Command(resume=...)` 恢复被 interrupt 的会话        | body 含 thread_id + 用户输入                         |      |
| GET      | `/api/student/mastery/{id}`              | 掌握度数据                                             | 支持 \`?granularity=chapter                       | kp\` |
| **POST** | **`/api/student/quiz/generate`**         | **S-10 生成诊断测验**，body 含 `days`（默认 30）、`size`（默认 8） | 同步接口，返回 target_points + 题目列表 + generated_reason |      |
| **GET**  | **`/api/student/quiz/{quiz_id}`**        | 取测验详情与题目                                          | 不含答案，作答走通用作答接口                                  |      |
| **POST** | **`/api/student/quiz/{quiz_id}/submit`** | 提交整套测验作答                                          | source_type=quiz，逐题走批改 + BKT 回写                 |      |

### 8.2 老师端

| 方法   | 路径                                       | 说明                                    |
| ---- | ---------------------------------------- | ------------------------------------- |
| POST | `/api/teacher/homework/create`           | 按知识点组卷并布置                             |
| GET  | `/api/teacher/homework/{id}/submissions` | 该作业全班作答情况                             |
| POST | `/api/teacher/grade/{answer_id}`         | 触发批改（主观题会挂起等待确认）                      |
| POST | `/api/teacher/grade/{answer_id}/confirm` | 老师确认/修改/驳回，`Command(resume=decision)` |
| GET  | `/api/teacher/class/{id}/error-summary`  | 班级错题汇总 + Top3 薄弱知识点                   |

### 8.3 教研端

| 方法       | 路径                                         | 说明                                        |                                        |                                  |                                       |
| -------- | ------------------------------------------ | ----------------------------------------- | -------------------------------------- | -------------------------------- | ------------------------------------- |
| GET      | `/api/research/grade/{grade_id}/mastery`   | 年级掌握度，\`?view=chapter                     | overall\`                              |                                  |                                       |
| GET      | `/api/research/mastery/compare`            | 班级横向对比                                    |                                        |                                  |                                       |
| POST     | `/api/research/paper/{paper_id}/analyze`   | 触发 CrewAI analysis_crew，返回 JSON 指标 + 教研建议 |                                        |                                  |                                       |
| **POST** | **`/api/research/exam/generate`**          | **R-08 生成试卷**，body 含 \`exam_type=monthly  | midterm                                | final`、`total_score`、`grade_id\` | 返回 paper_id + QuotaPlan + 题目清单 + 审核报告 |
| **GET**  | **`/api/research/exam/{paper_id}/quota`**  | 取配额表                                      | 教研组长可人工改后再触发重抽                         |                                  |                                       |
| **POST** | **`/api/research/exam/{paper_id}/assign`** | 试卷定稿并布置到目标班级                              | 定稿后 student_answers 的 source_type=exam |                                  |                                       |

> **异常题不单独开清单接口**（2026-10-26 下线 `GET /api/research/questions/abnormal`）：
> R-04 返回的 `report.abnormal_items` 已带题号 / P / D / 成因，信息更全；
> `questions.quality_flag='abnormal'` 只作为 R-08 抽题时的沉底降权依据，不对外暴露列表。
> 写回（`write_back_quality_flags`）与降权（`selection_engine` 排序）链路保持不变。

### 8.4 认证与双通道 scope（v1.5 新增）

**角色分配原则**：不做自主注册，角色由管理员在创建账号时指定；二期开放注册走角色邀请码（`STU-` / `TCH-`）。

```python
# app/core/deps.py —— 依赖注入：get_current_user / require_any / get_scopes
def get_scopes(user = Depends(get_current_user)) -> list[dict]:
    # 每个角色各算各的 scope，返回列表；明细/聚合两条通道独立返回、独立消费
    scopes = []
    if "teacher" in user.roles:
        # 数据源：teaching_assignments。示例数据：
        # [("C01","math"), ("C03","math")] → 王老师教 C01、C03 两个班的数学
        classes = get_teacher_classes(user.id)
        scopes.append({"type": "detail", "class_ids": classes, "subject": user.subject})
    if "researcher" in user.roles:
        # 数据源：research_leads。示例数据：grade="初二", subject="math"
        scopes.append({"type": "aggregate", "grade": user.grade, "subject": user.subject})
    if "student" in user.roles:
        scopes.append({"type": "self", "student_id": user.id})
    return scopes

def require_any(*roles):
    # 接口级角色守卫：命中任一角色即放行，admin 恒放行
    ...
```

**消费规则（关键禁令）**：

- 学生明细类接口（作答内容、批改详情）**只认 `detail` / `self` 通道**——researcher 调用直接 403，接口层面就不提供按年级查学生明细的路
- 年级/班级聚合类接口（R-01、R-04、T-03）**只认 `aggregate` / `detail` 通道**并按其范围聚合
- **两个 scope 永不融合**：不要把 (grade=初二, subject=math) 展开成"初二全部班级明细"喂给 detail 通道，否则组长意外获得全年级学生明细，越权比不隔离还糟

**越权单测（必写）**：一个只有 researcher 角色的账号调用 `/api/student/answer/{id}` 必须返回 403。

### 8.5 管理端 API（A-01 最小后台，v1.5 新增）

| 方法 | 路径 | 说明 | 备注 |
|---|---|---|---|
| GET | `/api/admin/users` | 用户列表，`?role=&keyword=&page=` | 停用用户带标记 |
| POST | `/api/admin/users` | 单条创建 | body 含 roles 数组 |
| PUT | `/api/admin/users/{id}` | 修改 / 停用（is_active）/ 重置密码 | 修改 roles 时同步 user_roles |
| POST | `/api/admin/users/import` | **CSV 批量导入**，返回导入报告 | 逐行校验、全过才写库（事务包裹）；报告含每失败行的行号与原因 |
| GET | `/api/admin/users/import-template` | 下载 CSV 模板 | role / class_id / subject 带校验说明 |
| GET/POST/DELETE | `/api/admin/teaching-assignments` | 授课关系维护 | teacher ↔ 班级 ↔ 学科，scope 数据源 |
| GET/POST/DELETE | `/api/admin/research-leads` | 教研组长任命 | user ↔ 年级 ↔ 学科 |
| GET/POST/PUT/DELETE | `/api/admin/classes` | 班级管理 | 年级 / 班名 / 班主任 |
| GET | `/api/admin/audit-logs` | 操作日志查询，`?action=&user_id=&page=` | 只读 |

**admin API 整体守卫**：`require_any("admin")`，挂在 router 依赖上，漏挂一个端点就是越权入口。

---

## 九、项目目录结构

```
HHT-PrismEdu_project/
├── docs/
│   ├── 01_PRD_HHT棱镜课堂_PrismEdu.md
│   └── 02_技术架构设计_HHT棱镜课堂_PrismEdu.md
├── backend/
│   ├── app/
│   │   ├── main.py                    # FastAPI 入口
│   │   ├── core/
│   │   │   ├── config.py              # 环境变量、LLM 实例单例
│   │   │   ├── security.py            # JWT 签发与校验
│   │   │   └── deps.py                # 依赖注入：get_current_user / require_any / get_scopes（双通道，见 8.4）
│   │   ├── api/v1/
│   │   │   ├── auth.py
│   │   │   ├── student.py             # 含 SSE 与 resume
│   │   │   ├── teacher.py
│   │   │   ├── research.py
│   │   │   └── admin.py               # ★ A-01 管理端：用户/授课关系/任命/班级/日志，整体挂 require_any("admin")
│   │   ├── db/
│   │   │   ├── models.py              # SQLAlchemy 模型
│   │   │   └── session.py
│   │   ├── graph_db/
│   │   │   ├── neo4j_client.py        # driver 单例
│   │   │   └── cypher.py              # 所有 Cypher 集中管理
│   │   ├── agents/
│   │   │   ├── tools/                 # ★ 三端共享 Tool 层
│   │   │   │   ├── graph_tools.py
│   │   │   │   ├── stats_tools.py
│   │   │   │   └── bank_tools.py
│   │   │   ├── student/
│   │   │   │   ├── state.py
│   │   │   │   ├── nodes.py
│   │   │   │   ├── graph.py           # LangGraph 编译出口
│   │   │   │   └── quiz_graph.py      # ★ S-10 诊断测验生成子图（独立 StateGraph）
│   │   │   ├── teacher/
│   │   │   │   └── grading_graph.py
│   │   │   └── research/
│   │   │       ├── analysis_crew.py   # R-04 考后分析三角色
│   │   │       ├── exam_crew.py       # ★ R-08 试卷生成三角色（规划/选题/审核）
│   │   │       └── tasks.py
│   │   ├── rag/
│   │   │   ├── ingest.py              # 教材 PDF 入库
│   │   │   ├── embeddings.py
│   │   │   └── retriever.py
│   │   ├── services/
│   │   │   ├── mastery.py             # ★ BKT 掌握度三步更新（含遗忘衰减与错因联动）
│   │   │   ├── bkt_repository.py      # bkt_state 读写 + mastery_records 快照 + Neo4j 同步
│   │   │   ├── grading.py             # 客观题判分规则 + sympy
│   │   │   ├── selection_engine.py    # ★ 组卷引擎：T-01 / S-10 / R-08 三处共用
│   │   │   ├── quiz_service.py        # S-10 错题知识点 → 图谱前置 → 掌握度排序
│   │   │   ├── exam_service.py        # R-08 配额表持久化与 hard metric 校验
│   │   │   └── analysis.py            # Pandas 试卷指标计算
│   │   └── schemas/
│   │       ├── error.py               # ErrorAttribution
│   │       ├── grade.py               # SubjectiveGrade
│   │       ├── quiz.py                # DiagnosticQuiz / TargetPoint
│   │       ├── exam.py                # QuotaPlan / QuotaItem
│   │       └── common.py
│   ├── scripts/
│   │   ├── seed_knowledge_graph.py    # Neo4j 图谱导入
│   │   ├── seed_question_bank.py      # 题库导入
│   │   ├── gen_demo_data.py           # 合成演示作答数据（含虚拟班级的考试作答，供 R-04 分析）
│   │   └── check_neo4j.py             # D1 上午连通性自检，跑不通就切 PG 邻接表
│   └── tests/
│       ├── test_mastery_bkt.py        # BKT 方向与幅度验证（连答对/连答错/粗心）
│       ├── test_selection_engine.py   # 组卷配额、超额截断、异常题降权
│       ├── test_quiz_generation.py    # S-10 题目全落在候选知识点上
│       ├── test_scope_isolation.py    # ★ 双通道 scope：researcher 调学生明细必须 403；多角色并集正确
│       └── test_admin_import.py       # ★ CSV 导入：坏行不写库、报告行号正确
├── web_app/                           # 前端（原生 ES Modules，无构建）
│   ├── index.html
│   ├── serve.py                       # 静态服务（no-store，改完刷新即生效）
│   ├── assets/apple.css               # 视觉体系（Apple 风）
│   └── src/
│       ├── app.js  core.js  shell.js  charts.js  kp-names.js
│       └── views/                     # login / student / teacher / research / admin
├── data/
│   ├── raw/                           # 教材 PDF、题库原始文件
│   └── knowledge_graph/               # 图谱 seed JSON
├── pyproject.toml
├── .env.example
└── README.md
```

---

## 十、依赖清单

> BKT 采用手写实现，不引入额外依赖。若后期要做参数拟合，可选装 `pyBKT`，用它自带的 EM 算法估计每个知识点的四个参数。

```toml
[project]
name = "hht-prismedu"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    # Agent 编排
    "langchain>=1.0",
    "langchain-core>=1.5",
    "langchain-community>=0.3",
    "langgraph>=0.2",
    # 多 Agent 协作
    "crewai>=0.100",
    "crewai-tools>=0.30",
    # LLM / Embedding
    "openai>=1.50",              # DeepSeek 经 DashScope 兼容接口调用
    "dashscope>=1.20",
    # API
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sse-starlette>=2.1",
    "python-multipart>=0.0.9",
    # 数据
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "psycopg[binary]>=3.2",      # PostgreSQL 驱动（业务表 + PostgresSaver 同库）
    "langgraph-checkpoint-postgres>=2.0",  # PostgresSaver
    "neo4j>=5.26",
    "pymilvus>=2.4",
    "langchain-milvus>=0.1",
    "pandas>=2.2",
    "numpy>=1.26",
    "sympy>=1.13",               # 表达式等价判分
    # 文档解析
    "pypdf>=4.3",
    "python-docx>=1.1",
    # 工具
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "python-jose[cryptography]", # JWT
    "passlib[bcrypt]",
    "python-dotenv>=1.0",
    # 前端：web_app/ 是原生 ES Modules，不占 Python 依赖
    #（原 "streamlit>=1.38" / "plotly>=5.24" 已于 2026-10-26 随演示端删除）
]

[tool.uv]
dev-dependencies = ["pytest>=8.0", "ruff>=0.6"]
```

安装命令：

```bash
cd ~/zty-Developer/HHT-PrismEdu_project
uv venv && source .venv/bin/activate
uv pip install -r pyproject.toml
```

向量库单独起容器（standalone 已把 etcd、MinIO 打进一个镜像，不用另起依赖服务）：

```bash
# 方式一：官方单机脚本
curl -sfL https://raw.githubusercontent.com/milvus-io/milvus/master/scripts/standalone_embed.sh -o standalone_embed.sh
bash standalone_embed.sh start

# 方式二：直接 docker run，ARM Mac 上更稳
docker run -d --name milvus-standalone -p 19530:19530 -p 9091:9091 \
  -e ETCD_USE_EMBED=true -e ETCD_DATA_DIR=/etcd-data \
  -e COMMON_STORAGETYPE=local milvusdb/milvus:v2.4.15 standalone

# 验证：容器起来后 30~60 秒内 19530 端口可连即成功
docker ps --filter name=milvus
```

> Milvus standalone 首次启动要 30~60 秒（内部 etcd 就绪才监听 19530），**立刻连会报 connection refused，这不是配置错**，等一下重试即可。建 collection 时 `dim` 必须等于 1024（DashScope text-embedding-v3 输出维度），写成常量 `EMBED_DIM = 1024` 别硬编码。

PostgreSQL 容器（业务表 + PostgresSaver checkpoint 同库，一个数据卷全搞定）：

```bash
docker run -d --name prismedu-pg -p 5432:5432 \
  -e POSTGRES_USER=prismedu -e POSTGRES_PASSWORD=prismedu_dev \
  -e POSTGRES_DB=prismedu \
  -v prismedu_pgdata:/var/lib/postgresql/data postgres:16

# 验证：能连上并建库即成功
docker exec -it prismedu-pg psql -U prismedu -c "SELECT version();"
```

> 备份决策（v1.6 定稿）：**一周版不做自动备份**——演示数据全部由 seed/合成脚本生成，丢失后重跑脚本即可再生；README 的"已知边界"里声明这一点。**上真学校前必须补**：PG dump + Neo4j dump 定时任务 + 一次恢复演练（已列入二期演进 C-04），届时因 checkpointer 同库，一套备份覆盖业务数据与挂起会话。

---

## 十一、开发排期（按实际投入时间重排，v1.6）

时间预算：10.13(二)~10.16(五) 每晚 3h（12h）+ 10.17(六)~10.18(日) 每天 5h（10h）+ 10.19~10.23 全天 5 天（约 40h），合计约 62h。与 PRD 第七章同源，此处列技术验收口径：

| 阶段 | 时间 | 任务 | 硬性技术验收 |
|---|---|---|---|
| 一 地基 | 10.13~10.14 晚（6h） | uv 项目 + Docker 起 Neo4j/Milvus/**PostgreSQL** + PG 数据模型含 user_roles / teaching_assignments / research_leads + Alembic 初始化 + **PostgresSaver 接入（setup()）** + JWT 双通道 scope 骨架 + **密码强度策略与失败锁定** | 多角色 JWT 签发与 get_scopes 单测通过；Neo4j 多跳前置链可查；Milvus 19530 可连；**后端重启后 checkpoint 仍在** |
| 一 地基 | 10.15~10.16 晚（6h） | 图谱 seed + 题库/教材入库 + RAG Retriever（双 collection + role 过滤） | 检索带出处；学生 role 检索不到 teacher 资源 |
| 二 核心Agent | 10.17（5h） | 学生 Agent StateGraph + interrupt 引导 + 意图分类 + S-10 quiz_graph 子图 + **内容安全简版过滤（入口层）** | 命令行完成"求答案→给提示→再输入"；注入类输入被兜底拦截 |
| 二 核心Agent | 10.18（5h） | 批改 Agent（客观直判 + 主观 HITL）+ BKT 三步更新 + 三处同步写入 + **敏感操作写 audit_logs** | 主观题挂起被 resume；BKT 四条验证序列全绿 |
| 三 全天 | 10.19（8h） | CrewAI 最小示例 → analysis_crew + exam_crew + 组卷引擎补全 | 两个 crew 各产出结果 |
| 三 全天 | 10.20（8h） | FastAPI 全部端点 + SSE + interrupt resume 打通 + admin API | 界面走完一条完整行为链；admin 接口越权返回 403 |
| 三 全天 | 10.21（8h） | Streamlit 三端页 + **A-01 管理后台第四页**（6 项按 P0→P1） | CSV 导入含失败报告；授课关系改后权限即时生效 |
| 三 全天 | 10.22（8h） | 端到端联调 + **越权测试全矩阵（每接口 × 每角色）** + 演示数据 + 两条闭环线 | S-10 测验线、R-08→R-04 回流线走通；越权矩阵全 403/正常 |
| 三 全天 | 10.23（8h） | 验收清单逐条打勾 + README（含"已知边界：无自动备份"声明）+ 录屏 + 缓冲 | 验收全绿 |

> 排期纪律：A-01 固定在 10.21；前面积压则按 6 项优先级砍尾（先砍日志页、班级页），P0 三项（CSV 导入 / 用户 CRUD / 授课关系）不可砍。



---

## 十二、关键技术坑预警

| 坑                              | 表现                         | 解法                                                                                                        |
| ------------------------------ | -------------------------- | --------------------------------------------------------------------------------------------------------- |
| **interrupt 必须在节点函数内直接调用**     | 包在子函数或 try 里会失效            | `interrupt()` 只能作为节点函数体内的直接调用，不能嵌套包装；恢复只能靠 `Command(resume=...)`                                          |
| **checkpointer 与 thread_id**   | 不传 thread_id 导致多轮丢失        | thread_id 用业务键 `{student_id}_{question_id}`，在 config 里传                                                   |
| **SSE 与 interrupt 的配合**        | 前端不知道何时该调 resume           | SSE 事件里增加 `type` 字段，`type=interrupt` 时前端弹出输入框并调 resume 接口                                                 |
| **SSE 生成器里的异常被吞**              | 前端无提示地断开                   | 生成器里 try/except，yield 一个 `type=error` 事件再关闭                                                               |
| **CrewAI 输出格式不稳定**             | 期望 JSON 实际是自然语言            | Task 里明确 `output_pydantic` 或 `output_json`，并要求 agent 只输出结构化内容                                             |
| **Neo4j APOC 未装**              | 部分高级 Cypher 报错             | 只用原生 Cypher，避免依赖 APOC 插件                                                                                  |
| **sympy 判分超时**                 | 复杂表达式卡死                    | 加 `sympify` 超时保护，失败则降级为字符串比对                                                                              |
| **Milvus 过滤语法与 Chroma 不同**        | 照搬字典写法会静默查不到东西           | 用类 SQL 的 `expr` 字符串，字符串值带引号、数字不带：'role_visible != "teacher_only"'；先在小数据集上验证                  |
| **Milvus 维度填错 / 刚起就连**          | 建 collection 报错或 connection refused | `EMBED_DIM = 1024` 写成常量（DashScope text-embedding-v3 输出维度）；standalone 首启要等 30~60 秒再连         |
| **BKT 后验分母为 0**                | 掌握度计算崩溃                    | 加 `den > 0` 判断后回退到衰减值；p_guess 取 0.05 这类极小值时尤其要防                                                           |
| **错因枚举值对不上**                   | slip 取不到值，静默退化             | `SLIP_BY_ERROR.get(error_type, 默认 slip)` 兜底，新增错因类型不会炸                                                     |
| **BKT 参数随意调**                  | 掌握度数值不可解释，面试被问穿            | 严格用领域默认值，只用单测验证方向，不凭感觉调参                                                                                  |
| **CrewAI Agent 设 llm=None 报错** | 回落到环境默认 provider，无 key 直接崩 | 抽题这类确定性工作**把逻辑放进 tool**，Agent 配低温模型 + 禁用 delegations + `max_iter` 限次；真想完全绕开 LLM 就在 crew 外用普通函数调用，不走 Agent |
| **同一知识点在测验里重复出现**              | S-10 出的卷子堆在同一处，看不出诊断效果     | `selection_engine` 里做单知识点 ≤2 题的硬截断，配额让给次优先知识点                                                             |
| **组卷时题库返回空集**                  | 冷知识点或难度组合无题可用              | 优雅降级：放宽到相邻难度档 → 仍不足则记录缺口清单，**宁可少题不错题**，绝不随机乱补                                                             |
| **两个 crew 的 OpenAI 环境变量互相打架**  | CrewAI 默认读 OPENAI_API_KEY  | 显式给每个 crew/agent 传同一个 `llm` 实例（复用 FastAPI 那套 ChatOpenAI 兼容配置），不要依赖默认环境变量                                  |
| **InMemoryStore 重启清零**              | 以为存了长期记忆，重启后画像没了       | PostgreSQL 当真源、Store 当缓存，进节点前幂等 `warm_start` 预热；升级持久化 Store 只改一行（PostgresStore）                       |
| **PostgresSaver 忘调 setup()**         | 首次写 checkpoint 报关系不存在          | 应用启动时幂等调用一次 `checkpointer.setup()`；多 worker 部署时只允许一个进程执行                                  |
| **学生对话内容无兜底过滤**              | prompt 注入 / 不当内容直达未成年人    | 节点入口过一层简版过滤（system prompt 硬约束 + 关键词黑名单），完整审核服务放生产化                             |
| **Store 的 namespace 写成字符串**        | 前缀检索失效                     | 必须用元组 `("student", str(student_id))`，不要拼成 "student_1001"                                          |
| **多角色 scope 被融合**              | 组长获得全年级学生明细，越权比不隔离还糟    | 明细/聚合两通道独立返回、独立消费，禁止把 (grade, subject) 展开成班级明细集合；必写越权单测（researcher 调明细接口须 403） |
| **勾了 researcher 未填任命**          | 空 scope，登录处处 403 难排查      | 管理后台勾 researcher 强制先填 research_leads（年级+学科），保存前校验；JWT 里 scope 为空时登录接口直接报错而不是静默发 token |
| **CSV 导入部分写入**                | 2000 行导一半失败，库里脏一半       | 事务包裹"逐行校验、全过才写"；报告返回失败行号与原因；先在 10 行小文件上验证再上大文件                                        |

---

## 十三、演示脚本建议（D7）

为了让演示有说服力，建议录制一个三到五分钟视频，按这个顺序走：

1. 老师端布置一次作业（选 3 个知识点，5 道题）
2. 切到学生端，学生问"这道题答案是什么"，观察 Agent 给的是提示而非答案
3. 学生答错 → 观察错因归因结果 + 前置薄弱点追溯 + 同类题推荐
4. 学生答对 → 切回老师端看客观题自动生效、主观题等待老师确认
5. 老师确认主观题分数 → BKT 掌握度更新
6. 切到教研端看班级 Top3、年级掌握度对比、CrewAI 的教研建议
7. **顺时针闭合第二段**：教研端发起 R-08 出月考卷（看配额表里 ≥70% 落在年级错题热点上）→ 脚本让虚拟班级作答 → 同一份卷触发 R-04 分析 → 展示"以前出题→现在改题"的回流
8. **回到学生单点**：学生在自己的错题库上点"生成测验" → S-10 返回 8 道题，每题标了"为什么选它" → 作答后雷达图变化

> 第 7 步是整套系统最值得讲的一幕：**上一份卷子的分析结果，自动变成了下一份卷子的组卷依据**。这条"考后 → 考前"的回流解释了为什么这是一个闭环而不是一堆功能的拼盘。

这条线走完，业务闭环和技术栈展示一次性到位。

