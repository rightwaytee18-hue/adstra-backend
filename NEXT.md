# Next session brief — adstra-backend

_FastAPI engines for adstra-app (deployed on Railway, APScheduler crons)._

## Status (2026-06-21): full_autopilot_engine hardened

Done this pass (grounded against live Supabase `iuhabzdacuwbrprnfuqo`):
- **Bootstrap rules now insert correctly** — was writing a nonexistent `enabled`
  column + double-encoding `conditions` + using op/value vocab the rules engine
  can't read. Fixed to `status`/jsonb list/`less_than`-`greater_than`/`value`/`send_alert`.
  (DB had 0 rules — bootstrap had never actually persisted any.)
- **Replacement ads can now be created** — `run_full_daily` passes `page_id`; ad
  insights now return `adset_id`/`campaign_id` (Claude can target replacements).
- **Meta backoff/retry** added in `meta_client._request` (429 + rate-limit codes,
  exponential backoff + jitter, max 4 retries). Every Meta call is logged.
- **Observability**: raw Claude decision + parsed counts logged each daily run.
- **Budget safety**: scale capped to `max_daily_budget_usd`; refuses to scale a
  $0 (CBO) ad-set budget instead of zeroing spend.
- Fixed `daily_budget_usd`→`daily_budget_cap`, broken `_save_action` insert chain,
  removed dead competitor-image iteration (no image column exists).
- Test script: `test_autopilot.py` (health → preflight → bootstrap → daily run).
- Crons confirmed UTC (scheduler.py `timezone="UTC"`, cron hours in UTC).

### Second pass: CBO scaling + approval_mode + adversarial review
- **Campaign-level (CBO) budget scaling**: `meta_client.get_entity_budget` /
  `get_budget_info` read any entity's budget; `_resolve_budget_target` redirects an
  ad-set budget change to its parent campaign when the ad set has no own budget
  (lifetime-budget aware). Snapshot now includes `campaigns`; prompt allows
  `entity_level: "campaign"`. Campaign-level PAUSE is refused autonomously.
- **Pause flow respects `approval_mode`**: manual mode queues pauses as `pending`;
  `execute_approved_action` pauses + creates the replacement on approval (atomic
  `pending→executing` claim makes approval idempotent). Auto mode unchanged.
- **Replacement ads now launch `ACTIVE`** (a paused replacement never serves).
  ⚠️ This means autonomous spend on fresh creatives — confirm this is desired.
- Two-round adversarial review (5 lenses → verify; then fix-verification) drove:
  idempotency-gated Meta retries (create POSTs never retry → no duplicate spend),
  non-2xx/unparseable responses raise, `get_insights` pagination, `set_budget`
  round+min-clamp, deletion of dead CBO-broken legacy `run_for_project`.

Open product decisions (need owner sign-off):
1. **Replacement ads auto-activate (`ACTIVE`)** — real autonomous spend. OK?
2. In manual mode, approving an ad pause also auto-creates+activates the
   replacement. Confirm that coupling is desired.

```
You're working on Adstra Backend — the FastAPI engine powering rules execution, autopilot optimization, competitor scraping, creative generation, campaign publishing, and Meta API integrations. It runs on Railway with APScheduler (rules every 6h, autopilot daily 06:00 UTC, briefing weekly Mon 08:00 UTC). Highest-value task: verify and harden full_autopilot_engine (docs/full_autopilot_engine.py) for production — audit the bootstrap flow (3 creatives + 4 protection rules + Meta publish + bootstrapped flag), audit run_full_daily (Meta insights → Claude analysis → scale winners / pause losers / generate replacements, respecting approval_mode), add observability logging every Claude decision and Meta API call, and write a test script that triggers bootstrap + daily-run against a dev Meta ad account. Run locally with `pip install -r requirements.txt`, set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, API_SECRET, ANTHROPIC_API_KEY in .env, then `python main.py` (localhost:8000). Verify by calling POST /api/autopilot/run with the x-api-secret header + project_id. Watch Meta rate limits (add backoff/retry) and confirm all APScheduler crons use UTC.
```
