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

API_SECRET = os.environ.get("API_SECRET", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched.start()
    yield
    sched.stop()


app = FastAPI(title="Adstra Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.adstra.live", "http://localhost:3001"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


def verify_secret(x_api_secret: str = Header(...)):
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/rules/{project_id}/run", dependencies=[Depends(verify_secret)])
def run_rules(project_id: str):
    """Manually trigger rules for a project. Called from the frontend."""
    actions = run_for_project(project_id)
    return {"actions_taken": len(actions), "actions": actions}


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
