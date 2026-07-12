"""后台定时抓取。用 APScheduler 的 BackgroundScheduler，
应用启动时开始，每隔 REFRESH_INTERVAL_MIN 分钟自动刷新所有订阅源。

间隔可用环境变量 BLOGREADER_INTERVAL_MIN 覆盖（默认 30 分钟）。
"""
import os
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from backend import feed_service

logger = logging.getLogger("blogreader.scheduler")

_scheduler: BackgroundScheduler | None = None


def _job():
    try:
        res = feed_service.refresh_all()
        logger.info("定时刷新完成：%s 个源，新增 %s 篇", res.get("feeds"), res.get("total_new"))
    except Exception:  # noqa: BLE001
        logger.exception("定时刷新失败")
    # 增量刷新后，给尚未抓完历史的源接力补一轮 backfill（每轮限速，分多次抓完）
    try:
        from backend import backfill_service
        backfill_service.backfill_all()
    except Exception:  # noqa: BLE001
        logger.exception("定时回溯失败")


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    interval = int(os.environ.get("BLOGREADER_INTERVAL_MIN", "30"))
    _scheduler = BackgroundScheduler(timezone="UTC")
    # 立即跑一次之后按间隔循环；misfire_grace_time 容忍调度延迟
    _scheduler.add_job(_job, "interval", minutes=interval, id="refresh_all",
                       max_instances=1, coalesce=True, misfire_grace_time=300)
    _scheduler.start()
    logger.info("定时抓取已启动，间隔 %s 分钟", interval)
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
