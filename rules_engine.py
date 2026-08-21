"""
Adstra Rules Engine — evaluates user-defined rules against live Meta data.
Ported and generalized from Blueprint Ads Manager (rules_engine.py).
"""
import logging
from datetime import datetime, timezone
from typing import Any

import guards
from crypto import token_for_project
from db import get_db
from meta_client import MetaClient

logger = logging.getLogger(__name__)

METRIC_KEYS = {"roas", "cpa", "cpm", "spend", "ctr", "frequency"}


def _meets_condition(value: float, op: str, threshold: float) -> bool:
    if op == "less_than":
        return value < threshold
    if op == "greater_than":
        return value > threshold
    if op == "equals":
        return abs(value - threshold) < 0.001
    return False


def _hours_since(iso_str: str) -> float | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def run_for_project(project_id: str) -> list[dict]:
    """
    Load all active rules for a project, fetch Meta data, evaluate conditions,
    execute actions, log results. Returns list of action dicts.
    """
    db = get_db()

    # Load project (need meta token + account id)
    proj_resp = db.table("projects").select(
        # ⚠️ user_id is load-bearing and was NOT in this list, so
        # project.get("user_id") was always None and every queued row landed with
        # a null owner. RLS on autopilot_actions is `auth.uid() = user_id`, so a
        # legacy profile-owned project's customer could never see the decision
        # while the rules engine re-queued it every six hours. Reveal tenants are
        # unaffected (they have no auth user by design) but the column still has
        # to be selected to be read. ad_credential_id is the same trap: this is
        # the ONLY project loader in the codebase using a named list rather than
        # select("*"), so a column added to the resolution path has to be added
        # here too or the six-hourly rules engine silently sees every
        # agency-credentialled project as disconnected while the daily loop works.
        "id,user_id,meta_access_token,meta_access_token_enc,ad_credential_id,"
        "ad_account_id,meta_connected,target_roas,target_cpa,daily_budget_cap,goal"
    ).eq("id", project_id).maybe_single().execute()

    project = proj_resp.data if proj_resp else None
    token = token_for_project(project) if project else None
    if not project or not project.get("meta_connected") or not token:
        logger.info(f"Project {project_id} not Meta-connected, skipping.")
        return []

    # Load active rules
    rules_resp = db.table("rules").select("*").eq("project_id", project_id).eq("status", "active").execute()
    rules = rules_resp.data or []
    if not rules:
        return []

    # Same settings row the autopilot reads, so both engines obey one answer to
    # "does this customer want to be asked first".
    settings_resp = (
        db.table("autopilot_settings")
        .select("approval_mode, paused_at")
        .eq("project_id", project_id)
        .maybe_single()
        .execute()
    )
    settings = (settings_resp.data if settings_resp else None) or {}
    approval_mode = settings.get("approval_mode") or "manual"

    # Stop switches apply here too. This engine runs every six hours, four times
    # more often than the autopilot, so a kill switch it did not honour would be
    # the first thing to keep spending after someone pulled the handle.
    stopped = guards.killed() or guards.project_paused(settings)
    if stopped:
        logger.warning(f"[rules] {project_id} halted: {stopped}")
        return []

    meta = MetaClient(token, project["ad_account_id"])
    goal = project.get("goal") or "lead"
    actions_taken = []

    # Group rules by (level, window_days) to minimize Meta API calls
    # For each unique (level, window) combo, fetch insights once
    cache: dict[tuple, list[dict]] = {}

    def get_insights(level: str, window_days: int) -> list[dict]:
        key = (level, window_days)
        if key not in cache:
            try:
                cache[key] = meta.get_insights(level, window_days, goal=goal)
            except Exception as e:
                logger.error(f"Meta insights fetch failed for {level}/{window_days}d: {e}")
                cache[key] = []
        return cache[key]

    for rule in rules:
        conditions: list[dict] = rule.get("conditions") or []
        if not conditions:
            continue

        level: str = rule.get("level", "adset")
        action: str = rule.get("action", "")
        action_value: float | None = rule.get("action_value")

        # Use max window across conditions for the insights fetch
        max_window = max((c.get("window_days", 3) for c in conditions), default=3)
        entities = get_insights(level, max_window)

        for entity in entities:
            # Evaluate all conditions (AND logic)
            all_met = True
            for cond in conditions:
                metric = cond.get("metric")
                op = cond.get("op")
                threshold = float(cond.get("value", 0))

                if metric not in METRIC_KEYS:
                    all_met = False
                    break

                # ⚠️ An ABSENT metric is not a zero one. This line used to read
                # `entity.get(metric, 0.0) or 0.0`, which turned "this account
                # has no ROAS because it sells no products" into "this ad earned
                # nothing", so a rule like `roas less_than 0.5` matched every ad
                # on every lead-gen account and the engine paused all of them.
                # A metric we cannot measure fails the condition instead.
                entity_value = entity.get(metric)
                if entity_value is None:
                    all_met = False
                    break

                if not _meets_condition(float(entity_value), op, threshold):
                    all_met = False
                    break

            if not all_met:
                continue

            # Check 24h budget cooldown for budget-changing actions
            if action in ("scale_budget", "reduce_budget"):
                cooldown_resp = db.table("budget_changes") \
                    .select("changed_at") \
                    .eq("project_id", project_id) \
                    .eq("adset_id", entity["id"]) \
                    .order("changed_at", desc=True) \
                    .limit(1) \
                    .execute()
                last_change = (cooldown_resp.data or [{}])[0].get("changed_at")
                hours_since = _hours_since(last_change)
                if hours_since is not None and hours_since < 24:
                    logger.info(f"Skipping {entity['id']} — budget changed {hours_since:.1f}h ago")
                    continue

            # ⚠️ Money-moving rule actions go to the SAME approval queue the
            # autopilot uses. This engine used to execute pauses and budget
            # changes outright, every six hours, with no approval step of any
            # kind, while the autopilot beside it queued the identical action for
            # the customer. So the customer-approves promise held for one code
            # path and not the other, and the faster one won.
            #
            # send_alert is exempt: it moves no money and queueing a notification
            # behind an approval is just a notification nobody sees.
            if action in ("pause", "scale_budget", "reduce_budget") and approval_mode != "auto":
                _queue_action(db, meta, project_id, project.get("user_id"), rule, entity, level, action, action_value)
                actions_taken.append({
                    "rule": rule["name"],
                    "action": action,
                    "target": entity.get("name"),
                    "status": "pending",
                })
                continue

            # Execute action
            result = _execute_action(meta, db, project_id, rule, entity, action, action_value)
            if result:
                actions_taken.append(result)
                # Update rule trigger count + last_triggered
                db.table("rules").update({
                    "trigger_count": rule.get("trigger_count", 0) + 1,
                    "last_triggered_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", rule["id"]).execute()

    return actions_taken


def _queue_action(
    db: Any,
    meta: MetaClient,
    project_id: str,
    user_id: str | None,
    rule: dict,
    entity: dict,
    level: str,
    action: str,
    action_value: float | None,
) -> None:
    """
    Write a rule's proposed action to the shared approval queue.

    Same table and same shape as the autopilot's own queued actions, so the
    portal renders both through one path and the customer never learns that two
    different engines exist inside their ad manager.

    The reasoning is written in plain language for the same reason: it is shown
    verbatim on the decision card the customer taps.
    """
    entity_id = entity["id"]
    name = entity.get("name") or "this ad"
    spend = float(entity.get("spend") or 0)

    # ⚠️ DO NOT QUEUE A DUPLICATE.
    #
    # This ran every six hours with no check for an existing pending row, and the
    # 24h budget cooldown above it only sees EXECUTED changes, never queued ones.
    # Pause had no cooldown at all. So one rule matching one ad produced four
    # identical decision cards a day until the customer acted, and /api/portal/ads
    # pulls fifty of them: the "Waiting for you" list became the same sentence
    # over and over and the page announced "There are 50 things waiting for you
    # to decide."
    try:
        existing = (
            db.table("autopilot_actions")
            .select("id")
            .eq("project_id", project_id)
            .eq("entity_id", entity_id)
            .eq("action_type", action)
            .eq("status", "pending")
            .limit(1)
            .execute()
        )
        if existing.data:
            logger.info(f"[rules] {action} on {entity_id} already waiting for approval, not queueing again")
            return
    except Exception as e:
        # Fail CLOSED on the dedupe read. Queueing anyway risks the fifty-card
        # pile-up this guard exists to prevent, and a missed decision resurfaces
        # on the next run six hours later.
        logger.warning(f"[rules] Could not check for an existing pending {action} on {entity_id}: {e}")
        return

    # ⚠️ The customer is being asked to approve a MONEY change, so the card has to
    # carry the money. value_before/value_after were both None, so the portal's
    # describeDecision dropped the "from $40 to $48 a day" clause entirely and the
    # owner approved "Spend a bit more on Summer promo" with no figure anywhere on
    # screen. Read the budget now rather than at approval time, because the number
    # they agree to should be the number they were shown.
    value_before: float | None = None
    value_after: float | None = None
    if action in ("scale_budget", "reduce_budget"):
        try:
            current = meta.get_entity_budget(entity_id)
            if current > 0:
                pct = float(action_value or (20 if action == "scale_budget" else 25))
                value_before = round(current, 2)
                value_after = round(
                    current * (1 + pct / 100) if action == "scale_budget" else current * (1 - pct / 100),
                    2,
                )
        except Exception as e:
            # A card with no figure is worse than none, but refusing to queue a
            # real finding because we could not read a budget is worse still.
            logger.warning(f"[rules] Could not read budget for {entity_id} while queueing: {e}")

    if action == "pause":
        why = f"\"{name}\" has spent ${spend:,.0f} and is not performing, so I want to switch it off."
    elif action == "scale_budget":
        why = f"\"{name}\" is doing well, so I want to put a bit more behind it."
    else:
        why = f"\"{name}\" is costing more than it is bringing in, so I want to spend less on it."

    try:
        db.table("autopilot_actions").insert({
            "project_id": project_id,
            "user_id": user_id,
            "action_type": action,
            "entity_id": entity_id,
            "entity_name": name,
            # ⚠️ The rule's REAL level, not a hardcoded 'adset'. Stamping every
            # row 'adset' meant execute_approved_action's `entity_level == "ad"`
            # test never fired for a rules-engine pause, so an ad paused by a
            # rule silently never got the replacement an identical autopilot
            # pause would have built.
            "entity_level": level,
            "metric": "rule",
            "value_before": value_before,
            "value_after": value_after,
            "reasoning": f"{why} (Rule: {rule.get('name')})",
            "status": "pending",
        }).execute()
    except Exception as e:
        logger.error(f"[rules] Could not queue {action} on {entity_id}: {e}")


def _execute_action(
    meta: MetaClient,
    db: Any,
    project_id: str,
    rule: dict,
    entity: dict,
    action: str,
    action_value: float | None,
) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    log = {
        "project_id": project_id,
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "action": action,
        "target_id": entity["id"],
        "target_name": entity["name"],
        "roas": entity.get("roas"),
        "spend": entity.get("spend"),
        "purchases": entity.get("purchases"),
        "triggered_at": now,
    }

    try:
        if action == "pause":
            meta.pause(entity["id"])

        elif action == "scale_budget":
            pct = float(action_value or 20)
            old, new = meta.scale_budget(entity["id"], pct)
            log["metric"] = "Budget"
            log["value_before"] = round(old, 2)
            log["value_after"] = round(new, 2)
            db.table("budget_changes").insert({
                "project_id": project_id,
                "adset_id": entity["id"],
                "changed_at": now,
            }).execute()

        elif action == "reduce_budget":
            pct = float(action_value or 25)
            old, new = meta.reduce_budget(entity["id"], pct)
            log["metric"] = "Budget"
            log["value_before"] = round(old, 2)
            log["value_after"] = round(new, 2)
            db.table("budget_changes").insert({
                "project_id": project_id,
                "adset_id": entity["id"],
                "changed_at": now,
            }).execute()

        elif action == "send_alert":
            pass  # Just log it — no Meta call needed

        elif action == "duplicate":
            pass  # Phase 3 v2 — skip for now

        db.table("rule_action_log").insert(log).execute()
        logger.info(f"Rule '{rule['name']}' fired on '{entity['name']}' → {action}")
        return log

    except Exception as e:
        logger.error(f"Action {action} failed on {entity['id']}: {e}")
        return None


def run_all_projects() -> dict[str, list[dict]]:
    """Run rules for every Meta-connected project. Called by scheduler."""
    db = get_db()
    resp = db.table("projects").select("id").eq("meta_connected", True).execute()
    results = {}
    for row in (resp.data or []):
        pid = row["id"]
        try:
            results[pid] = run_for_project(pid)
        except Exception as e:
            logger.error(f"Rules run failed for project {pid}: {e}")
            results[pid] = []
    return results
