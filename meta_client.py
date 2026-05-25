import requests
import json
import base64
from datetime import datetime, timedelta
from typing import Optional

API_VERSION = "v22.0"
BASE = f"https://graph.facebook.com/{API_VERSION}"


class MetaClient:
    def __init__(self, access_token: str, ad_account_id: str, page_id: Optional[str] = None):
        self.token = access_token
        self.account = f"act_{ad_account_id}" if not ad_account_id.startswith("act_") else ad_account_id
        self.page_id = page_id

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _get(self, endpoint: str, params: dict) -> dict:
        params["access_token"] = self.token
        r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=30)
        return r.json()

    def _post(self, endpoint: str, data: dict) -> dict:
        """POST to Graph API with full error parsing (prefers user-facing messages)."""
        data["access_token"] = self.token
        r = requests.post(f"{BASE}/{endpoint}", data=data, timeout=60)
        result = r.json()
        if "error" in result:
            err = result["error"]
            # Prefer human-readable message Meta shows to end users
            msg = err.get("error_user_msg") or err.get("message") or str(err)
            raise MetaAPIError(msg, code=err.get("code"), subcode=err.get("error_subcode"))
        return result

    # ------------------------------------------------------------------ #
    # READ helpers (rules engine)                                         #
    # ------------------------------------------------------------------ #

    def get_insights(self, level: str, window_days: int) -> list[dict]:
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

        resp = self._get(endpoint, {
            "fields": (
                "id,name,status,effective_status,"
                f"insights.time_range({time_range}){{"
                "spend,impressions,clicks,ctr,frequency,cpm,"
                "actions,action_values"
                "}}"
            ),
            "limit": 200,
        })

        results = []
        for item in resp.get("data", []):
            if item.get("effective_status") not in ("ACTIVE", "PAUSED"):
                continue
            ins = (item.get("insights", {}).get("data") or [{}])[0]
            spend = float(ins.get("spend", 0))
            impressions = int(ins.get("impressions", 0))
            clicks = int(ins.get("clicks", 0))
            ctr = float(ins.get("ctr", 0))
            cpm = float(ins.get("cpm", 0))
            frequency = float(ins.get("frequency", 0))

            purchases = 0
            revenue = 0.0
            for a in ins.get("actions", []):
                if a.get("action_type") == "purchase":
                    purchases = int(float(a.get("value", 0)))
            for a in ins.get("action_values", []):
                if a.get("action_type") == "purchase":
                    revenue = float(a.get("value", 0))

            roas = revenue / spend if spend > 0 else 0.0
            cpa = spend / purchases if purchases > 0 else 0.0

            results.append({
                "id": item["id"],
                "name": item.get("name", ""),
                "status": item.get("effective_status", ""),
                "spend": spend,
                "roas": roas,
                "cpa": cpa,
                "cpm": cpm,
                "ctr": ctr,
                "frequency": frequency,
                "purchases": purchases,
            })

        return results

    def get_adset_budget(self, adset_id: str) -> float:
        data = self._get(adset_id, {"fields": "daily_budget"})
        return float(data.get("daily_budget", 0)) / 100

    def pause(self, entity_id: str) -> dict:
        return self._post(entity_id, {"status": "PAUSED"})

    def set_budget(self, adset_id: str, new_budget_dollars: float) -> dict:
        cents = str(int(new_budget_dollars * 100))
        return self._post(adset_id, {"daily_budget": cents})

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

    def create_ad(self, name: str, adset_id: str, creative_id: str) -> str:
        """Create a PAUSED ad. Returns ad_id."""
        result = self._post(f"{self.account}/ads", {
            "name": name,
            "adset_id": adset_id,
            "creative": json.dumps({"creative_id": creative_id}),
            "status": "PAUSED",
        })
        return result["id"]


class MetaAPIError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, subcode: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
