"""依赖注入与双通道 scope：get_current_user / require_any / get_scopes / 明细·聚合守卫。

 ★ 本文件是权限模型的心脏，两条纪律（PRD 2.3 关键禁令）：
   1) detail（明细）与 aggregate（聚合）两条通道独立返回、独立消费，永不融合；
   2) 明细接口只认 detail/self，聚合接口只认 aggregate/detail，
      绝不允许把 (grade, subject) 展开成 "全年级班级明细" 喂给明细通道。数据隔离
"""
from typing import Any

from fastapi import Depends, HTTPException, status

from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_token
from app.db.session import get_db


# 陌生类 OAuth2PasswordBearer：FastAPI 内置，标准化"取 token值"函数。
# 它不验证任何东西，只干一件事：
#   看到请求头 Authorization: Bearer eyJhbGci... → 抽出 eyJhbGci... 
#   头里没 token → 直接回 401，后面的代码根本不执行
# tokenUrl 只是 OpenAPI 文档里"去哪换 token"的展示信息，不影响运行逻辑
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class CurrentUser:
    """从 JWT 解出的当前用户。roles 保证非空、已过滤非法值。"""

    def __init__(self, user_id: int, username: str, roles: list[str]):
        self.id = user_id
        self.username = username
        self.roles = roles


def get_current_user(
    # 陌生点 Depends：FastAPI 依赖注入：
    #   参数声明 Depends(某函数) 后，每个请求进来 FastAPI 自动先跑那个函数，把返回值填进这个参数。
    token: str = Depends(oauth2_scheme),  # 从请求头抽 token 填进来
    db: Session = Depends(get_db),        # yield一个session出来
) -> CurrentUser:
    
    """JWT → CurrentUser。is_active=False 一律 401（停用账号即使 token 未过期也不许用）。"""

    # 把 401 异常对象提前造好存进变量
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

    # ---- 第 2 步：查库验人
    from app.models import User     # 局部 import 避免循环依赖
    user = db.get(User, user_id)    # 调用 get_db 得到一个 pgsql的 session，然后在User表里，按主键id=user_id查
    if user is None or not user.is_active:
        raise invalid
    
    return CurrentUser(user_id=user_id, username=payload.get("username", user.username), roles=roles)



# ----------------scope 三件套：查原料 → 拼通道 → 挂依赖
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
            select(TeachingAssignment.class_id, TeachingAssignment.subject) # 只查两列
            .where(TeachingAssignment.teacher_id == user.id)
        ).all()  

        # 产出形态：[(3, "math"), (7, "math")] 
        teacher_classes = [(cid, subj) for cid, subj in rows]

    if "researcher" in user.roles or "admin" in user.roles:
        rows = db.execute(
            select(ResearchLead.grade, ResearchLead.subject)
            .where(ResearchLead.user_id == user.id)
        ).all()

        # 产出形态：[("初二", "math")]
        research_leads = [(g, s) for g, s in rows]

    return teacher_classes, research_leads



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

    要做的操作：每个角色各算各的通道，append 进同一个列表，但通过 scopes["type"] 隔离数据

    返回scopes数据结构：
      [{"type": "detail",    "class_ids": [3, 7], "subject": "math"},
       {"type": "aggregate", "grade": "初二",    "subject": "math"}]
    """
    scopes: list[dict[str, Any]] = []

    # ① teacher/admin → 明细通道（班级粒度）
    if "teacher" in roles or "admin" in roles:
        class_ids = [cid for cid, _ in (teacher_classes or [])]
        subjects = sorted({subj for _, subj in (teacher_classes or [])})
        scopes.append({"type": "detail", "class_ids": class_ids, "subjects": subjects})

    # ② researcher/admin → 聚合通道（年级粒度），每条任命一个 scope，兼任两个年级就两条，不合并
    if "researcher" in roles or "admin" in roles:
        for grade, subject in research_leads or []:
            scopes.append({"type": "aggregate", "grade": grade, "subject": subject})

    # ③ student → self 通道（只看自己）
    if "student" in roles:
        scopes.append({"type": "self", "student_id": user_id})

    return scopes




def get_scopes(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    
    """FastAPI 依赖：scope 列表。明细/聚合两个通道在列表里并存但类型隔离。"""
    teacher_classes, research_leads = gather_scope_inputs(db, user)
    return build_scopes(user.roles, teacher_classes, research_leads, user.id)



# ---------------------------------------------------------------- 接口级守卫
def require_any(*required_roles: str):
    """
    接口级角色守卫工厂：命中任一角色即放行；admin 恒放行。

    *required_roles：收集所有位置参数，装成元组，

    陌生写法"装饰器工厂"（函数返回函数）：外层 require_any 收角色参数，内层 guard 收请求上下文。
    用法：
        @router.get(..., dependencies=[Depends(require_any("teacher", "researcher"))])
    """
    def guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser: 
        if "admin" in user.roles or set(user.roles) & set(required_roles): # set() 把 tuple 转成集合
            # admin 恒放行
            # 两个集合存在交集role
            return user

        # 将403异常对象提前造好存进变量
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden: insufficient role")
    
    return guard  # 返回工具函数本身，先不执行


def require_detail_channel(
    user: CurrentUser = Depends(get_current_user),
    scopes: list[dict] = Depends(get_scopes),
) -> list[dict]:
    """明细通道守卫：学生明细类接口（作答内容、批改详情）专用。

    放行条件：scope 里存在 type ∈ {detail, self}，或用户是 admin。
    🔴 researcher 只有 aggregate 通道 → 到这里必 403。
       本接口不提供 "按年级查学生明细"的路
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

"""
角色设定：R003，researcher，负责初二数学。目标接口：GET /api/students/records（学生作答明细）。

完整时间线：

t1  请求到达，请求头 Authorization: Bearer eyJhbGciOi...
    （前端登录时 create_access_token 签的，12h 有效期内）

t2  依赖树启动：oauth2_scheme 从请求头抽出 token 字符串；
    get_db 借出 session（session.py 的惰性单例，此刻连接从池里取）

t3  get_current_user 执行：
    decode_token 验签 ✅（没人改过）/ 验 exp ✅（没过期）
    → 查库 db.get(User, 3) → is_active=True ✅
    → 产出 CurrentUser(id=3, username="R003", roles=["researcher"])

t4  get_scopes 执行：
    gather_scope_inputs 查 research_leads 表 → [("初二", "math")]
    build_scopes 拼通道：
    → [{"type": "aggregate", "grade": "初二", "subject": "math"}]
    （注意：researcher 分支只进②不进①，列表里【没有】detail 通道）

t5  require_detail_channel 检查：
    admin？ → roles 里没有，跳过
    any(s["type"] in {"detail","self"})？ → 唯一的 scope 是 aggregate，不命中 → False

t6  raise HTTPException(403, "forbidden: detail scope required")
    ★ 请求在此终止。接口函数体一行代码都没执行——
      对业务代码来说，这次"越权请求"从来没发生过

t7  同一个 token，换调 GET /api/stats/grade-mistakes（聚合接口）
    t3/t4 原样重跑（scopes 还是那条 aggregate）

t8  require_aggregate_channel 检查：
    any(s["type"] in {"aggregate","detail"})？ → aggregate 命中 ✅
    → scopes 注入接口 → 接口按 (初二, math) 出聚合数据 → 200 返回

"""