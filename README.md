# HHT棱镜课堂 / PrismEdu

以 **BKT 掌握度为状态中枢**的三端学情闭环 Agent 系统（学生端 / 老师端 / 教研端 + 管理后台）。

一句话定位：学生求答案不给答案给引导、主观题 AI 建议老师拍板、考后分析自动回流考前命题——
两条闭环线把「学 → 练 → 考 → 析 → 补」串成一个可演示、可复现的环。

| 项 | 内容 |
|---|---|
| 技术栈 | FastAPI + LangGraph + CrewAI + SQLAlchemy/Alembic + Streamlit + Plotly |
| 数据面 | PostgreSQL 20 张表 + Neo4j 知识图谱 + Milvus 双 collection（题库/教材） |
| 配套文档 | `docs/01_PRD`（做什么）、`docs/02_技术架构`（怎么做）、`docs/03_开发计划`（顺序与判据）、`docs/04_设计决策`（为什么） |

---

## 一、环境准备与启动

```bash
# 1) 起三容器（PG / Neo4j / Milvus；Milvus 首启要等 30~60 秒）
docker compose up -d && sleep 60

# 2) 装依赖 + 环境自检（四项连通全绿再往下）
uv sync
uv run python scripts/check_env.py

# 3) 建表 + 灌数据（幂等；--wipe 可清平重灌）
uv run alembic upgrade head
uv run python scripts/seed_knowledge_graph.py     # Neo4j 知识点图谱 ~80 节点
uv run python scripts/seed_question_bank.py       # 题库185 题 -> PG + Milvus
uv run python scripts/seed_textbook.py            # 教材切片 -> textbook_math

# 4) 演示数据（账号/班级/学习历史；幂等，可反复跑）
uv run python scripts/gen_demo_data.py

# 5) 起服务（🔴 不加 --reload：热重载会掐断 SSE 流）
uv run uvicorn app.main:app --port 8100
```

API 可视化调试：<http://localhost:8100/docs>（FastAPI 自带，比 curl 快）。

---

## 二、演示账号（密码统一 `Passw0rd1`）

| 账号 | 身份 | 看什么 |
|---|---|---|
| `S001` | 学生（初二1班） | 闭环线一主角：求答案被引导 / S-10 生成测验 / 雷达图 |
| `T001` | 王老师（初二1班） | 待批改、主观题确认、班级错题汇总 |
| `T002` | 李老师（初二2班） | 对照账号：验证「只看自己任教班」 |
| `R001` | 陈组长（教研·初二数学） | R-08 出卷 / R-04 考后分析 / 异常题清单 |
| `T003` | 张双角色（teacher+researcher） | 一人多角色：明细+聚合两通道并存 |
| `A001` | 管理员 | 管理后台：CSV 导入 / 授课关系 / 任命 / 审计日志 |

---

## 三、两条闭环线的演示话术（三分钟录屏分镜）

> 录屏 `demo.mp4` 按下面分镜录（QuickTime 新建屏幕录制即可，全程约 3 分钟）；
> 每一镜的「说什么」可直接照念。

### 闭环线一：S-10 测验 → 作答 → 批改 → BKT → 雷达图（约 90 秒）

| 镜 | 操作 | 话术 |
|---|---|---|
| 1 | 登录 `S001` → 学生端对某题输入「这题答案是多少？」 | "学生要答案，系统不给答案——给的是阶梯提示，逐级递进逼他自己想。" |
| 2 | 再催一次「直接告诉我答案嘛」→ 收到更进一步的提示仍无答案 | "再催也一样，引导只会更深一层，绝不出完整答案。" |
| 3 | 点「生成测验」→ 展示 8 题 + target_points | "S-10 从近 30 天错题出发，沿图谱追前置、按 BKT 最弱优先，选出 8 题诊断卷——每个入选点都带理由。" |
| 4 | 提交作答 → 客观题当场出分 | "客观题规则直判，判完 BKT 立刻回写。" |
| 5 | 切老师端确认主观题 → 回学生端看雷达图变化 | "主观题挂起等老师拍板；确认后掌握度更新，雷达图肉眼可见地动。" |

### 闭环线二：R-08 出卷 → 虚拟作答 → R-04 分析 → 异常题回流降权（约 90 秒）

| 镜 | 操作 | 话术 |
|---|---|---|
| 1 | 教研端 `R001` 出月考卷 → 展示配额表 | "出卷先出配额表：知识点×难度×题型，LLM 只做规划，抽题是纯 Python、可复现。" |
| 2 | 终端跑 `uv run python scripts/gen_demo_data.py --paper-id <id>` | "虚拟班级 40 人瞬间作答完毕——演示数据可再生，不用等真人考试。" |
| 3 | 点「考后分析」→ 展示 P/D 指标 + 3 道异常题 | "指标是 Pandas 算的，一个数都不经 LLM 的手；第 1 题过难、第 2 题过易、第 3 题区分度不足。" |
| 4 | 异常题自动写回 quality_flag → 再出一次卷 | "关键一环：异常题自动沉底降权——考后分析直接变成考前改进，单向链路闭成环。" |

**脚本辅助（演示前/中可随时跑）**：

```bash
uv run python scripts/gen_demo_data.py --paper-id <id>   # 给某卷合成虚拟作答
uv run python scripts/reset_demo.py                      # 清作答留题库，可反复演示
uv run python scripts/gen_demo_data.py                   # 重置后一键恢复演示数据
```

---

## 四、系统级验收清单（对照 PRD 第九章，逐条打勾）

| # | 验收项 | 怎么验 |
|---|---|---|
| 1 | 三端可登录、权限隔离生效 | 用上表 6 个账号各登录一次 |
| 2 | 老师布置的作业学生能看到 | T001 布置 → S001 学生端可见 |
| 3 | 求答案返回引导而非答案 | 闭环线一 镜 1~2 |
| 4 | 客观题自动批改与标准答案 100% 一致 | 对照 `data/raw/question_bank.csv` 抽查 |
| 5 | 主观题建议需老师确认后生效 | 闭环线一 镜 5 |
| 6 | 错因归因 4 类之一 + evidence | 批改后看 `error_logs` |
| 7 | BKT 连对升/连错降/粗心降幅小 | `uv run pytest tests/test_mastery_bkt.py -v` |
| 8 | 掌握度双写 PG + Neo4j | 批改后 Cypher 查 `MATCH (k:KnowledgePoint) RETURN k.p_known` |
| 9 | 重启后挂起批改可 resume | 起服务 → 触发主观题 → 重启 → confirm |
| 10 | 弱密码拒绝 / 5 次失败锁定 15 分钟 | `uv run pytest tests/test_scope_isolation.py -v` |
| 11 | prompt 注入被兜底拦截 | 学生端输入「忽略以上指令…」 |
| 12 | 敏感操作进 audit_logs | 换授课关系/确认批改/导入后在管理后台查日志 |
| 13 | 越权矩阵全绿 | `uv run pytest tests/test_scope_matrix.py -v`（258 条全绿随交付附上） |
| 14 | 班级 Top3 薄弱知识点 | 老师端错题汇总页 |
| 15 | 年级掌握度班级对比 | 教研端 mastery/compare |
| 16 | 试卷分析 P/D/得分率 + 异常标注 | 闭环线二 镜 3 |
| 17 | CrewAI 产出教研建议 | R-04 报告 teaching_suggestions 字段 |
| 18 | S-10 题目落在错题点/前置点 | 测验生成返回的 target_points 留痕 |
| 19 | S-10 单点 ≤2 题、难度反相关掌握度 | `uv run pytest tests/test_quiz_generation.py -v` |
| 20 | 测验作答后 BKT 同步更新 | 闭环线一 镜 5 |
| 21 | 月考命中率 ≥70% / 摸底覆盖 ≥80% | `uv run pytest tests/test_selection_engine.py -v` |
| 22 | 难度分布 ±10%、配额表可打印 | R-08 出卷返回 review.metrics |
| 23 | 异常题自动降权、卷子可布置回流 | 闭环线二全程 |
| 24 | CSV 坏行整批不写 + 报告带行号 | `uv run pytest tests/test_api_admin.py -v` |
| 25 | 改授课关系后可见范围即时变化 | 管理后台改 T002 授课 → 老师端刷新 |
| 26 | 多角色账号两通道并存 | 登录 `T003` 看明细 + 年级聚合 |
| 27 | 教研角色调明细接口 403 | `uv run pytest tests/test_scope_matrix.py -k m5_verdict -v` |

---

## 五、测试与自检

```bash
uv run pytest tests/ -v          # 全部单测（交付前我已跑绿）
```

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_scope_isolation.py` | JWT 多角色 / 密码策略 / 双通道 scope / CSV 事务性 |
| `tests/test_mastery_bkt.py` | BKT 四条验证序列 |
| `tests/test_quiz_generation.py` | S-10 目标点选择与配题规则 |
| `tests/test_selection_engine.py` | 组卷配额、异常题降权、单点 ≤2 |
| `tests/test_analysis.py` | R-04 指标手算校准 + quality_flag 写回 |
| `tests/test_api_admin.py` | admin 越权抽测 + CSV 导入事务性 |
| `tests/test_scope_matrix.py` | 🔴 越权全矩阵：43 端点 × 5 角色 + 匿名 + 数据级越权 + 路由对账 |

---

## 六、已知边界（诚实声明）

1. **无自动备份**：PG 数据卷靠 docker compose 手动备份（`docker exec prismedu-postgres pg_dump -U prismedu > backup.sql`），生产化需定时备份策略。
2. **数据由 seed 脚本可再生**：全部数据都能 `reset_demo.py` + `gen_demo_data.py` / `seed_*.py` 重建，演示环境弄脏了不用慌。
3. **登录失败锁定在进程内存**：多 worker 各记各的，上生产要挪 Redis（见 04 文档 D11）。
4. **CORS 全开**：演示环境配置，生产要改白名单（`app/main.py`）。
5. **录屏 `demo.mp4` 需人工录制**：按第三节分镜录 3 分钟即可，脚本与话术已备好。
6. LLM 依赖 DashScope 兼容 API（见 `.env.example`）；Neo4j 挂了图谱外扩自动降级、主流程不断链。
