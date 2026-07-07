"""
Adstra Rules Engine — evaluates user-defined rules against live Meta data.
Ported and generalized from Blueprint Ads Manager (rules_engine.py).
"""
import logging
from datetime import datetime, timezone
from typing import Any

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
        "id,meta_access_token,ad_account_id,meta_connected,target_roas,target_cpa,daily_budget_cap"
    ).eq("id", project_id).maybe_single().execute()

    project = proj_resp.data if proj_resp else None
    if not project or not project.get("meta_connected") or not project.get("meta_access_token"):
        logger.info(f"Project {project_id} not Meta-connected, skipping.")
        return []

    # Load active rules
    rules_resp = db.table("rules").select("*").eq("project_id", project_id).eq("status", "active").execute()
    rules = rules_resp.data or []
    if not rules:
        return []

    meta = MetaClient(project["meta_access_token"], project["ad_account_id"])
    actions_taken = []

    # Group rules by (level, window_days) to minimize Meta API calls
    # For each unique (level, window) combo, fetch insights once
    cache: dict[tuple, list[dict]] = {}

    def get_insights(level: str, window_days: int) -> list[dict]:
        key = (level, window_days)
        if key not in cache:
            try:
                cache[key] = meta.get_insights(level, window_days)
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

                entity_value = entity.get(metric, 0.0) or 0.0
                if not _meets_condition(entity_value, op, threshold):
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
