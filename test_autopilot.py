#!/usr/bin/env python3
"""
End-to-end smoke test for the Full Autopilot Engine.

Triggers bootstrap + a daily optimization run against a (dev) Meta ad account by
calling the running FastAPI server's endpoints. Use this against a DEV/sandbox
Meta ad account first — the daily run can pause ads and change budgets.

Usage:
    # 1. Start the server in another terminal:  python main.py
    # 2. Run this against a project id:
    python test_autopilot.py --project-id <uuid>

    # Options:
    #   --base-url   default http://localhost:8000 (or env BASE_URL)
    #   --bootstrap-only / --run-only   run just one phase (default: both)
    #   --skip-preflight                skip the Meta preflight check

Reads API_SECRET (and optional BASE_URL / TEST_PROJECT_ID) from the environment
/ .env, matching the server's config.
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

API_SECRET = os.environ.get("API_SECRET", "")
DEFAULT_BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
DEFAULT_PROJECT_ID = os.environ.get("TEST_PROJECT_ID", "")


def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def _pretty(data) -> str:
    try:
        return json.dumps(data, indent=2, default=str)
    except (TypeError, ValueError):
        return str(data)


def _call(method: str, base_url: str, path: str, *, body: dict | None = None, timeout: int = 300) -> dict:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"x-api-secret": API_SECRET}
    print(f"\n→ {method} {url}")
    resp = requests.request(method, url, headers=headers, json=body, timeout=timeout)
    print(f"← {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    print(_pretty(data))
    if resp.status_code >= 400:
        raise SystemExit(f"Request failed ({resp.status_code}): {path}")
    return data


def check_health(base_url: str) -> None:
    _print_header("Health check")
    r = requests.get(f"{base_url.rstrip('/')}/health", timeout=10)
    print(f"← {r.status_code} {r.text}")
    if r.status_code != 200:
        raise SystemExit("Server is not healthy — is it running? (python main.py)")


def preflight(base_url: str, project_id: str) -> None:
    """Validate Meta token / account / page before doing anything destructive."""
    _print_header("Meta preflight (token, ad account, page, pixel)")
    result = _call(
        "POST", base_url, f"/campaign/{project_id}/preflight",
        body={"template_key": "sales"},
    )
    if not result.get("ok"):
        failed = [s for s in result.get("steps", []) if not s.get("ok")]
        print("\n⚠️  Preflight reported issues:")
        for s in failed:
            print(f"   - {s.get('step')}: {s.get('detail')}")
        print("Bootstrap will still create rules, but the Meta publish step may be skipped.")


def run_bootstrap(base_url: str, project_id: str) -> None:
    _print_header("Phase 1 — Bootstrap (creatives + rules + first campaign)")
    result = _call("POST", base_url, f"/autopilot/{project_id}/bootstrap")
    if result.get("skipped"):
        print(f"\nℹ️  Already bootstrapped: {result.get('reason')}")
        return
    print(
        f"\nSummary: {result.get('creatives_generated', 0)} creatives, "
        f"{result.get('rules_created', 0)} rules, "
        f"{len(result.get('errors', []))} error(s)."
    )


def run_daily(base_url: str, project_id: str) -> None:
    _print_header("Phase 2 — Daily optimization run")
    result = _call("POST", base_url, f"/autopilot/{project_id}/run")
    if result.get("skipped_reason"):
        print(f"\nℹ️  Skipped: {result['skipped_reason']}")
        return
    print(
        f"\nSummary: paused={result.get('ads_paused', 0)}, "
        f"new_ads={result.get('ads_created', 0)}, "
        f"budget_taken={result.get('budget_actions_taken', 0)}, "
        f"budget_queued={result.get('budget_actions_queued', 0)}, "
        f"errors={len(result.get('errors', []))}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the Adstra full autopilot engine.")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID, help="Project UUID (or env TEST_PROJECT_ID)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL (default http://localhost:8000)")
    parser.add_argument("--bootstrap-only", action="store_true", help="Run only the bootstrap phase")
    parser.add_argument("--run-only", action="store_true", help="Run only the daily phase")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip the Meta preflight check")
    args = parser.parse_args()

    if not args.project_id:
        sys.exit("Provide --project-id <uuid> (or set TEST_PROJECT_ID).")
    if not API_SECRET:
        print("⚠️  API_SECRET is empty — calls will only work if the server also has it unset.")

    check_health(args.base_url)
    if not args.skip_preflight:
        preflight(args.base_url, args.project_id)

    do_bootstrap = not args.run_only
    do_run = not args.bootstrap_only

    if do_bootstrap:
        run_bootstrap(args.base_url, args.project_id)
    if do_run:
        run_daily(args.base_url, args.project_id)

    _print_header("Done")
    print("Check the server logs for [bootstrap]/[daily]/[meta] lines and")
    print("inspect autopilot_actions / rules / notifications in Supabase.")


if __name__ == "__main__":
    main()
