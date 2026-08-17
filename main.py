import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Depends, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import scheduler as sched
from db import get_db
from rules_engine import run_for_project
from competitor_engine import run_for_project as scrape_for_project
from creative_engine import generate_for_project
from campaign_builder import preflight_for_project, publish_for_project
from autopilot_engine import (
    briefing_for_project,
    execute_approved_action,
)
from full_autopilot_engine import (
    run_full_daily as autopilot_run,
    bootstrap_project,
)

API_SECRET = os.environ.get("API_SECRET", "")
if not API_SECRET:
    raise RuntimeError("API_SECRET environment variable is required — refusing to start without it")


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched.start()
    yield
    sched.stop()


app = FastAPI(title="Adstra Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # The Adstra customer app is retired. The only browser origin that talks to
    # this service now is the Reveal portal, and every portal call is proxied
    # through a Reveal server route carrying x-api-secret, so no browser hits
    # this directly at all. Kept narrow rather than opened up.
    allow_origins=["https://revealai.live", "http://localhost:3000"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


def verify_secret(x_api_secret: str = Header(...)):
    if not API_SECRET or x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────
# Rules
# ─────────────────────────────────────────────────────────────

@app.post("/rules/{project_id}/run", dependencies=[Depends(verify_secret)])
def run_rules(project_id: str):
    """Manually trigger rules for a project. Called from the frontend."""
    actions = run_for_project(project_id)
    return {"actions_taken": len(actions), "actions": actions}


@app.get("/rules/{project_id}/log", dependencies=[Depends(verify_secret)])
def get_log(project_id: str, limit: int = 50):
    """Fetch rule action log for a project."""
    db = get_db()
    resp = db.table("rule_action_log") \
        .select("*") \
        .eq("project_id", project_id) \
        .order("triggered_at", desc=True) \
        .limit(limit) \
        .execute()
    return {"log": resp.data or []}


# ─────────────────────────────────────────────────────────────
# Competitors
# ─────────────────────────────────────────────────────────────

@app.post("/competitor/{project_id}/scrape", dependencies=[Depends(verify_secret)])
def scrape_competitors(project_id: str):
    """Kick off competitor scrape in a background thread and return immediately."""
    def run():
        try:
            scrape_for_project(project_id)
        except Exception as e:
            logger.error(f"Background scrape error for {project_id}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "project_id": project_id}


# ─────────────────────────────────────────────────────────────
# Creative Studio
# ─────────────────────────────────────────────────────────────

@app.post("/creative/{project_id}/generate", dependencies=[Depends(verify_secret)])
def generate_creative(project_id: str, body: dict = Body(...)):
    """Generate an ad creative using Gemini. Modes: fresh | iterate | edit."""
    mode = body.pop("mode", None)
    if not mode:
        raise HTTPException(status_code=400, detail="mode required")
    result = generate_for_project(project_id, mode, body)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ─────────────────────────────────────────────────────────────
# Campaign Builder
# ─────────────────────────────────────────────────────────────

@app.post("/campaign/{project_id}/preflight", dependencies=[Depends(verify_secret)])
def campaign_preflight(project_id: str, body: dict = Body(...)):
    """Validate Meta credentials + account before publishing a campaign."""
    return preflight_for_project(project_id, body)


@app.post("/campaign/{project_id}/publish", dependencies=[Depends(verify_secret)])
def campaign_publish(project_id: str, body: dict = Body(...)):
    """Publish a campaign to Meta (always starts PAUSED)."""
    result = publish_for_project(project_id, body)
    if result.get("fatal"):
        raise HTTPException(status_code=500, detail=result["fatal"])
    return result


# ─────────────────────────────────────────────────────────────
# Autopilot (Phase 8)
# ─────────────────────────────────────────────────────────────

@app.post("/autopilot/{project_id}/bootstrap", dependencies=[Depends(verify_secret)])
def autopilot_bootstrap_endpoint(project_id: str):
    """
    Run the one-time bootstrap: generate creatives, create rules, publish first campaign.
    Called when user enables autopilot for the first time.
    """
    result = bootstrap_project(project_id)
    return result


@app.post("/autopilot/{project_id}/run", dependencies=[Depends(verify_secret)])
def autopilot_run_endpoint(project_id: str):
    """
    Run the full daily optimization loop on-demand.
    Bootstraps first if not yet done, then: scan competitors, analyze ads,
    pause losers, generate replacements, scale winners.
    """
    result = autopilot_run(project_id)
    return result


@app.post("/autopilot/{project_id}/briefing", dependencies=[Depends(verify_secret)])
def autopilot_briefing_endpoint(project_id: str):
    """Generate (or re-generate) the weekly AI strategy briefing."""
    result = briefing_for_project(project_id)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Briefing failed"))
    return result


@app.post("/autopilot/{project_id}/actions/{action_id}/approve", dependencies=[Depends(verify_secret)])
def approve_action(project_id: str, action_id: str):
    """Execute a pending autopilot action that the user has approved."""
    result = execute_approved_action(action_id, project_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/autopilot/{project_id}/actions/{action_id}/reject", dependencies=[Depends(verify_secret)])
def reject_action(project_id: str, action_id: str):
    """Mark a pending autopilot action as rejected (no Meta call)."""
    db = get_db()
    db.table("autopilot_actions").update({"status": "rejected"}).eq("id", action_id).eq("project_id", project_id).execute()
    return {"ok": True}
