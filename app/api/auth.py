"""
M5-T1 · 登录端点：用户名密码 → 多角色 JWT（roles: [...] 数组一次签发）。

【完整时间线】（开发计划 M5 完成判据的 curl 场景）
  t1  POST /api/auth/login {"username": "R003", "password": "..."}
  t2  锁定检查：security.is_locked —— 15 分钟窗口内错满 5 次 → 423，试都不让试
      （计数存哪由 login_guard 决定：有 REDIS_URL 就用 Redis，多 worker 共享；
        没配或 Redis 挂了则退回进程内存，登录绝不因 Redis 故障而失败）
  t3  查 users 表：找不到 / is_active=False → 401（停用账号即使密码对也不放行）
  t4  verify_password（bcrypt checkpw）失败 → register_failed_login 记一笔 → 401
  t5  查 user_roles 表拼 roles 数组，如 ["teacher", "researcher"]
  t6  create_access_token 签 12h JWT；清空失败计数；返回 token + 角色 + 学生档案 id

【登录留痕（0004 迁移起）】
  上面 t2（locked）/ t4（bad_password）/ t5.5（inactive）/ t6（success）四个出口
  各写一行 login_logs：谁、什么时候、从哪个 IP、登没登成。
  成功行给使用监控（usage/summary 算活跃），失败行给安全监控（locked 突增=爆破）。

【为什么返回体里带 student_id】
  JWT 里只有 users.id，但学生端所有业务接口用的都是 students.id（档案 id）。
  登录时顺手查一次 students 表带上，前端就不用再调"我是谁"接口。
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
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
from app.models import LoginLog, Student, User, UserRole
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


def _log_attempt(db: Session, request: Request, username: str,
                 status_: str, user_id: int | None = None) -> None:
    """往 login_logs 写一行用户登录尝试，并就地提交。

    得到的数据：登录名、成败状态、可选的 user_id、Request 对象（取 IP/User-Agent）
    要做的操作：插一行 LoginLog → 立即 commit（失败路径随后抛 HTTPException，
                不提交的话行会被 get_db 的 close 连带回滚丢掉）
    返回：None

    为什么四个出口都要记：
        success = 使用监控（谁今天用了系统）；
        bad_password/inactive/locked = 安全监控（locked 突增 = 正在被爆破）。

    为什么 user_id 允许 NULL：爆破试探的登录名在 users 表里不存在。
    """
    client = request.client
    ua = request.headers.get("user-agent", "")
    db.add(LoginLog(
        user_id=user_id,
        username=username[:64],
        status=status_,
        ip=client.host if client else None,
        user_agent=ua[:255] if ua else None,
    ))
    db.commit()


def _authenticate(db: Session, request: Request, username: str, password: str) -> dict:
    """登录核心流程：账户锁定检查 → 查人 → 验密 → 拼角色 → 签发。两个登录端点共用。

    得到的数据：用户名明文 + 密码明文（来自 JSON body 或 OAuth2 form）
    要做的操作：t2~t6 完整链路，每个出口写一行 login_logs
    返回：成功 → 含 access_token 的完整响应体 dict；失败 → 抛对应 HTTPException
    为什么抽出来：/login 收 JSON（前端用），/token 收 form（Swagger Authorize 用），
    两边的"格式壳"不同但核心流程必须逐字一致——抽一处，防止双份实现漂移。
    """
    # ---- t2 锁定检查：被锁的账号连"试密码"的机会都不给（防爆破）----
    if is_locked(username):
        _log_attempt(db, request, username, "locked")
        raise HTTPException(
            status.HTTP_423_LOCKED,
            detail="账号已锁定（连续失败 5 次），15 分钟后自动解锁",
        )

    # ---- t3 查人 ----
    user = db.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()

    # ---- t4 密码比对 ----
    if user is None or not verify_password(password, user.password_hash):
        register_failed_login(username)   # 记一笔失败（滑动窗口内第 5 次触发锁定）
        _log_attempt(db, request, username, "bad_password",
                     user_id=user.id if user else None)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    if not user.is_active:
        # 停用账号：密码对也 401。注意不记失败次数——它不是爆破，是权限问题
        _log_attempt(db, request, username, "inactive", user_id=user.id)
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

    # ---- t6 签发 + 清空失败记录计数 + LoginLog 表留痕 ----
    clear_failures(username)
    token = create_access_token(user_id=user.id, username=user.username, roles=roles)
    _log_attempt(db, request, username, "success", user_id=user.id)

    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "name": user.name,
        "roles": roles,
        "student_id": student_id,
    }


# api接口层：
@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    """登录（JSON 版）：供前端 web_app 用。成功返回 JWT；失败按原因分别响应。

    每个出口都写 login_logs 一行，使用监控与安全监控共用这张表。
    """
    return _authenticate(db, request, body.username, body.password)


@router.post("/token", summary="登录（OAuth2 form 版，供 /docs 的 Authorize 按钮用）")
def token(request: Request, form: OAuth2PasswordRequestForm = Depends(),
          db: Session = Depends(get_db)):
    """登录（form 版）：收 application/x-www-form-urlencoded，专供 /docs 的 Authorize。

    得到的数据：Swagger UI Authorize 弹窗提交的 username + password 表单字段
    要做的操作：复用 _authenticate 全流程（含 login_logs 留痕），只取 token 字段
    返回：标准 OAuth2 响应 {access_token, token_type}——Swagger 只认这个结构

    为什么必须单独有它：Swagger 的 Authorize 表单按 OAuth2 规范发 form 数据，
    而前端 /login 收 JSON——格式壳不同，强行兼容会把主端点签名弄脏；
    独立一个端点各走各的壳，核心逻辑共用 _authenticate 不漂移。
    """
    out = _authenticate(db, request, form.username, form.password)
    return {"access_token": out["access_token"], "token_type": out["token_type"]}


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
