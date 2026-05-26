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
import random
from datetime import datetime, timezone
from typing import Optional

import anthropic

from db import get_db
from meta_client import MetaClient, MetaAPIError

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"

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
    resp = db.table("projects").select("*").eq("id", project_id).single().execute()
    if not resp.data:
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
    resp = db.table("autopilot_actions").insert(payload).select("id").single().execute()
    return (resp.data or {}).get("id", "")


# ─────────────────────────────────────────────────────────────
# PHASE 1: Bootstrap
# ─────────────────────────────────────────────────────────────

DEFAULT_RULES = [
    {
        "name": "🛡️ Kill: Low ROAS after spend",
        "level": "adset",
        "conditions": [
            {"metric": "roas", "op": "lt", "threshold": 0.5, "window_days": 7},
            {"metric": "spend", "op": "gt", "threshold": 50, "window_days": 7},
        ],
        "action": "pause",
        "action_value": None,
    },
    {
        "name": "🚀 Scale: High ROAS winner",
        "level": "adset",
        "conditions": [
            {"metric": "roas", "op": "gt", "threshold": 4.0, "window_days": 7},
            {"metric": "spend", "op": "gt", "threshold": 20, "window_days": 7},
        ],
        "action": "scale_budget",
        "action_value": 20,
    },
    {
        "name": "⚠️ Reduce: Negative ROAS",
        "level": "adset",
        "conditions": [
            {"metric": "roas", "op": "lt", "threshold": 1.0, "window_days": 3},
            {"metric": "spend", "op": "gt", "threshold": 30, "window_days": 3},
        ],
        "action": "reduce_budget",
        "action_value": 25,
    },
    {
        "name": "📉 Alert: High frequency",
        "level": "adset",
        "conditions": [
            {"metric": "frequency", "op": "gt", "threshold": 4.0, "window_days": 7},
        ],
        "action": "alert",
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

    user_id = project.get("user_id", "")

    # Idempotency guard — never run bootstrap twice
    settings = _load_settings(project_id)
    if settings and settings.get("bootstrapped"):
        logger.info(f"[bootstrap] Project {project_id} already bootstrapped — skipping")
        return {"ok": True, "skipped": True, "reason": "Already bootstrapped"}

    # Mark bootstrap as in-progress
    _update_settings(project_id, {"bootstrap_status": "running", "bootstrap_error": None})
    _create_notification(
        user_id, project_id, "info",
        "Adstra Autopilot is warming up 🚀",
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
                "conditions": json.dumps(rule["conditions"]),
                "action": rule["action"],
                "action_value": rule["action_value"],
                "enabled": True,
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
    elif not project.get("meta_connected") or not project.get("meta_access_token"):
        steps.append({"step": "campaign", "status": "skipped", "detail": "Meta not connected"})
        logger.warning(f"[bootstrap] Meta not connected — skipping campaign publish for {project_id}")
    else:
        logger.info(f"[bootstrap] Publishing initial campaign for {project_id}")
        try:
            from campaign_builder import publish_for_project

            business_name = project.get("business_name") or "Our Brand"
            store_url = project.get("store_url") or project.get("website") or ""

            draft = {
                "name": f"{business_name} — Autopilot Launch",
                "template_key": "sales",
                "objective": "OUTCOME_SALES",
                "countries": project.get("target_countries") or ["US"],
                "age_min": project.get("target_age_min") or 18,
                "age_max": project.get("target_age_max") or 65,
                "gender": project.get("target_gender") or "all",
                "daily_budget_cents": int((project.get("daily_budget_usd") or 50) * 100),
                "budget_mode": "cbo",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "link_url": store_url,
                "cta_type": "SHOP_NOW",
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
            "🎉 Your first campaign is live!",
            f"Adstra created 3 ad creatives, {len(DEFAULT_RULES)} protection rules, and published your first campaign. Autopilot is now running.",
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

DAILY_SYSTEM_PROMPT = """\
You are Adstra's autonomous ad optimizer. You receive ad-level performance data and project targets.

Your job: identify SPECIFIC ads to pause AND specific adsets to scale or reduce.

Return ONLY a JSON object with two keys:
{
  "ads_to_pause": [
    {"ad_id": "...", "ad_name": "...", "adset_id": "...", "reasoning": "1 sentence"}
  ],
  "budget_actions": [
    {"entity_id": "...", "entity_name": "...", "entity_level": "adset", "action": "scale_budget"|"reduce_budget"|"pause", "pct": 20, "reasoning": "1 sentence"}
  ]
}

Rules for pausing ads:
- Pause if CTR < 0.5% AND spend > $20
- Pause if ROAS < 0.5 AND spend > $30
- Only pause up to 3 ads per run

Rules for budget actions:
- Scale adset if ROAS >= scale_roas_min AND spend >= $20
- Reduce adset if ROAS <= kill_roas_max AND spend >= kill_spend_min
- Never exceed max_daily_budget_usd
- Max 3 budget actions per run

If nothing needs changing, return {"ads_to_pause": [], "budget_actions": []}
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

    user_id = project.get("user_id", "")
    token = project.get("meta_access_token")
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
    scale_pct = float(settings.get("scale_pct") or 20.0)
    max_daily_budget = float(settings.get("max_daily_budget_usd") or 500.0)
    approval_mode = settings.get("approval_mode", "manual")

    meta = MetaClient(token, account)
    summary = {
        "project_id": project_id,
        "competitor_ads_scraped": 0,
        "ads_paused": 0,
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
        ads = meta.get_insights("ad", window_days)
        adsets = meta.get_insights("adset", window_days)
    except Exception as e:
        logger.error(f"[daily] Meta insights failed: {e}")
        summary["errors"].append(f"Meta insights: {e}")
        return summary

    if not ads and not adsets:
        summary["skipped_reason"] = "No active ads or ad sets"
        return summary

    # ── Step 3: Claude analysis ───────────────────────────────
    snapshot = {
        "thresholds": {
            "scale_roas_min": scale_roas_min,
            "scale_pct": scale_pct,
            "kill_roas_max": kill_roas_max,
            "kill_spend_min": kill_spend_min,
            "max_daily_budget_usd": max_daily_budget,
        },
        "window_days": window_days,
        "ads": [
            {
                "ad_id": a.get("id"),
                "ad_name": a.get("name"),
                "adset_id": a.get("adset_id"),
                "roas": round(float(a.get("roas") or 0), 2),
                "ctr": round(float(a.get("ctr") or 0), 2),
                "spend": round(float(a.get("spend") or 0), 2),
                "status": a.get("status"),
            }
            for a in (ads or [])
        ],
        "adsets": [
            {
                "entity_id": a.get("id"),
                "entity_name": a.get("name"),
                "roas": round(float(a.get("roas") or 0), 2),
                "cpa": round(float(a.get("cpa") or 0), 2),
                "spend": round(float(a.get("spend") or 0), 2),
                "frequency": round(float(a.get("frequency") or 0), 2),
                "status": a.get("status"),
            }
            for a in (adsets or [])
        ],
    }

    try:
        client = _get_anthropic()
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(snapshot, indent=2)}],
        )
        raw = (msg.content[0].text if msg.content else "{}").strip()
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

    # ── Step 4: Pause underperforming ads ─────────────────────
    for rec in ads_to_pause:
        ad_id = rec.get("ad_id", "")
        ad_name = rec.get("ad_name", "")
        adset_id = rec.get("adset_id", "")
        reasoning = rec.get("reasoning", "")

        if not ad_id:
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

        # ── Step 5: Generate replacement creative ──────────────
        if not adset_id:
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

        if approval_mode == "auto":
            _execute_budget_action(meta, entity_id, entity_name, entity_level,
                                   action_type, pct, project_id, user_id, reasoning, summary)
        else:
            # Queue for user approval
            _save_action(
                project_id, user_id, action_type, entity_id, entity_name,
                entity_level, "roas", None, None, reasoning, "pending",
            )
            summary["budget_actions_queued"] += 1
            logger.info(f"[daily] Queued {action_type} on {entity_name}")

    # ── Summary notification ──────────────────────────────────
    total_changes = (
        summary["ads_paused"] +
        summary["ads_created"] +
        summary["budget_actions_taken"] +
        summary["budget_actions_queued"]
    )

    if total_changes > 0:
        if approval_mode == "auto":
            _create_notification(
                user_id, project_id, "success",
                f"Autopilot optimized your campaigns ✅",
                (
                    f"Paused {summary['ads_paused']} underperformers, "
                    f"launched {summary['ads_created']} new creatives, "
                    f"took {summary['budget_actions_taken']} budget actions."
                ),
                "/autopilot",
            )
        else:
            queued = summary["budget_actions_queued"]
            _create_notification(
                user_id, project_id, "action",
                f"{queued} budget action{'s' if queued != 1 else ''} need your approval",
                (
                    f"Adstra paused {summary['ads_paused']} ads and "
                    f"launched {summary['ads_created']} new creatives automatically. "
                    f"Review budget decisions in Autopilot."
                ),
                "/autopilot",
            )

    logger.info(
        f"[daily] {project_id} — paused={summary['ads_paused']}, "
        f"new_ads={summary['ads_created']}, budget_taken={summary['budget_actions_taken']}, "
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
    Generate a replacement creative. Tries competitor iteration first,
    falls back to fresh generation. Returns image_url or None.
    """
    try:
        from creative_engine import NanaBanana
        banana = NanaBanana(get_db())

        # Try to grab a competitor ad URL for iteration
        competitor_url = _get_competitor_ad_url(project_id)

        if competitor_url:
            image_url, ad_text = banana.iterate_competitor(
                project,
                competitor_url,
                "Create a higher-converting version for our brand",
            )
            mode = "iterate"
        else:
            image_url, ad_text = banana.generate_fresh(
                project,
                "Fresh high-converting creative optimized for performance. Bold hook, clear CTA.",
                project.get("business_name", ""),
            )
            mode = "fresh"

        if image_url:
            db = get_db()
            db.table("creative_generations").insert({
                "project_id": project_id,
                "user_id": user_id,
                "mode": mode,
                "prompt": f"Autopilot replacement for adset {adset_id}",
                "image_url": image_url,
                "is_saved": True,
                "metadata": {"text": ad_text, "autopilot": True, "adset_id": adset_id},
            }).execute()
            summary["creatives_generated"] += 1
            logger.info(f"[daily] Replacement creative generated ({mode}): {image_url[:60]}")
            return image_url

    except Exception as e:
        logger.warning(f"[daily] Creative generation failed (non-fatal): {e}")
        summary["errors"].append(f"Creative gen: {e}")

    return None


def _get_competitor_ad_url(project_id: str) -> Optional[str]:
    """Get a recent competitor ad image URL to iterate on."""
    try:
        db = get_db()
        resp = db.table("competitor_ads") \
            .select("image_url") \
            .eq("project_id", project_id) \
            .not_.is_("image_url", "null") \
            .order("scraped_at", desc=True) \
            .limit(10) \
            .execute()
        rows = resp.data or []
        if rows:
            return random.choice(rows)["image_url"]
    except Exception:
        pass
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

        image_hash = meta.upload_image_from_url(image_url, f"autopilot_{int(datetime.now(timezone.utc).timestamp())}.jpg")

        creative_id = meta.create_ad_creative(
            name=f"Autopilot Creative — {timestamp_label}",
            image_hash=image_hash,
            link=store_url,
            message=f"Shop {business_name} now →",
            headline=business_name,
            description=None,
            cta_type="SHOP_NOW",
        )

        ad_name = f"Autopilot — Replacement {timestamp_label}"
        ad_id = meta.create_ad(name=ad_name, adset_id=adset_id, creative_id=creative_id)

        summary["ads_created"] += 1
        _save_action(
            project_id, user_id, "create_ad", ad_id, ad_name, "ad",
            "replacement", None, None,
            f"Autopilot replacement for paused ad '{original_ad_name}'",
            "executed",
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(f"[daily] New ad created: {ad_id} in adset {adset_id}")

    except Exception as e:
        logger.warning(f"[daily] Ad creation failed (non-fatal): {e}")
        summary["errors"].append(f"Create ad: {e}")


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
) -> None:
    """Execute a budget action immediately (auto mode)."""
    value_before = None
    value_after = None
    exec_status = "executed"

    try:
        if action_type == "scale_budget":
            old, new = meta.scale_budget(entity_id, pct)
            value_before, value_after = old, new
        elif action_type == "reduce_budget":
            old, new = meta.reduce_budget(entity_id, pct)
            value_before, value_after = old, new
        elif action_type == "pause":
            meta.pause(entity_id)

        summary["budget_actions_taken"] += 1
        logger.info(f"[daily] {action_type} on {entity_name}: {value_before} → {value_after}")
    except Exception as e:
        exec_status = "failed"
        logger.error(f"[daily] Budget action {action_type} failed: {e}")
        summary["errors"].append(f"{action_type} {entity_name}: {e}")

    _save_action(
        project_id, user_id, action_type, entity_id, entity_name,
        entity_level, "roas", value_before, value_after, reasoning, exec_status,
        executed_at=datetime.now(timezone.utc).isoformat() if exec_status == "executed" else None,
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
