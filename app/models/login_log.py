"""登录留痕一张表：login_logs（谁、什么时候、从哪、登没登成）。

 得到的数据：每次登录尝试（成功/密码错/被锁/停用）各一行
 要做的操作：auth.login 的四个出口各插一行，随请求事务提交
 返回：无（纯写入，查询走 admin 的 usage/summary 聚合）

【为什么独立建表，而不是复用 audit_logs】
  audit_logs 是"敏感业务操作"留痕（批改确认/CSV 导入/授课变更），量小、action 语义重；
  登录是每个用户每天都会发生的热路径事件，混进去会把审计日志刷屏，
  两边的查询口径也完全不同（审计按 action 查、登录按人/时间/成败查）。

【为什么 user_id 用裸 Integer 不带外键】
  和 audit_logs 同一个设计：登录日志的寿命必须长于账号本身——
  账号被删除（users 行没了）后，"这个账号曾经在什么时候登录过"仍要能查。
  带外键的话账号一删日志级联消失或报错，恰好毁掉审计价值。
  user_id 为 NULL = 登录名在 users 表里根本不存在（爆破试探的典型特征）。

【为什么记失败尝试而不只记成功】
  成功 = 使用监控（谁今天用了系统）；
  失败 = 安全监控（locked 突增 = 正在被爆破，bad_password = 有人忘密码）。
  一张表两种价值，成本只多一个 status 列。
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LoginLog(Base):
    __tablename__ = "login_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 裸 Integer 不带 FK：账号删除后日志仍留存（同 audit_logs 的设计）
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="users.id；登录名不存在时为 NULL")
    username: Mapped[str] = mapped_column(String(64), index=True, comment="登录名（用户输入原文）")
    # status ∈ {success, bad_password, inactive, locked}
    status: Mapped[str] = mapped_column(String(16), index=True, comment="success/bad_password/inactive/locked")
    ip: Mapped[str | None] = mapped_column(String(64), comment="客户端 IP；取不到为 NULL")
    user_agent: Mapped[str | None] = mapped_column(String(255), comment="浏览器 UA，截断到 255")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
