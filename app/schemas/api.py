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

    student_id   students 表主键（学生档案 id，不是 users.id）；
    question_id  当前在做题的 id —— thread_id 用它拼 {student_id}_{question_id}；
    message      学生这句话原文，如 "这题答案是多少"；
    homework_id  学生是从哪份作业点进来的（自由练习时为 None）——
                 判分落库时决定 source_type：有值 → homework（老师端按它查全班作答）；
                 None → chat（只给诊断测验供错题数据）。
    """

    student_id: int = Field(description="学生档案 id（students.id）")
    question_id: int = Field(description="题目 id")
    message: str = Field(min_length=1, max_length=2000, description="学生输入")
    homework_id: int | None = Field(default=None, description="作业 id；自由练习不传")


class StudentResumeIn(BaseModel):
    """恢复被 interrupt 挂起的会话（学生引导场景）。

    字段与 StudentChatIn 相同：thread_id 由 student_id+question_id 拼出，
    message 会包进 Command(resume=message) 喂给挂起的图。
    homework_id 也带着：挂起状态下 resume 不会走新 invoke，
    若图里记的作业 id 与本次不一致，接口层用 update_state 打补丁补上。
    """

    student_id: int = Field(description="学生档案 id")
    question_id: int = Field(description="题目 id")
    message: str = Field(min_length=1, max_length=2000, description="学生对上一次提示的回应")
    homework_id: int | None = Field(default=None, description="作业 id；自由练习不传")


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


class ExamAnswerItem(BaseModel):
    """考试里单题的作答。

    与 QuizAnswerItem 的区别只有 attachments：
        选择题 content="A"；填空题 content="x=1或x=3"；
        解答题可能只交了手写图 → content 允许为空串，attachments 给图片地址。
    """

    question_id: int = Field(description="题目 id")
    content: str = Field(default="", max_length=8000, description="学生作答原文（可为空，见 attachments）")
    attachments: list[str] = Field(default_factory=list, max_length=5,
                                  description="手写解题图片地址，最多 5 张")


class ExamSubmitIn(BaseModel):
    """交卷：一次提交整份试卷。

    为什么一次性提交而不是逐题实时入库：
        考试是一次性行为，逐题入库会让"交卷"失去意义（学生边做老师边看，
        还会被当成已交卷）；也要防重复提交盖掉记录。
    """

    answers: list[ExamAnswerItem] = Field(min_length=1, description="逐题作答")


class ImageUploadIn(BaseModel):
    """手写解题图上传（base64 data URL 形式，不用 multipart）。

    为什么不用文件上传：
        前端 core.js 的 http 只发 JSON，引入 FormData 要再写一套请求封装；
        base64 走同一条通路、同一套鉴权与报错处理，代价是体积大 1/3——
        前端已用 canvas 压到 1280px/JPEG，一张 200~400KB，可以接受。
    """

    filename: str = Field(default="answer.png", max_length=128, description="原始文件名（只用于取扩展名）")
    data: str = Field(min_length=32, description="data:image/png;base64,... 或裸 base64")
    purpose: str = Field(default="answer", max_length=32, description="用途，决定存放子目录")


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

    为什么 reject 还留着：前端已只暴露「采纳 / 改分」两条路——
        老师认为 AI 判错时，走「自主批改」直接给分更符合直觉（还能部分给分），
        reject 只是"这题不算分"，批完这题仍是待批状态，容易让老师以为批过了。
        保留这个值是为了兼容已落库的 rejected 记录与脚本调用，前端不再发它。
    """

    action: Literal["confirm", "edit", "reject"] = Field(description="裁决动作")
    score: float | None = Field(default=None, ge=0, description="action=edit 时必填")
    comment: str | None = Field(default=None, max_length=500, description="可选，覆盖 AI 评语")


class ManualGradeIn(BaseModel):
    """老师完全自主批改（不经过 AI 建议）。

    为什么需要它：
        原链路只有"AI 批改 → 老师采纳/改分"，老师想自己判一道题
        （尤其手写图题、AI 明显判错的题）没有入口，只能先跑 AI 再改分，
        多两次点击还容易把 AI 的错误建议带进成绩。

    judgement 三选一：
        correct → 直接给满分（题目分值）
        wrong   → 给 0 分
        partial → 用 score 字段的分（必须 0 < score < 满分）
    """

    judgement: Literal["correct", "wrong", "partial"] = Field(description="正确/错误/部分正确")
    score: float | None = Field(default=None, ge=0,
                                description="judgement=partial 时必填，0 < score < 满分")
    comment: str | None = Field(default=None, max_length=500, description="可选，给学生的评语")


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
