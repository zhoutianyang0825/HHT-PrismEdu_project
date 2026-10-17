# HHT棱镜课堂 / PrismEdu · 开发计划（里程碑制）

| 项 | 内容 |
|---|---|
| 文档版本 | v1.1（v1.1 变更：更正协作原则——单元测试由 AI 生成并跑绿后交付，老大只做系统级测试；老大想要某个单测时直接点名） |
| 创建日期 | 2026-10-14 |
| 配套文档 | `01_PRD_HHT棱镜课堂_PrismEdu.md`（管"做什么/验收什么"）、`02_技术架构设计_HHT棱镜课堂_PrismEdu.md`（管"怎么做/代码长什么样"） |
| 本文档管什么 | **按什么顺序开发、每个任务完成判据是什么、卡住何时触发降级** |
| 日期引用 | 里程碑对应 PRD 第七章排期（10.13 ~ 10.23，总预算约 62h），本文不重复列日期表 |
| 协作模式 | 老大定的原则：① 代码由 AI 生成；② 老大负责阅读代码 + 理清业务 + 拍板方案 + **系统级测试**（单元测试默认不参与，想加某个单测会直接点名）；③ 按里程碑推进，一个里程碑 = 一轮"生成 → 自检 → 系统验收 → 确认" |

---

## 一、里程碑总览与依赖

| 里程碑 | 名称 | 对应排期 | 依赖 | 产出 |
|---|---|---|---|---|
| **M0** | 环境地基 | 10.13~10.14 晚 | 无 | uv 项目骨架 + 三容器（PG/Neo4j/Milvus）全部可连 |
| **M1** | 数据 + 权限 + 知识层 | 10.15~10.16 晚 | M0 | 20 张表迁移完成、图谱 seed、题库/教材双库入库、RAG 检索带权限 |
| **M2** | 学生 Agent（含 S-10） | 10.17（5h） | M1 | interrupt 引导可跑 + 测验生成子图 |
| **M3** | 批改 Agent + BKT | 10.18（5h） | M1（BKT 不依赖 M2） | 主观题 HITL + BKT 四条序列全绿 |
| **M4** | CrewAI + 组卷引擎 | 10.19（8h） | M1（统计消费 M3 的作答数据，可用合成数据先行） | analysis_crew + exam_crew + selection_engine |
| **M5** | FastAPI + 前端 | 10.20~10.21（16h） | M2 + M3 + M4 | 全部端点 + SSE + Streamlit 四页（含 A-01 管理后台） |
| **M6** | 联调 + 验收 | 10.22~10.23（16h） | M5 | 两条闭环线走通 + 越权矩阵全绿 + 录屏 |

```
M0 ──► M1 ──┬─► M2 ──┐
            ├─► M3 ──┼─► M5 ──► M6
            └─► M4 ──┘
```

> **关键路径（🔴 全局堵点，优先保障）**：① M0 的 Milvus 连通性；② M2/M3 的 interrupt-resume 链路；③ M3 的 BKT 单测。这三处任何一个卡住，后续里程碑全部顺延，必须最先攻坚。

> **任务标签**：【生】= AI 生成（含单测自检）；【测】= 老大系统级验收（CLI/页面/闭环走查）。

---

## 二、里程碑详解

### M0 环境地基（10.13~10.14 晚）

**目标一句话**：三容器全部起得来、连得上，Python 依赖一次装齐，后面所有里程碑不再碰环境问题。

| # | 任务 | 分工 | 产出文件 |
|---|---|---|---|
| M0-T1 | uv 初始化项目 + pyproject 全量依赖（langchain/langgraph/langgraph-checkpoint-postgres/crewai/fastapi/neo4j/pymilvus/langchain-milvus/psycopg/alembic/streamlit/plotly/pandas） | 【生】 | `pyproject.toml`、`.env.example` |
| M0-T2 | Docker 起 PostgreSQL 16 + Neo4j 5.26 + Milvus standalone 三容器（含健康检查与数据卷） | 【生】 | `docker-compose.yml` |
| M0-T3 | 环境自检脚本：一键探测 PG(5432) / Neo4j(7687) / Milvus(19530) / LLM API 连通性 | 【生】 | `scripts/check_env.py` |
| M0-T4 | 跑自检脚本，确认四项全绿 | 【测】 | 终端输出截图 |

**完成判据**（可执行验证）：

```bash
docker compose up -d && sleep 60        # Milvus 首启要等 30~60 秒
uv run python scripts/check_env.py
```

预期输出（模拟）：

```
[OK] PostgreSQL   localhost:5432   ✓ connected (db=prismedu)
[OK] Neo4j        localhost:7687   ✓ connected (v5.26-community)
[OK] Milvus       localhost:19530  ✓ connected (collections=0)
[OK] LLM API      dashscope compatible ✓ chat test: "你好" -> 200
4/4 passed — M0 done
```

**降级触发**：
- Neo4j 起不来 → **10.15 中午前**仍未解决，改用 PostgreSQL 邻接表存图谱（SQL 递归 CTE 替代 Cypher），改表不加服务
- Milvus 连不上 → 降级 **pgvector**（PG 已在栈内，加扩展即可，业务代码只换 Retriever 底座）
- LLM API 不通 → 检查本地代理与 `.env`，此项无降级，是硬依赖

---

### M1 数据 + 权限 + 知识层（10.15~10.16 晚）

**目标一句话**：数据层一次到位——关系表、图谱、向量库三处都有种子数据，权限 scope 骨架可单测，RAG 检索带出处且按角色过滤。

| # | 任务 | 分工 | 产出文件 |
|---|---|---|---|
| M1-T1 | 全量 SQLAlchemy 模型（20 张表：users/user_roles/teaching_assignments/research_leads/classes/students/chapters/knowledge_points/questions/homeworks/homework_questions/student_answers/grading_records/bkt_state/mastery_records/error_logs/diagnostic_quizzes/exam_papers/audit_logs…）+ Alembic 首版迁移 | 【生】 | `app/models/`、`alembic/` |
| M1-T2 | JWT 认证 + 多角色签发（`roles: [...]`）+ 双通道 scope（detail / aggregate 独立返回，永不融合）+ 密码强度策略与失败锁定 | 【生】 | `app/core/security.py`、`app/core/deps.py` |
| M1-T3 | 数学知识点图谱 JSON（60~100 节点，含前置关系）+ Neo4j seed 脚本 | 【生】 | `data/knowledge_graph/math.json`、`scripts/seed_knowledge_graph.py` |
| M1-T4 | 题库 CSV（≥150 题）+ 入库脚本（PG + Milvus 双写）；教材 PDF 切片入 `textbook_math` collection | 【生】 | `data/raw/`、`scripts/seed_question_bank.py`、`app/rag/ingest.py` |
| M1-T5 | RAG Retriever：双 collection + role 过滤（Milvus expr 语法）+ 溯源返回 | 【生】 | `app/rag/retriever.py` |
| M1-T6 | scope 单测：多角色 JWT 解析、researcher 无明细接口（403）、CSV 导入事务性（**单测由我生成并跑绿后交付，属交付自检**） | 【生】 | `tests/test_scope_isolation.py` 全绿 |

**完成判据**：

```bash
uv run alembic upgrade head                      # 20 张表创建成功，无报错
uv run python scripts/seed_knowledge_graph.py    # 导入 ~80 节点
uv run python scripts/seed_question_bank.py      # 150 题 -> PG + Milvus
uv run pytest tests/test_scope_isolation.py -v   # 全绿
```

预期输出（模拟）：

```
tests/test_scope_isolation.py::test_teacher_sees_own_class_only PASSED
tests/test_scope_isolation.py::test_researcher_detail_api_403   PASSED   # 🔴 关键：聚合角色摸不到明细
tests/test_scope_isolation.py::test_multi_role_union_scope      PASSED
3 passed in 1.2s

# 图谱多跳前置链验证（Cypher 手工验证）
MATCH p=(a:KnowledgePoint {name:"因式分解"})-[:REQUIRES*1..2]->(b) RETURN b.name
-> ["整式乘法", "十字相乘"]     # 能追到两个月前的前置点，S-05 依赖此链路
```

**降级触发**：无独立降级项——本里程碑产物是纯地基，做不完就是后面全部堵住，只能压缩任务粒度（题库先 80 题、图谱先 40 节点跑通，演示前再补量）。

---

### M2 学生 Agent（含 S-10，10.17）

**目标一句话**：命令行里完整走通"求答案 → 给阶梯提示 → 再试 → 批改"，S-10 能按 BKT 薄弱点生成 8 题诊断测验。

| # | 任务 | 分工 | 产出文件 |
|---|---|---|---|
| M2-T1 | 学生图 State 定义 + 意图分类节点（求答案/提交作答/要讲解/闲聊 四类路由） | 【生】 | `app/graphs/student/state.py`、`nodes.py` |
| M2-T2 | 🔴 引导节点 + interrupt 最小样例：先脱离业务写 10 行 demo 验证 MemorySaver→PostgresSaver 的挂起-恢复 | 【生】 | `app/graphs/student/graph.py` + 独立 demo 脚本 |
| M2-T3 | S-10 quiz_graph 子图：错题知识点 → 图谱 1~2 跳外扩 → BKT 升序 Top5 → 配额 8 题（复用 selection_engine 骨架） | 【生】 | `app/graphs/student/quiz_graph.py` |
| M2-T4 | （可选加分项）InMemoryStore 学生画像 + warm_start 兜底，时间紧直接砍 | 【生】 | `app/services/student_profile.py` |
| M2-T5 | 命令行全流程测试：扮演学生求答案两次、验证提示逐级递进不给答案 | 【测】 | CLI 交互记录 |

**完成判据**：

```bash
uv run python -m app.graphs.student.cli_demo
```

预期交互（模拟）：

```
学生> 这题答案是多少？
Agent> 先观察常数项：哪两个数相乘等于 6，相加又等于 -5？
学生> 直接告诉我答案嘛
Agent> 提示再进一步：试 (x-2)(x-?)   # 仍不给完整答案，引导递进
学生> (x-2)(x-3)=0
Agent> ✓ 正确！x₁=2, x₂=3
```

```bash
uv run pytest tests/test_quiz_generation.py -v
# test_quiz_targets_weak_kps      PASSED   # 题目全落在薄弱点+前置候选集
# test_quiz_difficulty_inverse    PASSED   # 掌握度越低难度越基础
```

**降级触发**：
- 🔴 interrupt-resume 到 **10.17 深夜**仍未跑通 → 引导流程降级为"同步两段式"（状态存 PG，前端两次请求拼接），M5 联调少一个难点，技术演示少一个亮点
- S-10 子图来不及 → 砍掉，验收清单对应项标"未实现"，不影响主链路

---

### M3 批改 Agent + BKT（10.18）

**目标一句话**：客观题规则直判生效、主观题 LLM 建议挂起等老师确认，BKT 三步更新带错因联动且四条验证序列全绿。

| # | 任务 | 分工 | 产出文件 |
|---|---|---|---|
| M3-T1 | 客观题判分规则（选择/填空精确匹配 + sympy 数值等价） | 【生】 | `app/services/grading.py` |
| M3-T2 | 🔴 主观题批改图：LLM 结构化建议（分数+采分点对照）→ interrupt 挂起 → 老师确认/改分 → 生效 | 【生】 | `app/graphs/teacher/grading_graph.py` |
| M3-T3 | BKT `update_mastery`：遗忘衰减 → 贝叶斯后验 → 学习效应；错因替换本次 p_slip（概念不清 0.05/方法不会 0.10/粗心 0.45/审题失误 0.35） | 【生】 | `app/services/mastery.py` |
| M3-T4 | BKT 四条验证序列单测 + PG/Neo4j 双写 | 【生】 | `tests/test_mastery_bkt.py`、`app/services/bkt_repository.py` |
| M3-T5 | 敏感操作写 audit_logs（换授课关系、确认批改、导入用户） | 【生】 | 各服务埋点 |
| M3-T6 | 用一道主观题验证"挂起 → resume 确认 → 分数生效"全链 | 【测】 | CLI 交互记录 |

**完成判据**：

```bash
uv run pytest tests/test_mastery_bkt.py -v
```

预期输出（模拟，四条序列一个不许歪）：

```
test_consecutive_correct_rises    PASSED   # p_known: 0.20 -> 0.44 -> 0.62（升）
test_consecutive_wrong_falls      PASSED   # p_known: 0.80 -> 0.55（降）
test_careless_penalty_lighter     PASSED   # 同分错题：粗心 0.72 > 概念不清 0.31（粗心掉得少）
test_decay_after_30_days          PASSED   # 无作答 30 天：0.80 -> 0.09（跌破 0.1 预警线）
```

**降级触发**：
- BKT 数值方向异常调不平 → 去掉遗忘衰减层只留贝叶斯更新（两级简化路径），**不允许**凭感觉改四个默认参数
- resume 链路问题已在 M2 攻坚过，此处复用同套机制，无新增降级

---

### M4 CrewAI + 组卷引擎（10.19）

**目标一句话**：selection_engine 三处复用跑通；analysis_crew 出 R-04 指标+教研建议；exam_crew 出 R-08 配额表+卷子（月考/期中期末两种模式）。

| # | 任务 | 分工 | 产出文件 |
|---|---|---|---|
| M4-T1 | 共享 Tool 层：query_knowledge_graph / query_student_records / search_question_bank / query_paper_stats（LangChain @tool 定义一次） | 【生】 | `app/tools/` |
| M4-T2 | 组卷引擎：配额执行、异常题降权、近 7 天去重、单知识点 ≤2 题截断、不足宁可缺题不乱补 | 【生】 | `app/services/selection_engine.py` |
| M4-T3 | CrewAI 最小示例先跑通（确认环境无依赖冲突），再搭 analysis_crew 三角色 sequential（output_json 卡格式） | 【生】 | `app/crews/analysis_crew.py` |
| M4-T4 | exam_crew 三角色：命题规划师（唯一调 LLM，产 QuotaPlan 配额表）→ 选题执行（纯 Python tool）→ 质量审核（硬校验，≤2 轮回退） | 【生】 | `app/crews/exam_crew.py` |
| M4-T5 | Pandas 统计先行：P/D/得分率用手算样例校准，异常阈值 P<0.3 或 >0.9、D<0.2 | 【生】 | `app/services/analysis.py` |
| M4-T6 | 验证：一张月考卷配额表 + R-04 对合成数据分析报告 | 【测】 | 终端输出 |

**完成判据**：

```bash
uv run python -m app.crews.demo_analysis   # R-04 演示（用 gen_demo_data 合成作答）
```

预期输出（模拟）：

```
[数据分析师] 计算 25 题：异常 3 道（Q07 P=0.28 偏难 / Q13 P=0.91 过易 / Q19 D=0.15 区分度不足）
[命题专家]   Q07 需同时调用「十字相乘」「配方」两个前置点，2 班前置平均 p_known=0.31 -> 成因：前置薄弱
[教研组长]   建议：下周补 1 课时十字相乘专题，2/5 班优先；Q13 移出题库待复核
```

**降级触发**（按优先级保 R-04）：
- CrewAI 环境跑不通 → analysis_crew 降级为 LangGraph 三节点顺序图（同一套 tool，产出格式不变）
- exam_crew 来不及 → 降级为"只出 QuotaPlan 配额表、不出整卷"，验收项相应缩窄
- 🔴 **时间闸门**：10.19 收工时两个 crew 至少要有一个能出结果，否则 M5 顺延一天，从 10.23 缓冲里扣

---

### M5 FastAPI + 前端（10.20~10.21）

**目标一句话**：全部端点 + SSE 流式 + interrupt resume 打通；Streamlit 四页（学生/老师/教研/管理后台）能走完整行为链。

| # | 任务 | 分工 | 产出文件 |
|---|---|---|---|
| M5-T1 | 全部 API 端点（auth/student/teacher/research/admin 五组，见架构文档 8.5）+ 越权守卫挂载 | 【生】 | `app/api/` |
| M5-T2 | 🔴 SSE 流式 + interrupt 的 resume 与 HTTP 打通（学生引导、主观题确认两个场景） | 【生】 | `app/api/student.py`、`app/api/teacher.py` |
| M5-T3 | admin API：CSV 导入（逐行校验、全过才写、失败报告带行号）/用户 CRUD/授课关系/组长任命/班级/日志 | 【生】 | `app/api/admin.py` |
| M5-T4 | Streamlit 四页 + Plotly 雷达图；A-01 管理后台 6 项按 P0（导入/CRUD/授课关系）→P1（任命/班级/日志） | 【生】 | `streamlit_app/pages/` |
| M5-T5 | 界面走查：学生做一题被引导 → 老师确认主观题 → 教研看汇总 → admin 改授课关系后权限即时变化 | 【测】 | 页面操作记录 |

**完成判据**：

```bash
uv run uvicorn app.main:app --port 8000          # 注意：不加 --reload（会话在 PG 但热重载仍断 SSE）
uv run streamlit run streamlit_app/Home.py
```

预期验证（模拟）：

```bash
# 登录拿多角色 JWT
curl -s -X POST localhost:8000/api/auth/login -d '{"username":"R003","password":"..."}' | jq .roles
-> ["teacher", "researcher"]          # 一人多角色签发成功

# researcher 调明细接口必须 403
curl -s localhost:8000/api/teacher/answers/1001 -H "Authorization: Bearer $R003_TOKEN"
-> {"detail":"forbidden: detail scope required"}   403
```

**降级触发**：
- SSE 与 resume 配合复杂 → 该两个场景改同步接口（请求-等待-返回），其余端点保留流式
- 管理后台 P1 三项（任命/班级/日志页）来不及 → 退回 CLI 脚本（v1.4 的老路本来就是脚本），P0 三项必须保

---

### M6 联调 + 验收（10.22~10.23）

**目标一句话**：两条闭环线端到端走通、越权矩阵全绿、演示数据就绪、README + 录屏完成。

| # | 任务 | 分工 | 产出 |
|---|---|---|---|
| M6-T1 | 🔴 闭环线一：S-10 生成测验 → 学生作答 → 批改 → BKT 变化 → 雷达图可见 | 【测】 | 走查记录 |
| M6-T2 | 🔴 闭环线二：R-08 出卷 → 虚拟班级作答（gen_demo_data）→ R-04 分析 → 异常题写回 quality_flag → 下次出卷降权生效 | 【测】 | 走查记录 |
| M6-T3 | 越权测试全矩阵：每接口 × 每角色（学生/teacher/researcher/双角色/admin），未授权一律 403（**单测由我生成跑绿，矩阵结果随交付附上**） | 【生】 | `tests/test_scope_matrix.py` 全绿 |
| M6-T4 | 演示数据脚本 + reset_demo.py（清作答留题库，可反复演示） | 【生】 | `scripts/gen_demo_data.py`、`reset_demo.py` |
| M6-T5 | README（含"已知边界：无自动备份；数据由 seed 脚本可再生"+ 演示话术）+ 三分钟演示录屏 | 【生】 | `README.md`、`demo.mp4` |
| M6-T6 | 对照 PRD 第九章验收清单逐条打勾（24+ 条） | 【测】 | 勾选记录 |

**完成判据**：PRD 第九章验收清单全绿 + 两条闭环线录屏留存。

**降级触发**：10.23 为缓冲日——前面任何顺延在此吸收；若 10.23 中午仍有硬伤未修，**按"闭环完整性 > 功能数量"原则砍演示范围，保两条闭环线优先**。

---

## 三、每轮协作流程（固定套路）

```
1. 老大说"开工 Mx"  → 我按任务清单生成该里程碑全部代码（一次到位，不挤牙膏）
2. 我交付前自检     → 单元测试由我生成并跑绿（scope / BKT / 组卷 / 越权矩阵等 pytest 全部我负责）
3. 我交付时附上     → 改动文件清单 + "系统级验收命令"（只留需要老大动手的部分）
4. 老大做系统级测试 → 起服务、走 CLI 交互、点 Streamlit 页面、跑两条闭环线——这是测试重点
5. 全绿             → 老大拍板进下一个里程碑；有红 → 贴报错给我，我根因修复
6. 每个里程碑完成   → 老大在本文档第四节勾掉对应项（进度一眼可见）
```

**测试分工（v1.1 更正）**：

| 层级 | 谁做 | 内容 |
|---|---|---|
| 单元测试 | 【生】我，交付前跑绿 | scope 隔离、BKT 四序列、组卷配额、越权矩阵等 pytest——老大默认不参与，**想要某个单测时直接点名** |
| 系统级测试 | 【测】老大，里程碑验收 | 判据里的 CLI 交互、服务启动、页面走查、两条闭环线——**测试重心在这里** |

**两条纪律**：
- 我生成的代码不带 TODO 占位——每个里程碑交付即可运行
- 降级触发后动方案，需老大一句"同意降级"再执行，不默默降

---

## 四、进度追踪（边做边勾）

- [ ] **M0** 环境地基 —— 四项连通性全绿
- [ ] **M1** 数据+权限+知识层 —— 迁移/seed/scope 单测全绿
- [ ] **M2** 学生 Agent —— CLI 引导链路 + S-10 测验（Store 画像可选）
- [ ] **M3** 批改+BKT —— 四条序列全绿 + 主观题 resume 链路
- [ ] **M4** CrewAI+组卷 —— R-04 报告 + R-08 配额表出结果
- [ ] **M5** API+前端 —— 四页走通 + 403 验证 + SSE/resume
- [ ] **M6** 联调验收 —— 两条闭环 + 越权矩阵 + 录屏
