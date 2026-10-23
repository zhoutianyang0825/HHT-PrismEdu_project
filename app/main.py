# app/main.py
# M5 · FastAPI 入口：五组路由挂载 + CORS + 启动预热。
#
# 【启动方式】（开发计划 M5 完成判据，注意不加 --reload）
#   uv run uvicorn app.main:app --port 8000
#
#   为什么不加 --reload：会话在 PG 不会丢，但热重载会重启进程 →
#   ① 正在进行的 SSE 流被掐断；② 多 worker 抢连接池 / 重复 setup()。
#
# 【OpenAPI 文档】启动后开 http://localhost:8000/docs 可视化调试全部端点——
#   这是 FastAPI 自带的调试福利，比 curl 快。
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, auth, research, student, teacher


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/退出钩子。

    启动时预热 checkpointer 与 store：两个 setup() 幂等建内部表（checkpoints / store）。
    不预热也能跑（首次请求时 lru_cache 懒建），但第一个学生请求会多等 1~2 秒建表

    陌生写法 @asynccontextmanager + yield：异步上下文管理器+生成器
    yield 前是启动逻辑（服务起来之前跑），
    yield 后是退出逻辑（进程关闭时跑 finally）。
    """
    try:
        from app.db.checkpointer import get_checkpointer
        from app.db.store import get_store

        get_checkpointer()   # 内部 setup()：checkpoint 表
        get_store()          # 内部 setup()：store 表
        print("[startup] checkpointer / store 预热完成（PG 内部表就绪）")

    except Exception as exc:   
        # PG 没起也允许服务先起来（auth/admin 不依赖 checkpointer），
        # 学生端第一次用时会再试——报错信息直指容器，不吞
        print(f"[startup] ⚠️ 预热失败（PG 未就绪？）：{exc}\n"
              "          学生端/老师端批改接口在 PG 可用后自动恢复")

    yield   # ← 服务运行中

    # 退出：关 PG 连接池（checkpointer/store 各一个）
    try:
        from app.db.checkpointer import close_checkpointer
        close_checkpointer()

    except Exception:
        pass


app = FastAPI(
    title="HHT棱镜课堂 / PrismEdu API",
    version="0.5.0",
    description="以 BKT 掌握度为状态中枢的三端学情闭环 Agent 系统",
    lifespan=lifespan,
)


# CORS：Streamlit 调本 API 属跨源（8501 → 8000），演示环境放开
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # 一周版演示全开；上生产改白名单
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- 五组路由挂载（前缀与架构文档 8.1~8.5 一一对应）----
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(student.router, prefix="/api/student", tags=["student"])
app.include_router(teacher.router, prefix="/api/teacher", tags=["teacher"])
app.include_router(research.router, prefix="/api/research", tags=["research"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])



@app.get("/health", tags=["meta"])
def health():
    """存活探针：容器编排/演示前自检用，不鉴权。"""
    return {"status": "ok", "service": "prismedu-api"}
