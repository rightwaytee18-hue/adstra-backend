"""
Writes what the portal reads.

The Reveal portal must never call the Meta API. Until App Review grants Advanced
Access the app sits on the Dev tier, where ads_insights is capped at 600 calls
per hour per ad account against 190,000 on Standard, so a customer refreshing
their dashboard a few times could exhaust the quota the next optimization run
needs to do its job. So the engine writes a daily snapshot once per run and the
portal reads only that table.

Money is stored in CENTS. Meta returns spend as a decimal string of major units
("12.34"), and carrying that through as a float means every downstream sum
inherits binary rounding error. Converting once, here, at the boundary, is the
whole reason this module exists rather than the portal reading raw insight rows.
"""

import logging
from datetime import datetime, timezone

from db import get_db

logger = logging.getLogger(__name__)


def _cents(v) -> int:
    """Major units to cents, without float rounding drift."""
    try:
        return int(round(float(v or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _row(project_id: str, day: str, level: str, e: dict) -> dict:
    return {
        "project_id": project_id,
        "day": day,
        "entity_level": level,
        "entity_id": e.get("id") or "",
        "entity_name": e.get("name"),
        "parent_id": e.get("adset_id") or e.get("campaign_id"),
        "status": e.get("status"),
        "spend_cents": _cents(e.get("spend")),
        "impressions": int(e.get("impressions") or 0),
        "clicks": int(e.get("clicks") or 0),
        "conversions": float(e.get("results") or 0),
        "revenue_cents": _cents(e.get("revenue")),
        "thumbnail_url": e.get("thumbnail_url"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def write_daily_snapshot(
    project_id: str,
    ads: list[dict],
    adsets: list[dict],
    campaigns: list[dict],
) -> int:
    """
    Upsert one row per entity PER DAY, plus one account-level total per day.

    Callers MUST pass rows from MetaClient.get_daily_insights, not get_insights.
    Every row carries its own `day`, read from Meta's `date_start`.

    This function used to take the aggregate reader's output and stamp every row
    with today's date. Each of those rows covered the whole trailing window, so a
    7-day window wrote 7 days of spend onto one calendar day, and the portal --
    which sums 30 of these rows -- reported roughly 7x the money the customer was
    actually charged. Rows arriving without a `day` are dropped and logged rather
    than dated from the clock, so that failure can never come back silently.

    The account row is written explicitly rather than left for the portal to
    compute. Summing ad, adset and campaign rows together counts the same dollar
    three times, so a portal that added up everything it found would show a
    customer three times the spend they were actually charged. With an account
    row present the portal reads that and nothing else for its totals.

    The account total is summed from the AD level, not from campaigns. A campaign
    with no ads still reports its own spend, but an ad whose campaign row was
    truncated by the page cap would be missing entirely from a campaign-level
    sum. Ads are the finest grain and do not overlap each other.

    Upserted on (project_id, day, entity_level, entity_id) so re-running on the
    same day corrects the numbers rather than doubling them. That matters: the
    daily job can legitimately run twice after a Railway restart, and it now
    rewrites the whole window each run, which also repairs days a previous run
    missed entirely.
    """
    rows: list[dict] = []
    undated = 0

    for level, entities in (("ad", ads), ("adset", adsets), ("campaign", campaigns)):
        for e in entities or []:
            if not e.get("id"):
                continue
            day = e.get("day")
            if not day:
                undated += 1
                continue
            rows.append(_row(project_id, day, level, e))

    if undated:
        logger.error(
            f"[snapshot] {project_id}: dropped {undated} rows with no day. "
            "Caller must use get_daily_insights, not get_insights."
        )

    # One account row per day the ads actually delivered on.
    by_day: dict[str, list[dict]] = {}
    for a in ads or []:
        if a.get("id") and a.get("day"):
            by_day.setdefault(a["day"], []).append(a)

    for day, day_ads in by_day.items():
        rows.append(_row(project_id, day, "account", {
            "id": "account",
            "name": None,
            "status": None,
            "spend": sum(float(a.get("spend") or 0) for a in day_ads),
            "impressions": sum(int(a.get("impressions") or 0) for a in day_ads),
            "clicks": sum(int(a.get("clicks") or 0) for a in day_ads),
            "results": sum(float(a.get("results") or 0) for a in day_ads),
            "revenue": sum(float(a.get("revenue") or 0) for a in day_ads),
        }))

    if not rows:
        return 0

    try:
        get_db().table("ad_insights_daily").upsert(
            rows, on_conflict="project_id,day,entity_level,entity_id"
        ).execute()
        logger.info(
            f"[snapshot] {project_id}: wrote {len(rows)} rows across {len(by_day)} days"
        )
        return len(rows)
    except Exception as e:
        # Non-fatal to the optimization run. The customer's dashboard going
        # stale for a day is bad; refusing to pause a money-losing ad because
        # the reporting write failed is worse.
        logger.error(f"[snapshot] {project_id}: snapshot write failed: {e}")
        return 0
