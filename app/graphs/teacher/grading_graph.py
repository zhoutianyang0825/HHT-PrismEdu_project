"""M3-T2 · 批改 Agent 图：客观题规则直判 + 主观题 LLM 建议 → interrupt 等老师确认。

 状态图拓扑（按架构文档 5.2）：

    START → fetch_answer 取作答+题目+标准答案
              │
        route_by_type（条件边，按题型分流）
   objective │            │ subjective
              ▼            ▼
      grade_objective   llm_grade_subjective
      规则直判，        LLM 结构化建议 → INTERRUPT 挂起，
      status=confirmed  Command(resume=老师决定) 恢复
              │            │
              │      route_by_decision（条件边）
              │   reject│      │ confirm/edit
              │         │      ▼
              │         │  apply_teacher_decision
              │         │  采纳/改分 → status=confirmed
              │         ▼  │
              └──► commit_grade ◄┘
                 写 grading_records + student_answers + audit_logs
                    │
              route_after_commit（条件边）
        rejected│   答错│      答对│
             END ▼       ▼          ▼
           （驳回不动     attribute_error  update_mastery
             BKT/分数）   LLM 错因归因 ←────┘
                         写 error_logs        │
                              └──► update_mastery
                                   BKT 三步更新 + 三处同步 → END

 本文件定位：批改链路唯一入口，对外暴露 build_grading_graph / make_thread_id
 工程边界：节点内经 get_session_factory 直接碰 DB 是 M2~M3 过渡写法，M4-T1 建 Tool 层后搬走
"""
import json

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from sqlalchemy import select
from typing_extensions import TypedDict

from app.core.llm import get_llm
from app.db.session import get_session_factory
from app.models import (
    ErrorLog,
    GradingRecord,
    HomeworkQuestion,
    Question,
    StudentAnswer,
)
from app.services.audit_service import record_audit
from app.services.bkt_repository import get_bkt_state, sync_mastery
from app.services.grading import grade_objective
from app.services.mastery import update_mastery

# 主观题"算对"的判定线：得分率达到 60% 视为答对（BKT 的 is_correct 输入）
# 为什么不用 is_correct=得分>0：主观题答对一半给 40% 分，算"会了一半"比"全错"合理
SUBJECTIVE_PASS_RATE = 0.6


# =====================================================================
# State 定义（架构文档 5.1 + M3 落地补字段）
# =====================================================================
class GradingState(TypedDict):
    # ---- 输入（invoke 时传入）----
    answer_id: int                # student_answers 主键，批改的唯一目标
    teacher_user_id: int | None   # 触发批改的老师（审计用）；系统自动批可空

    # ---- fetch_answer 装填 ----
    question_id: int
    student_id: int               # students 表主键（BKT 的 key，不是 users.id）
    kp_id: str                    # 本题考察知识点（BKT 的 key）
    question_type: str            # choice / fill / subjective
    question_content: str         # 题干原文（LLM 批改/归因的必要上下文）
    student_answer: str
    standard_answer: str
    reference_analysis: str       # 题目解析，喂给 LLM 做批改依据
    full_score: float             # 满分：作业题取 homework_questions.score，其他默认

    # ---- 判分中间产物 ----
    rule_result: dict             # 客观题规则判分结果 {"is_correct", "score", "method"}
    ai_score: float               # 主观题 LLM 建议分（已换算到 full_score 尺度）
    ai_comment: str               # 主观题 LLM 评语
    ai_scoring_basis: list[str]   # 采分点逐条对照
    ai_confidence: float          # LLM 评分置信度 0~1

    # ---- 老师决定（interrupt resume 回来后装填）----
    teacher_decision: dict        # {"action": confirm/edit/reject, "score"?, "comment"?}

    # ---- 最终产物 ----
    final_score: float | None
    final_comment: str | None
    status: str                   # pending → confirmed / rejected
    is_correct: bool | None       # BKT 的输入；主观题按 60% 得分率线判
    error_type: str | None        # 错因（答错时 attribute_error 节点装填）
    p_known_before: float         # BKT 更新前（CLI 展示对比用）
    p_known_after: float          # BKT 更新后


# =====================================================================
# 结构化输出 Schema（M4 搬 app/schemas/；先就近定义，少一层目录好 review）
# =====================================================================
class SubjectiveGrade(BaseModel):
    """主观题 LLM 批改建议的结构化输出格式。

    用途：with_structured_output 把本类字段 schema 塞给 LLM，逼它按格式吐 JSON 再自动解析
    字段：ai_score 百分制建议分(0~100) / ai_comment 评语 / scoring_basis 采分点对照 / confidence 置信度
    """

    ai_score: float = Field(ge=0, le=100, description="建议得分，按百分制打分")
    ai_comment: str = Field(description="评语，指出答对了哪几步、缺了哪一步")
    scoring_basis: list[str] = Field(
        description="采分点逐条对照，例如 ['正确列出判别式', '未讨论 Δ=0 情形']"
    )
    confidence: float = Field(ge=0, le=1, description="评分置信度")


class ErrorAttribution(BaseModel):
    """错因归因的结构化输出格式（S-05）。

    用途：LLM 四选一归因，error_type 必须与 error_logs.error_type 枚举严格对齐
    字段：error_type 错因 / confidence 置信度 / evidence 作答原文依据 / explanation_strategy 讲解策略
    """

    error_type: str = Field(
        description="四选一：概念不清 / 方法不会 / 粗心 / 审题失误"
    )
    confidence: float = Field(ge=0, le=1, description="归因置信度")
    evidence: str = Field(description="归因依据，必须引用学生作答的具体片段")
    explanation_strategy: str = Field(description="讲解策略，一句话")


# =====================================================================
# DB 过渡期助手（M4 搬 Tool 层，与 M2 nodes.py 同一套写法）
# =====================================================================
def _fetch_full_score(answer: StudentAnswer, question: Question) -> float:
    """取该题满分。

    得到的数据：answer 一条作答记录 / question 一道题目
    要做的操作：source_type=homework → 查 homework_questions 拿该题分值；其他来源按 100
    返回：满分分值（float），形如 4.0；作业题查不到分值时退回 100.0
    为什么：作业题分值由老师布置时定（可能 4 分），统计得分率必须用真分值，不能统一按 100
    """
    if answer.source_type == "homework":
        session = get_session_factory()()
        try:
            hq = session.execute(
                select(HomeworkQuestion)
                .where(HomeworkQuestion.homework_id == answer.source_id)
                .where(HomeworkQuestion.question_id == question.id)
            ).scalar_one_or_none()
            if hq:
                return hq.score
        finally:
            session.close()
    return 100.0


# =====================================================================
# 节点 1：取数
# =====================================================================
def fetch_answer(state: GradingState) -> dict:
    """取作答记录 + 题目 + 标准答案，一次性装满 State 的上下文字段。

    得到的数据：state["answer_id"]，student_answers 主键，类似 1001
    要做的操作：查 student_answers + questions 两张表，组装判分所需全部字段
    返回：增量 dict，形如 {"question_id": 88, "student_id": 42, "kp_id": "kp_panbie",
          "question_type": "subjective", "student_answer": "Δ=4-4=0，所以方程有根。", ...}
    为什么：后续节点都只读 state，取数集中在这里避免每个节点各查一遍库；
            answer_id 不存在直接 raise——静默吞掉只会产生假 pending 记录
    """
    session = get_session_factory()()
    try:
        answer = session.get(StudentAnswer, state["answer_id"])
        if answer is None:
            raise ValueError(f"student_answers 里没有 id={state['answer_id']} 的作答")
        question = session.get(Question, answer.question_id)
        if question is None:
            raise ValueError(f"questions 里没有 id={answer.question_id} 的题目")

        return {
            "question_id": question.id,
            "student_id": answer.student_id,
            "kp_id": question.kp_id,
            "question_type": question.question_type,
            "question_content": question.content,
            "student_answer": answer.content,
            "standard_answer": question.answer,
            "reference_analysis": question.analysis or "",
            "full_score": _fetch_full_score(answer, question),
        }
    finally:
        session.close()


# =====================================================================
# 条件边 1：按题型分流
# =====================================================================
def route_by_type(state: GradingState) -> str:
    """按题型分流：客观题走规则判分，主观题走 LLM 批改。

    得到的数据：state["question_type"]，choice / fill / subjective
    要做的操作：一次字符串判断
    返回：节点名，形如 "grade_objective" 或 "llm_grade_subjective"
    为什么：客观题答案确定，规则判分又快又能复现，没必要让 LLM 参与
    """
    return "grade_objective" if state["question_type"] in ("choice", "fill") \
        else "llm_grade_subjective"


# =====================================================================
# 节点 2a：客观题规则直判（不挂起，一步到位）
# =====================================================================
def grade_objective_node(state: GradingState) -> dict:
    """客观题：调 grading.grade_objective 判分，直接 confirmed。

    得到的数据：state 里 fetch_answer 装好的题型/作答/标准答案/满分
    要做的操作：纯规则判分 → is_correct/final_score/status 一次装填
    返回：增量 dict，形如 {"is_correct": True, "final_score": 4.0,
          "status": "confirmed", "teacher_decision": {"action": "auto"}}
    为什么：规则判分是确定性的，没有"建议"环节，不需要 interrupt 让老师确认
    """
    result = grade_objective(
        question_type=state["question_type"],
        student_answer=state["student_answer"],
        standard_answer=state["standard_answer"],
        full_score=state["full_score"],
    )
    return {
        "rule_result": result,
        "is_correct": result["is_correct"],
        "final_score": result["score"],
        "final_comment": f"规则判分（{result['method']}）",
        "status": "confirmed",
        "teacher_decision": {"action": "auto"},   # 客观题没有老师介入
    }


# =====================================================================
# 节点 2b：主观题 LLM 建议 + 🔴 interrupt 挂起（本图的心脏）
# =====================================================================
def llm_grade_subjective(state: GradingState) -> dict:
    """主观题：LLM 结构化建议 → interrupt 挂起等老师 → resume 后装填老师决定。

    得到的数据：题干/标准答案/解析/学生作答/满分
    要做的操作：
      1. LLM 按百分制打分（Pydantic 强格式）→ 换算到本题满分尺度
      2. interrupt() 把建议抛给前端并挂起
      3. Command(resume=老师决定) 回来 → 装填 teacher_decision
    返回：增量 dict，形如
         {"ai_score": 50.0, "ai_comment": "...", "ai_scoring_basis": [...],
          "ai_confidence": 0.82, "teacher_decision": {"action": "edit", "score": 65.0}}
    为什么：主观题不能机器拍板，LLM 只出"建议"，最终分数必须过老师这道闸

    resume 值的约定格式（前端/CLI 必须按这个传）：
      {"action": "confirm", "comment": "可选，覆盖评语"}
      {"action": "edit",    "score": 8.5, "comment": "可选"}
      {"action": "reject"}
    """
    # ---- 1. LLM 结构化批改 ----
    system = (
        "你是严谨的初中数学阅卷老师。按标准答案和解析给学生作答打分：\n"
        "1. 按采分点给分，部分正确给部分分，不允许一律 0 分或满分；\n"
        "2. ai_comment 必须指出答对了哪几步、缺了哪一步；\n"
        "3. scoring_basis 逐条对照采分点（对的标注'已达成'，缺的标注'缺失'）。"
    )
    human = (
        f"题目满分：{state['full_score']} 分（请按百分制打分，代码会换算）\n"
        f"题干：{state['question_content']}\n"
        f"标准答案：{state['standard_answer']}\n"
        f"参考解析：{state['reference_analysis'] or '无'}\n"
        f"学生作答：{state['student_answer']}"
    )
    try:
        grade: SubjectiveGrade = get_llm(temperature=0).with_structured_output(
            SubjectiveGrade
        ).invoke([("system", system), ("human", human)])
        # 百分制 → 本题满分尺度：LLM 按 100 打分好约束（ge=0 le=100），
        # 换算后才能直接和 full_score 比、写进 student_answers.score
        ai_score = round(grade.ai_score / 100.0 * state["full_score"], 1)
    except Exception:
        # LLM 挂了不能堵死批改链路：给保守建议（0 分 + 说明），老师手动改
        ai_score = 0.0
        grade = SubjectiveGrade(
            ai_score=0,
            ai_comment="（LLM 批改服务暂不可用，请老师人工批改）",
            scoring_basis=["LLM 不可用，无法生成采分点对照"],
            confidence=0.0,
        )

    # ---- 2. interrupt：图在此挂起，payload 给前端渲染确认框 ----
    # 陌生点 interrupt 三铁律：
    #   ① 必须是节点函数体内的直接调用（包进子函数/try 会失效）
    #   ② 挂起状态靠 compile 时绑定的 checkpointer 落库，否则无法 resume
    #   ③ 恢复只能靠 Command(resume=...)，其值 = interrupt() 的返回值
    teacher_decision = interrupt({
        "type": "ai_suggest",                       # M5 SSE 约定：按 type 弹老师确认框
        "answer_id": state["answer_id"],
        "full_score": state["full_score"],
        "ai_score": ai_score,
        "ai_comment": grade.ai_comment,
        "scoring_basis": grade.scoring_basis,
        "confidence": grade.confidence,
    })

    # ---- 3. resume 回来从这里继续：校验并装填老师决定 ----
    action = teacher_decision.get("action")
    if action not in ("confirm", "edit", "reject"):
        # 前端传错格式：按 reject 处理最安全（不产生任何分数）
        teacher_decision = {"action": "reject"}

    return {
        "ai_score": ai_score,
        "ai_comment": grade.ai_comment,
        "ai_scoring_basis": grade.scoring_basis,
        "ai_confidence": grade.confidence,
        "teacher_decision": teacher_decision,
    }


# =====================================================================
# 条件边 2：老师决定分流
# =====================================================================
def route_by_decision(state: GradingState) -> str:
    """按老师决定分流。

    得到的数据：state["teacher_decision"]["action"]，confirm / edit / reject
    要做的操作：一次判断
    返回：节点名，形如 "apply_teacher_decision" 或 "commit_grade"
    为什么：驳回的分数不生效，不需要走"落实最终分"，直接去 commit 记录驳回即可
    """
    return "apply_teacher_decision" if state["teacher_decision"]["action"] != "reject" \
        else "commit_grade"


# =====================================================================
# 节点 3：落实老师决定
# =====================================================================
def apply_teacher_decision(state: GradingState) -> dict:
    """把老师的 confirm/edit 翻译成最终分数。

    得到的数据：ai_score（LLM 建议分）、teacher_decision（老师决定 dict）
    要做的操作：confirm 采纳 AI 建议分；edit 用老师改的分；评语同理可覆盖
    返回：增量 dict，形如 {"final_score": 65.0, "status": "confirmed", "is_correct": True}
    为什么：is_correct 按 60% 得分率判——主观题拿 40% 分说明"会了一半"，
            比"全错"更接近真实掌握水平，BKT 的输入要反映这个中间态
    """
    action = state["teacher_decision"]["action"]
    if action == "edit":
        final_score = float(state["teacher_decision"]["score"])
        final_comment = state["teacher_decision"].get("comment") or state["ai_comment"]
    else:  # confirm
        final_score = state["ai_score"]
        final_comment = state["teacher_decision"].get("comment") or state["ai_comment"]

    return {
        "final_score": final_score,
        "final_comment": final_comment,
        "status": "confirmed",
        "is_correct": final_score >= state["full_score"] * SUBJECTIVE_PASS_RATE,
    }


# =====================================================================
# 节点 4：落库 + 审计（三条记录，一个事务）
# =====================================================================
def commit_grade(state: GradingState) -> dict:
    """写 grading_records + 更新 student_answers + 审计埋点，同一事务 commit。

    得到的数据：state 里判分/老师决定的全部最终值
    要做的操作：
      1. grading_records 插一行：grader_type 按来源标 rule/llm/teacher
      2. student_answers 回填 is_correct/score（客观题直接生效，主观题经老师确认）
      3. audit_logs 埋点（M3-T5）：批改确认/驳回是敏感操作，必须留痕
    返回：{"status": "confirmed", "final_score": 65.0}；重复批改时返回已有结果
    为什么：批改是"一次定音"的动作，三张表必须同事务落库，且不能重复写
    """
    session = get_session_factory()()
    try:
        # 防重复批改（幂等保护）：同一 answer_id 已有 confirmed 记录就直接返回，
        #   否则老师手抖点两次会写两条批改记录 + 两次 BKT 更新（掌握度被"刷"两遍）
        already = session.execute(
            select(GradingRecord)
            .where(GradingRecord.answer_id == state["answer_id"])
            .where(GradingRecord.status == "confirmed")
        ).scalar_one_or_none()
        if already is not None:
            return {"status": already.status, "final_score": already.final_score}

        # ---- 1. grader_type 定型：谁拍的板就记谁 ----
        action = state["teacher_decision"].get("action", "auto")
        if state["question_type"] in ("choice", "fill"):
            grader_type = "rule"                    # 规则直判
        elif action == "edit":
            grader_type = "teacher"                 # 老师改过分
        else:
            grader_type = "llm"                     # 老师确认采纳了 AI 建议

        # ---- 2. 批改记录 ----
        record = GradingRecord(
            answer_id=state["answer_id"],
            grader_type=grader_type,
            ai_suggested_score=state.get("ai_score"),
            ai_comment=state.get("ai_comment"),
            final_score=state.get("final_score"),
            final_comment=state.get("final_comment"),
            status=state["status"],                 # confirmed / rejected
        )
        session.add(record)

        # ---- 3. 作答回填：rejected 不回填分数（is_correct 留 NULL 待重批）----
        answer = session.get(StudentAnswer, state["answer_id"])
        if state["status"] == "confirmed":
            answer.is_correct = state["is_correct"]
            answer.score = state["final_score"]

        # ---- 4. 审计埋点（M3-T5）：确认/驳回都是敏感操作 ----
        record_audit(
            db=session,
            user_id=state.get("teacher_user_id"),
            action="grading.confirm" if state["status"] == "confirmed" else "grading.reject",
            target=f"student_answers:{state['answer_id']}",
            detail=json.dumps({
                "grader_type": grader_type,
                "action": action,
                "final_score": state.get("final_score"),
                "status": state["status"],
            }, ensure_ascii=False),
        )

        session.commit()   # 三张表一个事务：批改记录/作答回填/审计同生共死
    finally:
        session.close()

    return {"status": state["status"], "final_score": state.get("final_score")}


# =====================================================================
# 条件边 3：commit 之后去哪
# =====================================================================
def route_after_commit(state: GradingState) -> str:
    """commit 之后的去向。

    得到的数据：state["status"]、state["is_correct"]
    要做的操作：三段判断
    返回：节点名或 END，形如 "attribute_error" / "update_mastery" / END
    为什么：驳回不动分数所以也不动掌握度；答错要先归因，错因决定本次 slip 的惩罚轻重
    """
    if state["status"] == "rejected":
        return END
    if state["is_correct"] is False:
        return "attribute_error"     # 答错要错因联动 BKT（slip 替换）
    return "update_mastery"


# =====================================================================
# 节点 5a：错因归因（答错才走）
# =====================================================================
def attribute_error(state: GradingState) -> dict:
    """LLM 结构化归因 → 写 error_logs → error_type 交给 BKT 替换 slip。

    得到的数据：题干 / 学生作答 / 标准答案
    要做的操作：LLM 四选一归因 → evidence 必须引用作答原文片段 → 落库 error_logs
    返回：增量 dict，形如 {"error_type": "粗心"}
    为什么：错因不直接改掌握度，只替换本次 p_slip——粗心(0.45) 掉得轻、概念不清(0.05) 掉得重；
            LLM 挂了退默认"方法不会"，主链路继续
    """
    system = (
        "你是初中数学错因诊断专家。判断学生答错的根因，四选一：\n"
        "概念不清=对知识点本身理解错误；方法不会=不知道解题方法；\n"
        "粗心=方法对但计算/抄写出错；审题失误=看错题目要求。\n"
        "evidence 必须引用学生作答里的具体片段作依据。"
    )
    human = (
        f"题干：{state['question_content'][:200]}\n"
        f"学生作答：{state['student_answer']}\n"
        f"标准答案：{state['standard_answer']}"
    )
    try:
        result: ErrorAttribution = get_llm(temperature=0).with_structured_output(
            ErrorAttribution
        ).invoke([("system", system), ("human", human)])
        error_type = result.error_type if result.error_type in ("概念不清", "方法不会", "粗心", "审题失误") \
            else "方法不会"
    except Exception:
        # fail-soft：归因失败退默认错因，主链路继续
        result = ErrorAttribution(
            error_type="方法不会", confidence=0.0,
            evidence="（归因服务暂不可用）", explanation_strategy="按默认策略讲解",
        )
        error_type = "方法不会"

    # 错因落库（error_logs 表）：S-10 诊断测验和 T-03 班级错题汇总都从这里取数
    session = get_session_factory()()
    try:
        session.add(ErrorLog(
            answer_id=state["answer_id"],
            error_type=error_type,
            confidence=result.confidence,
            evidence=result.evidence,
            explanation_strategy=result.explanation_strategy,
        ))
        session.commit()
    finally:
        session.close()

    return {"error_type": error_type}


# =====================================================================
# 节点 5b：BKT 更新（终点站）
# =====================================================================
def update_mastery_node(state: GradingState) -> dict:
    """读旧掌握度 → BKT 三步更新 → 三处同步落库。

    得到的数据：student_id / kp_id / is_correct / error_type
    要做的操作：
      1. get_bkt_state 读 (p_known, days_since_last)
      2. update_mastery 三步更新（错因替换 slip 在它内部）
      3. sync_mastery 三处同步（bkt_state + 快照 + Neo4j）
    返回：增量 dict，形如 {"p_known_before": 0.10, "p_known_after": 0.568}
    为什么：批改的最终价值是"更新学生状态"，BKT 是全系统唯一的掌握度写入口；
            rejected 不会走到这里（route_after_commit 已拦）
    """
    p_before, days = get_bkt_state(state["student_id"], state["kp_id"])
    p_after = update_mastery(
        p_known=p_before,
        is_correct=state["is_correct"],       # 已确认 False 才进这分支，True 直达
        error_type=state.get("error_type"),   # 答对时为 None
        days_since_last=days,
    )
    sync_mastery(state["student_id"], state["kp_id"], p_after)

    return {"p_known_before": p_before, "p_known_after": p_after}


# =====================================================================
# 组装 + 编译出口
# =====================================================================
def build_grading_graph(checkpointer: BaseCheckpointSaver):
    """组装并编译批改图。

    得到的数据：checkpointer 短期记忆对象，必填
                正式服务 → get_checkpointer()（PostgresSaver，重启后仍能 resume）
                单测/演示 → get_memory_checkpointer() 或 MemorySaver()（内存，进程退出即丢）
                传 None 直接 ValueError——避免拖到主观题 interrupt 时才炸
    要做的操作：登记 7 个节点 → 连 8 条边（3 条条件边）→ compile 冻结并绑定 checkpointer
    返回：CompiledStateGraph，可直接 invoke/stream，调用形如
          graph.invoke({"answer_id": 1001}, {"configurable": {"thread_id": "grading_1001"}})
    为什么：interrupt 挂起必须靠 checkpointer 落库；装配与业务解耦后，演示脚本和 M5 API 各自 build 一份
    """
    if checkpointer is None:
        raise ValueError(
            "checkpointer 不能为 None：主观题 interrupt 挂起要靠它落库才能 resume。\n"
            "  正式服务 → app.db.checkpointer.get_checkpointer()（PostgresSaver）\n"
            "  单元测试 → app.db.checkpointer.get_memory_checkpointer()（内存）"
        )

    builder = StateGraph(GradingState)
    builder.add_node("fetch_answer", fetch_answer)
    builder.add_node("grade_objective", grade_objective_node)
    builder.add_node("llm_grade_subjective", llm_grade_subjective)
    builder.add_node("apply_teacher_decision", apply_teacher_decision)
    builder.add_node("commit_grade", commit_grade)
    builder.add_node("attribute_error", attribute_error)
    builder.add_node("update_mastery", update_mastery_node)

    builder.add_edge(START, "fetch_answer")
    builder.add_conditional_edges("fetch_answer", route_by_type, {
        "grade_objective": "grade_objective",
        "llm_grade_subjective": "llm_grade_subjective",
    })
    # 客观题：直判 → commit（不经过老师）
    builder.add_edge("grade_objective", "commit_grade")

    # 主观题：LLM+interrupt → 按老师决定分流
    builder.add_conditional_edges("llm_grade_subjective", route_by_decision, {
        "apply_teacher_decision": "apply_teacher_decision",
        "commit_grade": "commit_grade",
    })

    builder.add_edge("apply_teacher_decision", "commit_grade")

    # commit 后：
    # rejected 直接走END结束；
    # 答错 先 attribute_error（错因归因），再计算 BKT-> update_mastery；
    # 答对直接计算 BKT -> update_mastery
    builder.add_conditional_edges("commit_grade", route_after_commit, {
        "attribute_error": "attribute_error",
        "update_mastery": "update_mastery",
        END: END,
    })
    builder.add_edge("attribute_error", "update_mastery")
    builder.add_edge("update_mastery", END)

    return builder.compile(checkpointer=checkpointer)


def make_thread_id(answer_id: int) -> str:
    """
    thread_id 约定：grading_{answer_id}
    
    一份作答一个批改会话，天然幂等。
    """
    return f"grading_{answer_id}"
