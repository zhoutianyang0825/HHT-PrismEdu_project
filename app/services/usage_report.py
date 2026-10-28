"""使用周报服务：监控第一层的定时聚合 + SMTP 推送。

 得到的数据：db 会话、settings 里的 SMTP 配置与 cron 参数
 要做的操作：
   collect_usage_summary()  —— 三端使用聚合（admin 端点与本任务共用同一份逻辑）
   run_weekly_report()      —— APScheduler 每周触发：聚合 → 格式化 → SMTP 发送
 返回：collect 返回 dict；run 返回 None（结果只体现在邮件与日志里）

【为什么聚合逻辑抽成服务，而不是让定时任务直接调 admin 端点】
  ① 分层：service 不 import api（反向依赖会让两层缠死，测试也没法单跑）；
  ② 复用：端点是"人看"的 JSON，周报是"机器定时跑"的同一份数据，
     口径必须绝对一致——抽到一处，改统计口径只改一个文件。

【为什么用 smtplib 标准库而不是再引一个邮件框架】
  发一封纯文本周报 15 行代码，引框架（如 aiosmtplib）纯属杀鸡用牛刀。

【为什么容错要这么宽】
  周报是"锦上添花"，绝不是主链路：聚合失败 / SMTP 发不出去 / 库还没起来，
  都只打 warning 绝不上抛——调度器里抛异常只会污染 uvicorn 日志，别无他用。
"""
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    AuditLog,
    Class,
    ErrorLog,
    ExamAttempt,
    ExamPaper,
    GradingRecord,
    Homework,
    LoginLog,
    Student,
    StudentAnswer,
)

logger = logging.getLogger(__name__)


# =====================================================================
# 聚合：与 admin 的 usage/summary 端点共用同一份口径
# =====================================================================
def collect_usage_summary(db: Session, days: int = 7) -> dict:
    """三端使用情况聚合（口径详见 admin.py 端点的 docstring，此处不重复）。

    得到的数据：统计窗口天数
    要做的操作：对 login_logs / student_answers / error_logs / grading_records /
                homeworks / exam_papers / exam_attempts 各跑一条 group by
    返回：logins / students / teachers / research / by_class 五段 dict
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

    # ---- 2. 学生 ----
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
    graded_n = correct_n + wrong_n
    error_rows = db.execute(
        select(ErrorLog.error_type, func.count())
        .select_from(ErrorLog)
        .join(StudentAnswer, ErrorLog.answer_id == StudentAnswer.id)
        .where(StudentAnswer.submitted_at >= since)
        .group_by(ErrorLog.error_type)
    ).all()

    # ---- 3. 老师 ----
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

    # ---- 4. 教研 ----
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

    # ---- 5. 班级分布 ----
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


# =====================================================================
# 邮件：格式化 + SMTP 发送
# =====================================================================
def format_report_text(summary: dict) -> tuple[str, str]:
    """把聚合 dict 排成纯文本邮件（纯文本比 HTML 稳：任何客户端不变形、无图片拦截）。

    得到的数据：collect_usage_summary 的返回值
    要做的操作：按 登录/学生/老师/教研/班级 五段排成可扫读的行
    返回：(subject, body)
    """
    now = datetime.now(timezone.utc).astimezone()
    d = summary["days"]
    subject = f"[HHT监控] 三端使用周报（近 {d} 天）· {now:%m-%d %H:%M}"

    s, t, r = summary["students"], summary["teachers"], summary["research"]
    rate = f"{s['correct_rate'] * 100:.1f}%" if s["correct_rate"] is not None else "暂无判分数据"
    adopt = f"{t['ai_adopt_rate'] * 100:.1f}%" if t["ai_adopt_rate"] is not None else "暂无主观题批改"

    lines = [
        f"HHT棱镜课堂 · 使用周报（近 {d} 天）",
        f"统计起点：{summary['since'][:19]} UTC",
        "",
        f"【登录】总 {summary['logins']['total']} 次 · 成功登录 {summary['logins']['by_status'].get('success', 0)} · "
        f"活跃用户 {summary['logins']['unique_users']}",
        f"        失败分布：密码错 {summary['logins']['by_status'].get('bad_password', 0)} · "
        f"锁定 {summary['logins']['by_status'].get('locked', 0)} · 停用 {summary['logins']['by_status'].get('inactive', 0)}"
        "（locked 突增 = 正在被爆破）",
        "",
        f"【学生】活跃 {s['active']} 人 · 作答 {s['answers_total']} 次 · 正确率 {rate}",
        f"        来源分布：{s['by_source'] or '无作答'}",
        f"        错因分布：{s['errors_by_type'] or '无错题'}",
        "",
        f"【老师】活跃 {t['active']} 人 · 批改 {t['gradings_total']} 次 · AI 采纳率 {adopt} · 布置作业 {t['homeworks_created']} 次",
        f"        批改来源：{t['by_grader'] or '无批改'}",
        "",
        f"【教研】出卷 {r['papers_created']} · 推送 {r['papers_published']} · 考试参与 {r['exam_attempts']} 人次",
        "",
        "【班级作答量 Top10】",
    ]
    top = sorted(summary["by_class"], key=lambda x: x["answers"], reverse=True)[:10]
    if top:
        for c in top:
            lines.append(f"        {c['class']}: {c['answers']} 次 / 活跃 {c['active_students']} 人")
    else:
        lines.append("        （窗口内没有作答数据）")

    return subject, "\n".join(lines)


def send_report_email(subject: str, body: str) -> None:
    """SMTP 发送（465 走 SSL；授权码与 Kuma 通知里配的是同一套）。

    得到的数据：主题 + 正文
    要做的操作：SMTP_SSL 登录 → sendmail → 退出
    返回：None；发不出去只 warning 不上抛（周报失败不该惊动任何人）

    为什么密码用 settings.report_email_password：那是邮箱的 SMTP 授权码，
    不是邮箱登录密码——和 Kuma 通知里填的同一串。
    """
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(["HHT监控", settings.report_email_user])
    # 收件人支持逗号分隔多个
    to_list = [x.strip() for x in settings.report_email_to.split(",") if x.strip()]
    msg["To"] = ", ".join(to_list)

    if settings.report_email_port == 465:
        server = smtplib.SMTP_SSL(settings.report_email_host,
                                  settings.report_email_port, timeout=15)
    else:
        server = smtplib.SMTP(settings.report_email_host,
                              settings.report_email_port, timeout=15)
        server.starttls()
    try:
        server.login(settings.report_email_user, settings.report_email_password)
        server.sendmail(settings.report_email_user, to_list, msg.as_string())
        logger.info("使用周报已发送至 %s", to_list)
    finally:
        server.quit()


def run_weekly_report(db: Session, days: int = 7) -> str:
    """周报任务主体：聚合 → 格式化 → 发送。APScheduler 每周调一次。

    得到的数据：db 会话（由调度侧从 SessionLocal 开出，用完即关）
    要做的操作：三步串行；任何一步失败只打 warning，绝不上抛
    返回：结果说明字符串（"sent: ..." / "skipped: ..." / "failed: ..."），便于调度日志排查
    """
    try:
        summary = collect_usage_summary(db, days)
        subject, body = format_report_text(summary)
        send_report_email(subject, body)
        return f"sent: {subject}"
    except Exception as exc:                # noqa: BLE001 周报容错，见模块 docstring
        logger.warning("使用周报发送失败（不影响服务）：%s: %s",
                       type(exc).__name__, exc)
        return f"failed: {type(exc).__name__}: {exc}"
