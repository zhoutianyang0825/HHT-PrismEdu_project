"""M1-T6 · scope 隔离单测（开发计划完成判据：本文件全绿）。

 覆盖四个面：
   ① 多角色 JWT 签发/解析；② 密码强度策略与失败锁定；
   ③ 双通道 scope：多角色并集、researcher 摸不到明细通道（🔴 关键用例）；
   ④ CSV 导入事务性：坏行整批不写库。

 运行：uv run pytest tests/test_scope_isolation.py -v

 pytest 组织规则速记：
   测试文件名必须 test_*.py；测试函数必须 test_*；类必须 Test* 开头（不用继承任何东西）。
   pytest 自动发现并逐个运行，assert 失败即该用例红。
   陌生写法 pytest.raises(异常类型)：上下文管理器——with 块里的代码"必须"抛出
   该异常才算通过；没抛或抛了别的类型都算失败。match= 再用正则校验异常信息，
   防止"碰巧抛了同类型但语义不对"的假绿。
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import JWTError, jwt

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 保证可从任意目录跑

from app.core import security
from app.core.config import settings
from app.core.deps import (
    CurrentUser,
    build_scopes,
    require_aggregate_channel,
    require_detail_channel,
)
from app.models import Question
from app.rag.ingest import ingest_questions, validate_question_rows


# ================================================================ ① JWT 多角色
class TestJwt:
    # 类只负责"按主题分组用例"，pytest 会把类里每个 test_* 方法当独立用例跑
    def test_multi_role_roundtrip(self):
        """多角色签发 → 解析，roles 数组原样保序保留。"""
        token = security.create_access_token(3, "R003", ["teacher", "researcher"])
        payload = security.decode_token(token)
        assert payload["sub"] == "3"
        assert payload["username"] == "R003"
        assert payload["roles"] == ["teacher", "researcher"]

    def test_invalid_role_rejected(self):
        """token 里塞非法角色 → 直接 ValueError，脏角色进不了 JWT。"""
        with pytest.raises(ValueError, match="非法角色"):
            security.create_access_token(1, "x", ["superuser"])

    def test_expired_token_raises(self):
        """过期 token 解析必须抛 JWTError（转 401），不能静默放行。

        为啥手搓 payload：create_access_token 只能签"没过期"的 token，
        要测过期只能自己构造 exp 是过去时间的 payload，用底层 jwt.encode 签——
        等于绕过被测函数的上游，专测 decode 对过期 token 的反应。
        """
        payload = {"sub": "1", "roles": ["student"],
                   "exp": datetime.now(timezone.utc) - timedelta(seconds=1)}
        token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
        with pytest.raises(JWTError):
            security.decode_token(token)


# ================================================================ ② 密码策略 / 锁定
class TestPasswordPolicy:
    def test_weak_passwords_rejected(self):
        """三种典型弱密码：太短 / 纯字母 / 纯数字，全部拒绝。"""
        for weak in ["short1", "nodigitpwd", "12345678"]:
            with pytest.raises(ValueError):
                security.validate_password_strength(weak)

    def test_strong_password_ok_and_hashed(self):
        security.validate_password_strength("Prism2026")
        hashed = security.hash_password("Prism2026")
        assert hashed != "Prism2026" and hashed.startswith("$2")   # bcrypt 前缀
        assert security.verify_password("Prism2026", hashed)
        assert not security.verify_password("wrong123", hashed)

    def test_lockout_after_5_failures(self):
        """滑动窗口内第 5 次失败触发锁定，第 4 次还不锁。"""
        user = "lock_test_user"
        security.clear_failures(user)
        for _ in range(4):
            assert not security.register_failed_login(user)
        assert not security.is_locked(user)          # 4 次还没锁
        assert security.register_failed_login(user)  # 第 5 次触发
        assert security.is_locked(user)

    def test_success_clears_failures(self):
        """登录成功清空失败记录：4 次失败 + 1 次成功 → 不锁。"""
        user = "clear_test_user"
        security.clear_failures(user)
        for _ in range(4):
            security.register_failed_login(user)
        security.clear_failures(user)
        assert not security.is_locked(user)


# ================================================================ ③ 双通道 scope
def _scopes_for(roles, teacher_classes=None, research_leads=None, user_id=1):
    """build_scopes 的测试便捷封装：模拟从 PG 查出的两份关系数据。"""
    return build_scopes(roles, teacher_classes, research_leads, user_id)


class TestScopes:
    def test_teacher_detail_channel(self):
        """teacher → detail 通道，class_ids 来自 teaching_assignments。"""
        scopes = _scopes_for(["teacher"], teacher_classes=[(3, "math"), (7, "math")])
        assert scopes == [{"type": "detail", "class_ids": [3, 7], "subjects": ["math"]}]

    def test_researcher_aggregate_only(self):
        """researcher → 只有 aggregate 通道，绝无 detail——隔离的第一性保证。"""
        scopes = _scopes_for(["researcher"], research_leads=[("初二", "math")])
        assert scopes == [{"type": "aggregate", "grade": "初二", "subject": "math"}]

    def test_multi_role_union(self):
        """teacher+researcher 双角色：两通道并存但类型隔离，互不展开融合。"""
        scopes = _scopes_for(["teacher", "researcher"],
                             teacher_classes=[(3, "math")],
                             research_leads=[("初二", "math")])
        types = [s["type"] for s in scopes]
        assert types == ["detail", "aggregate"]      # 两条通道独立返回
        assert scopes[0]["class_ids"] == [3]         # detail 只有任教班
        assert scopes[1]["grade"] == "初二"          # aggregate 是年级粒度

    def test_teacher_without_assignment_safe_empty(self):
        """teacher 有角色但没授课记录 → 空 class_ids（安全态：查空而非放全量）。"""
        scopes = _scopes_for(["teacher"], teacher_classes=[])
        assert scopes[0]["class_ids"] == []

    def test_student_self_scope(self):
        scopes = _scopes_for(["student"], user_id=42)
        assert scopes == [{"type": "self", "student_id": 42}]

    def test_admin_gets_both_channels(self):
        """admin 恒有 detail+aggregate 两通道（教学管理员场景）。"""
        scopes = _scopes_for(["admin"], teacher_classes=[(1, "math")],
                             research_leads=[("初三", "math")])
        types = [s["type"] for s in scopes]
        assert "detail" in types and "aggregate" in types


class TestChannelGuards:
    """接口守卫的直接调用测试：不等 M5 起服务，这里就把 403 行为钉死。"""

    def test_researcher_detail_channel_403(self):
        """🔴 关键用例：researcher 调学生明细接口必须 403（开发计划 M1 判据）。"""
        researcher = CurrentUser(3, "R003", ["researcher"])
        scopes = _scopes_for(["researcher"], research_leads=[("初二", "math")])
        # pytest.raises as exc：把抛出的异常对象抓进 exc——
        #   exc.value 就是那个 HTTPException，接着还能断言它的 status_code 是 403
        with pytest.raises(HTTPException) as exc:
            require_detail_channel(researcher, scopes)
        assert exc.value.status_code == 403

    def test_teacher_detail_channel_ok(self):
        """teacher 走明细通道放行，返回 scopes 供接口层过滤班级。"""
        teacher = CurrentUser(2, "T002", ["teacher"])
        scopes = _scopes_for(["teacher"], teacher_classes=[(3, "math")])
        assert require_detail_channel(teacher, scopes) == scopes

    def test_researcher_aggregate_channel_ok(self):
        """researcher 走聚合通道放行——他能用的只有这一条路。"""
        scopes = _scopes_for(["researcher"], research_leads=[("初二", "math")])
        assert require_aggregate_channel(scopes) == scopes

    def test_student_aggregate_channel_403(self):
        """student 只有 self 通道，聚合接口也不该放行。"""
        with pytest.raises(HTTPException) as exc:
            require_aggregate_channel([{"type": "self", "student_id": 42}])
        assert exc.value.status_code == 403


# ================================================================ ④ CSV 事务性
class TestCsvIngestTransaction:
    """开发计划 M1-T6：坏行整批不写库，不允许"导了一半"。"""

    # 陌生写法 **overrides（双星收集/展开）：
    #   定义处 def _row(self, **overrides)——调用时多传的 keyword 参数收进 dict；
    #   调用处 {**base, **overrides}——把两个 dict 摊开合并成新 dict，右边的优先。
    #   效果：每道题默认全字段合法，测试里只覆盖想改坏的那一两个字段
    def _row(self, **overrides):
        base = {"chapter_id": "ch_quadratic", "kp_id": "kp_crossmult",
                "question_type": "fill", "difficulty": "easy",
                "content": "解方程 x^2-4=0", "answer": "x=±2",
                "quality_flag": "normal"}
        return {**base, **overrides}

    def test_validate_catches_bad_rows(self, session):
        """非法枚举 / 空题干 / 重复题干，每一条都要被抓出来。"""
        rows = [
            self._row(),
            self._row(question_type="judge", content="已知 x=2，求 2x+1"),  # 非法题型
            self._row(content="   "),                                        # 空题干
            self._row(content="解方程 x^2-4=0"),                              # 与第 1 行重复
        ]
        errors = validate_question_rows(rows)
        assert len(errors) == 3
        assert any("第3行" in e for e in errors)
        assert any("第4行" in e for e in errors)
        assert any("第5行" in e for e in errors)   # 行号 = 索引+2（表头占第1行）

    def test_bad_batch_never_touches_db(self, session):
        """🔴 事务性判据：校验不过 → 整批不入库，questions 表保持 0 行。"""
        rows = [self._row(), self._row(question_type="judge")]  # 一好一坏
        errors = validate_question_rows(rows)
        assert errors                       # 校验不过 → 调用方短路，绝不调 ingest_questions
        assert session.query(Question).count() == 0

    def test_ingest_then_rollback_leaves_nothing(self, session):
        """就算 ingest 已执行，只要不 commit 就 rollback——PG 事务兜底。

        陌生点 rollback：事务三连 commit/rollback/flush——
        flush = 把改动发到数据库但"不盖章"（事务内可见，别的事务看不见）；
        commit = 盖章生效；rollback = 撤销本次事务里所有 flush 过的改动。
        这里专测最后一环：数据已经 flush 进去了，rollback 也能连本带利收回来。
        """
        rows = [self._row(), self._row(content="解方程 x^2-9=0")]
        assert validate_question_rows(rows) == []
        n = ingest_questions(session, rows)
        assert n == 2
        assert session.query(Question).count() == 2   # flush 后事务内可见
        session.rollback()
        assert session.query(Question).count() == 0   # 回滚 → 一行不留

    def test_valid_batch_persists_after_commit(self, session):
        """正常批次：校验通过 → 入库 → commit 落库。"""
        rows = [self._row(), self._row(content="分解因式 x^2-5x+6")]
        assert validate_question_rows(rows) == []
        ingest_questions(session, rows)
        session.commit()
        assert session.query(Question).count() == 2
