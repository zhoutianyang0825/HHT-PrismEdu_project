"""
审计域一张表：audit_logs（简化合规日志）。

 得到的数据：敏感操作留痕（授课关系变更、批改确认、CSV 导入、组长任命等）
 关键设计：target 用 "表名:id" 字符串（如 "teaching_assignments:12"），
          不做外键——被删对象的日志仍要留存，这正是审计的意义。
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="操作人，系统操作可空")
    action: Mapped[str] = mapped_column(String(64), index=True, comment="如 user.import / teaching.update")
    target: Mapped[str] = mapped_column(String(128), comment="操作对象，如 teaching_assignments:12")
    detail: Mapped[str | None] = mapped_column(Text, comment="变更摘要，如 JSON diff")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
