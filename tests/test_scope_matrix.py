"""M6-T3 · 越权测试全矩阵（开发计划完成判据：本文件全绿，矩阵结果随交付附上）。

【矩阵语义——两种断言，一条不多一条不少】
  ① 放行侧（persona ∈ ep.allow）：守卫必须放行。
     断言：状态码 < 500 且 ∉ {401, 403}。
     业务层的 404/409/422 也算放行成功——本矩阵只管"越权"，不管"数据在不在"。
  ② 拒绝侧（persona ∉ ep.allow）：必须 403。
  ③ 匿名（不带 Authorization 头）：必须 401（OAuth2PasswordBearer 在守卫之前就拦）。

【覆盖范围】app.main 挂载的全部 API 路由（test_all_api_routes_covered 钉住）：
  新增端点不写进矩阵 → 该用例直接红，防止"漏挂守卫的端点悄悄上线"。

【五个人设（fixture 里造好，token 现签）】
  student     S001（档案 11，初二1班）        —— self 通道
  teacher     T001（任教初二1班）             —— detail 通道（class_ids=[1]）
  researcher  R001（任命 初二×math）          —— aggregate 通道（只有年级聚合）
  dual        D001（teacher 1班 + researcher）—— 双角色两通道并存（永不融合）
  admin       A001（另带学生档案 13，代演场景）—— 恒放行

【为什么放行侧敢跑真接口体】
  三处重活全部 stub，测试不碰真 PG / Neo4j / LLM：
    1) app.db.session.get_session_factory → sqlite 会话工厂
       （selection_engine / graph_tools / write_back 等"自建 session"的服务层全落沙盒）
    2) app.api.student/teacher 的 _get_*_graph → 假图（LangGraph 的 interrupt 链路不在这测）
    3) app.crews.analysis_crew.run_paper_analysis / exam_crew.run_exam_generation → 假 crew
  守卫（require_any / require_*_channel / self 检查）全部走真代码，一行不替。

运行：uv run pytest tests/test_scope_matrix.py -v
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import app
from app.models import (
    Base,
    BKTState,
    Chapter,
    Class,
    DiagnosticQuiz,
    ErrorLog,
    ExamPaper,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    Question,
    ResearchLead,
    Student,
    StudentAnswer,
    TeachingAssignment,
    User,
    UserRole,
)

# 全部演示账号共用这一个 bcrypt 哈希（同密码"Passw0rd1"只算一次，省 200ms×N）
#   验证密码走登录接口的业务逻辑，矩阵只关心 JWT 后的守卫，共用哈希无副作用
PASSWORD = "Passw0rd1"
HASH = hash_password(PASSWORD)

ALL = {"student", "teacher", "researcher", "dual", "admin"}


# =====================================================================
# 一、人设与种子数据（显式 id：矩阵路径里写死 /api/teacher/answers/41 这类具体号）
# =====================================================================
# (user_id, username, roles) —— 具体 id 与下文路径一一对应，改这里就要同步改路径
PERSONA_USERS = {
    "student":    (1, "S001", ["student"]),
    "teacher":    (3, "T001", ["teacher"]),
    "researcher": (5, "R001", ["researcher"]),
    "dual":       (6, "D001", ["teacher", "researcher"]),
    "admin":      (7, "A001", ["admin"]),
}


def _seed(session) -> None:
    """造一整间演示教室（全部显式主键，矩阵路径写死 id 才对得上）。

    组织：班级 1/2（初二1班、初二2班）；
          用户 1~9（1/2/9=学生，3/4/8=老师，5=组长，6=双角色，7=管理员）；
          授课关系 71/72/73（T001→1班，T002→2班，D001→1班）；
          任命 81/82（R001、D001 都管 初二×math）。
    内容：章节 ch_quadratic、知识点 kp_crossmult、题 21/22/23。
    学习过程：作业 31（1班）、作答 41（1班 S001）/42（2班 S002）、
              测验 51（S001 的）、卷子 61（初二）、错因 1 条（供错题汇总接口出数）。
    """
    # ---- 组织 ----
    session.add_all([
        Class(id=1, grade="初二", name="1班"),
        Class(id=2, grade="初二", name="2班"),
        User(id=1, username="S001", password_hash=HASH, name="学生甲"),
        User(id=2, username="S002", password_hash=HASH, name="学生乙"),
        User(id=3, username="T001", password_hash=HASH, name="王老师"),
        User(id=4, username="T002", password_hash=HASH, name="李老师"),
        User(id=5, username="R001", password_hash=HASH, name="陈组长"),
        User(id=6, username="D001", password_hash=HASH, name="张双角色"),
        User(id=7, username="A001", password_hash=HASH, name="管理员"),
        User(id=8, username="T003", password_hash=HASH, name="无课老师"),
        User(id=9, username="S003", password_hash=HASH, name="学生丙"),
    ])
    session.add_all([
        # 学生档案：11=S001(1班) 12=S002(2班) 13=A001(1班，代演) 14=S003(2班，写回测试备员)
        Student(id=11, user_id=1, class_id=1, student_no="2026001"),
        Student(id=12, user_id=2, class_id=2, student_no="2026002"),
        Student(id=13, user_id=7, class_id=1, student_no="2026003"),
        Student(id=14, user_id=9, class_id=2, student_no="2026004"),
    ])
    session.add_all([
        TeachingAssignment(id=71, teacher_id=3, class_id=1, subject="math"),
        TeachingAssignment(id=72, teacher_id=4, class_id=2, subject="math"),
        TeachingAssignment(id=73, teacher_id=6, class_id=1, subject="math"),
        ResearchLead(id=81, user_id=5, grade="初二", subject="math"),
        ResearchLead(id=82, user_id=6, grade="初二", subject="math"),
    ])
    # 角色表（登录接口 /admin 用户列表要读；JWT 签发不读它，但保持数据完整）
    all_user_roles = {
        1: ["student"], 2: ["student"], 3: ["teacher"], 4: ["teacher"],
        5: ["researcher"], 6: ["teacher", "researcher"], 7: ["admin"],
        8: ["teacher"], 9: ["student"],
    }
    session.add_all(
        UserRole(user_id=uid, role=r) for uid, rs in all_user_roles.items() for r in rs
    )

    # ---- 内容 ----
    session.add_all([
        Chapter(id="ch_quadratic", name="一元二次方程", subject="math", grade="初二"),
        KnowledgePoint(id="kp_crossmult", name="十字相乘法", chapter_id="ch_quadratic",
                       subject="math"),
    ])
    session.add_all([
        Question(id=21, chapter_id="ch_quadratic", kp_id="kp_crossmult",
                 question_type="fill", difficulty="easy",
                 content="解方程 x^2-5x+6=0", answer="x=2 或 x=3"),
        Question(id=22, chapter_id="ch_quadratic", kp_id="kp_crossmult",
                 question_type="choice", difficulty="medium",
                 content="下列哪个是 x^2-5x+6 的因式分解", answer="A",
                 options='["A. (x-2)(x-3)", "B. (x-1)(x-6)", "C. (x+2)(x+3)"]'),
        Question(id=23, chapter_id="ch_quadratic", kp_id="kp_crossmult",
                 question_type="fill", difficulty="hard",
                 content="解方程 2x^2-7x+3=0", answer="x=3 或 x=0.5"),
    ])

    # ---- 学习过程 ----
    session.add_all([
        Homework(id=31, teacher_id=3, class_id=1, title="演示作业",
                 kp_ids='["kp_crossmult"]'),
        HomeworkQuestion(homework_id=31, question_id=21, score=5.0),
        # 41 在 1 班（T001 可见），42 在 2 班（T001 越权验证口）
        StudentAnswer(id=41, source_type="homework", source_id=31, question_id=21,
                      student_id=11, content="x=2", is_correct=False, score=0.0),
        StudentAnswer(id=42, source_type="homework", source_id=31, question_id=22,
                      student_id=12, content="A", is_correct=True, score=5.0),
        ErrorLog(answer_id=41, error_type="概念不清", confidence=0.9,
                 evidence="演示数据：十字相乘常数项配错", explanation_strategy="回到整式乘法逆运算"),
        DiagnosticQuiz(id=51, student_id=11, target_points_json=[],
                       question_ids=[21, 22, 23], generated_reason="演示测验"),
        ExamPaper(id=61, title="初二数学演示卷", grade="初二", exam_type="monthly",
                  quota_plan_json={"plan": {"items": []}, "question_ids": [21, 22, 23],
                                   "gaps": []},
                  status="draft", created_by=5, remark="矩阵测试种子"),
        BKTState(student_id=11, kp_id="kp_crossmult", p_known=0.30),
        BKTState(student_id=12, kp_id="kp_crossmult", p_known=0.60),
    ])
    session.commit()


# =====================================================================
# 二、重活 stub：图 / crew 假实现（守卫之外的一切外部依赖）
# =====================================================================
class _FakeSnapshot:
    """graph.get_state 的返回品：只被读 .next（空=没挂起，非空=挂在 interrupt 上）。"""

    def __init__(self, next_nodes):
        self.next = next_nodes


class _FakeStudentGraph:
    """学生主图替身：一进一出，模拟 respond 节点产出一条回复。"""

    def get_state(self, config):
        return _FakeSnapshot(())

    def stream(self, graph_input, config, stream_mode=None):
        yield {"respond": {"messages": [AIMessage("（stub）先观察常数项，哪两个数相乘等于 6？")]}}


class _FakeGradingGraph:
    """批改图替身：恒"有挂起"（测 confirm 幂等保护的前置）+ 恒回终局结果。"""

    def get_state(self, config):
        return _FakeSnapshot(("apply_teacher_decision",))

    def invoke(self, graph_input, config=None):
        return {"status": "confirmed", "is_correct": True,
                "final_score": 5.0, "p_known_before": 0.30, "p_known_after": 0.44}


class _FakeQuizGraph:
    """S-10 子图替身：直接回"暂无错题"（矩阵不测出题逻辑，那是 test_quiz_generation 的活）。"""

    def invoke(self, state, config=None):
        return {"quiz_id": None, "generated_reason": "（stub）最近没有错题记录",
                "target_points": [], "question_ids": []}


def _install_stubs(monkeypatch) -> None:
    """把四类外部依赖换成假实现。stub 只替"业务重活"，守卫一律走真代码。"""
    fake_student, fake_grading, fake_quiz = _FakeStudentGraph(), _FakeGradingGraph(), _FakeQuizGraph()
    monkeypatch.setattr("app.api.student._get_student_graph", lambda: fake_student)
    monkeypatch.setattr("app.api.student._get_grading_graph", lambda: fake_grading)
    monkeypatch.setattr("app.api.student._get_quiz_graph", lambda: fake_quiz)
    monkeypatch.setattr("app.api.teacher._get_grading_graph", lambda: fake_grading)

    # crew 假报告：AnalysisReport 三字段合法即可，端点只做 model_dump 转发
    def fake_paper_analysis(metrics, title="", grade=""):
        from app.schemas.analysis import AnalysisReport

        return AnalysisReport(overview="（stub）演示报告", abnormal_items=[],
                              teaching_suggestions=["（stub）下周补 1 课时十字相乘专题"])

    monkeypatch.setattr("app.crews.analysis_crew.run_paper_analysis", fake_paper_analysis)

    def fake_exam_generation(exam_type, grade, subject="math", total_score=100, created_by=None):
        return {"paper_id": 999, "plan": {"exam_type": exam_type, "items": []},
                "question_ids": [21, 22], "gaps": [],
                "review": {"passed": True, "problems": [], "summary": "（stub）通过"}}

    monkeypatch.setattr("app.crews.exam_crew.run_exam_generation", fake_exam_generation)


# =====================================================================
# 三、端点矩阵表：path_tmpl 与 app.main 路由表逐条对账（漏一条测试就红）
# =====================================================================
class Ep:
    """一条端点矩阵行。

    属性：
      key       用例显示名（pytest -v 里看的就是它）
      method    HTTP 方法
      path_tmpl FastAPI 路由模板，必须与 app.routes 的 path 完全一致（对账用）
      path      带真实 id 的请求路径（矩阵实际打的）
      body      请求体 dict 或 None
      allow     放行角色集合；其余角色一律 403
    """

    def __init__(self, key, method, path_tmpl, path, body=None, allow=()):
        self.key = key
        self.method = method
        self.path_tmpl = path_tmpl
        self.path = path
        self.body = body
        self.allow = frozenset(allow)


STUDENT_OK = {"student", "admin"}                 # self 通道：本人 + 代演 admin
TEACHER_OK = {"teacher", "dual", "admin"}         # require_any("teacher")
RESEARCH_GRADE_OK = {"researcher", "dual", "admin"}  # 年级聚合：还要任命匹配年级

ENDPOINTS = [
    # ---- auth ----
    Ep("auth.me", "GET", "/api/auth/me", "/api/auth/me", allow=ALL),

    # ---- student 组（无角色守卫，全靠 self 通道数据层拦截）----
    Ep("stu.answer_stream", "POST", "/api/student/answer/stream",
       "/api/student/answer/stream",
       {"student_id": 11, "question_id": 21, "message": "这题答案是多少？"}, STUDENT_OK),
    Ep("stu.resume", "POST", "/api/student/resume", "/api/student/resume",
       {"student_id": 11, "question_id": 21, "message": "x=2"}, STUDENT_OK),
    Ep("stu.homework", "GET", "/api/student/homework", "/api/student/homework",
       allow=STUDENT_OK),
    Ep("stu.homework_questions", "GET", "/api/student/homework/{homework_id}/questions",
       "/api/student/homework/31/questions", allow=STUDENT_OK),
    Ep("stu.mastery", "GET", "/api/student/mastery/{student_id}",
       "/api/student/mastery/11", allow=STUDENT_OK),
    Ep("stu.quiz_generate", "POST", "/api/student/quiz/generate",
       "/api/student/quiz/generate", allow=STUDENT_OK),
    Ep("stu.quiz_detail", "GET", "/api/student/quiz/{quiz_id}", "/api/student/quiz/51",
       allow=STUDENT_OK),
    Ep("stu.quiz_submit", "POST", "/api/student/quiz/{quiz_id}/submit",
       "/api/student/quiz/51/submit",
       {"answers": [{"question_id": 21, "content": "A"}]}, STUDENT_OK),

    # ---- teacher 组（router 级 require_any("teacher")）----
    Ep("tea.homework_create", "POST", "/api/teacher/homework/create",
       "/api/teacher/homework/create",
       {"class_id": 1, "title": "演示作业", "kp_ids": ["kp_crossmult"],
        "per_kp": 1, "score": 5.0},
       TEACHER_OK),
    Ep("tea.knowledge_points", "GET", "/api/teacher/knowledge-points",
       "/api/teacher/knowledge-points", allow=TEACHER_OK),
    Ep("tea.homeworks", "GET", "/api/teacher/homeworks", "/api/teacher/homeworks",
       allow=TEACHER_OK),
    Ep("tea.homework_submissions", "GET", "/api/teacher/homework/{homework_ref}/submissions",
       "/api/teacher/homework/31/submissions", allow=TEACHER_OK),
    Ep("tea.grade_trigger", "POST", "/api/teacher/grade/{answer_id}",
       "/api/teacher/grade/41", allow=TEACHER_OK),
    Ep("tea.grade_confirm", "POST", "/api/teacher/grade/{answer_id}/confirm",
       "/api/teacher/grade/41/confirm", {"action": "confirm"}, TEACHER_OK),
    Ep("tea.grade_pending", "GET", "/api/teacher/grade/pending",
       "/api/teacher/grade/pending", allow=TEACHER_OK),
    Ep("tea.answer_detail", "GET", "/api/teacher/answers/{answer_id}",
       "/api/teacher/answers/41", allow=TEACHER_OK),
    Ep("tea.error_summary", "GET", "/api/teacher/class/{class_id}/error-summary",
       "/api/teacher/class/1/error-summary", allow=TEACHER_OK),
    Ep("tea.class_mastery", "GET", "/api/teacher/class/{class_id}/mastery",
       "/api/teacher/class/1/mastery", allow=TEACHER_OK),
    Ep("tea.classes", "GET", "/api/teacher/classes", "/api/teacher/classes",
       allow=TEACHER_OK),

    # ---- research 组（router 级 require_aggregate_channel）----
    #   年级类端点还有 _ensure_grade_in_scope：teacher 只有 detail 通道、没有
    #   aggregate 任命 → 年级集合为空 → 403。矩阵如实钉住这个行为：
    #   "看年级聚合"必须有 researcher 任命，teacher 的聚合权限止步于本班（T-03）。
    Ep("res.grade_mastery", "GET", "/api/research/grade/{grade}/mastery",
       "/api/research/grade/初二/mastery", allow=RESEARCH_GRADE_OK),
    Ep("res.mastery_compare", "GET", "/api/research/mastery/compare",
       "/api/research/mastery/compare?grade=初二", allow=RESEARCH_GRADE_OK),
    Ep("res.paper_analyze", "POST", "/api/research/paper/{paper_id}/analyze",
       "/api/research/paper/61/analyze", allow=RESEARCH_GRADE_OK),
    Ep("res.exam_generate", "POST", "/api/research/exam/generate",
       "/api/research/exam/generate",
       {"exam_type": "monthly", "grade": "初二", "subject": "math", "total_score": 100},
       RESEARCH_GRADE_OK),
    Ep("res.exam_quota", "GET", "/api/research/exam/{paper_id}/quota",
       "/api/research/exam/61/quota", allow=RESEARCH_GRADE_OK),
    Ep("res.exam_assign", "POST", "/api/research/exam/{paper_id}/assign",
       "/api/research/exam/61/assign", {"class_ids": [1]}, RESEARCH_GRADE_OK),
    # 异常题清单无年级参数、无明细（只回题干前 60 字）→ teacher 也放行
    Ep("res.abnormal_list", "GET", "/api/research/questions/abnormal",
       "/api/research/questions/abnormal",
       allow={"teacher", "researcher", "dual", "admin"}),

    # ---- admin 组（router 级 require_any("admin")）----
    Ep("adm.users", "GET", "/api/admin/users", "/api/admin/users", allow={"admin"}),
    Ep("adm.user_create", "POST", "/api/admin/users", "/api/admin/users",
       {"username": "newu9", "password": PASSWORD, "name": "新用户", "roles": ["student"],
        "class_id": 1},
       {"admin"}),
    Ep("adm.user_update", "PUT", "/api/admin/users/{user_id}", "/api/admin/users/2",
       {"name": "改名测试"}, {"admin"}),
    Ep("adm.user_delete", "DELETE", "/api/admin/users/{user_id}", "/api/admin/users/8",
       allow={"admin"}),   # 8=T003 无作业无档案，删除无历史数据直接走通
    Ep("adm.users_import", "POST", "/api/admin/users/import", "/api/admin/users/import",
       {"csv_content": "username,password,name,roles\nN001,Passw0rd1,甲,student\n"},
       {"admin"}),
    Ep("adm.import_template", "GET", "/api/admin/users/import-template",
       "/api/admin/users/import-template", allow={"admin"}),
    Ep("adm.ta_list", "GET", "/api/admin/teaching-assignments",
       "/api/admin/teaching-assignments", allow={"admin"}),
    Ep("adm.ta_create", "POST", "/api/admin/teaching-assignments",
       "/api/admin/teaching-assignments",
       {"teacher_id": 3, "class_id": 2, "subject": "math"}, {"admin"}),
    Ep("adm.ta_delete", "DELETE", "/api/admin/teaching-assignments/{ta_id}",
       "/api/admin/teaching-assignments/72", allow={"admin"}),
    Ep("adm.rl_list", "GET", "/api/admin/research-leads", "/api/admin/research-leads",
       allow={"admin"}),
    Ep("adm.rl_create", "POST", "/api/admin/research-leads", "/api/admin/research-leads",
       {"user_id": 3, "grade": "初二", "subject": "math"}, {"admin"}),
    Ep("adm.rl_delete", "DELETE", "/api/admin/research-leads/{rl_id}",
       "/api/admin/research-leads/82", allow={"admin"}),
    Ep("adm.classes", "GET", "/api/admin/classes", "/api/admin/classes", allow={"admin"}),
    Ep("adm.class_create", "POST", "/api/admin/classes", "/api/admin/classes",
       {"grade": "初二", "name": "3班"}, {"admin"}),
    Ep("adm.class_update", "PUT", "/api/admin/classes/{class_id}", "/api/admin/classes/2",
       {"grade": "初二", "name": "2班"}, {"admin"}),
    Ep("adm.class_delete", "DELETE", "/api/admin/classes/{class_id}", "/api/admin/classes/2",
       allow={"admin"}),
    Ep("adm.audit_logs", "GET", "/api/admin/audit-logs", "/api/admin/audit-logs",
       allow={"admin"}),
]

# 公开端点（无鉴权）：健康探针 + 登录口
PUBLIC_ENDPOINTS = [
    Ep("health", "GET", "/health", "/health", allow=ALL),
    Ep("auth.login", "POST", "/api/auth/login", "/api/auth/login",
       {"username": "T001", "password": PASSWORD}, allow=ALL),
]


# =====================================================================
# 四、公共夹具：sqlite 沙盒 + TestClient + stub + 种子
# =====================================================================
@pytest.fixture()
def api(monkeypatch):
    """每个用例一套独立沙盒，互不污染（与 test_api_admin 同套路）。

    StaticPool + check_same_thread=False：TestClient 请求跑在别的线程，
    sqlite :memory: 不锁单连接的话那条线程看到的是"空库"（no such table）。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, future=True, autoflush=False, expire_on_commit=False)
    session = maker()

    # ① FastAPI 的 Depends(get_db) → sqlite 会话
    app.dependency_overrides[get_db] = lambda: session
    # ② 服务层自建会话（get_session_factory）也落同一个 sqlite
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: maker)
    # ③ 图 / crew stub
    _install_stubs(monkeypatch)
    # ④ 种子数据
    _seed(session)

    client = TestClient(app)   # 不进 with：不触发 lifespan，不连真 PG
    yield client, session

    app.dependency_overrides.clear()
    session.close()
    engine.dispose()


def _headers(session, persona: str) -> dict:
    """按人设签发多角色 JWT，返回请求头。"""
    uid, username, roles = PERSONA_USERS[persona]
    token = create_access_token(uid, username, roles)
    return {"Authorization": f"Bearer {token}"}


# =====================================================================
# 五、矩阵主测试：每接口 × 每角色
# =====================================================================
@pytest.mark.parametrize("persona", sorted(ALL))
@pytest.mark.parametrize("ep", ENDPOINTS, ids=lambda e: e.key)
def test_endpoint_role_matrix(api, ep, persona):
    """矩阵主用例：放行侧非 403，拒绝侧必 403。

    模拟数据流（以 tea.answer_detail × researcher 为例）：
      t1 请求 GET /api/teacher/answers/41，头里是 R001 的 researcher token
      t2 router 级 require_any("teacher")：roles 交集为空 → 403
         （接口体一行没跑，这就是"未授权一律 403"的落点）
    反例（tea.answer_detail × teacher）：
      t2 require_any 命中 → t3 require_detail_channel 有 detail 通道 → 200
    """
    client, session = api
    headers = _headers(session, persona)
    resp = client.request(ep.method, ep.path, headers=headers, json=ep.body)

    if persona in ep.allow:
        assert resp.status_code < 500, f"{ep.key}×{persona} 放行侧炸了：{resp.text[:200]}"
        assert resp.status_code not in (401, 403), \
            f"{ep.key}×{persona} 应放行却 {resp.status_code}：{resp.text[:200]}"
    else:
        assert resp.status_code == 403, \
            f"{ep.key}×{persona} 越权未拦：{resp.status_code} {resp.text[:200]}"


@pytest.mark.parametrize("ep", ENDPOINTS, ids=lambda e: e.key)
def test_endpoint_anonymous_401(api, ep):
    """匿名（无 token）一律 401：OAuth2PasswordBearer 在一切守卫之前先斩。"""
    client, _ = api
    resp = client.request(ep.method, ep.path, json=ep.body)
    assert resp.status_code == 401, f"{ep.key} 匿名应 401，实际 {resp.status_code}"


@pytest.mark.parametrize("ep", PUBLIC_ENDPOINTS, ids=lambda e: e.key)
def test_public_endpoints_no_auth(api, ep):
    """公开端点不鉴权也放行（health 探针 / 登录口）。"""
    client, _ = api
    resp = client.request(ep.method, ep.path, json=ep.body)
    assert resp.status_code == 200, f"{ep.key} 公开端点应 200：{resp.text[:200]}"


def test_all_api_routes_covered():
    """🔴 对账用例：app.main 挂载的每条路由都必须出现在矩阵表里。

    漏了端点（新增接口忘写矩阵）→ 这里直接红，报出漏网路径。
    这就是"每接口 × 每角色"里"每接口"的机器保证，不靠人眼数。

    为什么走 openapi() 而不是 app.routes：
      新版 FastAPI 的 include_router 不再把子路由摊平进 app.routes
      （挂成 _IncludedRouter），遍历 app.routes 只能看到 /health 一条；
      openapi()["paths"] 是最终生效的完整路由表（44 条操作），拿来对账最准。
    """
    spec = app.openapi()
    actual = {
        (path, method.upper())
        for path, ops in spec["paths"].items()
        for method in ops
        if method.upper() not in ("HEAD", "OPTIONS")
    }
    covered = {(e.path_tmpl, e.method) for e in ENDPOINTS + PUBLIC_ENDPOINTS}
    assert actual == covered, (
        f"矩阵漏覆盖：{sorted(actual - covered)}；矩阵多余（路径对不上路由）：{sorted(covered - actual)}"
    )


# =====================================================================
# 六、数据级越权（角色对了、数据范围不对，同样必须 403）
# =====================================================================
class TestDataScopeIsolation:
    """角色守卫之外的第二道闸：self / 班级 / 年级 三个数据维度的越权。"""

    def test_teacher_other_class_answer_403(self, api):
        """T001 只教 1 班 → 摸 2 班作答 42 必 403（班级级越权）。

        说明：answers/{id} 这条路由的班级校验走 require_detail_channel + 自查
        class_ids，报错文案是 "detail scope required"（不是 _ensure_class_in_scope
        的"不在你的任教范围"）——断言按实际文案钉住，谁改文案谁来看这条。
        """
        client, session = api
        resp = client.get("/api/teacher/answers/42", headers=_headers(session, "teacher"))
        assert resp.status_code == 403
        assert "forbidden" in resp.json()["detail"]

    def test_teacher_no_assignment_403(self, api):
        """有 teacher 角色但零授课记录 → 空 class_ids（安全态：查空而非放全量）。"""
        client, session = api
        uid, username, roles = 8, "T003", ["teacher"]
        headers = {"Authorization": f"Bearer {create_access_token(uid, username, roles)}"}
        resp = client.get("/api/teacher/class/1/error-summary", headers=headers)
        assert resp.status_code == 403

    def test_student_other_student_mastery_403(self, api):
        """S001 查 S002 的掌握度 → self 通道拦截（学生 A 摸学生 B 的数据）。"""
        client, session = api
        resp = client.get("/api/student/mastery/12", headers=_headers(session, "student"))
        assert resp.status_code == 403

    def test_student_stream_as_other_student_403(self, api):
        """S001 拿自己的 token 替 S002 作答 → 403（body.student_id 归属校验）。"""
        client, session = api
        resp = client.post("/api/student/answer/stream",
                           headers=_headers(session, "student"),
                           json={"student_id": 12, "question_id": 21, "message": "代答"})
        assert resp.status_code == 403

    def test_researcher_wrong_grade_403(self, api):
        """R001 只任命了 初二 → 查 初三 年级聚合 403（年级级越权）。"""
        client, session = api
        resp = client.get("/api/research/grade/初三/mastery",
                          headers=_headers(session, "researcher"))
        assert resp.status_code == 403
        assert "只负责" in resp.json()["detail"]

    def test_researcher_detail_api_403_m5_verdict(self, api):
        """🔴 M5 完成判据原场景：researcher 调作答明细接口必须 403。"""
        client, session = api
        resp = client.get("/api/teacher/answers/41", headers=_headers(session, "researcher"))
        assert resp.status_code == 403

    def test_inactive_user_401(self, api):
        """停用账号（is_active=False）：token 未过期也 401，权限即时回收。"""
        client, session = api
        session.add(User(id=15, username="OFF01", password_hash=HASH, name="停用老师",
                         is_active=False))
        session.add(UserRole(user_id=15, role="teacher"))
        session.commit()
        headers = {"Authorization": f"Bearer {create_access_token(15, 'OFF01', ['teacher'])}"}
        resp = client.get("/api/teacher/classes", headers=headers)
        assert resp.status_code == 401

    def test_dual_role_both_channels(self, api):
        """双角色并集：D001 既有明细（1 班）又有聚合（初二），两通道各自放行、互不融合。"""
        client, session = api
        headers = _headers(session, "dual")
        detail = client.get("/api/teacher/answers/41", headers=headers)     # 明细通道
        aggregate = client.get("/api/research/grade/初二/mastery", headers=headers)  # 聚合通道
        assert detail.status_code == 200
        assert aggregate.status_code == 200


# =====================================================================
# 七、R-04 → R-08 回流在 API 层打通（M6-T2 闭环线二的"分析→写回"一段）
# =====================================================================
def test_paper_analyze_writes_back_quality_flags(api):
    """走真 analyze 链路（crew 除外）：指标 → 异常题写回 quality_flag=abnormal。

    模拟数据（4 个学生 × 3 题，满分 100/3≈33.33）：
      q21 全错 → P=0.0（过难）；q22 全对 → P=1.0（过易）；q23 高低分开 → P=0.5 D=1.0（正常）
    预期：abnormal_written == 2（q21/q22 从 normal 翻成 abnormal，q23 不动）
    为什么重要：这一步就是"考后分析 → 考前改进"的回流落点，
              下次 /exam/generate 抽题时 abnormal 沉底降权（selection_engine._pool_sort_key）。
    """
    client, session = api
    full = 100.0 / 3

    # 造 4 人作答（档案 11/12/13/14 已在种子数据里）
    answers = []
    for sid in (11, 12, 13, 14):
        rank = {11: 0, 12: 1, 13: 2, 14: 3}[sid]     # 能力序：11 最弱 → 14 最强
        answers += [
            StudentAnswer(source_type="exam", source_id=61, question_id=21,
                          student_id=sid, content="（演示）", is_correct=False, score=0.0),
            StudentAnswer(source_type="exam", source_id=61, question_id=22,
                          student_id=sid, content="（演示）", is_correct=True, score=full),
            StudentAnswer(source_type="exam", source_id=61, question_id=23,
                          student_id=sid, content="（演示）",
                          is_correct=rank >= 2, score=full if rank >= 2 else 0.0),
        ]
    session.add_all(answers)
    session.commit()

    resp = client.post("/api/research/paper/61/analyze", headers=_headers(session, "researcher"))
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["abnormal_written"] == 2

    from app.models import Question

    flags = {q.id: q.quality_flag for q in session.query(Question).all()}
    assert flags[21] == "abnormal" and flags[22] == "abnormal"
    assert flags[23] == "normal", "正常题绝不能被误伤"

    # 再次 analyze 幂等：已 abnormal 不重复计数、不覆盖 disabled
    resp2 = client.post("/api/research/paper/61/analyze", headers=_headers(session, "researcher"))
    assert resp2.json()["abnormal_written"] == 0
