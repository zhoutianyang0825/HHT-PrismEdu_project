"""M3-T5 · 敏感操作审计埋点：所有埋点统一走 record_audit。

 本文件定位：一行 AuditLog 的写入口，M3 用它记录批改确认/驳回，M5 admin 的授课关系变更 / 用户导入 也直接复用
"""
from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(
    db: Session,
    user_id: int | None,
    action: str,
    target: str,
    detail: str | None = None,
) -> AuditLog:
    """往 session 里追加一条审计日志，不 commit。

    得到的数据（逐参数）：
      db        SQLAlchemy Session
      user_id   操作人 id；系统自动操作（如 seed 脚本）传 None
      action    动作名，约定 "<域>.<动作>"，如 grading.confirm / grading.reject
      target    操作对象，"表名:id" 字符串，如 "student_answers:1001"
      detail    变更摘要（JSON 字符串），如 '{"final_score": 8, "action": "edit"}'

    要做的操作：db.add + db.flush
    返回：AuditLog 对象（已带自增 id），形如 AuditLog(id=37, action="grading.confirm", ...)

    为什么：审计与业务写入必须同事务 —— 业务回滚而审计留下就是假账；
  
    """

    # 陌生点 target 不做外键：被删对象的日志仍要留存，这正是审计的意义
    log = AuditLog(user_id=user_id, action=action, target=target, detail=detail)
    db.add(log)
    # 陌生点 flush ≠ commit：flush 只把 SQL 发给库、拿到自增 id，事务仍开着；
    #   commit 交给调用方，保证审计与业务写入同生共死
    db.flush()
    return log
