# HHT 棱镜课堂 · Web 前端

纯静态前端，**无构建、无 npm 依赖**：原生 ES Modules + Hash 路由，视觉体系参照 Apple 官网（`#fbfbfd` 底、`#1d1d1f` 字、`#0071e3` 蓝、胶囊按钮、毛玻璃顶栏）。

---

## 一、启动

前端和后端是两个独立端口，**先起后端，再起前端**。

### 1. 起后端

```bash
cd /Users/zhoutianyang/zty-Developer/HHT-PrismEdu_project
uv run uvicorn app.main:app --port 8100
```

> **为什么这里写 8100 而不是 8000**：本机 8000 常被别的服务/OrbStack 端口转发占用（`curl localhost:8000/health` 会返回 Node 的 `{"statusCode":404...}` 而不是 FastAPI 的 `{"status":"ok"}`）。起之前先探一下：
>
> ```bash
> curl -s localhost:8000/health    # 是 {"status":"ok",...} 才能用 8000
> ```

**不要加 `--reload`**：热重载会重启进程，正在进行的 SSE 流被掐断，且多 worker 会抢连接池。改后端代码后手动重启。

自检：`curl http://localhost:8100/health` → `{"status":"ok"}`；接口文档 <http://localhost:8100/docs>。

### 2. 起前端（推荐用自带脚本）

```bash
cd /Users/zhoutianyang/zty-Developer/HHT-PrismEdu_project/web_app
python3 serve.py                       # 默认 5173
```

终端会直接打印可点的地址，**点开就行，地址栏不需要带任何参数**：

```
  前端   http://localhost:5173/
  后端   http://localhost:8100   ← 默认就直连，地址栏不用带 ?api=
```

常用参数：

```bash
python3 serve.py --port 5174     # 换前端端口
python3 serve.py --bind 0.0.0.0  # 局域网演示（手机/另一台电脑）
```

`serve.py` 只做三件额外的事：给 `.js/.css/.html` 打 `Cache-Control: no-store`（否则改完代码刷新还是旧的）、启动前检查端口占用（不许和后端都占 8100）、起服务时探一下后端 `/health` 告诉你连不连得上。

它还提供 `/__version`：切回标签页时前端会对比一次，发现源码变了就弹「前端文件已更新，按 ⌘⇧R 刷新才会生效」——避免「明明改了还是老样子」的误判。**改了 `serve.py` 自己必须重启**，改了 `src/*.js` 刷新即可。

不想用脚本也行，但要自己处理缓存：

```bash
python3 -m http.server 5173    # 改了代码记得硬刷新 ⌘⇧R
npx serve .                    # Node 生态
```

> **`python3 -m http.server` 不写端口时默认就是 8000**（Python 内置默认值，不是本项目配的）。
> 而本机 8000 归 OrbStack 的端口转发，一旦 OrbStack 起来/VM 状态变化，静态服务就可能被顶掉或连不上（表现为 502 或 `ERR_CONNECTION_REFUSED`）。**前端请固定在 5173**，别用 8000。

> 必须用 HTTP 服务打开，不能双击 `index.html`：`file://` 协议下 ES Module 会被浏览器的同源策略拦掉，页面会白屏。

### 3. 后端地址怎么配（默认零配置）

前端不写死后端主机名，而是取**当前页面的主机名 + 8100 端口**（`src/core.js` 第 19~22 行）：

| 你打开的地址 | 实际请求的后端 |
| --- | --- |
| `http://localhost:5173/` | `http://localhost:8100` |
| `http://127.0.0.1:5173/` | `http://127.0.0.1:8100` |
| `http://192.168.1.20:5173/` | `http://192.168.1.20:8100`（后端需 `--host 0.0.0.0`） |

所以**直接打开 `http://localhost:5173/` 就能连后端**，不需要 `?api=`。

| 场景 | 做法 |
| --- | --- |
| 常规开发 | 什么都不用做，直接开 `http://localhost:5173/` |
| 后端不在 8100 / 在别的机器 | 地址栏加 `?api=http://host:port`（临时代理，不改代码） |
| 部署到固定环境 | 改 `src/core.js` 的 `API_PORT` / `API_HOST` |

界面上**不出现**后端地址，也不会回显——避免把内网地址暴露给学生。`?api=` 只是开发期的临时后门，README 里的 `serve.py` 默认不生成它。

跨源已打通：后端 `app/main.py` 的 CORS 为 `allow_origins=["*"]`，静态服务器另起端口可直连（已验证 `OPTIONS /api/auth/login` 返回 200，真实 admin 登录 200）。

---

## 二、登录与角色路由

首页只有登录页，账号由管理员统一分配，不开放注册。

| 账号角色 | 落地页 | 左侧「工作台」切换 |
| --- | --- | --- |
| `student` | 学生端 · 概览 | 不显示（单角色） |
| `teacher` | 老师端 · 概览 | 不显示 |
| `researcher` | 教研端 · 年级学情 | 不显示 |
| `admin` | 管理后台 · 概览 | 不显示 |
| 多角色（如 teacher + researcher） | **一律老师端** | 显示「老师端 / 教研端」，一键切换不重登 |

规则实现在 `src/shell.js` 的 `workbenches()` 与 `defaultRoute()`；单角色跟随自身，多角色老师端优先，没有 teacher 才顺延教研 → 管理 → 学生。

---

## 三、目录结构

```
web_app/
├── index.html            入口（含启动兜底：模块没加载起来时提示刷新，不白屏）
├── serve.py              开发用静态服务器（关缓存 + 端口检查 + 后端探活 + /__version）
├── assets/apple.css      设计系统（变量 / 组件 / 断点 1080 & 900）
└── src/
    ├── app.js            Hash 路由、会话守卫、非法路由纠正
    ├── core.js           API_BASE、JWT 会话、REST + SSE、toast/modal/icon
    ├── shell.js          顶栏 + 侧栏 + 角色路由规则
    ├── charts.js         雷达图（SVG）/ 柱状图（HTML）/ 条形行
    ├── kp-names.js       知识点与章节的中文名映射（由知识图谱生成）
    └── views/            login / student / teacher / research / admin
```

---

## 四、页面与后端接口对照

| 端 · 页面 | 主要接口 |
| --- | --- |
| 登录 | `POST /api/auth/login` → `{access_token, roles, student_id…}` |
| 学生 · 学习助手 | `GET /api/student/homework`、`GET /api/student/homework/{id}/questions`、`POST /api/student/answer/stream`（SSE）、`POST /api/student/resume` |
| 学生 · 诊断测验 | `POST /api/student/quiz/generate`、`POST /api/student/quiz/{id}/submit` |
| 学生 · 掌握度 | `GET /api/student/mastery/{student_id}?granularity=kp\|chapter` |
| 老师 · 待批改 | `GET /api/teacher/grade/pending`、`POST /api/teacher/grade/{answer_id}`、`POST /api/teacher/grade/{answer_id}/confirm` |
| 老师 · 布置作业 | `GET /api/teacher/classes`、`POST /api/teacher/homework/create` |
| 老师 · 班级学情 | `GET /api/teacher/class/{id}/error-summary`、`GET /api/teacher/class/{id}/mastery`、`GET /api/teacher/homework/{id}/submissions` |
| 教研 · 年级学情 | `GET /api/research/grade/{grade}/mastery?subject=math` |
| 教研 · 班级对比 | `GET /api/research/mastery/compare?grade&subject` |
| 教研 · 智能出卷 | `POST /api/research/exam/generate`、`POST /api/research/exam/{id}/assign` |
| 教研 · 考后分析 | `POST /api/research/paper/{id}/analyze`（报告里自带异常题清单与成因） |
| 管理 · 账户导入 | `GET /api/admin/users/import-template`、`POST /api/admin/users/import` |
| 管理 · 用户管理 | `GET/POST /api/admin/users`、`PUT /api/admin/users/{id}` |
| 管理 · 授课关系 / 教研任命 / 班级 / 日志 | `/api/admin/teaching-assignments`、`/research-leads`、`/classes`、`/audit-logs` |

### 两条硬约束

1. **SSE 帧格式**：`data: {json}\n\n`，`type ∈ node | interrupt | message | error | end`。
   前端只在收到 `interrupt` 时把输入锁成「等你回答」，下次发送走 `POST /api/student/resume`，否则走 `/answer/stream`。
2. **scope 只透传**：越权时后端返回 403，前端原样显示 `detail` 原文，不在本地猜权限。

---

## 五、排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 白屏，5 秒后出现「页面没能加载完整」 | 模块被网络中断加载失败 | 刷新一次；仍失败看控制台 Network |
| `ERR_CONNECTION_REFUSED`，地址是 `localhost:5173` | **前端静态服务器没起**（不是后端问题） | 在 `web_app` 目录执行 `python3 serve.py` |
| 登录报「后端在跑，但请求被浏览器拦下…5xx」 | **接口 500**（多半是 PostgreSQL 没起）。500 由 Starlette 最外层返回，不带 CORS 头，浏览器统一报 `MissingAllowOriginHeader`，看起来像跨源被拒 | `curl localhost:8100/api/auth/login -X POST -H 'Content-Type: application/json' -d '{"username":"x","password":"y"}'` 实际打一次；再 `lsof -iTCP:5432 -sTCP:LISTEN` 确认 PG。OrbStack/容器没起时 PG 一定连不上 |
| 前端占用 8000，之后打不开 / 502 | `python3 -m http.server` **不写端口默认就是 8000**，而 8000 是 OrbStack 的转发端口 | 前端固定 `python3 serve.py`（5173） |
| 登录后立刻退回登录页 | 后端没起 / 端口不对 | `curl http://localhost:8100/health` 应是 `{"status":"ok"}` |
| 登录报 JS 解析错（`Unexpected token '<'`） | 后端地址指到了非 FastAPI 服务（返回 HTML） | 确认端口：FastAPI 才有 `/docs` 与 `{"status":"ok"}` |
| 答题没有逐字输出，一次性蹦出全部 | 反向代理缓冲了 SSE | Nginx 关 `proxy_buffering`，加 `X-Accel-Buffering: no` |
| 登录报「后端 X 上没有这个接口（HTTP 404，返回的不是 JSON）」 | 端口指错，或**页面还是旧版本**（旧版默认连 8000） | ⌘⇧R 强制刷新；仍 404 就 `curl http://localhost:8100/health` 确认后端端口 |
| 改了 `src/*.js` 但页面没变 | 标签页一直开着，跑的是旧模块 | 看是否弹了「前端文件已更新」提示；无论有无都按 ⌘⇧R |
| 知识点显示成 `kp_crossmult` 这类键 | 知识图谱里有新增知识点，`kp-names.js` 未包含 | 重新生成映射，或在图谱改名 |

---

## 六、已知边界

- 桌面优先：≥1080px 三栏布局，900px 以下侧栏转顶部横向导航（可用，但不作为主场景）。
- 无前端构建链：所有文件浏览器直接执行，改完刷新即生效；代价是没有 tree-shaking 与类型检查。
- 图表为手写 SVG / HTML，不引第三方图表库：雷达图与柱状图够用，复杂交互图表需要另接库。
