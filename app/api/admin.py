# app/api/admin.py
# M5-T3 · 管理后台 API（A-01）：用户 CRUD / CSV 导入 / 授课关系 / 组长任命 / 班级 / 日志。
#
# 【整体守卫】router 级挂 require_any("admin")——架构文档 8.5 的原话：
#   "admin API 整体守卫挂在 router 依赖上，漏挂一个端点就是越权入口"。
#   挂在 APIRouter(dependencies=...) 上，本文件所有端点一次覆盖，谁也漏不掉。
#
# 【CSV 导入的事务性（架构文档坑表"CSV 导入部分写入"）】
#   逐行校验 → 全部通过才 commit；任何一行坏 → rollback，整批一行不进库；
#   报告返回每条失败行的行号与原因。先在 10 行小文件验证，再上大文件。
#
# 【勾 researcher 必须有任命（架构文档坑表"勾了 researcher 未填任命"）】
#   创建/修改用户时 roles 含 researcher → 必须同时给 research_leads，否则 400。
#   空任命的 researcher 登录后处处 403，排查一次一下午——保存前拦住最便宜。
from csv import DictReader
from io import StringIO
import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user, require_any
from app.core.security import hash_password, validate_password_strength
from app.db.session import get_db
from app.models import (
    AuditLog,
    Class,
    ResearchLead,
    TeachingAssignment,
    User,
    UserRole,
)
from app.schemas.api import (
    ClassIn,
    CsvImportIn,
    ResearchLeadIn,
    TeachingAssignmentIn,
    UserCreateIn,
    UserUpdateIn,
)
from app.services.audit_service import record_audit

# ★ 整体守卫：require_any("admin") 工厂产出的 guard 挂在组级依赖上
router = APIRouter(dependencies=[Depends(require_any("admin"))])

VALID_ROLES = {"student", "teacher", "researcher", "admin"}

# CSV 模板表头（import-template 端点与导入校验共用，防止两头不一致）
CSV_HEADERS = ["username", "password", "name", "roles"]


# =====================================================================
# 用户查询 / 单条 CRUD
# =====================================================================
def _user_roles(db: Session, user_id: int) -> list[str]:
    rows = db.execute(select(UserRole.role).where(UserRole.user_id == user_id)).all()
    return [r[0] for r in rows]


def _user_out(db: Session, u: User) -> dict:
    """User 行 → 响应 dict（带角色数组与停用标记）。"""
    return {"id": u.id, "username": u.username, "name": u.name,
            "is_active": u.is_active, "roles": _user_roles(db, u.id),
            "created_at": str(u.created_at)}


@router.get("/users")
def list_users(role: str | None = Query(default=None),
               keyword: str | None = Query(default=None, description="按用户名/姓名模糊"),
               page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
               db: Session = Depends(get_db)):
    """用户列表：?role=&keyword=&page= 。停用用户带 is_active=False 标记。"""
    stmt = select(User).order_by(User.id)
    if keyword:
        stmt = stmt.where(User.username.contains(keyword) | User.name.contains(keyword))
    if role:
        stmt = stmt.join(UserRole, UserRole.user_id == User.id).where(UserRole.role == role)

    total = len(db.execute(stmt).scalars().all())   # 量小（全校师生），简版先全查再切片
    rows = db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all()
    return {"total": total, "page": page, "items": [_user_out(db, u) for u in rows]}


def _sync_roles(db: Session, user_id: int, roles: list[str]) -> None:
    """整体替换 user_roles：删旧插新（关系表小，直接重建最不易漏）。"""
    db.query(UserRole).filter(UserRole.user_id == user_id).delete()
    for r in dict.fromkeys(roles):          # dict.fromkeys 保序去重
        db.add(UserRole(user_id=user_id, role=r))


def _ensure_researcher_has_lead(roles: list[str], leads: list[dict] | None) -> None:
    """勾 researcher 必须带任命，否则 400（坑表原话：保存前校验）。"""
    if "researcher" in roles and not leads:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="勾选 researcher 角色必须同时提供 research_leads（年级+学科），"
                                   "否则该账号空 scope 登录后处处 403")


def _write_research_leads(db: Session, user_id: int, leads: list[dict]) -> None:
    """重建 research_leads（与 _sync_roles 同一套"整体替换"策略）。"""
    db.query(ResearchLead).filter(ResearchLead.user_id == user_id).delete()
    for it in leads:
        db.add(ResearchLead(user_id=user_id, grade=it["grade"], subject=it.get("subject", "math")))


@router.post("/users", status_code=201)
def create_user(body: UserCreateIn, user: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """单条创建用户。密码强度 / 用户名唯一 / 角色合法 / researcher 任命四处校验。"""
    validate_password_strength(body.password)   # 不合格抛 ValueError → 转 400
    if db.execute(select(User).where(User.username == body.username)).scalar_one_or_none():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"用户名 {body.username} 已存在")

    _ensure_researcher_has_lead(body.roles, body.research_leads)

    u = User(username=body.username, password_hash=hash_password(body.password), name=body.name)
    db.add(u)
    db.flush()                                   # 拿 u.id 供角色/任命表用
    _sync_roles(db, u.id, body.roles)
    if body.research_leads:
        _write_research_leads(db, u.id, body.research_leads)

    record_audit(db, user.id, "user.create", f"users:{u.id}",
                 json.dumps({"username": body.username, "roles": body.roles}, ensure_ascii=False))
    db.commit()
    return _user_out(db, u)


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UserUpdateIn,
                user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """修改 / 停用 / 重置密码 / 调整角色。传了哪个字段才改哪个。"""
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="用户不存在")

    changed = {}
    if body.name is not None:
        u.name = body.name
        changed["name"] = body.name
    if body.is_active is not None:
        u.is_active = body.is_active
        changed["is_active"] = body.is_active
    if body.password is not None:
        validate_password_strength(body.password)
        u.password_hash = hash_password(body.password)
        changed["password"] = "reset"

    old_roles = _user_roles(db, user_id)
    if body.roles is not None:
        _ensure_researcher_has_lead(body.roles, body.research_leads or [])
        _sync_roles(db, user_id, body.roles)
        changed["roles"] = old_roles + body.roles
    if body.research_leads is not None:
        _write_research_leads(db, user_id, body.research_leads)
        changed["research_leads"] = body.research_leads

    record_audit(db, user.id, "user.update", f"users:{user_id}",
                 json.dumps(changed, ensure_ascii=False))
    db.commit()
    return _user_out(db, u)


# =====================================================================
# CSV 批量导入（P0 核心：逐行校验、全过才写、报告带行号）
# =====================================================================
def _validate_csv_rows(csv_text: str, db: Session) -> tuple[list[dict], list[dict]]:
    """逐行校验 CSV，返回 (合格行, 失败报告)。

    校验五关（按检查成本从低到高排，坏行早死早超生）：
      ① 列名齐不齐  ② 密码强度  ③ roles 合法非空  ④ 用户名格式
      ⑤ 唯一性（库内已有 + 本批内重复，两处都查）
    返回的合格行还未写库——写库由调用方事务控制。
    """
    errors: list[dict] = []
    goods: list[dict] = []
    seen_usernames: set[str] = set()

    reader = DictReader(StringIO(csv_text.strip()))
    if reader.fieldnames is None or not set(CSV_HEADERS).issubset(set(reader.fieldnames)):
        return [], [{"line": 1, "reason": f"表头必须包含 {CSV_HEADERS}"}]

    for line_no, row in enumerate(reader, start=2):   # start=2：第 1 行是表头
        username = (row.get("username") or "").strip()
        password = row.get("password") or ""
        name = (row.get("name") or "").strip()
        roles = [r.strip() for r in (row.get("roles") or "").replace("；", ";").split(";") if r.strip()]

        # ② 密码强度
        try:
            validate_password_strength(password)
        except ValueError as e:
            errors.append({"line": line_no, "reason": f"密码不合格：{e}"})
            continue

        # ③ roles 合法非空
        if not roles:
            errors.append({"line": line_no, "reason": "roles 为空"})
            continue
        bad = set(roles) - VALID_ROLES
        if bad:
            errors.append({"line": line_no, "reason": f"非法角色 {bad}，合法值 {sorted(VALID_ROLES)}"})
            continue

        # ④ 用户名格式
        if len(username) < 2 or " " in username:
            errors.append({"line": line_no, "reason": f"用户名 '{username}' 不合法（≥2 字符且无空格）"})
            continue

        # ⑤ 唯一性：库内 + 本批
        exists = db.execute(select(User.id).where(User.username == username)).scalar_one_or_none()
        if exists is not None or username in seen_usernames:
            errors.append({"line": line_no, "reason": f"用户名 {username} 重复"})
            continue

        seen_usernames.add(username)
        goods.append({"username": username, "password": password,
                      "name": name or username, "roles": roles})

    return goods, errors


@router.post("/users/import")
def import_users(body: CsvImportIn, user: CurrentUser = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """CSV 批量导入：逐行校验、全过才写、失败报告带行号。

    完整时间线（2000 行、第 37 行密码太弱）：
      t1  _validate_csv_rows 逐行校验 → 合格 1999 行 + errors=[{line:37, reason:...}]
      t2  有失败行 → return 200 + {"ok": False, "errors": [...], "imported": 0}
          —— 注意：一行不写库。返回 200 是因为"导入被拒绝"是正常业务结果，不是服务器错误
      t3  全过 → 循环建 User+UserRole（同一事务）→ record_audit → commit
      t4  中途任何异常 → rollback，库里干干净净
    """
    goods, errors = _validate_csv_rows(body.csv_content, db)

    if errors:
        # 全过才写：有一行坏就整批拒绝。imported 恒 0，报告照实给行号
        return {"ok": False, "imported": 0, "errors": errors,
                "message": f"共 {len(errors)} 行不合格，整批未导入（事务性：坏行不写库）"}

    try:
        created = []
        for g in goods:
            u = User(username=g["username"], password_hash=hash_password(g["password"]),
                     name=g["name"])
            db.add(u)
            db.flush()
            _sync_roles(db, u.id, g["roles"])
            created.append({"id": u.id, "username": u.username, "roles": g["roles"]})

        record_audit(db, user.id, "user.import", "users",
                     json.dumps({"count": len(created)}, ensure_ascii=False))
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"ok": True, "imported": len(created), "errors": [], "items": created}


@router.get("/users/import-template")
def import_template():
    """下载 CSV 模板（text/csv）。前端直接展示说明 + 示例行。"""
    sample = "username,password,name,roles\nS101,Passw0rd1,张小明,student\nT020,Passw0rd2,李老师,teacher;researcher\n"
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        sample,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="users_import_template.csv"'},
    )


# =====================================================================
# 授课关系（teacher 明细 scope 的数据源）
# =====================================================================
@router.get("/teaching-assignments")
def list_assignments(db: Session = Depends(get_db)):
    rows = db.execute(
        select(TeachingAssignment, User.username, Class.grade, Class.name)
        .join(User, User.id == TeachingAssignment.teacher_id)
        .join(Class, Class.id == TeachingAssignment.class_id)
        .order_by(TeachingAssignment.teacher_id)
    ).all()
    return [{"id": ta.id, "teacher_id": ta.teacher_id, "teacher": username,
             "class_id": ta.class_id, "class_name": f"{grade}{name}", "subject": ta.subject}
            for ta, username, grade, name in rows]


@router.post("/teaching-assignments", status_code=201)
def create_assignment(body: TeachingAssignmentIn,
                      user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """加一条授课关系。改完即时生效：下一个请求 gather_scope_inputs 就查到新数据。"""
    dup = db.execute(
        select(TeachingAssignment).where(
            TeachingAssignment.teacher_id == body.teacher_id,
            TeachingAssignment.class_id == body.class_id,
            TeachingAssignment.subject == body.subject)
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="该授课关系已存在")

    ta = TeachingAssignment(teacher_id=body.teacher_id, class_id=body.class_id,
                            subject=body.subject)
    db.add(ta)
    db.flush()
    record_audit(db, user.id, "teaching.create", f"teaching_assignments:{ta.id}",
                 detail=json.dumps(body.model_dump(), ensure_ascii=False))
    db.commit()
    return {"id": ta.id}


@router.delete("/teaching-assignments/{ta_id}", status_code=204)
def delete_assignment(ta_id: int, user: CurrentUser = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """删授课关系（换班/离职）。同事务审计——"谁在何时撤了谁的权限"必须留痕。"""
    ta = db.get(TeachingAssignment, ta_id)
    if ta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="授课关系不存在")
    db.delete(ta)
    record_audit(db, user.id, "teaching.delete", f"teaching_assignments:{ta_id}",
                 detail=json.dumps({"teacher_id": ta.teacher_id, "class_id": ta.class_id}))
    db.commit()


# =====================================================================
# 组长任命（researcher 聚合 scope 的数据源）
# =====================================================================
@router.get("/research-leads")
def list_research_leads(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ResearchLead, User.username)
        .join(User, User.id == ResearchLead.user_id)
        .order_by(ResearchLead.user_id)
    ).all()
    return [{"id": rl.id, "user_id": rl.user_id, "username": username,
             "grade": rl.grade, "subject": rl.subject} for rl, username in rows]


@router.post("/research-leads", status_code=201)
def create_research_lead(body: ResearchLeadIn,
                         user: CurrentUser = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """任命组长。建议同时给该用户补 researcher 角色（这里只管任命表本身）。"""
    dup = db.execute(
        select(ResearchLead).where(ResearchLead.user_id == body.user_id,
                                   ResearchLead.grade == body.grade,
                                   ResearchLead.subject == body.subject)
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="该任命已存在")

    rl = ResearchLead(user_id=body.user_id, grade=body.grade, subject=body.subject)
    db.add(rl)
    db.flush()
    record_audit(db, user.id, "research_lead.create", f"research_leads:{rl.id}",
                 detail=json.dumps(body.model_dump(), ensure_ascii=False))
    db.commit()
    return {"id": rl.id}


@router.delete("/research-leads/{rl_id}", status_code=204)
def delete_research_lead(rl_id: int, user: CurrentUser = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    rl = db.get(ResearchLead, rl_id)
    if rl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="任命不存在")
    db.delete(rl)
    record_audit(db, user.id, "research_lead.delete", f"research_leads:{rl_id}",
                 detail=json.dumps({"user_id": rl.user_id, "grade": rl.grade}))
    db.commit()


# =====================================================================
# 班级管理
# =====================================================================
@router.get("/classes")
def list_classes(db: Session = Depends(get_db)):
    rows = db.execute(select(Class).order_by(Class.grade, Class.name)).scalars().all()
    return [{"id": c.id, "grade": c.grade, "name": c.name,
             "head_teacher_id": c.head_teacher_id} for c in rows]


@router.post("/classes", status_code=201)
def create_class(body: ClassIn, user: CurrentUser = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """建班。(grade, name) 复合唯一：初三2班全校只许一条（org.py 的 uq_grade_class_name）。"""
    dup = db.execute(select(Class).where(Class.grade == body.grade,
                                         Class.name == body.name)).scalar_one_or_none()
    if dup:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail=f"{body.grade}{body.name} 已存在")
    c = Class(grade=body.grade, name=body.name, head_teacher_id=body.head_teacher_id)
    db.add(c)
    db.flush()
    record_audit(db, user.id, "class.create", f"classes:{c.id}",
                 detail=json.dumps(body.model_dump(), ensure_ascii=False))
    db.commit()
    return {"id": c.id}


@router.put("/classes/{class_id}")
def update_class(class_id: int, body: ClassIn,
                 user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.get(Class, class_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="班级不存在")
    c.grade, c.name, c.head_teacher_id = body.grade, body.name, body.head_teacher_id
    record_audit(db, user.id, "class.update", f"classes:{class_id}",
                 detail=json.dumps(body.model_dump(), ensure_ascii=False))
    db.commit()
    return {"id": c.id}


@router.delete("/classes/{class_id}", status_code=204)
def delete_class(class_id: int, user: CurrentUser = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """删班。有学生还挂在这个班时靠 FK 拒绝（students.class_id 无 CASCADE）——IntegrityError 转 409。"""
    c = db.get(Class, class_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="班级不存在")
    try:
        db.delete(c)
        record_audit(db, user.id, "class.delete", f"classes:{class_id}")
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="班里还有学生/授课关系，先迁移再删")


# =====================================================================
# 审计日志（只读）
# =====================================================================
@router.get("/audit-logs")
def list_audit_logs(action: str | None = Query(default=None),
                    user_id: int | None = Query(default=None),
                    page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
                    db: Session = Depends(get_db)):
    """操作日志查询（只读）：换授课关系 / 批改确认 / 导入用户 / 任命全在这里。"""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        stmt = stmt.where(AuditLog.action.contains(action))
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    rows = db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all()
    return [{"id": a.id, "user_id": a.user_id, "action": a.action, "target": a.target,
             "detail": a.detail, "created_at": str(a.created_at)} for a in rows]
