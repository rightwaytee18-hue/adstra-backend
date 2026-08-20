"""
Adstra Full Autopilot Engine — Set-and-Forget Autonomous Ad Machine

After a user enables autopilot and their brand is customized, this engine:

  BOOTSTRAP (runs once):
    1. Generate 3 AI ad creatives via creative_engine
    2. Insert 4 default protection rules into the rules table
    3. Publish the initial campaign to Meta with those creatives
    4. Mark autopilot_settings.bootstrapped = True

  DAILY OPTIMIZATION (runs every day at 06:00 UTC):
    1. Scan competitors (collect new ads via competitor_engine)
    2. Fetch ad-level insights from Meta
    3. Claude analysis: identify underperformers + scaling opportunities
    4. Scale budgets on winners
    5. Pause losing ads + generate replacement creatives
    6. Upload new creatives to Meta as fresh ads in the same ad set
    7. Budget optimization actions (executed or queued per approval_mode)

  WEEKLY BRIEFING (runs every Monday at 08:00 UTC):
    - Already handled by autopilot_engine.briefing_for_project

Public API:
  bootstrap_project(project_id)         — called when user first enables autopilot
  run_full_daily(project_id)            — daily cron + on-demand "Run Now"
  run_full_daily_all_projects()         — cron entry point for all enabled projects
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic

import guards
from crypto import token_for
from db import get_db
from meta_client import MetaClient, MetaAPIError
from conversions import campaign_shape
from snapshots import write_daily_snapshot

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"

# Hard ceiling on a single budget increase, regardless of what the settings row
# says. Meta restarts an ad set's learning phase on a budget edit, and the
# published guidance is not to exceed roughly 20 percent per change. A customer
# who types 200 into the box would otherwise reset their own campaign daily.
MAX_SCALE_PCT = 20.0

# DRY_RUN logs every decision and makes no Meta call that changes anything.
# There was no such flag, so the only way to see what the engine would do to a
# real account was to let it do it.
DRY_RUN = os.environ.get("AUTOPILOT_DRY_RUN", "").lower() in ("1", "true", "yes")

_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


# ─────────────────────────────────────────────────────────────
# Shared helpers
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


def _update_settings(project_id: str, updates: dict) -> None:
    db = get_db()
    db.table("autopilot_settings").update(updates).eq("project_id", project_id).execute()


def _create_notification(
    user_id: str,
    project_id: str,
    ntype: str,
    title: str,
    body: str,
    action_url: Optional[str] = None,
) -> None:
    """Best-effort — never raises."""
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
        logger.error(f"[full_autopilot] Notification failed: {e}")


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
    executed_at: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> str:
    db = get_db()
    payload = {
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
    }
    if executed_at:
        payload["executed_at"] = executed_at
    if error_detail:
        payload["error_detail"] = error_detail
    resp = db.table("autopilot_actions").insert(payload).execute()
    rows = resp.data or []
    return rows[0].get("id", "") if rows else ""


# ─────────────────────────────────────────────────────────────
# PHASE 1: Bootstrap
# ─────────────────────────────────────────────────────────────

# NOTE: condition vocabulary MUST match rules_engine._meets_condition / run_for_project:
#   op    -> "less_than" | "greater_than" | "equals"
#   value -> numeric threshold (read via cond["value"])
#   metric in rules_engine.METRIC_KEYS, plus window_days
# and `action` must be one the rules engine handles ("pause", "scale_budget",
# "reduce_budget", "send_alert"). Anything else is silently ignored at run time.
DEFAULT_RULES = [
    {
        "name": "Switch off ads that lose money",
        "level": "adset",
        "conditions": [
            {"metric": "roas", "op": "less_than", "value": 0.5, "window_days": 7},
            {"metric": "spend", "op": "greater_than", "value": 50, "window_days": 7},
        ],
        "action": "pause",
        "action_value": None,
    },
    {
        "name": "Put more behind what is working",
        "level": "adset",
        "conditions": [
            {"metric": "roas", "op": "greater_than", "value": 4.0, "window_days": 7},
            {"metric": "spend", "op": "greater_than", "value": 20, "window_days": 7},
        ],
        "action": "scale_budget",
        "action_value": 20,
    },
    {
        "name": "Spend less on what is not paying off",
        "level": "adset",
        "conditions": [
            {"metric": "roas", "op": "less_than", "value": 1.0, "window_days": 3},
            {"metric": "spend", "op": "greater_than", "value": 30, "window_days": 3},
        ],
        "action": "reduce_budget",
        "action_value": 25,
    },
    {
        "name": "Tell me when people see an ad too often",
        "level": "adset",
        "conditions": [
            {"metric": "frequency", "op": "greater_than", "value": 4.0, "window_days": 7},
        ],
        "action": "send_alert",
        "action_value": None,
    },
]

BOOTSTRAP_CREATIVE_PROMPTS = [
    {
        "prompt": "Bold product hero shot with lifestyle context. Clean minimal layout. Strong headline overlay.",
        "type": "hero",
    },
    {
        "prompt": "Social proof creative. Testimonial or review style. Trust-building. Warm, authentic.",
        "type": "testimonial",
    },
    {
        "prompt": "Urgency/offer creative. Limited time feel. Discount or value prop front and center. High contrast.",
        "type": "offer",
    },
]


def bootstrap_project(project_id: str) -> dict:
    """
    Run once when user first enables autopilot.
    Creates 3 creatives, 4 rules, and publishes the first campaign.
    Idempotent: skips safely if already bootstrapped.
    """
    try:
        project = _load_project(project_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # None, never "". These columns are uuid; an empty string raises 22P02
    # (invalid input syntax) on every insert, and _save_action is unguarded,
    # so the whole daily run died the first time it tried to queue an action.
    # Reveal tenant-owned projects legitimately have no auth user.
    user_id = project.get("user_id") or None

    # Idempotency guard — never run bootstrap twice
    settings = _load_settings(project_id)
    if settings and settings.get("bootstrapped"):
        logger.info(f"[bootstrap] Project {project_id} already bootstrapped — skipping")
        return {"ok": True, "skipped": True, "reason": "Already bootstrapped"}

    # Mark bootstrap as in-progress
    _update_settings(project_id, {"bootstrap_status": "running", "bootstrap_error": None})
    _create_notification(
        user_id, project_id, "info",
        "Getting your ads ready",
        "Generating your first creatives and setting up your campaign. This takes ~2 minutes.",
        "/autopilot",
    )

    steps = []
    errors = []
    gemini_unavailable = False  # Set True when Gemini is not configured — not a hard failure

    # ── Step 1: Generate 3 ad creatives ──────────────────────
    logger.info(f"[bootstrap] Generating creatives for {project_id}")
    creatives_for_campaign = []

    try:
        from creative_engine import NanaBanana
        banana = NanaBanana(get_db())

        for cp in BOOTSTRAP_CREATIVE_PROMPTS:
            try:
                image_url, ad_text = banana.generate_fresh(
                    project,
                    cp["prompt"],
                    project.get("business_name", ""),
                )
                if image_url:
                    # Save to creative_generations table
                    db = get_db()
                    db.table("creative_generations").insert({
                        "project_id": project_id,
                        "user_id": user_id,
                        "mode": "fresh",
                        "prompt": cp["prompt"],
                        "image_url": image_url,
                        "is_saved": True,
                        "metadata": {"text": ad_text, "bootstrap_type": cp["type"]},
                    }).execute()
                    creatives_for_campaign.append({
                        "image_url": image_url,
                        "headline": _extract_headline(ad_text, project.get("business_name", "Ad")),
                        "message": _extract_body(ad_text),
                        "description": "",
                    })
                    steps.append({"step": f"creative_{cp['type']}", "status": "ok"})
                    logger.info(f"[bootstrap] Creative ({cp['type']}) generated: {image_url[:60]}")
                else:
                    steps.append({"step": f"creative_{cp['type']}", "status": "skipped", "detail": "No image returned"})
                    errors.append(f"Creative {cp['type']} returned no image")
            except Exception as e:
                logger.warning(f"[bootstrap] Creative {cp['type']} failed: {e}")
                steps.append({"step": f"creative_{cp['type']}", "status": "error", "detail": str(e)})
                errors.append(f"Creative generation failed: {e}")

    except Exception as e:
        # Gemini not available / not configured — not a hard failure, continue
        gemini_unavailable = True
        logger.warning(f"[bootstrap] Creative engine unavailable: {e}")
        steps.append({"step": "creatives", "status": "skipped", "detail": "Gemini not configured"})

    # ── Step 2: Create default protection rules ───────────────
    logger.info(f"[bootstrap] Creating default rules for {project_id}")
    try:
        db = get_db()
        for rule in DEFAULT_RULES:
            db.table("rules").insert({
                "project_id": project_id,
                "user_id": user_id,
                "name": rule["name"],
                "level": rule["level"],
                "status": "active",
                # conditions is a jsonb column — pass the list directly so it is
                # stored as a JSON array (json.dumps would store a JSON string scalar
                # that rules_engine cannot iterate).
                "conditions": rule["conditions"],
                "action": rule["action"],
                "action_value": rule["action_value"],
            }).execute()
        steps.append({"step": "default_rules", "status": "ok", "detail": f"{len(DEFAULT_RULES)} rules created"})
        logger.info(f"[bootstrap] {len(DEFAULT_RULES)} default rules created")
    except Exception as e:
        logger.error(f"[bootstrap] Rules creation failed: {e}")
        steps.append({"step": "default_rules", "status": "error", "detail": str(e)})
        errors.append(f"Rules creation failed: {e}")

    # ── Step 3: Publish initial campaign ─────────────────────
    if not creatives_for_campaign:
        # Can't publish without creatives — skip but don't fail
        steps.append({"step": "campaign", "status": "skipped", "detail": "No creatives available"})
        logger.warning(f"[bootstrap] No creatives — skipping campaign publish for {project_id}")
    elif not project.get("meta_connected") or not token_for(project):
        steps.append({"step": "campaign", "status": "skipped", "detail": "Meta not connected"})
        logger.warning(f"[bootstrap] Meta not connected — skipping campaign publish for {project_id}")
    else:
        logger.info(f"[bootstrap] Publishing initial campaign for {project_id}")
        try:
            from campaign_builder import publish_for_project

            business_name = project.get("business_name") or "Our Brand"
            store_url = project.get("store_url") or project.get("website") or ""

            # The launch draft was hardcoded to the ecommerce shape regardless of
            # projects.goal, so every service business Reveal onboarded had its
            # first campaign published as OUTCOME_SALES with a SHOP_NOW button.
            shape = campaign_shape(project.get("goal"))

            draft = {
                "name": f"{business_name} — Autopilot Launch",
                "template_key": shape["template_key"],
                "objective": shape["objective"],
                "countries": project.get("target_countries") or ["US"],
                "age_min": project.get("target_age_min") or 18,
                "age_max": project.get("target_age_max") or 65,
                "gender": project.get("target_gender") or "all",
                "daily_budget_cents": int((project.get("daily_budget_cap") or 50) * 100),
                "budget_mode": "cbo",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "link_url": store_url,
                "cta_type": shape["cta_type"],
                "creatives": creatives_for_campaign,
            }

            result = publish_for_project(project_id, draft)

            if result.get("ok"):
                steps.append({
                    "step": "campaign",
                    "status": "ok",
                    "detail": f"Campaign {result.get('campaign_id')} with {len(result.get('ad_ids', []))} ads",
                })
                logger.info(f"[bootstrap] Campaign published: {result.get('campaign_id')}")
            else:
                err = result.get("fatal") or "; ".join(result.get("errors", []))
                steps.append({"step": "campaign", "status": "error", "detail": err})
                errors.append(f"Campaign publish failed: {err}")
                logger.error(f"[bootstrap] Campaign publish failed: {err}")

        except Exception as e:
            logger.error(f"[bootstrap] Campaign publish exception: {e}")
            steps.append({"step": "campaign", "status": "error", "detail": str(e)})
            errors.append(f"Campaign publish exception: {e}")

    # ── Mark bootstrapped ────────────────────────────────────
    # A critical error is one where creatives failed for a real reason (not just Gemini unavailable)
    had_critical_error = not creatives_for_campaign and not gemini_unavailable
    _update_settings(project_id, {
        "bootstrapped": True,
        "bootstrap_status": "error" if had_critical_error else "complete",
        "bootstrap_error": "; ".join(errors) if errors else None,
    })

    # Success notification
    campaign_step = next((s for s in steps if s["step"] == "campaign"), None)
    if campaign_step and campaign_step["status"] == "ok":
        _create_notification(
            user_id, project_id, "success",
            "Your first ads are ready",
            f"We built 3 ads for you and set up {len(DEFAULT_RULES)} rules that protect your budget. Nothing is spending yet, and we will ask you before anything goes live.",
            "/autopilot",
        )
    else:
        _create_notification(
            user_id, project_id, "info",
            "Autopilot setup complete",
            f"Created {len(creatives_for_campaign)} creatives and {len(DEFAULT_RULES)} rules. Connect Meta to publish your campaign.",
            "/autopilot",
        )

    logger.info(f"[bootstrap] Complete for {project_id}. Steps: {len(steps)}, Errors: {len(errors)}")
    return {
        "ok": True,
        "project_id": project_id,
        "creatives_generated": len(creatives_for_campaign),
        "rules_created": len(DEFAULT_RULES),
        "steps": steps,
        "errors": errors,
    }


def _extract_headline(text: str, fallback: str) -> str:
    """Pull first short line from AI text as headline, or use fallback."""
    if not text:
        return fallback
    for line in text.split("\n"):
        line = line.strip().lstrip("#").strip()
        if line and len(line) < 60:
            return line
    return fallback[:60]


def _extract_body(text: str) -> str:
    """Use first substantive paragraph as body copy."""
    if not text:
        return ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) > 1:
        return lines[1][:125]
    return lines[0][:125] if lines else ""


# ─────────────────────────────────────────────────────────────
# PHASE 2: Daily Optimization
# ─────────────────────────────────────────────────────────────

def _daily_system_prompt(goal: str, s: dict) -> str:
    """
    Build the decision prompt from THIS project's settings.

    ⚠️ This used to be a module-level constant carrying literals: "pause if ROAS
    < 0.5", "spend > $20". Those literals sat next to a settings object that also
    defined kill_roas_max (default 0.8) and were handed to the model together, so
    the customer's own threshold was quietly overridden by a number nobody could
    see or change. Every threshold now comes from the row.

    ⚠️ And the whole prompt assumed an ecommerce advertiser. A lead-gen account
    has no ROAS at all, so the purchase-shaped version of these rules told the
    model to pause everything. The goal decides which rules it even sees.
    """
    scale_roas_min = float(s.get("scale_roas_min") or 3.0)
    kill_roas_max = float(s.get("kill_roas_max") or 0.8)
    kill_spend_min = float(s.get("kill_spend_min") or 50.0)
    max_daily = float(s.get("max_daily_budget_usd") or 500.0)
    max_account_daily = float(s.get("max_account_daily_usd") or 1000.0)
    min_conv = int(s.get("min_conversions_to_scale") or 50)
    cooldown = int(s.get("scale_cooldown_hours") or 72)
    target_cpr = s.get("target_cost_per_result")

    if goal == "purchase":
        performance_rules = f"""\
This account sells products, so ROAS is real and is the primary signal.

Pause an ad when:
- ROAS is below {kill_roas_max} AND spend is at least ${kill_spend_min:.0f}
- or CTR is below 0.5% AND spend is at least ${kill_spend_min:.0f}

Budget:
- Scale when ROAS is at or above {scale_roas_min} and spend is at least ${kill_spend_min:.0f}
- Reduce when ROAS is at or below {kill_roas_max} and spend is at least ${kill_spend_min:.0f}"""
    elif goal == "lead":
        cpr_line = (
            f"The customer's target cost per enquiry is ${float(target_cpr):.2f}."
            if target_cpr
            else
            "The customer has not set a target cost per enquiry, so compare each "
            "entity against the account average rather than an absolute number."
        )
        performance_rules = f"""\
This account does NOT sell products online. It generates enquiries: form fills,
phone taps and messages. There is no ROAS and no revenue. The `roas` field will
be null on every row and you must IGNORE it completely rather than treat null as
zero. The number that matters is `cost_per_result`, in dollars per enquiry.

{cpr_line}

Pause an ad when:
- It has spent at least ${kill_spend_min:.0f} and produced ZERO results
- or its cost_per_result is more than twice the account average, on at least
  ${kill_spend_min:.0f} of spend

Budget:
- Scale when cost_per_result is comfortably below the account average and the
  entity has at least {min_conv} results in the window
- Reduce when cost_per_result is well above the account average on meaningful spend

An entity with zero spend has not been tested. Never pause it and never scale it."""
    else:
        performance_rules = """\
This account is running an awareness campaign. There are no purchases and no
enquiries to count, so do NOT pause anything for a lack of conversions. Judge
only on reach efficiency: CPM and frequency. Recommend a pause only when
frequency is above 4.0, which means the same people are being shown the ad over
and over."""

    return f"""\
You are an autonomous ad optimizer working on one advertiser's account. You
receive ad-level, adset-level and campaign-level performance plus their targets.

Identify SPECIFIC ads to pause AND specific adsets or campaigns to scale or reduce.

Return ONLY a JSON object with two keys:
{{
  "ads_to_pause": [
    {{"ad_id": "...", "ad_name": "...", "adset_id": "...", "reasoning": "1 sentence"}}
  ],
  "budget_actions": [
    {{"entity_id": "...", "entity_name": "...", "entity_level": "adset"|"campaign", "action": "scale_budget"|"reduce_budget"|"pause", "pct": 20, "reasoning": "1 sentence"}}
  ]
}}

{performance_rules}

Hard limits that apply to every goal:
- Never pause more than 3 ads in one run
- Never take more than 3 budget actions in one run
- Never take a single entity's daily budget above ${max_daily:.0f}
- The whole account must stay at or below ${max_account_daily:.0f} per day
- Never propose a budget increase above 20 percent. Meta puts an ad set back
  into its learning phase on a budget edit, and a bigger jump costs more in lost
  delivery than the extra budget gains. Increases are spaced {cooldown} hours apart
  and the system will reject one that comes too soon.
- Do not touch an entity that is still learning. An entity with fewer than
  {min_conv} results in the window has not produced a reliable signal yet, so
  leave its budget alone even if the early numbers look good or bad.
- Budget lives on the AD SET for ABO campaigns and on the CAMPAIGN for CBO
  campaigns. Prefer the entity that actually holds the budget. If you target an
  ad set whose campaign uses CBO, the system applies the change at campaign level.

Write every `reasoning` in plain language a small business owner would
understand, with no jargon. The customer reads this sentence and approves or
rejects it. Say "this ad cost $61 per enquiry when the rest of the account
averages $22", not "CPA exceeds threshold".

If nothing needs changing, return {{"ads_to_pause": [], "budget_actions": []}}
"""


def run_full_daily(project_id: str) -> dict:
    """
    Full daily optimization run for a single project.
    Returns a summary dict with all actions taken/queued.
    """
    try:
        project = _load_project(project_id)
    except Exception as e:
        logger.error(f"[daily] Could not load project {project_id}: {e}")
        return {"project_id": project_id, "skipped_reason": str(e)}

    # None, never "". These columns are uuid; an empty string raises 22P02
    # (invalid input syntax) on every insert, and _save_action is unguarded,
    # so the whole daily run died the first time it tried to queue an action.
    # Reveal tenant-owned projects legitimately have no auth user.
    user_id = project.get("user_id") or None
    token = token_for(project)
    account = project.get("ad_account_id")

    if not token or not account or not project.get("meta_connected"):
        return {"project_id": project_id, "skipped_reason": "Meta not connected"}

    settings = _load_settings(project_id)
    if not settings or not settings.get("enabled"):
        return {"project_id": project_id, "skipped_reason": "Autopilot disabled"}

    # ── Bootstrap check: run bootstrap if never done ──────────
    if not settings.get("bootstrapped"):
        logger.info(f"[daily] Project {project_id} not bootstrapped — running bootstrap first")
        bootstrap_project(project_id)
        # Reload settings after bootstrap
        settings = _load_settings(project_id) or settings

    window_days = int(settings.get("window_days") or 7)
    scale_roas_min = float(settings.get("scale_roas_min") or 3.0)
    kill_roas_max = float(settings.get("kill_roas_max") or 0.8)
    kill_spend_min = float(settings.get("kill_spend_min") or 50.0)
    max_daily_budget = float(settings.get("max_daily_budget_usd") or 500.0)
    max_account_daily = float(settings.get("max_account_daily_usd") or 1000.0)
    min_conversions_to_scale = int(settings.get("min_conversions_to_scale") or 50)
    scale_cooldown_hours = int(settings.get("scale_cooldown_hours") or 72)
    approval_mode = settings.get("approval_mode", "manual")

    # ── Stop switches, checked before a single Meta call ──────────────────
    stopped = guards.killed() or guards.project_paused(settings)
    if stopped:
        logger.warning(f"[daily] {project_id} halted before any action: {stopped}")
        return {"project_id": project_id, "skipped_reason": stopped}

    # What counts as a result on this account. Everything downstream keys off it:
    # which Meta action families are summed, whether ROAS exists at all, and
    # which half of the decision prompt the model is shown. See conversions.py.
    goal = project.get("goal") or "lead"

    # ⚠️ Meta puts an ad set back into its learning phase on a budget edit, and a
    # jump beyond about 20 percent costs more in lost delivery than the budget
    # buys. The setting is clamped rather than trusted, because a customer typing
    # 200 into a box should not be able to reset their own campaign.
    scale_pct = min(float(settings.get("scale_pct") or 20.0), MAX_SCALE_PCT)

    # page_id is REQUIRED to create ad creatives for replacement ads.
    meta = MetaClient(token, account, page_id=project.get("facebook_page_id"))
    summary = {
        "project_id": project_id,
        "competitor_ads_scraped": 0,
        "ads_paused": 0,
        "ads_pause_queued": 0,
        "creatives_generated": 0,
        "ads_created": 0,
        "budget_actions_taken": 0,
        "budget_actions_queued": 0,
        "errors": [],
    }

    # ── Step 1: Competitor scan ───────────────────────────────
    try:
        from competitor_engine import run_for_project as scrape_competitors
        scrape_result = scrape_competitors(project_id)
        if isinstance(scrape_result, dict):
            summary["competitor_ads_scraped"] = scrape_result.get("scraped", 0)
        logger.info(f"[daily] Competitor scan: {summary['competitor_ads_scraped']} new ads")
    except Exception as e:
        logger.warning(f"[daily] Competitor scan failed (non-fatal): {e}")
        summary["errors"].append(f"Competitor scan: {e}")

    # ── Step 2: Fetch ad-level insights ──────────────────────
    try:
        ads = meta.get_insights("ad", window_days, goal=goal)
        adsets = meta.get_insights("adset", window_days, goal=goal)
        campaigns = meta.get_insights("campaign", window_days, goal=goal)
    except Exception as e:
        logger.error(f"[daily] Meta insights failed: {e}")
        summary["errors"].append(f"Meta insights: {e}")
        return summary

    # Written BEFORE the decision step and before the early return below, so the
    # customer's dashboard fills in on a day when the model decides nothing needs
    # doing. Reporting is not a side effect of taking an action.
    #
    # A SECOND read, deliberately. The rows above are one aggregate per entity
    # covering the whole window, which is what the decision path needs and what
    # keeps `frequency` meaningful. The dashboard needs one row per calendar day,
    # and the two cannot be derived from each other. Feeding the aggregate rows
    # to the snapshot is what made reported spend roughly `window_days` times the
    # real figure. Never pass `ads` here.
    #
    # Wrapped separately so a reporting failure cannot stop the optimizer from
    # pausing a money-losing ad.
    try:
        snap_days = max(window_days, 7)
        summary["snapshot_rows"] = write_daily_snapshot(
            project_id,
            meta.get_daily_insights("ad", snap_days, goal=goal),
            meta.get_daily_insights("adset", snap_days, goal=goal),
            meta.get_daily_insights("campaign", snap_days, goal=goal),
        )
    except Exception as e:
        logger.error(f"[daily] Snapshot read failed (non-fatal): {e}")
        summary["errors"].append(f"Snapshot: {e}")

    if not ads and not adsets:
        summary["skipped_reason"] = "No active ads or ad sets"
        return summary

    # Map each ad set to its parent campaign so budget actions can be redirected
    # to the campaign when the campaign uses CBO (ad set has no own budget).
    adset_to_campaign = {a.get("id"): a.get("campaign_id") for a in (adsets or []) if a.get("id")}

    # Every entity by id, so a guard can look up the results count behind a
    # proposed budget change without a second Meta call.
    entity_by_id = {
        e["id"]: e
        for e in list(adsets or []) + list(campaigns or []) + list(ads or [])
        if e.get("id")
    }

    # The account's committed daily spend, for the portfolio ceiling. Read once
    # per run: on the Dev tier the app has 600 ads_insights calls per hour per
    # account, so a per-entity budget read inside the action loop would burn the
    # quota the next morning's run needs.
    account_budgets: list[dict] = []
    for e in list(adsets or []) + list(campaigns or []):
        try:
            account_budgets.append({"id": e["id"], "daily_budget": meta.get_entity_budget(e["id"])})
        except Exception as exc:
            logger.warning(f"[daily] Could not read budget for {e.get('id')}: {exc}")

    # ── Step 3: Claude analysis ───────────────────────────────
    # ⚠️ `roas` and `cost_per_result` stay null when they do not exist. Rounding
    # them through `or 0` (which this did) hands the model a measured-looking
    # zero for every ad on a lead-gen account, and a zero ROAS reads as "this ad
    # earned nothing" rather than "this account has no ROAS".
    def _r(v, nd=2):
        return None if v is None else round(float(v), nd)

    # The account average is what a lead-gen judgement is made against, since
    # there is no absolute ROAS number to compare to. Computed from spend and
    # results across every ad rather than by averaging per-ad rates, so one ad
    # with a single cheap result cannot drag the benchmark down.
    total_spend = sum(float(a.get("spend") or 0) for a in (ads or []))
    total_results = sum(float(a.get("results") or 0) for a in (ads or []))
    account_cost_per_result = (total_spend / total_results) if total_results > 0 else None

    snapshot = {
        "goal": goal,
        "thresholds": {
            "scale_roas_min": scale_roas_min,
            "scale_pct": scale_pct,
            "kill_roas_max": kill_roas_max,
            "kill_spend_min": kill_spend_min,
            "max_daily_budget_usd": max_daily_budget,
            "max_account_daily_usd": max_account_daily,
            "min_conversions_to_scale": min_conversions_to_scale,
        },
        "account": {
            "spend": _r(total_spend),
            "results": _r(total_results),
            "cost_per_result": _r(account_cost_per_result),
        },
        "window_days": window_days,
        "ads": [
            {
                "ad_id": a.get("id"),
                "ad_name": a.get("name"),
                "adset_id": a.get("adset_id"),
                "roas": _r(a.get("roas")),
                "results": _r(a.get("results")),
                "cost_per_result": _r(a.get("cost_per_result")),
                "ctr": _r(a.get("ctr")),
                "spend": _r(a.get("spend")),
                "status": a.get("status"),
            }
            for a in (ads or [])
        ],
        "adsets": [
            {
                "entity_id": a.get("id"),
                "entity_name": a.get("name"),
                "entity_level": "adset",
                "campaign_id": a.get("campaign_id"),
                "roas": _r(a.get("roas")),
                "results": _r(a.get("results")),
                "cost_per_result": _r(a.get("cost_per_result")),
                "spend": _r(a.get("spend")),
                "frequency": _r(a.get("frequency")),
                "status": a.get("status"),
            }
            for a in (adsets or [])
        ],
        "campaigns": [
            {
                "entity_id": c.get("id"),
                "entity_name": c.get("name"),
                "entity_level": "campaign",
                "roas": _r(c.get("roas")),
                "results": _r(c.get("results")),
                "cost_per_result": _r(c.get("cost_per_result")),
                "spend": _r(c.get("spend")),
                "status": c.get("status"),
            }
            for c in (campaigns or [])
        ],
    }

    logger.info(
        f"[daily] {project_id} sending {len(snapshot['ads'])} ads / "
        f"{len(snapshot['adsets'])} adsets to Claude ({MODEL})"
    )
    try:
        client = _get_anthropic()
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=_daily_system_prompt(goal, settings),
            messages=[{"role": "user", "content": json.dumps(snapshot, indent=2)}],
        )
        raw = (msg.content[0].text if msg.content else "{}").strip()
        # Observability: record the model's raw decision before parsing.
        logger.info(f"[daily] {project_id} Claude raw decision: {raw[:2000]}")
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        recommendations: dict = json.loads(raw)
    except Exception as e:
        logger.error(f"[daily] Claude analysis failed: {e}")
        summary["errors"].append(f"AI analysis: {e}")
        recommendations = {"ads_to_pause": [], "budget_actions": []}

    ads_to_pause: list[dict] = recommendations.get("ads_to_pause", [])[:3]
    budget_actions: list[dict] = recommendations.get("budget_actions", [])[:3]
    logger.info(
        f"[daily] {project_id} Claude decision parsed: "
        f"{len(ads_to_pause)} ad(s) to pause, {len(budget_actions)} budget action(s) "
        f"(approval_mode={approval_mode})"
    )

    # Ad sets a budget action will pause this run — don't create a replacement ad
    # inside an ad set we're about to pause.
    pause_target_entities = {
        rec.get("entity_id")
        for rec in budget_actions
        if rec.get("action") == "pause" and rec.get("entity_id")
    }

    # ── Step 4: Pause underperforming ads ─────────────────────
    # In manual mode the pause (and its replacement) is queued for the user to
    # approve; on approval execute_approved_action pauses the ad and creates the
    # replacement. In auto mode it runs immediately here.
    for rec in ads_to_pause:
        ad_id = rec.get("ad_id", "")
        ad_name = rec.get("ad_name", "")
        adset_id = rec.get("adset_id", "")
        reasoning = rec.get("reasoning", "")

        if not ad_id:
            continue

        if approval_mode != "auto":
            _save_action(
                project_id, user_id, "pause", ad_id, ad_name, "ad",
                "roas/ctr", None, None, reasoning, "pending",
            )
            summary["ads_pause_queued"] += 1
            logger.info(f"[daily] Queued pause on {ad_name} ({ad_id}) for approval")
            continue

        try:
            meta.pause(ad_id)
            summary["ads_paused"] += 1
            logger.info(f"[daily] Paused ad {ad_name} ({ad_id})")

            _save_action(
                project_id, user_id, "pause", ad_id, ad_name, "ad",
                "roas/ctr", None, None, reasoning, "executed",
                executed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.warning(f"[daily] Pause failed for {ad_id}: {e}")
            summary["errors"].append(f"Pause {ad_name}: {e}")
            continue

        # ── Step 5: Generate replacement creative ──────────────
        if not adset_id or adset_id in pause_target_entities:
            continue
        replacement_url = _generate_replacement(project, project_id, user_id, adset_id, summary)

        # ── Step 6: Upload replacement to Meta as new ad ───────
        if replacement_url and adset_id:
            _upload_and_create_ad(
                meta, project, project_id, user_id,
                adset_id, replacement_url, ad_name, summary
            )

    # ── Step 7: Budget actions ────────────────────────────────
    for rec in budget_actions:
        entity_id = rec.get("entity_id", "")
        entity_name = rec.get("entity_name", "")
        entity_level = rec.get("entity_level", "adset")
        action_type = rec.get("action", "")
        pct = float(rec.get("pct") or scale_pct)
        reasoning = rec.get("reasoning", "")

        if not entity_id or action_type not in ("scale_budget", "reduce_budget", "pause"):
            continue

        # Pausing an entire campaign autonomously halts all its delivery — too
        # high-impact to ride the budget-action path. Skip it.
        if action_type == "pause" and entity_level == "campaign":
            logger.warning(
                f"[daily] Skipping campaign-level pause on {entity_name} ({entity_id}) — "
                f"too high-impact for autonomous execution"
            )
            summary["errors"].append(f"Skipped campaign pause {entity_name} (needs manual action)")
            continue

        # Redirect ad-set budget changes to the parent campaign when the campaign
        # uses CBO. Resolved before queueing so a queued action approved later
        # targets the correct entity.
        if entity_level == "adset" and action_type in ("scale_budget", "reduce_budget"):
            entity_id, entity_level, entity_name, reasoning = _resolve_budget_target(
                meta, entity_id, entity_name, reasoning, adset_to_campaign
            )

        # ── Guards, before anything is queued OR executed ─────────────────
        # Deliberately ahead of the approval branch. A blocked action must not
        # reach the customer's queue: asking someone to approve a budget rise
        # the engine will then refuse teaches them the buttons do nothing.
        if action_type == "scale_budget":
            entity_row = entity_by_id.get(entity_id) or {}
            block = guards.learning_phase_block(entity_row, min_conversions_to_scale)
            if not block:
                block = guards.cooldown_block(
                    project_id, entity_id, "increase", scale_cooldown_hours
                )
            if block:
                logger.info(f"[daily] Skipping scale on {entity_name}: {block}")
                _save_action(
                    project_id, user_id, action_type, entity_id, entity_name,
                    entity_level, "budget", None, None,
                    f"{reasoning} Not done: {block}", "rejected",
                )
                continue

        if approval_mode == "auto":
            _execute_budget_action(meta, entity_id, entity_name, entity_level,
                                   action_type, pct, project_id, user_id, reasoning, summary,
                                   max_daily_budget=max_daily_budget,
                                   max_account_daily=max_account_daily,
                                   account_budgets=account_budgets)
        else:
            # Queue for user approval
            _save_action(
                project_id, user_id, action_type, entity_id, entity_name,
                entity_level, "roas", None, None, reasoning, "pending",
            )
            summary["budget_actions_queued"] += 1
            logger.info(f"[daily] Queued {action_type} on {entity_name} ({entity_level})")

    # ── Summary notification ──────────────────────────────────
    total_changes = (
        summary["ads_paused"] +
        summary["ads_pause_queued"] +
        summary["ads_created"] +
        summary["budget_actions_taken"] +
        summary["budget_actions_queued"]
    )

    if total_changes > 0:
        if approval_mode == "auto":
            _create_notification(
                user_id, project_id, "success",
                "We updated your ads",
                (
                    f"Paused {summary['ads_paused']} underperformers, "
                    f"launched {summary['ads_created']} new creatives, "
                    f"took {summary['budget_actions_taken']} budget actions."
                ),
                "/autopilot",
            )
        else:
            queued = summary["ads_pause_queued"] + summary["budget_actions_queued"]
            _create_notification(
                user_id, project_id, "action",
                f"{queued} autopilot action{'s' if queued != 1 else ''} need your approval",
                (
                    f"We think you should switch off {summary['ads_pause_queued']} ad(s) and "
                    f"{summary['budget_actions_queued']} budget change(s). "
                    f"Approve them in Autopilot to apply (replacements are created on approval)."
                ),
                "/autopilot",
            )

    logger.info(
        f"[daily] {project_id} — paused={summary['ads_paused']}, "
        f"pause_queued={summary['ads_pause_queued']}, new_ads={summary['ads_created']}, "
        f"budget_taken={summary['budget_actions_taken']}, "
        f"budget_queued={summary['budget_actions_queued']}"
    )
    return summary


def _generate_replacement(
    project: dict,
    project_id: str,
    user_id: str,
    adset_id: str,
    summary: dict,
) -> Optional[str]:
    """
    Generate a fresh replacement creative. Returns image_url or None.

    (Competitor-image iteration is not used here: competitor_ads stores an
    ad-library snapshot page link, not a usable image URL.)
    """
    try:
        from creative_engine import NanaBanana
        banana = NanaBanana(get_db())

        image_url, ad_text = banana.generate_fresh(
            project,
            "Fresh high-converting creative optimized for performance. Bold hook, clear CTA.",
            project.get("business_name", ""),
        )

        if image_url:
            db = get_db()
            db.table("creative_generations").insert({
                "project_id": project_id,
                "user_id": user_id,
                "mode": "fresh",
                "prompt": f"Autopilot replacement for adset {adset_id}",
                "image_url": image_url,
                "is_saved": True,
                "metadata": {"text": ad_text, "autopilot": True, "adset_id": adset_id},
            }).execute()
            summary["creatives_generated"] += 1
            logger.info(f"[daily] Replacement creative generated: {image_url[:60]}")
            return image_url

    except Exception as e:
        logger.warning(f"[daily] Creative generation failed (non-fatal): {e}")
        summary["errors"].append(f"Creative gen: {e}")

    return None


def _upload_and_create_ad(
    meta: MetaClient,
    project: dict,
    project_id: str,
    user_id: str,
    adset_id: str,
    image_url: str,
    original_ad_name: str,
    summary: dict,
) -> None:
    """Upload image to Meta and create a new ad in the given adset."""
    try:
        business_name = project.get("business_name") or "Ad"
        store_url = project.get("store_url") or project.get("website") or ""
        timestamp_label = datetime.now(timezone.utc).strftime("%m/%d")

        # Every autopilot replacement ad on every account read "Shop {business}
        # now" with a SHOP_NOW button, including on plumbers and body shops.
        shape = campaign_shape(project.get("goal"))

        image_hash = meta.upload_image_from_url(image_url, f"autopilot_{int(datetime.now(timezone.utc).timestamp())}.jpg")

        creative_id = meta.create_ad_creative(
            name=f"Autopilot Creative — {timestamp_label}",
            image_hash=image_hash,
            link=store_url,
            message=shape["message"].format(business=business_name),
            headline=business_name,
            description=None,
            cta_type=shape["cta_type"],
        )

        ad_name = f"Replacement {timestamp_label}"
        # ⚠️ The replacement is created PAUSED and queued for the customer.
        #
        # It used to be created ACTIVE, on the reasoning that a paused
        # replacement never serves. That is true, and it is also the single
        # place where the engine spent real money on a creative no human had
        # ever seen: an AI-generated image went live in a running ad set the
        # moment a loser was paused. The customer now sees the image and
        # approves it, which is the same rule every other money-moving action
        # follows. Approving flips it to ACTIVE (see autopilot_engine).
        ad_id = meta.create_ad(name=ad_name, adset_id=adset_id, creative_id=creative_id, status="PAUSED")

        summary["ads_created"] += 1
        _save_action(
            project_id, user_id, "activate_ad", ad_id, ad_name, "ad",
            "replacement", None, None,
            (
                f"I made a new ad to replace '{original_ad_name}', which was not "
                f"working. It is switched off until you approve it. Have a look at "
                f"the image and the wording, then turn it on if you are happy."
            ),
            "pending",
        )
        logger.info(f"[daily] Replacement ad {ad_id} created PAUSED in adset {adset_id}, awaiting approval")

    except Exception as e:
        # ⚠️ Not silent. A replacement that was never created leaves the ad set
        # one ad lighter with nothing anywhere saying so, which reads as a
        # working autopilot quietly doing nothing.
        logger.error(f"[daily] Replacement ad creation FAILED for adset {adset_id}: {e}")
        summary["errors"].append(f"Create ad: {e}")
        _create_notification(
            user_id, project_id, "warning",
            "A replacement ad could not be created",
            (
                f"We paused an ad that was not working and tried to put a new one "
                f"in its place, but the new ad could not be created. Nothing is "
                f"broken and no money was spent on it. Reason: {e}"
            ),
        )


def _resolve_budget_target(
    meta: MetaClient,
    entity_id: str,
    entity_name: str,
    reasoning: str,
    adset_to_campaign: dict,
) -> tuple[str, str, str, str]:
    """
    Decide which entity a budget change should target.

    If an ad set has its own daily budget (ABO) the action stays on the ad set.
    If it has none (CBO — budget lives on the campaign), redirect to the parent
    campaign so the change actually takes effect instead of zeroing the ad set.

    Returns (entity_id, entity_level, entity_name, reasoning).
    """
    try:
        info = meta.get_budget_info(entity_id)
    except Exception as e:
        logger.warning(f"[daily] Could not read budget for ad set {entity_id}: {e}")
        return entity_id, "adset", entity_name, reasoning

    # ABO with a daily budget → scale the ad set directly.
    if info["daily"] > 0:
        return entity_id, "adset", entity_name, reasoning
    # ABO with a lifetime budget → not a CBO. Daily scaling does not apply; leave it
    # on the ad set and let _execute_budget_action's $0-daily guard refuse, rather
    # than wrongly redirecting a lifetime-budget ad set to the campaign.
    if info["lifetime"] > 0:
        logger.info(f"[daily] Ad set {entity_id} uses a lifetime budget — skipping daily-budget change")
        return entity_id, "adset", entity_name, reasoning

    campaign_id = adset_to_campaign.get(entity_id)
    if campaign_id:
        logger.info(
            f"[daily] Ad set {entity_id} has no own budget (CBO) — "
            f"redirecting budget action to campaign {campaign_id}"
        )
        return (
            campaign_id,
            "campaign",
            f"{entity_name} (campaign budget)",
            reasoning + " [applied at campaign level — campaign uses CBO]",
        )

    # No parent mapping available — leave on the ad set; the guard in
    # _execute_budget_action will refuse a $0 budget rather than do damage.
    return entity_id, "adset", entity_name, reasoning


def _execute_budget_action(
    meta: MetaClient,
    entity_id: str,
    entity_name: str,
    entity_level: str,
    action_type: str,
    pct: float,
    project_id: str,
    user_id: str,
    reasoning: str,
    summary: dict,
    max_daily_budget: Optional[float] = None,
    max_account_daily: Optional[float] = None,
    account_budgets: Optional[list[dict]] = None,
) -> None:
    """Execute a budget action immediately (auto mode), enforcing the spend ceilings."""
    value_before = None
    value_after = None
    exec_status = "executed"
    error_detail = None

    try:
        if action_type in ("scale_budget", "reduce_budget"):
            current = meta.get_entity_budget(entity_id)
            # A zero budget here means the entity carries no own budget (e.g. an ad
            # set under a CBO campaign that was not redirected). Scaling $0 would set
            # spend to $0 and effectively kill it — refuse rather than do damage.
            if current <= 0:
                raise MetaAPIError(
                    "Ad set has no own daily budget (campaign uses CBO) — "
                    "budget must be changed at the campaign level"
                )
            direction = "increase" if action_type == "scale_budget" else "decrease"

            if action_type == "scale_budget":
                # ⚠️ pct is clamped by the caller to MAX_SCALE_PCT, but clamp
                # again here: this function is also reachable from the approval
                # path, where the percentage arrives from a stored row.
                new = current * (1 + min(pct, MAX_SCALE_PCT) / 100)
                # Never exceed the configured per-entity ceiling.
                if max_daily_budget and new > max_daily_budget:
                    logger.info(
                        f"[daily] Capping scale on {entity_name}: "
                        f"{new:.2f} → max {max_daily_budget:.2f}"
                    )
                    new = max_daily_budget

                # Then the ceiling that the customer actually meant: the whole
                # account, not this one entity.
                if account_budgets is not None and max_account_daily:
                    blocked = guards.account_cap_block(
                        account_budgets, entity_id, current, new, max_account_daily
                    )
                    if blocked:
                        raise guards.Blocked(blocked)
            else:
                new = current * (1 - pct / 100)

            if DRY_RUN:
                logger.info(
                    f"[daily] DRY RUN would {action_type} {entity_name} "
                    f"({entity_id}): {current:.2f} -> {new:.2f}"
                )
                written = new
            else:
                written = meta.set_budget(entity_id, new)
                guards.record_budget_change(project_id, entity_id, entity_level, direction)

            # ⚠️ Keep the account snapshot CURRENT, or the cap is measured against
            # stale numbers for the rest of the run.
            #
            # account_budgets is read once before the loop and up to three budget
            # actions are taken against it. Without this, each increase is checked
            # against the pre-run totals, so three raises that each individually
            # fit under max_account_daily_usd collectively sail past it. The cap
            # is the number the customer was promised we would never exceed, so it
            # has to hold across the whole run, not per action.
            if account_budgets is not None:
                for row in account_budgets:
                    if row.get("id") == entity_id:
                        row["daily_budget"] = written
                        break
                else:
                    account_budgets.append({"id": entity_id, "daily_budget": written})

            value_before, value_after = round(current, 2), round(written, 2)
        elif action_type == "pause":
            if DRY_RUN:
                logger.info(f"[daily] DRY RUN would pause {entity_name} ({entity_id})")
            else:
                meta.pause(entity_id)

        summary["budget_actions_taken"] += 1
        logger.info(f"[daily] {action_type} on {entity_name}: {value_before} → {value_after}")
    except guards.Blocked as e:
        # A guard refusing is a normal outcome, not a failure. Recorded as
        # 'rejected' so it shows in the history as a decision that was
        # deliberately not taken, with the reason the customer can read.
        logger.info(f"[daily] Budget action {action_type} on {entity_name} blocked: {e}")
        _save_action(
            project_id, user_id, action_type, entity_id, entity_name,
            entity_level, "budget", None, None, f"{reasoning} Not done: {e}",
            "rejected",
        )
        return
    except Exception as e:
        exec_status = "failed"
        error_detail = str(e)
        logger.error(f"[daily] Budget action {action_type} failed: {e}")
        summary["errors"].append(f"{action_type} {entity_name}: {e}")

    _save_action(
        project_id, user_id, action_type, entity_id, entity_name,
        entity_level, "roas", value_before, value_after, reasoning, exec_status,
        executed_at=datetime.now(timezone.utc).isoformat() if exec_status == "executed" else None,
        error_detail=error_detail,
    )


# ─────────────────────────────────────────────────────────────
# All-projects runner (scheduler entry point)
# ─────────────────────────────────────────────────────────────

def run_full_daily_all_projects() -> dict[str, dict]:
    """
    Cron entry point: runs full daily optimization for every enabled project.
    Also runs bootstrap for any project that hasn't been bootstrapped yet.
    """
    db = get_db()
    resp = db.table("autopilot_settings").select("project_id, bootstrapped").eq("enabled", True).execute()
    results: dict[str, dict] = {}

    for row in (resp.data or []):
        pid = row["project_id"]
        try:
            results[pid] = run_full_daily(pid)
        except Exception as e:
            logger.error(f"[full_autopilot] Failed for project {pid}: {e}")
            results[pid] = {"project_id": pid, "error": str(e)}

    return results
