"""0004：登录留痕表 login_logs——使用监控的地基（2026-10-28）。

背景：
  要回答"谁在用系统、什么时候用的"，之前只有两条线索：
    ① student_answers / grading_records / exam_papers —— 只有"用了之后"的业务行为，
       一个今天只登录没做事的学生完全不可见；
    ② Redis 的登录失败计数 —— 只有失败、还有 15 分钟 TTL，一过期就尸骨无存。
  成功登录从来没有落过任何表，监控"活跃"无从谈起。

本迁移做一件事：
  新表 login_logs —— 每次登录尝试一行：
    status ∈ {success, bad_password, inactive, locked}
    成功 = 使用监控（谁今天用了系统）
    失败 = 安全监控（locked 突增 = 正在被爆破）
  user_id 裸 Integer 不带外键：账号删除后"他曾登录过"的记录仍要留存（同 audit_logs）。

应用：uv run alembic upgrade head

回滚：uv run alembic downgrade -1（只删 login_logs，不动任何历史数据）
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_login_logs"
down_revision = "0003_exam_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=True,
                  comment="users.id；登录名不存在时为 NULL"),
        sa.Column("username", sa.String(64), nullable=False,
                  comment="登录名（用户输入原文）"),
        sa.Column("status", sa.String(16), nullable=False,
                  comment="success/bad_password/inactive/locked"),
        sa.Column("ip", sa.String(64), nullable=True,
                  comment="客户端 IP；取不到为 NULL"),
        sa.Column("user_agent", sa.String(255), nullable=True,
                  comment="浏览器 UA，截断到 255"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    # 查询口径：usage/summary 按 created_at 聚合、安全排查按 username 找人
    op.create_index("ix_login_logs_user_id", "login_logs", ["user_id"])
    op.create_index("ix_login_logs_username", "login_logs", ["username"])
    op.create_index("ix_login_logs_status", "login_logs", ["status"])
    op.create_index("ix_login_logs_created_at", "login_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_login_logs_created_at", table_name="login_logs")
    op.drop_index("ix_login_logs_status", table_name="login_logs")
    op.drop_index("ix_login_logs_username", table_name="login_logs")
    op.drop_index("ix_login_logs_user_id", table_name="login_logs")
    op.drop_table("login_logs")
