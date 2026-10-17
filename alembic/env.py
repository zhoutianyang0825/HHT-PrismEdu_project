"""Alembic 环境：把 app/models 全部 import 进来，target_metadata 指向 Base.metadata。

 要做的操作：读 .env 的 DATABASE_URL（alembic.ini 里不写死连接串，避免两处配置漂移）
 陌生库 Alembic（数据库迁移工具）：表结构改动（M3 加字段等）不靠手改库，
   而是生成带版本号的迁移脚本（alembic/versions/xxx.py），按顺序 upgrade/downgrade。
   本文件是 Alembic 每次运行必读的"引导脚本"，负责告诉它连哪个库、对照哪份表结构。

 Alembic 工作流（记这三条命令就够）：
   uv run alembic upgrade head                 # 把库升到最新版（首次建 20 张表）
   uv run alembic revision --autogenerate -m "msg"   # 对比模型 vs 库，自动生成差异迁移
   uv run alembic downgrade -1                 # 回退一个版本
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 保证 alembic 命令在任何 cwd 下都能 import 到 app 包（backend/ 为项目根）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ★ 关键：import app.models 触发全部 20 张表的注册，metadata 才是完整的。
#   漏 import 哪个模型文件，那张表就是"黑户"——autogenerate 会对它视而不见
from app.models import Base  # noqa: E402
from app.core.config import settings  # noqa: E402

config = context.config

# fileConfig：把 alembic.ini 里 [loggers] 段的日志配置加载进来（Alembic 标准模板自带）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 连接串以代码里的 settings 为准（同步驱动 psycopg）。
# set_main_option：运行时改写 alembic.ini 的配置项——连接串只在代码里维护一份
config.set_main_option("sqlalchemy.url", settings.database_url)

# ★ target_metadata = "标准答案"：autogenerate 的原理就是拿它和真实库做 diff——
#   模型里有、库里没有 → 生成 create_table / add_column；反之生成 drop。
#   不设它，--autogenerate 会生成空迁移
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不连库（--sql 模式），本项目基本用不到但保留标准结构。"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直连 PG 执行迁移。alembic upgrade head 走这里。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
