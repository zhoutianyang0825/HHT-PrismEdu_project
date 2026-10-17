"""依赖注入与双通道 scope：get_current_user / require_any / get_scopes / 明细·聚合守卫。

 ★ 本文件是权限模型的心脏，两条纪律（PRD 2.3 关键禁令）：
   1) detail（明细）与 aggregate（聚合）两条通道独立返回、独立消费，永不融合；
   2) 明细接口只认 detail/self，聚合接口只认 aggregate/detail——
      绝不允许把 (grade, subject) 展开成"全年级班级明细"喂给明细通道。
"""
from typing import Any

from fastapi import Depends, HTTPException, status
# 陌生类 OAuth2PasswordBearer：FastAPI 内置的"取 token 插座"。它本身不验证任何东西，
#   只干一件事：看到请求头 Authorization: Bearer xxxxx 就把 xxxxx 抽出来，
#   传给下面标了 Depends(oauth2_scheme) 的参数；没有这个头直接回 401。
#   tokenUrl 只是 OpenAPI 文档里"去哪换 token"的展示信息，不影响运行逻辑
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_token
from app.db.session import get_db

# tokenUrl 只影响 OpenAPI 文档展示，M5 接上 /api/auth/login 后即可交互调试
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class CurrentUser:
    """从 JWT 解出的当前用户。roles 保证非空、已过滤非法值。"""

    def __init__(self, user_id: int, username: str, roles: list[str]):
        self.id = user_id
        self.username = username
        self.roles = roles


def get_current_user(
    # 陌生点 Depends：FastAPI 依赖注入——参数声明 Depends(某函数) 后，
    #   每个请求进来 FastAPI 自动先跑那个函数，把返回值填进这个参数。
    #   依赖还能嵌套：get_db 也是依赖，FastAPI 会自动先把 db 备好。
    #   好处：接口函数里看不到"取 token/开连接"的样板代码，只写业务
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """JWT → CurrentUser。is_active=False 一律 401（停用账号即使 token 未过期也不许用）。"""
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
        roles = [r for r in payload.get("roles", []) if r in {"student", "teacher", "researcher", "admin"}]
    except Exception:
        raise invalid
    if not roles:
        raise invalid

    from app.models import User  # 局部 import 避免循环依赖
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise invalid
    return CurrentUser(user_id=user_id, username=payload.get("username", user.username), roles=roles)


# ---------------------------------------------------------------- scope 构建
def build_scopes(
    roles: list[str],
    teacher_classes: list[tuple[int, str]] | None,
    research_leads: list[tuple[str, str]] | None,
    user_id: int,
) -> list[dict[str, Any]]:
    """角色集合 → 双通道 scope 列表（纯函数，单测主战场）。

    得到的数据：
      roles              角色数组，如 ["teacher", "researcher"]
      teacher_classes    该用户在 teaching_assignments 里的任教记录，如 [(3, "math"), (7, "math")]
      research_leads     该用户在 research_leads 里的任命记录，如 [("初二", "math")]
      user_id            用户 id（student 通道用）
    要做的操作：每个角色各算各的通道，append 进同一个列表但**各自独立、互不展开**
    返回：形如
      [{"type": "detail",    "class_ids": [3, 7], "subject": "math"},
       {"type": "aggregate", "grade": "初二",    "subject": "math"}]
    坑：teacher 有角色但 teaching_assignments 为空 → 明细通道 class_ids=[]，
        所有明细查询自然查空，而不是报错或放全量——空 scope 是安全态。
    """
    scopes: list[dict[str, Any]] = []
    # 每角色一个通道；同一通道可能多条（如兼任两个年级组长），逐条 append 不合并
    if "teacher" in roles or "admin" in roles:
        class_ids = [cid for cid, _ in (teacher_classes or [])]
        subjects = sorted({subj for _, subj in (teacher_classes or [])})
        scopes.append({"type": "detail", "class_ids": class_ids, "subjects": subjects})
    if "researcher" in roles or "admin" in roles:
        for grade, subject in research_leads or []:
            scopes.append({"type": "aggregate", "grade": grade, "subject": subject})
    if "student" in roles:
        scopes.append({"type": "self", "student_id": user_id})
    return scopes


def gather_scope_inputs(db: Session, user: CurrentUser) -> tuple[list, list]:
    """从 PG 拉取 scope 数据源（teaching_assignments / research_leads）。

    得到的数据：db 会话 + 当前用户
    返回：(teacher_classes, research_leads)，供 build_scopes 消费
    说明：改了授课关系/任命，下一个请求这里就查到新数据 → 权限即时生效，无需重新登录
    """
    from app.models import ResearchLead, TeachingAssignment

    teacher_classes: list[tuple[int, str]] = []
    research_leads: list[tuple[str, str]] = []
    if "teacher" in user.roles or "admin" in user.roles:
        rows = db.execute(
            select(TeachingAssignment.class_id, TeachingAssignment.subject)
            .where(TeachingAssignment.teacher_id == user.id)
        ).all()
        teacher_classes = [(cid, subj) for cid, subj in rows]
    if "researcher" in user.roles or "admin" in user.roles:
        rows = db.execute(
            select(ResearchLead.grade, ResearchLead.subject)
            .where(ResearchLead.user_id == user.id)
        ).all()
        research_leads = [(g, s) for g, s in rows]
    return teacher_classes, research_leads


def get_scopes(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """FastAPI 依赖：scope 列表。明细/聚合两个通道在列表里并存但类型隔离。"""
    teacher_classes, research_leads = gather_scope_inputs(db, user)
    return build_scopes(user.roles, teacher_classes, research_leads, user.id)


# ---------------------------------------------------------------- 接口级守卫
def require_any(*required_roles: str):
    """接口级角色守卫工厂：命中任一角色即放行；admin 恒放行。

    陌生写法"装饰器工厂"（函数返回函数）：外层 require_any 收角色参数，
    内层 guard 收请求上下文。用法：
        @router.get(..., dependencies=[Depends(require_any("teacher", "researcher"))])
    为啥要两层：Depends 注册的是 guard，但 guard 需要知道"允许哪些角色"——
    外层调用 require_any(...) 时把角色"记"进内层闭包（closure），
    一句话：外层调用一次做配置，内层每个请求跑一次做检查。
    *required_roles 的 *：可变参数，传几个角色都行，收进来是 tuple。
    用法：@router.get(..., dependencies=[Depends(require_any("teacher", "researcher"))])
    """
    def guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if "admin" in user.roles or set(user.roles) & set(required_roles):
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden: insufficient role")
    return guard


def require_detail_channel(
    user: CurrentUser = Depends(get_current_user),
    scopes: list[dict] = Depends(get_scopes),
) -> list[dict]:
    """明细通道守卫：学生明细类接口（作答内容、批改详情）专用。

    放行条件：scope 里存在 type ∈ {detail, self}，或用户是 admin。
    🔴 researcher 只有 aggregate 通道 → 到这里必 403。
       接口层面就不提供"按年级查学生明细"的路，这是设计而不是遗漏。
    """
    if "admin" in user.roles:
        return scopes
    if any(s["type"] in {"detail", "self"} for s in scopes):
        return scopes
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden: detail scope required")


def require_aggregate_channel(
    scopes: list[dict] = Depends(get_scopes),
) -> list[dict]:
    """聚合通道守卫：年级/班级聚合类接口（R-01、R-04、T-03）专用。

    放行条件：scope 里存在 type ∈ {aggregate, detail}。
    detail 通道天然可以看自己任教班级的聚合（T-03 班级错题汇总）。
    """
    if any(s["type"] in {"aggregate", "detail"} for s in scopes):
        return scopes
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden: aggregate scope required")
