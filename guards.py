"""
The checks that run before the engine is allowed to spend or move money.

Everything here answers one question: "is this action allowed right now?" and
returns a reason in plain language when the answer is no, because that reason is
shown to the customer and written to the action log.

None of this existed. The engine had a per-entity budget ceiling and a 24 hour
cooldown that only the rules engine consulted, and that was the whole of it.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from db import get_db

logger = logging.getLogger(__name__)


class Blocked(Exception):
    """An action a guard refused. The message is customer-readable."""


# ─── Account-wide stop ───────────────────────────────────────────────────────

def killed() -> Optional[str]:
    """
    Is the global kill switch down.

    The only off levers before this were a per-project `enabled` flag and
    removing an environment variable from Railway, neither of which is something
    you can reach for quickly when an account is spending wrongly.

    Fails CLOSED: if the control row cannot be read, the engine stops. An engine
    that keeps spending because it could not check whether it was allowed to is
    the exact failure this guard exists to prevent.
    """
    try:
        resp = get_db().table("autopilot_control").select("killed_at, reason").limit(1).execute()
    except Exception as e:
        logger.error(f"[guards] Could not read kill switch, refusing to run: {e}")
        return "Could not confirm the autopilot is allowed to run."

    rows = resp.data or []
    if not rows:
        return None
    row = rows[0]
    if row.get("killed_at"):
        return row.get("reason") or "The autopilot is switched off account-wide."
    return None


def project_paused(settings: dict) -> Optional[str]:
    """Per-project stop, independent of the `enabled` flag."""
    if settings.get("paused_at"):
        return "This account's autopilot is paused."
    return None


# ─── Learning phase ──────────────────────────────────────────────────────────

def learning_phase_block(entity: dict, min_results: int) -> Optional[str]:
    """
    Refuse a budget change to an entity that has not produced a reliable signal.

    Meta re-enters the learning phase whenever a budget is edited, and during
    learning delivery is worse and cost per result is higher. Editing on thin
    data therefore costs twice: the decision is likely wrong, and making it
    restarts learning. Meta's own published benchmark is around 50 optimization
    events in a rolling week, which is what min_results defaults to.

    Reads `results`, so it is goal-correct: enquiries for a lead account,
    purchases for a store. Counting only purchases here would block every budget
    change a service business ever needed.
    """
    results = float(entity.get("results") or 0)
    if results < min_results:
        return (
            f"Not enough data yet. This has brought in {int(results)} so far and "
            f"we wait for {min_results} before changing the budget, because "
            f"changing it early makes Facebook re-learn who to show it to."
        )
    return None


# ─── Cooldown ────────────────────────────────────────────────────────────────

def _hours_since(iso_ts: Optional[str]) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def cooldown_block(
    project_id: str,
    entity_id: str,
    direction: str,
    cooldown_hours: int,
) -> Optional[str]:
    """
    Refuse a budget change that comes too soon after the last one.

    ⚠️ Direction matters and used to be ignored. A cooldown exists to protect the
    learning phase from repeated increases; a DECREASE can only reduce spend and
    holding one back for three days keeps a customer paying for an ad already
    identified as losing money. So decreases are never blocked.

    The daily loop had no cooldown at all, only the 6-hourly rules engine did,
    which meant the autopilot could raise the same ad set's budget every single
    morning and keep it permanently in learning.
    """
    if direction != "increase":
        return None

    try:
        resp = (
            get_db().table("budget_changes")
            .select("changed_at")
            .eq("project_id", project_id)
            .eq("adset_id", entity_id)
            # ⚠️ Increases only. record_budget_change stamps a direction, and
            # without this filter a budget DECREASE two hours ago blocked an
            # increase for 72 hours with the sentence "We raised this budget 2
            # hours ago", which the customer reads in their history and which
            # did not happen.
            .eq("direction", "increase")
            .order("changed_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning(f"[guards] Cooldown lookup failed for {entity_id}: {e}")
        return None

    last = (resp.data or [{}])[0].get("changed_at") if resp.data else None
    hours = _hours_since(last)
    if hours is not None and hours < cooldown_hours:
        wait = cooldown_hours - hours
        return (
            f"We raised this budget {hours:.0f} hours ago. We leave at least "
            f"{cooldown_hours} hours between increases so Facebook can settle, "
            f"so this one waits about {wait:.0f} more hours."
        )
    return None


def record_budget_change(project_id: str, entity_id: str, entity_level: str, direction: str) -> None:
    """Stamp a budget change so the next cooldown check can see it."""
    try:
        get_db().table("budget_changes").insert({
            "project_id": project_id,
            "adset_id": entity_id,
            "entity_level": entity_level,
            "direction": direction,
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"[guards] Could not record budget change for {entity_id}: {e}")


# ─── Portfolio spend ceiling ─────────────────────────────────────────────────

def account_cap_block(
    entities: list[dict],
    entity_id: str,
    current: float,
    proposed: float,
    max_account_daily: float,
) -> Optional[str]:
    """
    Refuse an increase that would take the WHOLE account over its daily ceiling.

    ⚠️ max_daily_budget_usd is a ceiling per entity. Ten ad sets each sitting at
    a $500 entity cap is $5,000 a day, while the customer believes they set a
    $500 limit. This is the number they actually meant.

    `entities` is the budget-holding set, so summing them is the account's daily
    committed spend. The entity being changed is swapped for its proposed value
    rather than added, or its current budget would be counted twice.
    """
    if proposed <= current:
        return None

    total_others = sum(
        float(e.get("daily_budget") or 0)
        for e in entities
        if e.get("id") != entity_id
    )
    new_total = total_others + proposed

    if new_total > max_account_daily:
        headroom = max(0.0, max_account_daily - total_others)
        return (
            f"This would take your total daily spend to ${new_total:,.0f}, and "
            f"your limit is ${max_account_daily:,.0f} a day. The most this one "
            f"can go to is ${headroom:,.0f}."
        )
    return None
