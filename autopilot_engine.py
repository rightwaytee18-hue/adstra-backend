"""
Adstra Autopilot Engine — Phase 8

Analyzes campaign performance using Claude (claude-sonnet-4-6) and takes
automated actions (or queues them for user approval) based on configurable
thresholds.

Two public entry points:
  run_for_project(project_id)   — called daily by scheduler / on-demand via API
  briefing_for_project(project_id) — called weekly by scheduler
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic

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
    resp = db.table("projects").select("*").eq("id", project_id).single().execute()
    if not resp.data:
        raise ValueError(f"Project {project_id} not found")
    return resp.data


def _load_settings(project_id: str) -> Optional[dict]:
    db = get_db()
    resp = db.table("autopilot_settings").select("*").eq("project_id", project_id).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def _save_action(
    project_id: str,
    user_id: str,
    action_type: str,
    entity_id: str,
    entity_name: str,
    entity_level: str,
    metric: str,
    value_before: Optional[float],
    value_after: Optional[float],
    reasoning: str,
    status: str,
) -> str:
    """Insert autopilot_actions row, return its id."""
    db = get_db()
    resp = db.table("autopilot_actions").insert({
        "project_id": project_id,
        "user_id": user_id,
        "action_type": action_type,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "entity_level": entity_level,
        "metric": metric,
        "value_before": value_before,
        "value_after": value_after,
        "reasoning": reasoning,
        "status": status,
    }).select("id").single().execute()
    return (resp.data or {}).get("id", "")


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
# Core Autopilot Run
# ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are Adstra's autopilot AI — an expert Meta ads optimizer.
You receive a JSON snapshot of ad performance and project goals.
Your job is to identify specific, high-value actions to take.

Return a JSON array (and ONLY the array, no other text) of actions.
Each action object must have:
  - "entity_id": string (Meta ID)
  - "entity_name": string
  - "entity_level": "adset" | "campaign"
  - "action": "scale_budget" | "reduce_budget" | "pause" | "alert"
  - "pct": number (for scale/reduce, the % change — e.g. 20 means +20%)
  - "reasoning": string (1-2 sentences, plain English, what you saw and why)

Rules:
1. Only recommend scale_budget if ROAS >= scale_roas_min AND spend >= 20
2. Only recommend pause if ROAS <= kill_roas_max AND spend >= kill_spend_min
3. Never scale_budget past the max_daily_budget_usd cap
4. Prefer reduce_budget (25%) over pause when ROAS is borderline (within 20% of kill threshold)
5. Max 5 actions per run to avoid over-trading
6. If everything looks good, return an empty array []
"""


def run_for_project(project_id: str) -> dict:
    """
    Main autopilot run. Fetches Meta data, calls Claude, executes or queues actions.
    Returns { project_id, actions_taken, actions_queued, skipped_reason? }
    """
    try:
        project = _load_project(project_id)
    except Exception as e:
        logger.error(f"[autopilot] Could not load project {project_id}: {e}")
        return {"project_id": project_id, "actions_taken": 0, "actions_queued": 0, "skipped_reason": str(e)}

    user_id = project.get("user_id", "")
    token = project.get("meta_access_token")
    account = project.get("ad_account_id")

    if not token or not account or not project.get("meta_connected"):
        return {"project_id": project_id, "actions_taken": 0, "actions_queued": 0, "skipped_reason": "Meta not connected"}

    settings = _load_settings(project_id)
    if not settings or not settings.get("enabled"):
        return {"project_id": project_id, "actions_taken": 0, "actions_queued": 0, "skipped_reason": "Autopilot disabled"}

    window_days = settings.get("window_days", 7)
    scale_roas_min = float(settings.get("scale_roas_min") or 3.0)
    kill_roas_max = float(settings.get("kill_roas_max") or 0.8)
    kill_spend_min = float(settings.get("kill_spend_min") or 50.0)
    scale_pct = float(settings.get("scale_pct") or 20.0)
    max_daily_budget = float(settings.get("max_daily_budget_usd") or 500.0)
    approval_mode = settings.get("approval_mode", "manual")

    meta = MetaClient(token, account)

    try:
        adsets = meta.get_insights("adset", window_days)
    except Exception as e:
        logger.error(f"[autopilot] Meta insights failed for {project_id}: {e}")
        return {"project_id": project_id, "actions_taken": 0, "actions_queued": 0, "skipped_reason": f"Meta error: {e}"}

    if not adsets:
        return {"project_id": project_id, "actions_taken": 0, "actions_queued": 0, "skipped_reason": "No active ad sets"}

    # Build snapshot for Claude
    snapshot = {
        "project_goals": {
            "target_roas": project.get("target_roas"),
            "target_cpa": project.get("target_cpa"),
            "daily_budget_cap": project.get("daily_budget_cap"),
        },
        "autopilot_thresholds": {
            "scale_roas_min": scale_roas_min,
            "scale_pct": scale_pct,
            "kill_roas_max": kill_roas_max,
            "kill_spend_min": kill_spend_min,
            "max_daily_budget_usd": max_daily_budget,
        },
        "window_days": window_days,
        "adsets": [
            {
                "entity_id": a["id"],
                "entity_name": a["name"],
                "entity_level": "adset",
                "roas": round(a.get("roas", 0), 2),
                "cpa": round(a.get("cpa", 0), 2),
                "spend": round(a.get("spend", 0), 2),
                "ctr": round(a.get("ctr", 0), 2),
                "frequency": round(a.get("frequency", 0), 2),
                "status": a.get("status", ""),
            }
            for a in adsets
        ],
    }

    try:
        claude_response = _ask_claude(SYSTEM_PROMPT, json.dumps(snapshot, indent=2))
        # Strip markdown code fences if present
        text = claude_response.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        recommended_actions: list[dict] = json.loads(text)
    except Exception as e:
        logger.error(f"[autopilot] Claude analysis failed for {project_id}: {e}")
        return {"project_id": project_id, "actions_taken": 0, "actions_queued": 0, "skipped_reason": f"AI error: {e}"}

    if not recommended_actions:
        logger.info(f"[autopilot] No actions recommended for {project_id}")
        return {"project_id": project_id, "actions_taken": 0, "actions_queued": 0}

    actions_taken = 0
    actions_queued = 0

    for rec in recommended_actions[:5]:  # cap at 5
        action_type = rec.get("action", "")
        entity_id = rec.get("entity_id", "")
        entity_name = rec.get("entity_name", "")
        entity_level = rec.get("entity_level", "adset")
        # Use Claude's recommended pct, bounded by settings
        rec_pct = float(rec.get("pct", scale_pct))
        pct = min(rec_pct, 100.0)  # never more than 100%
        reasoning = rec.get("reasoning", "")

        if approval_mode == "auto":
            # Execute immediately
            value_before = None
            value_after = None
            exec_status = "executed"

            try:
                if action_type == "scale_budget":
                    old, new = meta.scale_budget(entity_id, pct)
                    value_before = old
                    value_after = new
                elif action_type == "reduce_budget":
                    old, new = meta.reduce_budget(entity_id, pct)
                    value_before = old
                    value_after = new
                elif action_type == "pause":
                    meta.pause(entity_id)
                elif action_type == "alert":
                    exec_status = "executed"  # alerts just get logged
                else:
                    continue

                logger.info(f"[autopilot] Executed {action_type} on {entity_name} — {reasoning}")
                actions_taken += 1
            except (MetaAPIError, Exception) as e:
                exec_status = "failed"
                logger.error(f"[autopilot] Action {action_type} failed on {entity_id}: {e}")

            action_id = _save_action(
                project_id, user_id, action_type, entity_id, entity_name,
                entity_level, "roas", value_before, value_after, reasoning, exec_status,
            )
            if exec_status == "executed":
                db = get_db()
                db.table("autopilot_actions").update(
                    {"executed_at": datetime.now(timezone.utc).isoformat()}
                ).eq("id", action_id).execute()

        else:
            # Queue for user approval
            _save_action(
                project_id, user_id, action_type, entity_id, entity_name,
                entity_level, "roas", None, None, reasoning, "pending",
            )
            actions_queued += 1
            logger.info(f"[autopilot] Queued {action_type} on {entity_name} for approval")

    # Create a single summary notification
    total = actions_taken + actions_queued
    if total > 0:
        if approval_mode == "auto":
            _create_notification(
                user_id, project_id, "info",
                f"Autopilot executed {actions_taken} action{'s' if actions_taken != 1 else ''}",
                f"Adstra optimized your campaigns automatically. {actions_taken} action(s) applied.",
                "/autopilot",
            )
        else:
            _create_notification(
                user_id, project_id, "action",
                f"{actions_queued} autopilot action{'s' if actions_queued != 1 else ''} need your approval",
                "Adstra identified optimization opportunities. Review and approve in the Autopilot tab.",
                "/autopilot",
            )

    return {
        "project_id": project_id,
        "actions_taken": actions_taken,
        "actions_queued": actions_queued,
    }


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
    token = project.get("meta_access_token")
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
        }).select("id").single().execute()
        briefing_id = (resp.data or {}).get("id", "")
    except Exception as e:
        logger.error(f"[autopilot] Failed to save briefing: {e}")
        briefing_id = ""

    # Notification
    _create_notification(
        user_id, project_id, "info",
        f"Your weekly briefing is ready ({period_label})",
        "Adstra AI analyzed your performance and has strategic recommendations waiting.",
        "/autopilot",
    )

    return {"ok": True, "briefing_id": briefing_id, "period_label": period_label}


# ─────────────────────────────────────────────────────────────
# Approve / Reject a queued action
# ─────────────────────────────────────────────────────────────

def execute_approved_action(action_id: str, project_id: str) -> dict:
    """
    Called when a user approves a pending autopilot action.
    Loads the action, executes it on Meta, updates status.
    Uses the project's current autopilot settings for scale/reduce percentages.
    """
    db = get_db()
    resp = db.table("autopilot_actions").select("*").eq("id", action_id).eq("project_id", project_id).single().execute()
    action = resp.data
    if not action:
        return {"ok": False, "error": "Action not found"}
    if action.get("status") != "pending":
        return {"ok": False, "error": f"Action status is '{action['status']}', not pending"}

    project = _load_project(project_id)
    settings = _load_settings(project_id)

    # Use settings-configured percentages so approval matches what was promised
    scale_pct = float((settings or {}).get("scale_pct") or 20.0)
    reduce_pct = 25.0  # reduce is always 25% (conservative)

    meta = MetaClient(project["meta_access_token"], project["ad_account_id"])
    action_type = action["action_type"]
    entity_id = action["entity_id"]

    value_before = None
    value_after = None

    try:
        if action_type == "scale_budget":
            old, new = meta.scale_budget(entity_id, scale_pct)
            value_before, value_after = old, new
        elif action_type == "reduce_budget":
            old, new = meta.reduce_budget(entity_id, reduce_pct)
            value_before, value_after = old, new
        elif action_type == "pause":
            meta.pause(entity_id)
        elif action_type == "alert":
            pass  # alerts are informational only
        else:
            return {"ok": False, "error": f"Unknown action type: {action_type}"}

        db.table("autopilot_actions").update({
            "status": "executed",
            "value_before": value_before,
            "value_after": value_after,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", action_id).execute()

        return {"ok": True, "action_type": action_type, "entity_id": entity_id,
                "value_before": value_before, "value_after": value_after}

    except (MetaAPIError, Exception) as e:
        db.table("autopilot_actions").update({
            "status": "failed",
            "error_detail": str(e),
        }).eq("id", action_id).execute()
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# All-projects runners (for scheduler)
# ─────────────────────────────────────────────────────────────

def run_all_projects() -> dict[str, dict]:
    """Run autopilot for all projects where autopilot is enabled + Meta connected."""
    db = get_db()
    resp = db.table("autopilot_settings").select("project_id").eq("enabled", True).execute()
    results = {}
    for row in (resp.data or []):
        pid = row["project_id"]
        try:
            results[pid] = run_for_project(pid)
        except Exception as e:
            logger.error(f"[autopilot] Failed for project {pid}: {e}")
            results[pid] = {"project_id": pid, "error": str(e)}
    return results


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
