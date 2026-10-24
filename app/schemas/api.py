# app/schemas/api.py
# M5 · API 层请求体契约：所有端点的入参 Pydantic 模型集中在本文件。
#
# 【为什么集中放而不写在各端点文件里】
#   1) 前端（Streamlit）与后端是两拨文件，契约放一处，改字段时两边都不会漏改；
#   2) FastAPI 自动用这些模型做"请求体校验"——字段缺失/类型错直接 422，
#      接口函数体一行不执行，业务代码永远拿到干净数据。
#
# 【BaseModel 的额外校验手段】
#   Field(min_length=...)  值域/长度约束，不合格直接 ValidationError -> 422
#   Literal["a","b"]       枚举锁死，传别的值直接 422
#   validator 见 admin.py 里的 CSV 行校验（那边是动态数据，用函数手校更直观）
from typing import Literal

from pydantic import BaseModel, Field


# ================================================================
# 认证
# ================================================================
class LoginIn(BaseModel):
    """登录请求体。

    username 如 "R003"；password 明文（HTTPS 层负责传输安全，应用层只存 bcrypt 哈希）。
    """

    username: str = Field(min_length=1, description="登录名")
    password: str = Field(min_length=1, description="明文密码")


# ================================================================
# 学生端
# ================================================================
class StudentChatIn(BaseModel):
    """学生单题作答/引导的入参（SSE 流式接口）。

    student_id  students 表主键（学生档案 id，不是 users.id）；
    question_id 当前在做题的 id —— thread_id 用它拼 {student_id}_{question_id}；
    message     学生这句话原文，如 "这题答案是多少"。
    """

    student_id: int = Field(description="学生档案 id（students.id）")
    question_id: int = Field(description="题目 id")
    message: str = Field(min_length=1, max_length=2000, description="学生输入")


class StudentResumeIn(BaseModel):
    """恢复被 interrupt 挂起的会话（学生引导场景）。

    字段与 StudentChatIn 相同：thread_id 由 student_id+question_id 拼出，
    message 会包进 Command(resume=message) 喂给挂起的图。
    """

    student_id: int = Field(description="学生档案 id")
    question_id: int = Field(description="题目 id")
    message: str = Field(min_length=1, max_length=2000, description="学生对上一次提示的回应")


class QuizAnswerItem(BaseModel):
    """整套测验里单题的作答。content 是学生写的原文（选择题传选项字母如 "A"）。"""

    question_id: int = Field(description="题目 id")
    content: str = Field(min_length=1, description="学生作答原文")


class QuizSubmitIn(BaseModel):
    """提交整套诊断测验（source_type=quiz）。

    answers 逐题作答列表；后端逐题建 student_answers 行并触发批改，
    客观题当场 confirmed，主观题挂起等老师（响应里逐题标明状态）。
    """

    answers: list[QuizAnswerItem] = Field(min_length=1, description="逐题作答")


# ================================================================
# 老师端
# ================================================================
class HomeworkCreateIn(BaseModel):
    """按知识点布置作业（T-01）。

    kp_ids     覆盖的知识点 id 列表，如 ["kp_crossmult", "kp_peifang"]；
    per_kp     每个知识点抽几道题（默认 2）；
    score      每题分值（默认 4 分）。
    题目由 selection_engine 从题库抽（可复现），不经过 LLM。
    """

    class_id: int = Field(description="布置给哪个班")
    title: str = Field(min_length=1, max_length=128, description="作业标题")
    kp_ids: list[str] = Field(min_length=1, description="覆盖知识点 id 列表")
    per_kp: int = Field(default=2, ge=1, le=5, description="每知识点题数")
    score: float = Field(default=4.0, gt=0, description="每题分值")


class GradeConfirmIn(BaseModel):
    """老师对主观题 AI 建议的裁决（resume 值的 HTTP 形态）。

    action=confirm  采纳 AI 建议分；
    action=edit     用 score 字段的分数（0~full_score）；
    action=reject   驳回，分数不生效，BKT 不更新。
    """

    action: Literal["confirm", "edit", "reject"] = Field(description="裁决动作")
    score: float | None = Field(default=None, ge=0, description="action=edit 时必填")
    comment: str | None = Field(default=None, max_length=500, description="可选，覆盖 AI 评语")


# ================================================================
# 教研端
# ================================================================
class ExamGenerateIn(BaseModel):
    """R-08 出卷请求。

    exam_type=monthly      月考（巩固型）：错题热点命中率 ≥70%；
    exam_type=midterm/final 摸底型：章节覆盖率 ≥80%。
    """

    exam_type: Literal["monthly", "midterm", "final"] = Field(description="考试类型")
    grade: str = Field(min_length=1, description="年级，如 初二")
    subject: str = Field(default="math", description="学科")
    total_score: int = Field(default=100, gt=0, description="卷面总分")


class ExamAssignIn(BaseModel):
    """试卷定稿布置。class_ids 是目标班级；作答数据由演示脚本合成（M6-T4）。"""

    class_ids: list[int] = Field(min_length=1, description="布置到哪些班")


# ================================================================
# 管理后台（A-01）
# ================================================================
class UserCreateIn(BaseModel):
    """单条创建用户。

    roles 数组多角色一次给足，如 ["teacher", "researcher"]；
    勾了 researcher 必须同时给 research_leads（年级+学科），否则保存前校验直接 400——
    空任命的 researcher 登录后处处 403，排查起来极痛苦（架构文档坑表点名）。
    """

    username: str = Field(min_length=2, max_length=64, description="登录名，全局唯一")
    password: str = Field(min_length=8, max_length=72, description="明文密码，服务端再做强度校验")
    name: str = Field(min_length=1, max_length=64, description="真实姓名")
    roles: list[Literal["student", "teacher", "researcher", "admin"]] = Field(
        min_length=1, description="角色数组"
    )
    
    research_leads: list[dict] | None = Field(
        default=None, description="researcher 任命 [{grade, subject}]，勾 researcher 时必填"
    )

    class_id: int | None = Field(
        default=None, description="student 就读班级 classes.id，勾 student 时必填（与 CSV 的 classes 列同规则）"
    )
    teaching: list[dict] | None = Field(
        default=None, description="teacher 授课关系 [{class_id, subject}]，勾 teacher 时可给；传了整体替换"
    )


class UserUpdateIn(BaseModel):
    """修改用户：全部字段可选，传了才改。roles 传了会整体替换 user_roles。"""

    name: str | None = Field(default=None, max_length=64)
    is_active: bool | None = Field(default=None, description="False=停用（保留历史数据）")
    roles: list[Literal["student", "teacher", "researcher", "admin"]] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=72, description="重置密码")
    research_leads: list[dict] | None = Field(default=None, description="roles 含 researcher 时同步")
    class_id: int | None = Field(default=None, description="student 就读班级 classes.id；传了即迁移班级")
    teaching: list[dict] | None = Field(
        default=None, description="teacher 授课关系 [{class_id, subject}]；传了整体替换，传 [] 清空"
    )


class CsvImportIn(BaseModel):
    """CSV 批量导入。不用 multipart 文件上传（省 python-multipart 依赖），
    前端读文件后把文本放进 JSON。格式约定见 GET /api/admin/users/import-template。"""

    csv_content: str = Field(min_length=1, description="CSV 全文（首行表头）")


class TeachingAssignmentIn(BaseModel):
    """一条授课关系：teacher ↔ 班级 ↔ 学科。这是 teacher 明细 scope 的数据源。"""

    teacher_id: int = Field(description="教师的 users.id")
    class_id: int = Field(description="班级 id")
    subject: str = Field(default="math", description="学科")


class ResearchLeadIn(BaseModel):
    """一条组长任命：user ↔ 年级 ↔ 学科。这是 researcher 聚合 scope 的数据源。"""

    user_id: int = Field(description="教研组长的 users.id")
    grade: str = Field(description="负责年级，如 初二")
    subject: str = Field(default="math", description="负责学科")


class ClassIn(BaseModel):
    """班级创建/修改。head_teacher_id 可空（班主任暂缺）。"""

    grade: str = Field(description="年级，如 初二")
    name: str = Field(description="班名，如 2班")
    head_teacher_id: int | None = Field(default=None, description="班主任 users.id，可空")
