import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import scheduler as sched
from db import get_db
from rules_engine import run_for_project

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
