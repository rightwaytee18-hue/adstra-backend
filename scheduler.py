import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _run_rules_job():
    try:
        from rules_engine import run_all_projects
        results = run_all_projects()
        total = sum(len(v) for v in results.values())
        logger.info(f"Scheduled rules run complete — {total} actions across {len(results)} projects")
    except Exception as e:
        logger.error(f"Scheduled rules run error: {e}")


def _run_competitor_job():
    try:
        from competitor_engine import run_all_projects
        results = run_all_projects()
        total = sum(r.get('scraped', 0) for r in results.values() if isinstance(r, dict))
        logger.info(f"Scheduled competitor scrape complete — {total} new ads across {len(results)} projects")
    except Exception as e:
        logger.error(f"Scheduled competitor scrape error: {e}")


def start():
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(_run_rules_job, "interval", hours=6, id="rules_job", replace_existing=True)
    _scheduler.add_job(_run_competitor_job, "interval", days=3, id="competitor_job", replace_existing=True)
    _scheduler.start()
    logger.info("Scheduler started — rules every 6h, competitor scrape every 3 days")


def stop():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
