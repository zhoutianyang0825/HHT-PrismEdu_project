"""使用周报的定时调度器：APScheduler 内置方案（监控第一层的推送腿）。

 得到的数据：settings.report_email_* 六项配置 + cron 参数（周几/几点）
 要做的操作：
   start_report_scheduler()    —— lifespan 启动时调：SMTP 配齐才建 BackgroundScheduler
   shutdown_report_scheduler() —— lifespan 退出时调：优雅关掉调度线程
 返回：start 返回 scheduler 或 None；shutdown 无返回

【为什么选 BackgroundScheduler 而不是 FastAPI 自带的重复任务】
  FastAPI 没有内置 cron；asyncio 的"每秒循环检查"写法又土又不准。
  APScheduler 的 cron 触发器原生支持"每周一 8 点"，misfire/coalesce 还免费送
  断电补跑语义——3.x 的 BackgroundScheduler 在进程内跑一个后台线程，够用。

【多 worker 警告（诚实声明）】
  BackgroundScheduler 是**进程内**的：`--workers N` 时每个 worker 各起一个调度器，
  周报会重复发 N 份。本项目单 worker 部署（README 口径），不受影响；
  若将来真开多 worker，把 start_report_scheduler 挪到环境变量守卫的独立进程，
  或换 APScheduler 的数据库 jobstore 做互斥——那是升级时的事。

【为什么作业体自开自关 SessionLocal】
  定时任务没有请求上下文，FastAPI 的 get_db 依赖帮不上忙；
  自己 SessionLocal() → finally close，和脚本里的用法同款。
"""
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_scheduler = None  # 进程内单例，供 shutdown 用


def _smtp_configured() -> bool:
    """四项 SMTP 配置必须齐全才启动——缺任何一项都发不出去，不如不启动。"""
    return bool(settings.report_email_host and settings.report_email_user
                and settings.report_email_password and settings.report_email_to)


def _run_job() -> None:
    """cron 作业体：自开自关 db 会话，全流程容错（失败只留在日志里）。"""
    from app.db.session import SessionLocal
    from app.services.usage_report import run_weekly_report

    db = SessionLocal()
    try:
        result = run_weekly_report(db, days=7)
        logger.info("使用周报任务：%s", result)
    finally:
        db.close()


def start_report_scheduler():
    """lifespan 启动时调用：SMTP 配齐才建调度器，否则直接跳过。

    得到的数据：无（读 settings）
    要做的操作：建 BackgroundScheduler（Asia/Shanghai 时区）+ 注册 cron 作业
    返回：scheduler 实例，未启动返回 None
    """
    global _scheduler

    if not _smtp_configured():
        logger.info("SMTP 未配置，使用周报定时任务未启动（要开启请填 .env 的 REPORT_EMAIL_*）")
        return None
    if _scheduler is not None:
        return _scheduler            # 防御：lifespan 重复调用时不再叠加作业

    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        _run_job,
        "cron",
        day_of_week=settings.report_day_of_week,
        hour=settings.report_hour,
        minute=0,
        id="weekly_usage_report",
        # 断电/重启错过触发点：1 小时内启动起来就补发一次，且多次错过只补一次
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    logger.info("使用周报定时任务已启动：每周%s %02d:00（Asia/Shanghai）",
                settings.report_day_of_week, settings.report_hour)
    return sched


def shutdown_report_scheduler() -> None:
    """lifespan 退出时调用：关后台线程，不等在跑的任务（wait=False 快速退）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("使用周报定时任务已关闭")
