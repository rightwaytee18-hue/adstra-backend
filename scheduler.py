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


def _run_autopilot_job():
    """Daily full autopilot — bootstrap new projects, optimize existing ones."""
    try:
        from full_autopilot_engine import run_full_daily_all_projects
        results = run_full_daily_all_projects()
        paused = sum(r.get("ads_paused", 0) for r in results.values() if isinstance(r, dict))
        created = sum(r.get("ads_created", 0) for r in results.values() if isinstance(r, dict))
        taken = sum(r.get("budget_actions_taken", 0) for r in results.values() if isinstance(r, dict))
        queued = sum(r.get("budget_actions_queued", 0) for r in results.values() if isinstance(r, dict))
        logger.info(
            f"Scheduled full autopilot run complete — {paused} ads paused, "
            f"{created} new ads created, {taken} budget actions taken, "
            f"{queued} queued for approval across {len(results)} projects"
        )
    except Exception as e:
        logger.error(f"Scheduled autopilot run error: {e}")


def _run_weekly_briefing_job():
    """Weekly briefing — Claude analyzes performance and writes strategy notes."""
    try:
        from autopilot_engine import briefing_all_projects
        results = briefing_all_projects()
        ok_count = sum(1 for r in results.values() if isinstance(r, dict) and r.get("ok"))
        logger.info(f"Weekly briefing complete — {ok_count} briefings generated for {len(results)} projects")
    except Exception as e:
        logger.error(f"Weekly briefing job error: {e}")


def start():
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")

    # Existing jobs
    _scheduler.add_job(_run_rules_job,      "interval", hours=6,  id="rules_job",      replace_existing=True)
    _scheduler.add_job(_run_competitor_job, "interval", days=3,   id="competitor_job", replace_existing=True)

    # Phase 8: Autopilot (daily at 6 AM UTC)
    _scheduler.add_job(_run_autopilot_job,       "cron", hour=6, minute=0, id="autopilot_job",  replace_existing=True)

    # Phase 8: Weekly briefing (every Monday at 8 AM UTC)
    _scheduler.add_job(_run_weekly_briefing_job, "cron", day_of_week="mon", hour=8, minute=0, id="briefing_job", replace_existing=True)

    _scheduler.start()
    logger.info(
        "Scheduler started — rules every 6h, competitor scrape every 3 days, "
        "autopilot daily at 06:00 UTC, briefings every Monday 08:00 UTC"
    )


def stop():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
