"""M5 · API 层自检单测：admin 越权矩阵抽测 + CSV 导入事务性（交付前跑绿）。

 用 FastAPI TestClient + sqlite 内存库（conftest 同套路，不碰真容器）：
   ① researcher 调 admin 接口 → 403（router 级守卫生效）
   ② researcher 调老师明细接口 → 403（M5 完成判据的 403 验证口）
   ③ CSV 全合格 → 全部写入
   ④ CSV 一行坏 → 整批不写（事务性），报告带行号
   ⑤ CSV 本批内用户名重复 → 拒绝

 运行：uv run pytest tests/test_api_admin.py -v

 关键机关：app.dependency_overrides[get_db] 把所有 Depends(get_db) 换成
 sqlite 会话——守卫（get_current_user）和端点用的是同一个函数对象，一次覆盖全局生效。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import app
from app.models import Base, User, UserRole

GOOD_CSV = ("username,password,name,roles,classes,student_no,lead_grades\n"
            "S301,Passw0rd1,张小明,student,初三:1班,2026001,\n"
            "T301,Passw0rd2,李老师,teacher;researcher,初三:1班,,初三\n")
BAD_CSV = GOOD_CSV + "S302,short,T王,student,初三:1班,,\n"     # 第 4 行密码太弱（第 1 行表头）


@pytest.fixture()
def api():
    """每个用例一套独立 sqlite + TestClient。

    override 闭包持有同一个 session：TestClient 的多次请求共享一个会话，
    断言"库里有没有写进数据"才能直接查这张表。
    """
    # StaticPool + check_same_thread=False：整个进程共用一条内存连接。
    # 为什么不能像 conftest 用默认池：TestClient 的请求跑在另一个线程，
    # sqlite :memory: 每条连接是独立的空库——不锁成单连接，请求线程会看到"no such table"
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, future=True, autoflush=False, expire_on_commit=False)
    session = maker()

    app.dependency_overrides[get_db] = lambda: session
    client = TestClient(app)     # 不进 with：不触发 lifespan，不连真 PG

    yield client, session

    app.dependency_overrides.clear()
    session.close()
    engine.dispose()


def _seed_user(session: sessionmaker, username: str, roles: list[str]) -> str:
    """造一个测试用户并签发 token，返回 "Bearer xxx" 请求头。"""
    s = session
    u = User(username=username, password_hash=hash_password("Passw0rd1"), name=username)
    s.add(u)
    s.flush()
    for r in roles:
        s.add(UserRole(user_id=u.id, role=r))
    s.commit()
    token = create_access_token(u.id, username, roles)
    return {"Authorization": f"Bearer {token}"}


# ================================================================
# ①② 越权矩阵抽测（M5 判据：researcher 摸不到明细与管理口）
# ================================================================
def test_admin_api_403_for_researcher(api):
    client, session = api
    headers = _seed_user(session, "R_Only", ["researcher"])
    resp = client.get("/api/admin/users", headers=headers)
    assert resp.status_code == 403, "researcher 调 admin 接口必须 403（router 级守卫）"


def test_teacher_answer_detail_403_for_researcher(api):
    client, session = api
    headers = _seed_user(session, "R_Only2", ["researcher"])
    resp = client.get("/api/teacher/answers/1", headers=headers)
    assert resp.status_code == 403, "researcher 调明细接口必须 403（require_any('teacher') 拦截）"


def test_admin_api_403_for_student(api):
    client, session = api
    headers = _seed_user(session, "S_Only", ["student"])
    resp = client.get("/api/admin/users", headers=headers)
    assert resp.status_code == 403


# ================================================================
# ③ CSV 全合格 → 全部写入
# ================================================================
def test_csv_import_all_good(api):
    client, session = api
    headers = _seed_user(session, "admin_u", ["admin"])
    resp = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": GOOD_CSV})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["imported"] == 2
    # 大宽表分流计数：S301→学生档案 1 条；T301→任教 1 条 + 任命 1 条
    assert body["students"] == 1
    assert body["teaching_assignments"] == 1
    assert body["research_leads"] == 1

    rows = session.query(User).all()
    assert {u.username for u in rows} == {"admin_u", "S301", "T301"}
    # 角色同步验证：T301 应有两行 user_roles
    t301 = next(u for u in rows if u.username == "T301")
    roles = session.query(UserRole).filter(UserRole.user_id == t301.id).all()
    assert sorted(r.role for r in roles) == ["researcher", "teacher"]

    # 分流入库验证：学生档案挂到自动建的初三1班；任教/任命按行拆分
    from app.models import Class, ResearchLead, Student, TeachingAssignment

    cls = session.query(Class).filter_by(grade="初三", name="1班").one()
    stu = session.query(Student).filter_by(student_no="2026001").one()
    assert stu.class_id == cls.id
    ta = session.query(TeachingAssignment).one()
    assert ta.teacher_id == t301.id and ta.class_id == cls.id
    rl = session.query(ResearchLead).one()
    assert rl.user_id == t301.id and rl.grade == "初三"


# ================================================================
# ④ CSV 一行坏 → 整批不写（🔴 事务性），报告带行号
# ================================================================
def test_csv_import_bad_line_writes_nothing(api):
    client, session = api
    headers = _seed_user(session, "admin_u2", ["admin"])
    resp = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": BAD_CSV})
    body = resp.json()
    assert body["ok"] is False
    assert body["imported"] == 0, "坏行必须让整批拒绝"
    assert any(e["line"] == 4 for e in body["errors"]), "报告必须标出坏行行号（第 4 行）"

    # 库里只有 admin_u2 自己——S301/T301 一行都没进来
    usernames = {u.username for u in session.query(User).all()}
    assert "S301" not in usernames and "T301" not in usernames


# ================================================================
# ⑤ 本批内用户名重复 → 拒绝
# ================================================================
def test_csv_import_duplicate_in_batch_rejected(api):
    client, session = api
    headers = _seed_user(session, "admin_u3", ["admin"])
    dup_csv = ("username,password,name,roles,classes\n"
               "S401,Passw0rd1,甲,student,初三:1班\n"
               "S401,Passw0rd2,乙,student,初三:1班\n")
    resp = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": dup_csv})
    body = resp.json()
    assert body["ok"] is False
    assert any("重复" in e["reason"] for e in body["errors"])
    assert session.query(User).filter(User.username == "S401").count() == 0


# ================================================================
# ⑥ 大宽表分流入库（M6 后补：一行一个人，按角色写四张表）
# ================================================================
def test_csv_wide_table_branches(api):
    """四类人四行：student/teacher/researcher/双角色 → 账号+档案+任教+任命各就各位。"""
    from app.models import Class, ResearchLead, Student, TeachingAssignment

    client, session = api
    headers = _seed_user(session, "admin_w", ["admin"])
    wide = ("username,password,name,roles,classes,student_no,lead_grades\n"
            "S501,Passw0rd1,甲同学,student,初三:2班,2026501,\n"
            "T501,Passw0rd2,乙老师,teacher,初三:2班;初三:3班,,\n"
            "R501,Passw0rd3,丙组长,researcher,,,初三;初一\n"
            "D501,Passw0rd4,丁双角色,teacher;researcher,初三:2班,,初三\n")
    body = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": wide}).json()
    assert body["ok"] is True
    assert (body["users"], body["students"], body["teaching_assignments"],
            body["research_leads"]) == (4, 1, 3, 3)

    # 自动建班：初三2班/初三3班各一条；双角色与老师共享 2 班不重复建
    names = {(c.grade, c.name) for c in session.query(Class).all()}
    assert ("初三", "2班") in names and ("初三", "3班") in names
    # T501 两个班两条任教；D501 一个班一条
    ta_teachers = [t.teacher_id for t in session.query(TeachingAssignment).all()]
    assert len(ta_teachers) == 3
    # R501 两个年级两条任命 + D501 一条
    assert session.query(ResearchLead).count() == 3
    # 学生档案：班级 + 学籍号落位
    stu = session.query(Student).one()
    assert stu.student_no == "2026501"


def test_csv_student_without_class_rejected(api):
    """student 没有班级 = 坏行（档案无处安放），整批不写 + 报告带行号。"""
    client, session = api
    headers = _seed_user(session, "admin_s", ["admin"])
    csv_text = ("username,password,name,roles,classes\n"
                "S601,Passw0rd1,甲,student,\n")
    body = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": csv_text}).json()
    assert body["ok"] is False and body["imported"] == 0
    assert any(e["line"] == 2 and "classes" in e["reason"] for e in body["errors"])
    assert session.query(User).count() == 1   # 只剩 admin 自己


def test_csv_researcher_without_lead_rejected(api):
    """勾 researcher 不给 lead_grades → 坏行（与单条创建同一约束，别让 CSV 走后门）。"""
    client, session = api
    headers = _seed_user(session, "admin_r", ["admin"])
    csv_text = ("username,password,name,roles,lead_grades\n"
                "R601,Passw0rd1,甲,researcher,\n")
    body = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": csv_text}).json()
    assert body["ok"] is False
    assert any("lead_grades" in e["reason"] for e in body["errors"])


def test_csv_bad_class_format_rejected(api):
    """classes 段缺冒号（写成 '初三1班'）→ 坏行，提示正确格式。"""
    client, session = api
    headers = _seed_user(session, "admin_c", ["admin"])
    csv_text = ("username,password,name,roles,classes\n"
                "S701,Passw0rd1,甲,student,初三1班\n")
    body = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": csv_text}).json()
    assert body["ok"] is False
    assert any("年级:班名" in e["reason"] for e in body["errors"])


def test_csv_old_style_teacher_only_still_works(api):
    """老 4 列模板兼容：teacher 不带 classes 只建账号，任教关系留给手动窗口。"""
    client, session = api
    headers = _seed_user(session, "admin_o", ["admin"])
    csv_text = ("username,password,name,roles\n"
                "T801,Passw0rd1,甲,teacher\n")
    body = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": csv_text}).json()
    assert body["ok"] is True and body["imported"] == 1
    assert body["teaching_assignments"] == 0
    assert session.query(User).filter(User.username == "T801").count() == 1


# ================================================================
# ⑦ 角色互斥（2026-10-24）：student/admin 单独成角，teacher+researcher 可并存
# ================================================================
def test_role_combo_student_with_teacher_rejected(api):
    client, session = api
    headers = _seed_user(session, "admin_cb", ["admin"])
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "S901", "password": "Passw0rd1", "name": "甲",
        "roles": ["student", "teacher"]})
    assert resp.status_code == 400
    assert "不合法" in resp.json()["detail"]
    assert session.query(User).filter(User.username == "S901").count() == 0


def test_role_combo_admin_with_anything_rejected(api):
    client, session = api
    headers = _seed_user(session, "admin_ca", ["admin"])
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "A901", "password": "Passw0rd1", "name": "乙",
        "roles": ["admin", "teacher"]})
    assert resp.status_code == 400


def test_role_combo_teacher_researcher_ok(api):
    client, session = api
    headers = _seed_user(session, "admin_tr", ["admin"])
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "T901", "password": "Passw0rd1", "name": "丙",
        "roles": ["teacher", "researcher"],
        "research_leads": [{"grade": "初三", "subject": "math"}]})
    assert resp.status_code == 201
    assert sorted(resp.json()["roles"]) == ["researcher", "teacher"]


def test_csv_role_combo_rejected(api):
    """CSV 大宽表同样拦：一行 student;teacher → 整批不写，报告点名角色组合。"""
    client, session = api
    headers = _seed_user(session, "admin_cc", ["admin"])
    csv_text = ("username,password,name,roles,classes\n"
                "S902,Passw0rd1,丁,student;teacher,初三:1班\n")
    body = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": csv_text}).json()
    assert body["ok"] is False and body["imported"] == 0
    assert any("角色组合" in e["reason"] for e in body["errors"])
    assert session.query(User).count() == 1   # 坏行整批不进


# ================================================================
# ⑧ 展示序号 seq + 删除账户（2026-10-24）
# ================================================================
def _seed_student_with_answer(session, username: str):
    """造一个带作答数据的学生：users+角色+班级档案+1 条作答，返回 (user_id, student_id)。"""
    from app.models import Chapter, Class, KnowledgePoint, Question, Student, StudentAnswer

    u = User(username=username, password_hash=hash_password("Passw0rd1"), name=username)
    session.add(u)
    session.flush()
    session.add(UserRole(user_id=u.id, role="student"))

    if session.query(Class).count() == 0:
        session.add(Class(grade="初三", name="1班"))
        session.flush()
    cls = session.query(Class).first()

    if session.query(Question).count() == 0:
        session.add(Chapter(id="ch_t", name="测试章", subject="math", grade="初三"))
        session.add(KnowledgePoint(id="kp_t", name="测试点", chapter_id="ch_t", subject="math"))
        session.flush()
        session.add(Question(chapter_id="ch_t", kp_id="kp_t", question_type="choice",
                             difficulty="easy", content="1+1=?", answer="2"))
        session.flush()
    q = session.query(Question).first()

    stu = Student(user_id=u.id, class_id=cls.id)
    session.add(stu)
    session.flush()
    session.add(StudentAnswer(source_type="quiz", source_id=1, question_id=q.id,
                              student_id=stu.id, content="2"))
    session.commit()
    return u.id, stu.id


def test_user_seq_renumber_after_delete(api):
    """展示序号：从 1 连续编号，删中间一个后面自动收拢（物理 id 不动）。"""
    client, session = api
    headers = _seed_user(session, "admin_seq", ["admin"])
    for name in ("u_a", "u_b", "u_c"):
        resp = client.post("/api/admin/users", headers=headers, json={
            "username": name, "password": "Passw0rd1", "name": name, "roles": ["teacher"]})
        assert resp.status_code == 201

    body = client.get("/api/admin/users", headers=headers).json()
    assert body["total"] == 4
    assert [u["seq"] for u in body["items"]] == [1, 2, 3, 4]

    mid_id = next(u["id"] for u in body["items"] if u["username"] == "u_b")
    assert client.delete(f"/api/admin/users/{mid_id}", headers=headers).status_code == 204

    body = client.get("/api/admin/users", headers=headers).json()
    assert body["total"] == 3
    assert [u["seq"] for u in body["items"]] == [1, 2, 3], "删中间一个，后面的序号自动收拢"


def test_delete_user_without_history_goes_through(api):
    """无历史数据：点了就删，不用二次确认。"""
    client, session = api
    headers = _seed_user(session, "admin_d1", ["admin"])
    client.post("/api/admin/users", headers=headers, json={
        "username": "u_plain", "password": "Passw0rd1", "name": "甲", "roles": ["teacher"]})
    uid = session.query(User).filter(User.username == "u_plain").one().id

    assert client.delete(f"/api/admin/users/{uid}", headers=headers).status_code == 204
    assert session.query(User).filter(User.id == uid).count() == 0
    assert session.query(UserRole).filter(UserRole.user_id == uid).count() == 0


def test_delete_user_with_history_needs_confirm(api):
    """有作答数据：先 409 提示；带 confirm=1 才连带删干净。"""
    from app.models import Student, StudentAnswer

    client, session = api
    headers = _seed_user(session, "admin_d2", ["admin"])
    uid, sid = _seed_student_with_answer(session, "u_smart")

    resp = client.delete(f"/api/admin/users/{uid}", headers=headers)
    assert resp.status_code == 409
    assert "作答" in resp.json()["detail"], "409 提示要说清有作答数据"
    assert session.query(Student).filter(Student.user_id == uid).count() == 1, "没确认不许动数据"

    resp = client.delete(f"/api/admin/users/{uid}?confirm=1", headers=headers)
    assert resp.status_code == 204
    assert session.query(User).filter(User.id == uid).count() == 0
    assert session.query(Student).count() == 0
    assert session.query(StudentAnswer).count() == 0
    assert session.query(UserRole).filter(UserRole.user_id == uid).count() == 0


def test_delete_admin_and_self_forbidden(api):
    """管理员账号不可删（只能停用）；自己不能删自己。"""
    client, session = api
    headers = _seed_user(session, "admin_self", ["admin"])
    _seed_user(session, "admin_other", ["admin"])
    self_id = session.query(User).filter(User.username == "admin_self").one().id
    other_id = session.query(User).filter(User.username == "admin_other").one().id

    assert client.delete(f"/api/admin/users/{self_id}", headers=headers).status_code == 400
    assert client.delete(f"/api/admin/users/{other_id}", headers=headers).status_code == 403


# ================================================================
# ⑨ 班级删除守卫（2026-10-24）：有学生/授课关系 → 409「请先迁移」
# ================================================================
def test_class_delete_blocked_until_migrated(api):
    """班里有学生或授课关系时拒绝删除（前端弹窗文案的来源），清空后才能删。"""
    from app.models import Class, Student, TeachingAssignment

    client, session = api
    headers = _seed_user(session, "admin_cls", ["admin"])
    cls_id = client.post("/api/admin/classes", headers=headers,
                         json={"grade": "初三", "name": "9班"}).json()["id"]

    # 挂一个学生 → 409，班级必须还在
    u = User(username="u_cls", password_hash=hash_password("Passw0rd1"), name="甲")
    session.add(u)
    session.flush()
    session.add(UserRole(user_id=u.id, role="student"))
    session.add(Student(user_id=u.id, class_id=cls_id))
    session.commit()
    resp = client.delete(f"/api/admin/classes/{cls_id}", headers=headers)
    assert resp.status_code == 409
    assert "请先迁移" in resp.json()["detail"]
    assert session.query(Class).filter(Class.id == cls_id).count() == 1

    # 学生迁走，换成一条授课关系 → 仍然 409
    session.query(Student).filter(Student.user_id == u.id).delete()
    t = User(username="u_cls_t", password_hash=hash_password("Passw0rd1"), name="乙")
    session.add(t)
    session.flush()
    session.add(UserRole(user_id=t.id, role="teacher"))
    session.add(TeachingAssignment(teacher_id=t.id, class_id=cls_id, subject="math"))
    session.commit()
    resp = client.delete(f"/api/admin/classes/{cls_id}", headers=headers)
    assert resp.status_code == 409
    assert session.query(Class).filter(Class.id == cls_id).count() == 1

    # 授课关系也清掉 → 可删
    session.query(TeachingAssignment).filter(TeachingAssignment.class_id == cls_id).delete()
    session.commit()
    assert client.delete(f"/api/admin/classes/{cls_id}", headers=headers).status_code == 204
    assert session.query(Class).filter(Class.id == cls_id).count() == 0


# ================================================================
# ⑩ teachers 档案同步（2026-10-24）：teacher 角色 ↔ teachers 行
# ================================================================
def test_teacher_profile_synced_with_role(api):
    """建 teacher 账号补档案行、CSV 导入同样补、取消角色删档案行。"""
    from app.models import Teacher

    client, session = api
    headers = _seed_user(session, "admin_tp", ["admin"])

    # 单条创建带 teacher 角色 → teachers 补一行
    body = client.post("/api/admin/users", headers=headers, json={
        "username": "T910", "password": "Passw0rd1", "name": "甲", "roles": ["teacher"]}).json()
    assert body["id"]
    assert session.query(Teacher).filter(Teacher.user_id == body["id"]).count() == 1

    # CSV 导入的 teacher 同样补档案
    client.post("/api/admin/users/import", headers=headers, json={
        "csv_content": "username,password,name,roles\nT911,Passw0rd1,乙,teacher\n"})
    t2 = session.query(User).filter(User.username == "T911").one()
    assert session.query(Teacher).filter(Teacher.user_id == t2.id).count() == 1

    # 取消 teacher 角色（换成 researcher）→ 档案行删掉
    resp = client.put(f"/api/admin/users/{body['id']}", headers=headers,
                      json={"roles": ["researcher"],
                            "research_leads": [{"grade": "初三", "subject": "math"}]})
    assert resp.status_code == 200
    assert session.query(Teacher).filter(Teacher.user_id == body["id"]).count() == 0


# ================================================================
# ⑪ 学生班级指定（2026-10-24）：手动建学生必须能选班级，改班级即迁移
# ================================================================
def test_student_create_requires_class_and_syncs(api):
    from app.models import Class, Student, TeachingAssignment

    client, session = api
    headers = _seed_user(session, "admin_sc", ["admin"])
    cls = Class(grade="初三", name="2班")
    session.add(cls)
    session.commit()

    # 不给班级 → 400（与 CSV"student 必须给 classes"同规则）
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "S920", "password": "Passw0rd1", "name": "甲", "roles": ["student"]})
    assert resp.status_code == 400
    assert "班级" in resp.json()["detail"]
    assert session.query(Student).count() == 0

    # 给班级 → students 档案落位，响应带 class_id（编辑弹窗回填用）
    body = client.post("/api/admin/users", headers=headers, json={
        "username": "S921", "password": "Passw0rd1", "name": "乙",
        "roles": ["student"], "class_id": cls.id}).json()
    assert body["class_id"] == cls.id
    assert session.query(Student).filter(Student.user_id == body["id"]).one().class_id == cls.id

    # 迁移班级：编辑只传 class_id
    cls2 = Class(grade="初三", name="3班")
    session.add(cls2)
    session.commit()
    resp = client.put(f"/api/admin/users/{body['id']}", headers=headers,
                      json={"class_id": cls2.id})
    assert resp.status_code == 200
    assert resp.json()["class_id"] == cls2.id
    assert session.query(Student).filter(Student.user_id == body["id"]).one().class_id == cls2.id

    # 非学生账号带 class_id → 400；班级 id 不存在 → 400
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "T922", "password": "Passw0rd1", "name": "丙",
        "roles": ["teacher"], "class_id": cls.id})
    assert resp.status_code == 400
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "S923", "password": "Passw0rd1", "name": "丁",
        "roles": ["student"], "class_id": 99999})
    assert resp.status_code == 400

    # 改名/停用不逼着补班级：老账号无档案只改 is_active 应放行
    legacy = User(username="S924", password_hash=hash_password("Passw0rd1"), name="戊")
    session.add(legacy)
    session.flush()
    session.add(UserRole(user_id=legacy.id, role="student"))
    session.commit()
    resp = client.put(f"/api/admin/users/{legacy.id}", headers=headers,
                      json={"is_active": False})
    assert resp.status_code == 200

    # 列表带班级信息：学生=就读班级；老师=任教班级（可多个）；管理员=空
    t = User(username="T925", password_hash=hash_password("Passw0rd1"), name="己")
    session.add(t)
    session.flush()
    session.add(UserRole(user_id=t.id, role="teacher"))
    session.add(TeachingAssignment(teacher_id=t.id, class_id=cls.id, subject="math"))
    session.add(TeachingAssignment(teacher_id=t.id, class_id=cls2.id, subject="math"))
    session.commit()

    items = client.get("/api/admin/users", headers=headers).json()["items"]
    by_name = {x["username"]: x for x in items}
    assert by_name["S921"]["class_names"] == ["初三3班"]   # 前面已迁到 cls2
    assert by_name["T925"]["class_names"] == ["初三2班", "初三3班"]
    assert by_name["admin_sc"]["class_names"] == []


# ================================================================
# ⑫ 任教班级随用户编辑同步（2026-10-24）：整体替换 teaching_assignments
# ================================================================
def test_teaching_synced_in_user_create_and_update(api):
    from app.models import Class, TeachingAssignment

    client, session = api
    headers = _seed_user(session, "admin_tt", ["admin"])
    cls_a = Class(grade="初三", name="1班")
    cls_b = Class(grade="初三", name="2班")
    session.add_all([cls_a, cls_b])
    session.commit()

    # 创建时带 teaching（双角色账号）→ 授课关系直接落位
    body = client.post("/api/admin/users", headers=headers, json={
        "username": "T930", "password": "Passw0rd1", "name": "甲",
        "roles": ["teacher", "researcher"],
        "research_leads": [{"grade": "初三", "subject": "math"}],
        "teaching": [{"class_id": cls_a.id, "subject": "math"},
                     {"class_id": cls_b.id, "subject": "math"}]}).json()
    assert body["teaching"] == [{"class_id": cls_a.id, "subject": "math"},
                                {"class_id": cls_b.id, "subject": "math"}]
    assert session.query(TeachingAssignment).filter(
        TeachingAssignment.teacher_id == body["id"]).count() == 2

    # 编辑：只留 1 班 → 整体替换为 1 条
    resp = client.put(f"/api/admin/users/{body['id']}", headers=headers,
                      json={"teaching": [{"class_id": cls_a.id, "subject": "math"}]})
    assert resp.status_code == 200
    rows = session.query(TeachingAssignment).filter(
        TeachingAssignment.teacher_id == body["id"]).all()
    assert [(r.class_id, r.subject) for r in rows] == [(cls_a.id, "math")]

    # 没了 teacher 角色 → teaching 传 [] 清空
    resp = client.put(f"/api/admin/users/{body['id']}", headers=headers,
                      json={"roles": ["researcher"],
                            "research_leads": [{"grade": "初三", "subject": "math"}],
                            "teaching": []})
    assert resp.status_code == 200
    assert session.query(TeachingAssignment).filter(
        TeachingAssignment.teacher_id == body["id"]).count() == 0

    # 非教师带 teaching → 400；班级不存在 → 400
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "S931", "password": "Passw0rd1", "name": "乙", "roles": ["student"],
        "class_id": cls_a.id, "teaching": [{"class_id": cls_a.id, "subject": "math"}]})
    assert resp.status_code == 400
    resp = client.post("/api/admin/users", headers=headers, json={
        "username": "T932", "password": "Passw0rd1", "name": "丙", "roles": ["teacher"],
        "teaching": [{"class_id": 99999, "subject": "math"}]})
    assert resp.status_code == 400
