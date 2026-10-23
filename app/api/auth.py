"""
M5-T1 · 登录端点：用户名密码 → 多角色 JWT（roles: [...] 数组一次签发）。

【完整时间线】（开发计划 M5 完成判据的 curl 场景）
  t1  POST /api/auth/login {"username": "R003", "password": "..."}
  t2  锁定检查：security.is_locked —— 15 分钟窗口内错满 5 次 → 423，试都不让试
  t3  查 users 表：找不到 / is_active=False → 401（停用账号即使密码对也不放行）
  t4  verify_password（bcrypt checkpw）失败 → register_failed_login 记一笔 → 401
  t5  查 user_roles 表拼 roles 数组，如 ["teacher", "researcher"]
  t6  create_access_token 签 12h JWT；清空失败计数；返回 token + 角色 + 学生档案 id

【为什么返回体里带 student_id】
  JWT 里只有 users.id，但学生端所有业务接口用的都是 students.id（档案 id）。
  登录时顺手查一次 students 表带上，前端就不用再调"我是谁"接口。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.security import (
    clear_failures,
    create_access_token,
    is_locked,
    register_failed_login,
    verify_password,
)
from app.db.session import get_db
from app.models import Student, User, UserRole
from app.schemas.api import LoginIn

router = APIRouter()


def _load_roles(db: Session, user_id: int) -> list[str]:
    """查 user_roles 拼角色数组。

    得到的数据：user_id（users.id）
    要做的操作：SELECT role FROM user_roles WHERE user_id=?
    返回：["teacher", "researcher"]（顺序按插入序，JWT 原样保序）
    为什么独立成函数：登录和"当前用户信息"接口都要用
    """
    rows = db.execute(select(UserRole.role).where(UserRole.user_id == user_id)).all()
    return [r[0] for r in rows]


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    """登录：成功返回 JWT；失败按锁定/停用/密码错三种原因分别响应。"""

    # ---- t2 锁定检查：被锁的账号连"试密码"的机会都不给（防爆破）----
    if is_locked(body.username):
        raise HTTPException(
            status.HTTP_423_LOCKED,
            detail="账号已锁定（连续失败 5 次），15 分钟后自动解锁",
        )

    # ---- t3 查人 ----
    user = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()

    # ---- t4 密码比对 ----
    if user is None or not verify_password(body.password, user.password_hash):
        register_failed_login(body.username)   # 记一笔失败（滑动窗口内第 5 次触发锁定）
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    if not user.is_active:
        # 停用账号：密码对也 401。注意不记失败次数——它不是爆破，是权限问题
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="账号已停用，请联系管理员")

    # ---- t5 拼角色。roles 为空 = 数据异常，直接报错而不是发一个处处 403 的 token ----
    roles = _load_roles(db, user.id)
    if not roles:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"用户 {user.username} 没有任何角色，请管理员先补 user_roles")

    # 学生档案 id：学生端业务全用它（students.id），登录时顺手查掉
    student_id = None
    if "student" in roles:
        profile = db.execute(
            select(Student.id).where(Student.user_id == user.id)
        ).scalar_one_or_none()
        student_id = profile   # 没建档案就是 None，前端提示找管理员补 students 行

    # ---- t6 签发 + 清失败计数 ----
    clear_failures(body.username)
    token = create_access_token(user_id=user.id, username=user.username, roles=roles)

    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "name": user.name,
        "roles": roles,
        "student_id": student_id,
    }


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """当前用户信息：前端启动时校验 token 是否仍有效 + 拿最新角色。

    为什么每次实时查库：admin 改了角色/停用了账号，这里立即反映——
    JWT 本身 12h 不可作废，靠这个接口对账（get_current_user 内部也查了 is_active）。
    """
    return {
        "user_id": user.id,
        "username": user.username,
        "roles": _load_roles(db, user.id),
    }
