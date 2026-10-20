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

GOOD_CSV = ("username,password,name,roles\n"
            "S301,Passw0rd1,张小明,student\n"
            "T301,Passw0rd2,李老师,teacher;researcher\n")
BAD_CSV = GOOD_CSV + "S302,short,T王,student\n"     # 第 4 行密码太弱（第 1 行表头）


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

    rows = session.query(User).all()
    assert {u.username for u in rows} == {"admin_u", "S301", "T301"}
    # 角色同步验证：T301 应有两行 user_roles
    t301 = next(u for u in rows if u.username == "T301")
    roles = session.query(UserRole).filter(UserRole.user_id == t301.id).all()
    assert sorted(r.role for r in roles) == ["researcher", "teacher"]


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
    dup_csv = ("username,password,name,roles\n"
               "S401,Passw0rd1,甲,student\n"
               "S401,Passw0rd2,乙,student\n")
    resp = client.post("/api/admin/users/import", headers=headers,
                       json={"csv_content": dup_csv})
    body = resp.json()
    assert body["ok"] is False
    assert any("重复" in e["reason"] for e in body["errors"])
    assert session.query(User).filter(User.username == "S401").count() == 0
