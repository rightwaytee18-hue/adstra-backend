import base64
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from conversions import derive

logger = logging.getLogger(__name__)

# Marketing API version. This is the ONLY place a version is written; anything
# that needs one imports it from here.
#
# ⚠️ Marketing API versions expire, and an expired one stops serving ad
# endpoints. This client sat on v22.0, which expired 2026-02-19, for roughly six
# months. The published schedule, so the next drift is visible rather than
# discovered in production:
#
#   v21.0  expired 2025-09-09
#   v22.0  expired 2026-02-19
#   v23.0  expired 2026-06-09
#   v24.0  expires 2026-10-06
#   v25.0  current
#
# Re-check before v25.0 ages out. Bumping is usually a one-line change, but read
# the changelog first: v25.0 removed the ability to create or update
# Advantage+ Shopping and Advantage+ App campaigns through the API entirely.
API_VERSION = "v25.0"
API_VERSION_EXPIRES = "unannounced as of 2026-08-15"
BASE = f"https://graph.facebook.com/{API_VERSION}"

# Meta Graph API error codes that signal throttling / transient failure and are
# safe to retry with backoff:
#   4   = app-level rate limit
#   17  = user request limit reached
#   32  = page-level rate limit
#   613 = custom-level rate limit
#   80000-80014 = business-use-case (ads) rate limits
RATE_LIMIT_CODES = {4, 17, 32, 341, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80014}
RATE_LIMIT_SUBCODES = {2446079, 1487742}
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 2.0

# Meta enforces a per-account minimum daily budget (≈ $1.00 for USD; higher for some
# currencies/optimization goals). We never write below this so a reduce can't round a
# budget down to ~0 and silently disable delivery.
MIN_DAILY_BUDGET_CENTS = 100


class MetaClient:
    def __init__(self, access_token: str, ad_account_id: str, page_id: Optional[str] = None):
        self.token = access_token
        self.account = f"act_{ad_account_id}" if not ad_account_id.startswith("act_") else ad_account_id
        self.page_id = page_id

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Exponential backoff with full jitter."""
        return BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)

    def _request(self, method: str, endpoint: str, payload: dict, idempotent: bool = False) -> dict:
        """
        Single Graph API call with retry/backoff on rate limits and transient errors.

        Retries happen ONLY for GET requests and explicitly idempotent writes
        (pause, set_budget, set_status). Non-idempotent POSTs — campaign/adset/ad/
        creative/image creation — are never retried, so a lost response can never
        silently create a duplicate entity that spends real money.

        Every call is logged. Meta errors AND non-2xx/unparseable responses become
        MetaAPIError; an unparseable body is never treated as success.
        """
        url = f"{BASE}/{endpoint}"
        can_retry = method == "GET" or idempotent
        attempt = 0
        while True:
            attempt += 1
            try:
                if method == "GET":
                    params = {**payload, "access_token": self.token}
                    r = requests.get(url, params=params, timeout=30)
                else:
                    data = {**payload, "access_token": self.token}
                    r = requests.post(url, data=data, timeout=60)
            except requests.RequestException as e:
                if can_retry and attempt <= MAX_RETRIES:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        f"[meta] {method} {endpoint} network error "
                        f"(attempt {attempt}/{MAX_RETRIES}): {e} — retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"[meta] {method} {endpoint} network error, giving up: {e}")
                raise MetaAPIError(f"Network error calling Meta: {e}")

            try:
                result = r.json()
            except ValueError:
                result = None

            error = result.get("error") if isinstance(result, dict) else None
            if error:
                code = error.get("code")
                subcode = error.get("error_subcode")
                msg = error.get("error_user_msg") or error.get("message") or str(error)
                retriable = (
                    r.status_code == 429
                    or code in RATE_LIMIT_CODES
                    or subcode in RATE_LIMIT_SUBCODES
                )
                if retriable and can_retry and attempt <= MAX_RETRIES:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        f"[meta] {method} {endpoint} rate-limited "
                        f"(code={code} subcode={subcode}, attempt {attempt}/{MAX_RETRIES}) — "
                        f"backing off {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"[meta] {method} {endpoint} failed (code={code} subcode={subcode}): {msg}")
                raise MetaAPIError(msg, code=code, subcode=subcode)

            # Unparseable body or non-2xx without a structured error (e.g. a 5xx HTML
            # gateway page). Never treat this as success — retry 5xx for retriable
            # calls, otherwise raise so callers don't act on empty data.
            if result is None or r.status_code >= 400:
                if r.status_code >= 500 and can_retry and attempt <= MAX_RETRIES:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        f"[meta] {method} {endpoint} http {r.status_code} "
                        f"(attempt {attempt}/{MAX_RETRIES}) — retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                snippet = (r.text or "")[:200]
                logger.error(f"[meta] {method} {endpoint} http {r.status_code}, unparseable/empty body: {snippet}")
                raise MetaAPIError(f"Meta returned HTTP {r.status_code}: {snippet}")

            logger.info(f"[meta] {method} {endpoint} ok (http {r.status_code}, attempt {attempt})")
            return result

    def _get(self, endpoint: str, params: dict) -> dict:
        return self._request("GET", endpoint, params)

    def _post(self, endpoint: str, data: dict, idempotent: bool = False) -> dict:
        return self._request("POST", endpoint, data, idempotent=idempotent)

    # ------------------------------------------------------------------ #
    # READ helpers (rules engine)                                         #
    # ------------------------------------------------------------------ #

    def get_insights(self, level: str, window_days: int, goal: str = "lead") -> list[dict]:
        """
        Insights for every entity at `level`, with results counted for `goal`.

        `goal` defaults to 'lead' rather than 'purchase' because the safe failure
        is under-reporting revenue on an ecommerce account, not reporting zero
        results forever on a service business. See conversions.py.
        """
        now = datetime.utcnow()
        since = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
        until = now.strftime("%Y-%m-%d")
        time_range = json.dumps({"since": since, "until": until})

        endpoint_map = {
            "campaign": f"{self.account}/campaigns",
            "adset": f"{self.account}/adsets",
            "ad": f"{self.account}/ads",
        }
        endpoint = endpoint_map[level]

        # Parent-entity ids are only valid fields on the endpoints that have them.
        base_fields = "id,name,status,effective_status"
        if level == "ad":
            base_fields += ",adset_id,campaign_id"
        elif level == "adset":
            base_fields += ",campaign_id"

        params = {
            "fields": (
                f"{base_fields},"
                f"insights.time_range({time_range}){{"
                "spend,impressions,clicks,ctr,frequency,cpm,"
                "actions,action_values"
                "}}"
            ),
            "limit": 200,
        }

        # Follow cursor pagination so accounts with >200 entities are not silently
        # truncated (which would hide ads from analysis and break the CBO map).
        raw_items: list[dict] = []
        pages = 0
        MAX_PAGES = 25  # safety cap → up to ~5000 entities
        while True:
            resp = self._get(endpoint, params)
            raw_items.extend(resp.get("data", []))
            pages += 1
            paging = resp.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after or pages >= MAX_PAGES:
                if paging.get("next") and pages >= MAX_PAGES:
                    logger.warning(
                        f"[meta] get_insights({level}) hit page cap ({MAX_PAGES}) — results truncated"
                    )
                break
            params = {**params, "after": after}

        results = []
        for item in raw_items:
            if item.get("effective_status") not in ("ACTIVE", "PAUSED"):
                continue
            ins = (item.get("insights", {}).get("data") or [{}])[0]
            spend = float(ins.get("spend", 0))
            impressions = int(ins.get("impressions", 0))
            clicks = int(ins.get("clicks", 0))
            ctr = float(ins.get("ctr", 0))
            cpm = float(ins.get("cpm", 0))
            frequency = float(ins.get("frequency", 0))

            # Counted per goal, and de-duplicated within each action family.
            # Previously this read the single action type "purchase", which is
            # always absent on a lead-gen account, so every service business
            # measured 0 results and 0.0 ROAS. See conversions.py.
            derived = derive(ins, goal)

            results.append({
                "id": item["id"],
                "name": item.get("name", ""),
                "status": item.get("effective_status", ""),
                "adset_id": item.get("adset_id"),
                "campaign_id": item.get("campaign_id"),
                "spend": spend,
                # None for any non-purchase goal. Callers MUST skip a None
                # rather than compare it; treating it as 0.0 is what made the
                # kill rule match every lead-gen ad.
                "roas": derived["roas"],
                "cpa": derived["cpa"],
                "cost_per_result": derived["cost_per_result"],
                "results": derived["results"],
                "revenue": derived["revenue"],
                "goal": goal,
                "cpm": cpm,
                "ctr": ctr,
                "frequency": frequency,
                # ⚠️ These were parsed into locals and then dropped on the floor.
                # snapshots._row reads them by name, so every ad_insights_daily
                # row recorded 0 impressions and 0 clicks, and the portal showed
                # zeroes for both forever.
                "impressions": impressions,
                "clicks": clicks,
                # Kept so anything still reading `purchases` keeps working. On a
                # lead goal this is the count of enquiries, not orders.
                "purchases": int(derived["results"]),
            })

        return results

    def get_adset_budget(self, adset_id: str) -> float:
        return self.get_entity_budget(adset_id)

    def get_entity_budget(self, entity_id: str) -> float:
        """
        Read the daily budget (dollars) of any budgeted entity — campaign or ad set.

        Returns 0.0 when the entity carries no own daily budget, e.g. an ad set under
        a CBO (campaign-budget-optimization) campaign, where the budget lives on the
        campaign. Callers use this to decide whether a budget change must target the
        parent campaign instead.
        """
        data = self._get(entity_id, {"fields": "daily_budget"})
        return float(data.get("daily_budget") or 0) / 100

    def get_ad_adset_id(self, ad_id: str) -> Optional[str]:
        """Return the parent ad-set id for an ad (used to place replacement ads)."""
        data = self._get(ad_id, {"fields": "adset_id"})
        return data.get("adset_id")

    def get_budget_info(self, entity_id: str) -> dict:
        """
        Return {'daily': dollars, 'lifetime': dollars} for an entity.

        Used to distinguish an ad set with its own budget (ABO, daily OR lifetime)
        from one under a CBO campaign (both zero) before redirecting a budget change.
        """
        data = self._get(entity_id, {"fields": "daily_budget,lifetime_budget"})
        return {
            "daily": float(data.get("daily_budget") or 0) / 100,
            "lifetime": float(data.get("lifetime_budget") or 0) / 100,
        }

    def pause(self, entity_id: str) -> dict:
        return self._post(entity_id, {"status": "PAUSED"}, idempotent=True)

    def set_status(self, entity_id: str, status: str) -> dict:
        """Set an entity's status (e.g. 'ACTIVE' to start a replacement ad serving)."""
        return self._post(entity_id, {"status": status}, idempotent=True)

    def set_budget(self, adset_id: str, new_budget_dollars: float) -> float:
        # Round (not truncate) to cents and clamp to Meta's minimum so a reduce can't
        # round a small budget down to ~0 and disable delivery. Returns the actual
        # dollar amount written so callers log the true value, not the pre-clamp one.
        cents = max(int(round(new_budget_dollars * 100)), MIN_DAILY_BUDGET_CENTS)
        self._post(adset_id, {"daily_budget": str(cents)}, idempotent=True)
        return cents / 100

    def scale_budget(self, adset_id: str, pct: float) -> tuple[float, float]:
        old = self.get_adset_budget(adset_id)
        new = old * (1 + pct / 100)
        self.set_budget(adset_id, new)
        return old, new

    def reduce_budget(self, adset_id: str, pct: float) -> tuple[float, float]:
        old = self.get_adset_budget(adset_id)
        new = old * (1 - pct / 100)
        self.set_budget(adset_id, new)
        return old, new

    # ------------------------------------------------------------------ #
    # VALIDATION (preflight checks before publish)                        #
    # ------------------------------------------------------------------ #

    def validate(self, pixel_id: Optional[str] = None) -> list[dict]:
        """Run preflight checks. Returns list of {step, ok, detail} dicts."""
        steps = []

        # 1. Token valid
        try:
            r = self._get("me", {"fields": "id,name"})
            steps.append({"step": "token", "ok": bool(r.get("id")), "detail": r.get("name", "")})
        except MetaAPIError as e:
            steps.append({"step": "token", "ok": False, "detail": str(e)})
            return steps  # Can't continue without a valid token

        # 2. Ad account active
        try:
            r = self._get(self.account, {"fields": "id,name,account_status"})
            active = r.get("account_status") == 1
            steps.append({
                "step": "ad_account",
                "ok": active,
                "detail": r.get("name", "") if active else f"Account status {r.get('account_status')} (1=active required)",
            })
        except MetaAPIError as e:
            steps.append({"step": "ad_account", "ok": False, "detail": str(e)})

        # 3. Facebook Page accessible
        if self.page_id:
            try:
                r = self._get(self.page_id, {"fields": "id,name"})
                steps.append({"step": "page", "ok": bool(r.get("id")), "detail": r.get("name", "")})
            except MetaAPIError as e:
                steps.append({"step": "page", "ok": False, "detail": str(e)})
        else:
            steps.append({"step": "page", "ok": False, "detail": "No Facebook Page ID set on project — add it in Setup."})

        # 4. Pixel (optional — only required for sales/leads objectives)
        if pixel_id:
            try:
                r = self._get(pixel_id, {"fields": "id,name"})
                steps.append({"step": "pixel", "ok": bool(r.get("id")), "detail": r.get("name", "")})
            except MetaAPIError as e:
                steps.append({"step": "pixel", "ok": False, "detail": str(e)})

        return steps

    # ------------------------------------------------------------------ #
    # WRITE helpers (campaign builder)                                    #
    # ------------------------------------------------------------------ #

    def create_campaign(
        self,
        name: str,
        objective: str,
        special_ad_categories: list[str],
        daily_budget_cents: Optional[int] = None,
        bid_strategy: str = "LOWEST_COST_WITHOUT_CAP",
    ) -> str:
        """Create a PAUSED campaign. Returns campaign_id."""
        data: dict = {
            "name": name,
            "objective": objective,
            "status": "PAUSED",
            "special_ad_categories": json.dumps(special_ad_categories),
        }
        # CBO: budget on campaign
        if daily_budget_cents:
            data["daily_budget"] = str(daily_budget_cents)
            data["bid_strategy"] = bid_strategy

        result = self._post(f"{self.account}/campaigns", data)
        return result["id"]

    def create_adset(
        self,
        campaign_id: str,
        name: str,
        optimization_goal: str,
        targeting: dict,
        attribution_spec: list[dict],
        promoted_object: Optional[dict] = None,
        daily_budget_cents: Optional[int] = None,   # ABO only
        bid_strategy: str = "LOWEST_COST_WITHOUT_CAP",
        bid_amount_cents: Optional[int] = None,
    ) -> str:
        """Create a PAUSED ad set. Returns adset_id."""
        data: dict = {
            "name": name,
            "campaign_id": campaign_id,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": optimization_goal,
            "targeting": json.dumps(targeting),
            "attribution_spec": json.dumps(attribution_spec),
            "status": "PAUSED",
        }
        if promoted_object:
            data["promoted_object"] = json.dumps(promoted_object)
        # ABO: budget on ad set
        if daily_budget_cents:
            data["daily_budget"] = str(daily_budget_cents)
            data["bid_strategy"] = bid_strategy
        if bid_amount_cents:
            data["bid_amount"] = str(bid_amount_cents)

        result = self._post(f"{self.account}/adsets", data)
        return result["id"]

    def upload_image_from_url(self, image_url: str, filename: str = "creative.jpg") -> str:
        """Download image from URL and upload to ad account. Returns image_hash."""
        # Download the image
        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()
        img_b64 = base64.b64encode(img_resp.content).decode("utf-8")

        # Upload to adimages
        result = self._post(f"{self.account}/adimages", {
            "bytes": img_b64,
            "name": filename,
        })
        # Response: { images: { <filename>: { hash, url } } }
        images = result.get("images", {})
        for _name, info in images.items():
            return info["hash"]
        raise MetaAPIError("Image upload returned no hash")

    def create_ad_creative(
        self,
        name: str,
        image_hash: str,
        link: str,
        message: str,
        headline: str,
        description: Optional[str] = None,
        cta_type: str = "LEARN_MORE",
    ) -> str:
        """Create an ad creative. Returns creative_id."""
        if not self.page_id:
            raise MetaAPIError("facebook_page_id is required to create ad creatives")

        link_data: dict = {
            "image_hash": image_hash,
            "link": link,
            "message": message,
            "name": headline,
            "call_to_action": {"type": cta_type},
        }
        if description:
            link_data["description"] = description

        object_story_spec = {
            "page_id": self.page_id,
            "link_data": link_data,
        }

        result = self._post(f"{self.account}/adcreatives", {
            "name": name,
            "object_story_spec": json.dumps(object_story_spec),
        })
        return result["id"]

    def create_ad(self, name: str, adset_id: str, creative_id: str, status: str = "PAUSED") -> str:
        """
        Create an ad. Returns ad_id.

        Defaults to PAUSED (used at publish, where the whole campaign launches paused).
        Autopilot passes status='ACTIVE' for replacement ads so they start serving in
        the already-live ad set instead of sitting paused.
        """
        result = self._post(f"{self.account}/ads", {
            "name": name,
            "adset_id": adset_id,
            "creative": json.dumps({"creative_id": creative_id}),
            "status": status,
        })
        return result["id"]


class MetaAPIError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, subcode: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
