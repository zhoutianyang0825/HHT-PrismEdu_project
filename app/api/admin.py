"""
M5-T3 · 管理后台 API（A-01）：用户 CRUD / CSV 导入 / 授课关系 / 组长任命 / 班级 / 日志。

【整体守卫】router 级挂 require_any("admin")——架构文档 8.5 的原话：
  "admin API 整体守卫挂在 router 依赖上，漏挂一个端点就是越权入口"。
  挂在 APIRouter(dependencies=...) 上，本文件所有端点一次覆盖，谁也漏不掉。

【CSV 导入的事务性（架构文档坑表"CSV 导入部分写入"）】
  逐行校验 → 全部通过才 commit；任何一行坏 → rollback，整批一行不进库；
  报告返回每条失败行的行号与原因。先在 10 行小文件验证，再上大文件。

【勾 researcher 必须有任命（架构文档坑表"勾了 researcher 未填任命"）】
  创建/修改用户时 roles 含 researcher → 必须同时给 research_leads，否则 400。
  空任命的 researcher 登录后处处 403，排查一次一下午——保存前拦住最便宜。
"""

from csv import DictReader
from datetime import datetime, timedelta, timezone
from io import StringIO
import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user, require_any
from app.core.security import hash_password, validate_password_strength
from app.db.session import get_db
from app.models import (
    AuditLog,
    BKTState,
    Class,
    DiagnosticQuiz,
    ErrorLog,
    ExamAttempt,
    ExamPaper,
    GradingRecord,
    Homework,
    HomeworkQuestion,
    LoginLog,
    MasteryRecord,
    ResearchLead,
    Student,
    StudentAnswer,
    Teacher,
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


# 整体角色守卫：require_any("admin") 工厂产出的 guard 挂在组级依赖上
router = APIRouter(dependencies=[Depends(require_any("admin"))])

VALID_ROLES = {"student", "teacher", "researcher", "admin"}

# 【角色互斥（2026-10-24 定稿）】合法组合只有 5 种，一份名单锁死：
#   {student}  {admin}  {teacher}  {researcher}  {teacher, researcher}
#   即 student / admin 必须单独成角；科任老师 ↔ 教研组长 可以双选。
#   单条创建、单条修改、CSV 批量导入三处共用同一个校验，防止大批量上传绕过。
VALID_ROLE_COMBOS = [
    frozenset({"student"}),
    frozenset({"admin"}),
    frozenset({"teacher"}),
    frozenset({"researcher"}),
    frozenset({"teacher", "researcher"}),
]


def _role_combo_error(roles: list[str]) -> str | None:
    """角色组合校验，不合法返回人话错误文案（供 400 与 CSV 行报告共用）。"""
    combo = frozenset(roles)
    if combo not in VALID_ROLE_COMBOS:
        return (f"角色组合 {sorted(combo)} 不合法：student / admin 只能单独成角，"
                "科任老师(teacher) 与 教研组长(researcher) 可以并存")
    return None

# CSV 模板表头（import-template 端点与导入校验共用，防止两头不一致）
CSV_HEADERS = ["username", "password", "name", "roles"]


# =====================================================================
# 用户查询 / 单条 CRUD
# =====================================================================
def _user_roles(db: Session, user_id: int) -> list[str]:
    rows = db.execute(select(UserRole.role).where(UserRole.user_id == user_id)).all()
    return [r[0] for r in rows]


def _seq_of(db: Session, user_id: int) -> int:
    """展示序号 = 按 id 排序时排在它前面（含自己）的人数。

    物理主键不动（PG sequence 只进不退、改 PK 还会连累一堆外键表），
    界面编号用这个 seq：从 1 起连续、新建自动 +1、删掉中间某个后面自动收拢。
    """
    return db.execute(select(func.count()).select_from(User)
                      .where(User.id <= user_id)).scalar_one()


def _user_out(db: Session, u: User) -> dict:
    """User 行 → 响应 dict（角色数组、停用标记、展示序号 seq、班级信息）。

    class_names 是展示用班级清单：学生=就读班级；老师=任教班级（可多个）；
    教研组长/管理员没有班级 → 空列表，前端显示"—"。
    """
    stu = db.execute(select(Student).where(Student.user_id == u.id)).scalar_one_or_none()
    class_id = stu.class_id if stu else None
    return {"id": u.id, "seq": _seq_of(db, u.id), "username": u.username, "name": u.name,
            "is_active": u.is_active, "roles": _user_roles(db, u.id),
            "class_id": class_id,
            "class_names": _class_names_of(db, u.id, class_id),
            "teaching": _teaching_of(db, u.id),
            "created_at": str(u.created_at)}


def _teaching_of(db: Session, user_id: int) -> list[dict]:
    """该账号的授课明细 [{class_id, subject}]（编辑弹窗预填任教班级用）。"""
    rows = db.execute(select(TeachingAssignment.class_id, TeachingAssignment.subject)
                      .where(TeachingAssignment.teacher_id == user_id)
                      .order_by(TeachingAssignment.class_id)).all()
    return [{"class_id": cid, "subject": subj} for cid, subj in rows]


def _class_names_of(db: Session, user_id: int, student_class_id: int | None) -> list[str]:
    """班级展示清单：学生就读班级在前，老师任教班级去重拼在后。"""
    names: list[str] = []
    if student_class_id is not None:
        c = db.get(Class, student_class_id)
        if c is not None:
            names.append(f"{c.grade}{c.name}")
    rows = db.execute(
        select(Class.grade, Class.name)
        .join(TeachingAssignment, TeachingAssignment.class_id == Class.id)
        .where(TeachingAssignment.teacher_id == user_id)
        .order_by(Class.grade, Class.name)
    ).all()
    for grade, name in rows:
        label = f"{grade}{name}"
        if label not in names:
            names.append(label)
    return names


# 用户查询api
# =====================================================================
@router.get("/users")
def list_users(role: str | None = Query(default=None),
               keyword: str | None = Query(default=None, description="按用户名/姓名模糊"),
               page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
               db: Session = Depends(get_db)):
    """用户列表查询：?role=&keyword=&page= 。停用用户带 is_active=False 标记。
    
    简短时间线：
    k1  admin.py  list_users(role="researcher")        
    k2  select(User).join(UserRole).where(role=="researcher")   ← 按角色过滤
    k3  切片返回 items → 前端塞进下拉框
    """

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


def _sync_teacher_profile(db: Session, user_id: int, roles: list[str]) -> None:
    """让 teachers 静态档案跟 teacher 角色保持一致。

    得到的数据：
        user_id（账号 id）、
        roles（该账号最终的角色列表）。

    要做的操作：
        有 teacher 角色且没有档案行就补一行（joined_at=成为老师的时刻）；
        没有 teacher 角色但有档案行就删掉。

    返回：
        无（直接改 session，由调用方 commit）。

    为什么：teachers 表原本没人写、一直是空的；账号进出 teacher 角色时档案要跟着动，
    teacher_overview 视图才看得出人。
    """
    row = db.execute(select(Teacher).where(Teacher.user_id == user_id)).scalar_one_or_none()
    if "teacher" in roles and row is None:
        db.add(Teacher(user_id=user_id, joined_at=datetime.now(timezone.utc)))
    elif "teacher" not in roles and row is not None:
        db.delete(row)


def _ensure_class_exists(db: Session, class_id: int | None) -> None:
    """校验 class_id 真的有这个班。

    得到的数据：
        class_id（前端传来的班级 id，可空）。

    要做的操作：
        非空时查 classes 表，不存在就 400。

    返回：
        无（校验不过直接抛异常）。

    为什么：下拉值能被伪造，服务端不能信前端，得自己查一次。
    """
    if class_id is not None and db.get(Class, class_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"班级 id {class_id} 不存在")


def _sync_student_profile(db: Session, user_id: int, roles: list[str],
                          class_id: int | None) -> None:
    """让 students 档案跟 student 角色保持一致。

    得到的数据：
        user_id（账号 id）、
        roles（最终角色列表）、
        class_id（就读班级，可空）。

    要做的操作：
        带 student 角色时——没有档案就建一行，传了 class_id 且与旧值不同就迁移班级；
        要班级却没有（既没传 class_id 也没旧档案）就 400；
        不带 student 角色却传了 class_id 也 400。

    返回：
        无（直接改 session，由调用方 commit）。

    为什么：学生必须有班（与 CSV 导入同规则），"没班的学生"建出来处处不对；
    换角离开 student 时档案保留不动——历史作答挂在 students.id 上，删档案等于动历史。
    """
    row = db.execute(select(Student).where(Student.user_id == user_id)).scalar_one_or_none()
    if "student" not in roles:
        if class_id is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail="只有学生账号能指定班级（class_id）")
        return
    
    if class_id is None and row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="学生账号必须指定就读班级（class_id）")
    
    if row is None:
        db.add(Student(user_id=user_id, class_id=class_id))

    elif class_id is not None and row.class_id != class_id:
        row.class_id = class_id     # 迁移班级


def _ensure_researcher_has_lead(roles: list[str], leads: list[dict] | None) -> None:
    """勾 researcher 必须带任命，否则 400（坑表原话：保存前校验）。"""
    if "researcher" in roles and not leads:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="勾选 researcher 角色必须同时提供 research_leads（年级+学科），"
                                   "否则该账号空 scope 登录后处处失效")


def _write_research_leads(db: Session, user_id: int, leads: list[dict]) -> None:
    """重建 research_leads（与 _sync_roles 同一套"整体替换"策略）。"""
    db.query(ResearchLead).filter(ResearchLead.user_id == user_id).delete()
    for it in leads:
        db.add(ResearchLead(user_id=user_id, grade=it["grade"], subject=it.get("subject", "math")))


def _write_teaching(db: Session, user_id: int, items: list[dict]) -> None:
    """重建 teaching_assignments（整体替换，与 _write_research_leads 同策略）。

    得到的数据：
        user_id（老师 id）、
        items（[{"class_id": 1, "subject": "math"}] 形式的授课关系）。

    要做的操作：
        先删该老师原有的授课关系，再按 items 逐条插入；
        同 (班级, 学科) 去重，班级必须真实存在（否则 400）。

    返回：
        无（直接改 session，由调用方 commit）。

    为什么：编辑弹窗里"任教班级"是整体勾选的，保存语义就该是整体替换，
    而不是逐条 diff——不然取消一个班要发一堆请求。
    """
    db.query(TeachingAssignment).filter(TeachingAssignment.teacher_id == user_id).delete()
    seen: set[tuple[int, str]] = set()
    for it in items:
        class_id = it.get("class_id")
        subject = (it.get("subject") or "math").strip() or "math"
        if class_id is None:
            continue
        _ensure_class_exists(db, class_id)
        key = (class_id, subject)
        if key in seen:
            continue
        seen.add(key)
        db.add(TeachingAssignment(teacher_id=user_id, class_id=class_id, subject=subject))



@router.post("/users", status_code=201)
def create_user(body: UserCreateIn, user: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """单条创建用户。密码强度 / 用户名唯一 / 角色合法 / researcher 任命四处校验。"""
    
    validate_password_strength(body.password)   # 不合格抛 ValueError → 转 400
    if db.execute(select(User).where(User.username == body.username)).scalar_one_or_none():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"用户名 {body.username} 已存在")

    combo_err = _role_combo_error(body.roles)   # 互斥：student/admin 单独成角
    if combo_err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=combo_err)
    
    _ensure_researcher_has_lead(body.roles, body.research_leads)  # 校验researcher角色，是否有带年级、学科的任命
    _ensure_class_exists(db, body.class_id)     # 拿传的class_id 校验是否真的存在这个班级

    u = User(username=body.username, password_hash=hash_password(body.password), name=body.name)
    db.add(u)
    db.flush()                                    # 拿 u.id 供角色/任命表用
    _sync_roles(db, u.id, body.roles)
    _sync_teacher_profile(db, u.id, body.roles)   # teachers 档案跟 teacher 角色同步
    _sync_student_profile(db, u.id, body.roles, body.class_id)   # students 档案：班级落位

    if body.research_leads:
        _write_research_leads(db, u.id, body.research_leads)

    if body.teaching:
        if "teacher" not in body.roles:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="只有教师账号能指定授课关系")
        _write_teaching(db, u.id, body.teaching)

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
        combo_err = _role_combo_error(body.roles)   # 与创建/CSV 同一套互斥规则
        if combo_err:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=combo_err)
        _ensure_researcher_has_lead(body.roles, body.research_leads or [])
        _sync_roles(db, user_id, body.roles)
        _sync_teacher_profile(db, user_id, body.roles)   # teachers 档案跟 teacher 角色同步
        changed["roles"] = old_roles + body.roles
    if body.research_leads is not None:
        _write_research_leads(db, user_id, body.research_leads)
        changed["research_leads"] = body.research_leads

    # 学生班级：改了角色或传了 class_id 才动档案（改名/停用/重置密码不逼着补班级）
    if body.roles is not None or body.class_id is not None:
        final_roles = body.roles if body.roles is not None else old_roles
        _ensure_class_exists(db, body.class_id)
        _sync_student_profile(db, user_id, final_roles, body.class_id)
        if body.class_id is not None:
            changed["class_id"] = body.class_id

    # 任教班级：传了 teaching 就整体替换（编辑弹窗的任教班级多选框）
    if body.teaching is not None:
        final_roles = body.roles if body.roles is not None else old_roles
        if body.teaching and "teacher" not in final_roles:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="只有教师账号能指定授课关系")
        _write_teaching(db, user_id, body.teaching)
        changed["teaching"] = body.teaching

    record_audit(db, user.id, "user.update", f"users:{user_id}",
                 json.dumps(changed, ensure_ascii=False))
    db.commit()
    return _user_out(db, u)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, confirm: bool = Query(default=False),
                user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """删除账户（硬删，仅非 admin 账号）。

    流程（对齐前端弹窗约定）：
      t1 守卫：404 不存在 / 400 删自己 / 403 目标是管理员
      t2 清点历史数据（学生作答、教师布置的作业）
          ├─ 有历史 且 confirm=False → 409 带人话提示，前端弹窗确认后带 ?confirm=1 重试
          └─ 无历史 / 已确认 → 直接删
      t3 连带清干净：作答→批改/错题、BKT、掌握度、诊断测验、教师作业（含题目清单）
      t4 遗留引用置空：classes.head_teacher_id、exam_papers.created_by（班级/试卷保留）
      t5 删 user_roles/teachers/授课关系/任命/users 本体 → 审计留痕 → commit
    审计日志本身不删——"谁删过谁"正是审计要留的。
    """
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="不能删除自己")
    roles = _user_roles(db, user_id)
    if "admin" in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="管理员账号不可删除，只能停用")

    # ---- t2 历史数据清点 ----
    student = db.execute(select(Student).where(Student.user_id == user_id)).scalar_one_or_none()
    answer_cnt = 0
    if student is not None:
        answer_cnt = db.execute(select(func.count()).select_from(StudentAnswer)
                                .where(StudentAnswer.student_id == student.id)).scalar_one()
    hw_cnt = db.execute(select(func.count()).select_from(Homework)
                        .where(Homework.teacher_id == user_id)).scalar_one()

    if (answer_cnt or hw_cnt) and not confirm:
        parts = []
        if answer_cnt:
            parts.append(f"作答 {answer_cnt} 条")
        if hw_cnt:
            parts.append(f"布置作业 {hw_cnt} 份")
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"该账户下有历史数据（{'，'.join(parts)}），"
                                   "确定删除将连带清空这些数据吗？")

    try:
        # ---- t3 学生学习数据：先删批改/错题，再删作答，最后档案 ----
        if student is not None:
            answer_ids = [row[0] for row in db.execute(
                select(StudentAnswer.id).where(StudentAnswer.student_id == student.id)).all()]
            if answer_ids:
                db.query(GradingRecord).filter(
                    GradingRecord.answer_id.in_(answer_ids)).delete(synchronize_session=False)
                db.query(ErrorLog).filter(
                    ErrorLog.answer_id.in_(answer_ids)).delete(synchronize_session=False)
                db.query(StudentAnswer).filter(
                    StudentAnswer.student_id == student.id).delete(synchronize_session=False)
            db.query(BKTState).filter(
                BKTState.student_id == student.id).delete(synchronize_session=False)
            db.query(MasteryRecord).filter(
                MasteryRecord.student_id == student.id).delete(synchronize_session=False)
            db.query(DiagnosticQuiz).filter(
                DiagnosticQuiz.student_id == student.id).delete(synchronize_session=False)
            db.delete(student)

        # ---- t3 教师布置的作业一并删（学生作答挂在 questions 上，不受影响）----
        hw_ids = [row[0] for row in db.execute(
            select(Homework.id).where(Homework.teacher_id == user_id)).all()]
        if hw_ids:
            db.query(HomeworkQuestion).filter(
                HomeworkQuestion.homework_id.in_(hw_ids)).delete(synchronize_session=False)
            db.query(Homework).filter(
                Homework.teacher_id == user_id).delete(synchronize_session=False)

        # ---- t4 遗留引用置空：班级保留班主任位、试卷保留但脱敏创建人 ----
        db.execute(update(Class).where(Class.head_teacher_id == user_id).values(head_teacher_id=None))
        db.execute(update(ExamPaper).where(ExamPaper.created_by == user_id).values(created_by=None))

        # ---- t5 关系与本体 ----
        db.query(TeachingAssignment).filter(
            TeachingAssignment.teacher_id == user_id).delete(synchronize_session=False)
        db.query(ResearchLead).filter(
            ResearchLead.user_id == user_id).delete(synchronize_session=False)
        db.query(Teacher).filter(
            Teacher.user_id == user_id).delete(synchronize_session=False)
        db.query(UserRole).filter(
            UserRole.user_id == user_id).delete(synchronize_session=False)
        username = u.username
        db.delete(u)

        record_audit(db, user.id, "user.delete", f"users:{user_id}",
                     json.dumps({"username": username, "roles": roles,
                                 "answers": answer_cnt, "homeworks": hw_cnt}, ensure_ascii=False))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="删除失败：存在未处理的关联数据，请改用停用")
    return None


# =====================================================================
# CSV 批量导入（P0 核心：逐行校验、全过才写、报告带行号）
# =====================================================================
# 【大宽表设计（M6 后补，覆盖"组织账户一次导入"）】
#   一行一个人，按角色分流写四张表。前 4 列是硬核心（老模板兼容），后 3 列按角色选填：
#     username,password,name,roles,classes,student_no,lead_grades
#     classes 就读/任教班级，格式"年级:班名"（如 初三:1班），多值分号分隔
#     student 取第一个班建档；
#     teacher 每个班建一条任教关系；班不存在自动建
#     student_no  学籍号，仅 student 用，可空
#     lead_grades 教研任命年级（如 初三），多值分号分隔，researcher 必填；学科固定 math
#  小批量仍走手动窗口（teaching-assignments / research-leads 接口），两路添加账户信息并存。
CSV_HEADERS = ["username", "password", "name", "roles"]          # 老 4 列硬核心，缺一不可
CSV_EXTRA_HEADERS = ["classes", "student_no", "lead_grades"]     # 新 3 列，可缺（老模板兼容）


def _split_multi(value: str) -> list[str]:
    """分号切多值（兼容中文分号），丢空段。'初三:1班;初三:2班' → ['初三:1班', '初三:2班']。"""
    return [seg.strip() for seg in (value or "").replace("；", ";").split(";") if seg.strip()]


def _validate_csv_rows(csv_text: str, db: Session) -> tuple[list[dict], list[dict]]:
    """逐行校验 CSV（大宽表），返回 (合格行, 失败报告)。

    校验七关（按检查成本从低到高排，坏行早死早超生）：
      ① 列名齐不齐  ② 密码强度  ③ roles 合法非空  ④ 用户名格式
      ⑤ 唯一性（库内已有 + 本批内重复，两处都查）
      ⑥ classes 格式（"年级:班名"）+ student 必须有班级  ⑦ researcher 必须有 lead_grades
    返回的合格行还未写库——写库由调用方事务控制（坏行整批不写）。
    """
    errors: list[dict] = []
    goods: list[dict] = []
    seen_usernames: set[str] = set()

    reader = DictReader(StringIO(csv_text.strip()))
    if reader.fieldnames is None or not set(CSV_HEADERS).issubset(set(reader.fieldnames)):
        return [], [{"line": 1, "reason": f"表头必须包含 {CSV_HEADERS}（可选列 {CSV_EXTRA_HEADERS}）"}]

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

        # ③+ 组合互斥（与单条创建同规则）：student/admin 单独成角，teacher+researcher 可并存
        combo_err = _role_combo_error(roles)
        if combo_err:
            errors.append({"line": line_no, "reason": combo_err})
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

        # ⑥ classes：每段必须是"年级:班名"；student 没有班级 = 坏行（档案无处安放）
        class_pairs: list[tuple[str, str]] = []
        bad_class = False
        for seg in _split_multi(row.get("classes") or ""):
            if ":" not in seg:
                errors.append({"line": line_no,
                               "reason": f"classes 段 '{seg}' 格式应为 年级:班名（如 初三:1班）"})
                bad_class = True
                break
            grade, cls = (part.strip() for part in seg.split(":", 1))
            if not grade or not cls:
                errors.append({"line": line_no, "reason": f"classes 段 '{seg}' 年级/班名不能为空"})
                bad_class = True
                break
            class_pairs.append((grade, cls))
        if bad_class:
            continue
        if "student" in roles and not class_pairs:
            errors.append({"line": line_no, "reason": "student 必须给 classes（就读班级，如 初三:1班）"})
            continue

        # ⑦ researcher 必须有任命年级（与单条创建 _ensure_researcher_has_lead 同一约束）
        lead_grades = _split_multi(row.get("lead_grades") or "")
        if "researcher" in roles and not lead_grades:
            errors.append({"line": line_no, "reason": "researcher 必须给 lead_grades（任教年级，如 初三）"})
            continue

        seen_usernames.add(username)
        # 同一行里班级去重（防 "初三:1班;初三:1班" 建出重复任教关系）
        class_pairs = list(dict.fromkeys(class_pairs))
        goods.append({"username": username, "password": password,
                      "name": name or username, "roles": roles,
                      "class_pairs": class_pairs,
                      "student_no": (row.get("student_no") or "").strip() or None,
                      "lead_grades": list(dict.fromkeys(lead_grades))})

    return goods, errors



@router.post("/users/import")
def import_users(body: CsvImportIn, user: CurrentUser = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """CSV 批量导入（大宽表）：逐行校验、全过才写、按角色分流入库、失败报告带行号。

    完整时间线（2000 行、第 37 行密码太弱）：
      t1  _validate_csv_rows 逐行校验 → 合格 1999 行 + errors=[{line:37, reason:...}]
      t2  有失败行 → return 200 + {"ok": False, "errors": [...], "imported": 0}
          —— 注意：一行不写库。返回 200 是因为"导入被拒绝"是正常业务结果，不是服务器错误
      t3  全过 → 逐人写 users+user_roles，再按角色分流：
            student   → students 档案（班级不存在自动建班）
            teacher   → teaching_assignments（每个班一条）
            researcher→ research_leads（每个年级一条，subject 固定 math）
      t4  record_audit（带四类计数）→ commit；中途任何异常 → rollback，库里干干净净
    """
    goods, errors = _validate_csv_rows(body.csv_content, db)

    if errors:
        # 全过才写：有一行坏就整批拒绝。imported 恒 0，报告照实给行号
        return {"ok": False, "imported": 0, "errors": errors,
                "message": f"共 {len(errors)} 行不合格，整批未导入（事务性：坏行不写库）"}

    # 班级缓存：同批多人都填"初三:1班"只建一次班
    class_cache: dict[tuple[str, str], int] = {}

    def _get_or_create_class(grade: str, cls_name: str) -> int:
        key = (grade, cls_name)
        if key in class_cache:
            return class_cache[key]
        row = db.execute(select(Class).where(Class.grade == grade,
                                            Class.name == cls_name)).scalar_one_or_none()
        if row is None:
            row = Class(grade=grade, name=cls_name)
            db.add(row)
            db.flush()
        class_cache[key] = row.id
        return row.id

    try:
        created = []
        counts = {"users": 0, "students": 0, "teaching_assignments": 0, "research_leads": 0}
        # 展示序号口径与列表一致：新账号接在现有名单末尾（现有人数 + 批内第 i 个）
        base_seq = db.execute(select(func.count()).select_from(User)).scalar_one()
        for g in goods:
            u = User(username=g["username"], password_hash=hash_password(g["password"]),
                     name=g["name"])
            db.add(u)
            db.flush()
            _sync_roles(db, u.id, g["roles"])
            _sync_teacher_profile(db, u.id, g["roles"])   # teachers 档案跟 teacher 角色同步
            counts["users"] += 1

            # ---- student → 学生档案（取第一个班级）----
            if "student" in g["roles"] and g["class_pairs"]:
                grade, cls_name = g["class_pairs"][0]
                db.add(Student(user_id=u.id, class_id=_get_or_create_class(grade, cls_name),
                               student_no=g["student_no"]))
                counts["students"] += 1

            # ---- teacher → 任教关系（每个班一条）----
            if "teacher" in g["roles"]:
                for grade, cls_name in g["class_pairs"]:
                    db.add(TeachingAssignment(teacher_id=u.id,
                                              class_id=_get_or_create_class(grade, cls_name),
                                              subject="math"))
                    counts["teaching_assignments"] += 1

            # ---- researcher → 教研任命（每个年级一条，学科固定 math）----
            if "researcher" in g["roles"]:
                for grade in g["lead_grades"]:
                    db.add(ResearchLead(user_id=u.id, grade=grade, subject="math"))
                    counts["research_leads"] += 1

            created.append({"id": u.id, "seq": base_seq + len(created) + 1,
                            "username": u.username, "roles": g["roles"]})

        record_audit(db, user.id, "user.import", "users",
                     json.dumps(counts, ensure_ascii=False))
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"ok": True, "imported": len(created), "errors": [], "items": created, **counts}



@router.get("/users/import-template")
def import_template():
    """下载 CSV 大宽表模板（text/csv）。前 4 列必填，后 3 列按角色选填。

    老 4 列模板继续可用（只建账号，任教关系/任命走手动窗口）。
    """
    sample = (
        "username,password,name,roles,classes,student_no,lead_grades\n"
        "S101,Passw0rd1,张小明,student,初三:1班,2026001,\n"
        "S102,Passw0rd2,李小红,student,初三:1班,,\n"
        "T020,Passw0rd3,王老师,teacher,初三:1班;初三:2班,,\n"
        "R003,Passw0rd4,陈组长,researcher,,,\"初三\"\n"
        "T021,Passw0rd5,张双角色,teacher;researcher,初三:1班,,初三\n"
    )
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
# 教研组长任命api（researcher 聚合 scope 的数据源）
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
# 班级列表获取 & 班级管理 api
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
    """删班。班里还有学生或授课关系 → 409「班级存在学生或授课关系，请先迁移」，前端弹窗提示。

    为什么要显式预检而不是赌外键报错：teaching_assignments.class_id 是
    ondelete=CASCADE——靠 FK 拒绝的话，任教关系会被数据库悄悄级联带走，
    老师的权限凭空消失还查不出原因。学生/授课关系一律先清点再删。
    """
    c = db.get(Class, class_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="班级不存在")

    stu_cnt = db.execute(select(func.count()).select_from(Student)
                         .where(Student.class_id == class_id)).scalar_one()
    ta_cnt = db.execute(select(func.count()).select_from(TeachingAssignment)
                        .where(TeachingAssignment.class_id == class_id)).scalar_one()
    if stu_cnt or ta_cnt:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="班级存在学生或授课关系，请先迁移")

    db.delete(c)
    record_audit(db, user.id, "class.delete", f"classes:{class_id}")
    db.commit()


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


# =====================================================================
# 使用情况聚合（只读）：三端谁在用、用了多少 —— 监控第一层的查询口
# =====================================================================
@router.get("/usage/summary")
def usage_summary(days: int = Query(default=7, ge=1, le=365),
                  db: Session = Depends(get_db)):
    """三端使用情况聚合：登录 / 学生作答 / 老师批改 / 教研出卷，一个端点全给。

    得到的数据：days（统计窗口天数，1~365）
    要做的操作：对 login_logs / student_answers / error_logs / grading_records /
                homeworks / exam_papers / exam_attempts 各跑一条 group by 聚合，
                全部只读、不碰业务写路径
    返回：按 logins / students / teachers / research / by_class 分组的计数 dict

    为什么全查现成表不埋点：
        三端行为落库是架构红利（student_answers 四种来源共用一张表），
        监控只是"换个视角读同一批数据"，不需要业务代码配合。

    口径说明（对齐 commit_grade 的定型规则）：
        - AI 采纳率 = grader_type=llm 且 confirmed ÷ (llm confirmed + grader_type=teacher)
          主观题批改落点只有两种：老师采纳 AI（记 llm）/ 老师自己拍板（记 teacher），
        - 正确率分母排除 is_correct 为 NULL 的行（主观题没批完不是"错"，模型注释同口径）。
        - 活跃学生 = 窗口内有作答的不同 student_id；
        - 活跃老师 = 审计里有批改动作的不同 user_id。
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # ---- 1. 登录：by_status 分组 + 成功登录的去重人数 ----
    login_rows = db.execute(
        select(LoginLog.status, func.count())
        .where(LoginLog.created_at >= since)
        .group_by(LoginLog.status)
    ).all()
    unique_login_users = db.execute(
        select(func.count(func.distinct(LoginLog.user_id)))
        .where(LoginLog.created_at >= since,
               LoginLog.status == "success",
               LoginLog.user_id.is_not(None))
    ).scalar() or 0
    logins_by_status = {s: c for s, c in login_rows}


    # ---- 2. 学生：作答总量 / 来源分布 / 活跃数 / 正确率 ----
    answers_total = db.execute(
        select(func.count()).select_from(StudentAnswer)
        .where(StudentAnswer.submitted_at >= since)
    ).scalar() or 0
    source_rows = db.execute(
        select(StudentAnswer.source_type, func.count())
        .where(StudentAnswer.submitted_at >= since)
        .group_by(StudentAnswer.source_type)
    ).all()
    active_students = db.execute(
        select(func.count(func.distinct(StudentAnswer.student_id)))
        .where(StudentAnswer.submitted_at >= since)
    ).scalar() or 0
    correct_n = db.execute(
        select(func.count()).select_from(StudentAnswer)
        .where(StudentAnswer.submitted_at >= since, StudentAnswer.is_correct.is_(True))
    ).scalar() or 0
    wrong_n = db.execute(
        select(func.count()).select_from(StudentAnswer)
        .where(StudentAnswer.submitted_at >= since, StudentAnswer.is_correct.is_(False))
    ).scalar() or 0
    graded_n = correct_n + wrong_n          # NULL（未判分）不计入正确率分母
    # 错因分布：join 回作答行，按作答时间过滤（error_logs 自己没有时间索引口径）
    error_rows = db.execute(
        select(ErrorLog.error_type, func.count())
        .select_from(ErrorLog)
        .join(StudentAnswer, ErrorLog.answer_id == StudentAnswer.id)
        .where(StudentAnswer.submitted_at >= since)
        .group_by(ErrorLog.error_type)
    ).all()


    # ---- 3. 老师：批改量 / AI 采纳率 / 活跃老师 / 出作业 ----
    grading_rows = db.execute(
        select(GradingRecord.grader_type, func.count())
        .where(GradingRecord.created_at >= since)
        .group_by(GradingRecord.grader_type)
    ).all()
    by_grader = {g: c for g, c in grading_rows}
    llm_confirmed = db.execute(
        select(func.count()).select_from(GradingRecord)
        .where(GradingRecord.created_at >= since,
               GradingRecord.grader_type == "llm",
               GradingRecord.status == "confirmed")
    ).scalar() or 0
    teacher_gradings = by_grader.get("teacher", 0)
    adopt_denom = llm_confirmed + teacher_gradings
    active_teachers = db.execute(
        select(func.count(func.distinct(AuditLog.user_id)))
        .where(AuditLog.created_at >= since,
               AuditLog.action.like("grading.%"),
               AuditLog.user_id.is_not(None))
    ).scalar() or 0
    homeworks_created = db.execute(
        select(func.count()).select_from(Homework)
        .where(Homework.created_at >= since)
    ).scalar() or 0


    # ---- 4. 教研：出卷 / 推送 / 考试参与 ----
    papers_created = db.execute(
        select(func.count()).select_from(ExamPaper)
        .where(ExamPaper.created_at >= since)
    ).scalar() or 0
    papers_published = db.execute(
        select(func.count()).select_from(ExamPaper)
        .where(ExamPaper.published_at.is_not(None),
               ExamPaper.published_at >= since)
    ).scalar() or 0
    exam_attempts = db.execute(
        select(func.count()).select_from(ExamAttempt)
        .where(ExamAttempt.submitted_at >= since)
    ).scalar() or 0


    # ---- 5. 班级分布：作答量 + 活跃学生，按年级班名排 ----
    class_rows = db.execute(
        select(Class.id, Class.grade, Class.name,
               func.count(StudentAnswer.id),
               func.count(func.distinct(StudentAnswer.student_id)))
        .select_from(Class)
        .join(Student, Student.class_id == Class.id)
        .join(StudentAnswer, StudentAnswer.student_id == Student.id)
        .where(StudentAnswer.submitted_at >= since)
        .group_by(Class.id, Class.grade, Class.name)
        .order_by(Class.grade, Class.name)
    ).all()

    
    return {
        "days": days,
        "since": str(since),
        "logins": {
            "total": sum(logins_by_status.values()),
            "unique_users": unique_login_users,
            "by_status": logins_by_status,
        },
        "students": {
            "active": active_students,
            "answers_total": answers_total,
            "by_source": {s: c for s, c in source_rows},
            "correct_rate": round(correct_n / graded_n, 4) if graded_n else None,
            "errors_by_type": {t: c for t, c in error_rows},
        },
        "teachers": {
            "active": active_teachers,
            "gradings_total": sum(by_grader.values()),
            "by_grader": by_grader,
            "ai_adopt_rate": round(llm_confirmed / adopt_denom, 4) if adopt_denom else None,
            "homeworks_created": homeworks_created,
        },
        "research": {
            "papers_created": papers_created,
            "papers_published": papers_published,
            "exam_attempts": exam_attempts,
        },
        "by_class": [
            {"class_id": cid, "class": f"{grade}{name}",
             "answers": cnt, "active_students": stu}
            for cid, grade, name, cnt, stu in class_rows
        ],
    }
