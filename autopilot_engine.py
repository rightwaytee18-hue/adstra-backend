"""
Adstra Autopilot Engine — Phase 8

Weekly AI strategy briefings + execution of user-approved autopilot actions.
The daily optimization loop now lives in full_autopilot_engine.run_full_daily;
this module's public entry points are:

  briefing_for_project(project_id)    — weekly strategy briefing (scheduler + API)
  briefing_all_projects()             — scheduler fan-out for briefings
  execute_approved_action(action_id, project_id) — run a user-approved pending action
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic

from crypto import token_for
from db import get_db
from meta_client import MetaClient, MetaAPIError

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"

# Module-level client — created once, reused across all calls
_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _load_project(project_id: str) -> dict:
    db = get_db()
    resp = db.table("projects").select("*").eq("id", project_id).maybe_single().execute()
    if not resp or not resp.data:
        raise ValueError(f"Project {project_id} not found")
    return resp.data


def _load_settings(project_id: str) -> Optional[dict]:
    db = get_db()
    resp = db.table("autopilot_settings").select("*").eq("project_id", project_id).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def _create_notification(
    user_id: str,
    project_id: str,
    ntype: str,
    title: str,
    body: str,
    action_url: Optional[str] = None,
) -> None:
    """Insert a notification row. Best-effort — never raises."""
    try:
        db = get_db()
        db.table("notifications").insert({
            "user_id": user_id,
            "project_id": project_id,
            "type": ntype,
            "title": title,
            "body": body,
            "action_url": action_url or "/autopilot",
        }).execute()
    except Exception as e:
        logger.error(f"[autopilot] Failed to create notification: {e}")


def _ask_claude(system: str, user_message: str) -> str:
    """Call Claude API and return the text response."""
    client = _get_anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return msg.content[0].text if msg.content else ""




# ─────────────────────────────────────────────────────────────
# Weekly Briefing
# ─────────────────────────────────────────────────────────────

BRIEFING_SYSTEM = """\
You are a senior performance marketing strategist writing a weekly briefing for an e-commerce founder.

Write a concise, actionable weekly briefing in Markdown (no H1, use H2 and H3 only).
Include:
1. **Performance Overview** — key numbers, vs last week if available, plain English
2. **What's Working** — top 2-3 performers with specific reasoning
3. **What Needs Attention** — top 2-3 issues or risks
4. **This Week's Priority** — ONE clear action the founder should focus on
5. **Quick Wins** — 2-3 small things they can do in <15 minutes

Be direct, confident, and specific. No fluff. Write like a trusted advisor, not a consultant.
Max 500 words.
"""


def briefing_for_project(project_id: str) -> dict:
    """
    Generate a weekly strategy briefing using Claude. Saves to autopilot_briefings.
    Returns { ok, briefing_id?, error? }
    """
    try:
        project = _load_project(project_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    user_id = project.get("user_id", "")
    token = token_for(project)
    account = project.get("ad_account_id")

    if not token or not account or not project.get("meta_connected"):
        return {"ok": False, "error": "Meta not connected"}

    settings = _load_settings(project_id)
    if not settings or not settings.get("enabled"):
        return {"ok": False, "error": "Autopilot disabled"}

    meta = MetaClient(token, account)

    try:
        adset_data = meta.get_insights("adset", 7)
        campaign_data = meta.get_insights("campaign", 7)
    except Exception as e:
        return {"ok": False, "error": f"Meta fetch failed: {e}"}

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    # %-d is Linux-only; use lstrip for cross-platform compatibility
    period_label = f"{week_ago.strftime('%b')} {week_ago.day}–{now.day}"

    snapshot = {
        "business_name": project.get("business_name", ""),
        "target_roas": project.get("target_roas"),
        "target_cpa": project.get("target_cpa"),
        "period": period_label,
        "campaigns": campaign_data,
        "adsets": adset_data,
    }

    try:
        briefing_text = _ask_claude(BRIEFING_SYSTEM, json.dumps(snapshot, indent=2))
    except Exception as e:
        return {"ok": False, "error": f"AI failed: {e}"}

    # Save to DB
    try:
        db = get_db()
        resp = db.table("autopilot_briefings").insert({
            "project_id": project_id,
            "user_id": user_id,
            "period_label": period_label,
            "content": briefing_text,
            "metrics_snapshot": snapshot,
        }).execute()
        rows = resp.data or []
        briefing_id = rows[0].get("id", "") if rows else ""
    except Exception as e:
        logger.error(f"[autopilot] Failed to save briefing: {e}")
        briefing_id = ""

    # Notification
    _create_notification(
        user_id, project_id, "info",
        f"Your weekly briefing is ready ({period_label})",
        "We looked over how your ads did this week and have some suggestions waiting for you.",
        "/autopilot",
    )

    return {"ok": True, "briefing_id": briefing_id, "period_label": period_label}


# ─────────────────────────────────────────────────────────────
# Approve / Reject a queued action
# ─────────────────────────────────────────────────────────────

def _replace_after_pause(
    meta: MetaClient,
    project: dict,
    project_id: str,
    action: dict,
    ad_id: str,
) -> dict:
    """
    After a user approves a pause, create a fresh replacement ad in the same ad set,
    mirroring the auto-mode flow. Best-effort — never raises.
    """
    try:
        from full_autopilot_engine import _generate_replacement, _upload_and_create_ad

        adset_id = meta.get_ad_adset_id(ad_id)
        if not adset_id:
            return {"replacement_created": False}

        user_id = action.get("user_id") or project.get("user_id", "")
        summary = {"creatives_generated": 0, "ads_created": 0, "errors": []}
        replacement_url = _generate_replacement(project, project_id, user_id, adset_id, summary)
        if replacement_url:
            _upload_and_create_ad(
                meta, project, project_id, user_id, adset_id,
                replacement_url, action.get("entity_name", ""), summary,
            )
        return {
            "replacement_created": summary["ads_created"] > 0,
            "replacement_errors": summary["errors"],
        }
    except Exception as e:
        logger.warning(f"[autopilot] Replacement after pause failed (non-fatal): {e}")
        return {"replacement_created": False, "replacement_errors": [str(e)]}


def execute_approved_action(action_id: str, project_id: str) -> dict:
    """
    Called when a user approves a pending autopilot action.
    Loads the action, executes it on Meta, updates status.
    Uses the project's current autopilot settings for scale/reduce percentages,
    enforces the max_daily_budget cap, redirects/refuses budget changes that would
    zero a CBO entity, and creates a replacement ad when a pause is approved.
    """
    db = get_db()
    # Atomically claim the action: flip pending -> executing in a single conditional
    # update so a double-click / retry / second tab cannot run the Meta side-effect
    # (pause + replacement, or a budget change) twice. Only the request that wins the
    # claim proceeds; a concurrent one matches 0 rows and bails.
    claim = (
        db.table("autopilot_actions")
        .update({"status": "executing"})
        .eq("id", action_id)
        .eq("project_id", project_id)
        .eq("status", "pending")
        .execute()
    )
    claimed = claim.data or []
    if not claimed:
        existing = (
            db.table("autopilot_actions")
            .select("status")
            .eq("id", action_id)
            .eq("project_id", project_id)
            .execute()
        )
        rows = existing.data or []
        if not rows:
            return {"ok": False, "error": "Action not found"}
        return {"ok": False, "error": f"Action status is '{rows[0]['status']}', not pending"}
    action = claimed[0]

    value_before = None
    value_after = None
    extra: dict = {}

    # Everything after the claim runs inside try/except so any failure flips the row
    # to 'failed' (visible) instead of stranding it in 'executing' (never re-claimable).
    try:
        project = _load_project(project_id)
        settings = _load_settings(project_id) or {}

        token = token_for(project)
        account = project.get("ad_account_id")
        if not token or not account or not project.get("meta_connected"):
            raise MetaAPIError("Meta not connected for this project")

        # Use settings-configured percentages so approval matches what was promised
        scale_pct = float(settings.get("scale_pct") or 20.0)
        reduce_pct = 25.0  # reduce is always 25% (conservative)
        max_daily_budget = float(settings.get("max_daily_budget_usd") or 500.0)

        # page_id is required to build the replacement creative when a pause is approved.
        meta = MetaClient(token, account, page_id=project.get("facebook_page_id"))
        action_type = action["action_type"]
        entity_id = action["entity_id"]

        if action_type in ("scale_budget", "reduce_budget"):
            # The target entity (ad set vs campaign) was resolved when the action
            # was queued, so a plain budget read/set on entity_id is correct here.
            current = meta.get_entity_budget(entity_id)
            if current <= 0:
                raise MetaAPIError(
                    "Entity has no own daily budget (campaign may use CBO) — "
                    "cannot change budget on this entity"
                )
            if action_type == "scale_budget":
                new = current * (1 + scale_pct / 100)
                if new > max_daily_budget:
                    new = max_daily_budget
            else:
                new = current * (1 - reduce_pct / 100)
            written = meta.set_budget(entity_id, new)
            value_before, value_after = round(current, 2), round(written, 2)
        elif action_type == "pause":
            meta.pause(entity_id)
            # Only ad-level pauses get a replacement ad — mirrors auto mode, where
            # adset/campaign pauses do not generate replacements.
            if action.get("entity_level") == "ad":
                extra = _replace_after_pause(meta, project, project_id, action, entity_id)
        elif action_type == "alert":
            pass  # alerts are informational only
        else:
            # Raise so the row is marked 'failed' rather than orphaned as 'executing'.
            raise MetaAPIError(f"Unknown action type: {action_type}")

        db.table("autopilot_actions").update({
            "status": "executed",
            "value_before": value_before,
            "value_after": value_after,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", action_id).execute()

        return {"ok": True, "action_type": action_type, "entity_id": entity_id,
                "value_before": value_before, "value_after": value_after, **extra}

    except (MetaAPIError, Exception) as e:
        db.table("autopilot_actions").update({
            "status": "failed",
            "error_detail": str(e),
        }).eq("id", action_id).execute()
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# All-projects runners (for scheduler)
# ─────────────────────────────────────────────────────────────




def briefing_all_projects() -> dict[str, dict]:
    """Generate weekly briefings for all enabled-autopilot projects."""
    db = get_db()
    resp = db.table("autopilot_settings").select("project_id").eq("enabled", True).execute()
    results = {}
    for row in (resp.data or []):
        pid = row["project_id"]
        try:
            results[pid] = briefing_for_project(pid)
        except Exception as e:
            logger.error(f"[autopilot] Briefing failed for project {pid}: {e}")
            results[pid] = {"ok": False, "error": str(e)}
    return results
