import requests
import json
from datetime import datetime, timedelta
from typing import Optional

API_VERSION = "v22.0"
BASE = f"https://graph.facebook.com/{API_VERSION}"


class MetaClient:
    def __init__(self, access_token: str, ad_account_id: str):
        self.token = access_token
        self.account = f"act_{ad_account_id}" if not ad_account_id.startswith("act_") else ad_account_id

    def _get(self, endpoint: str, params: dict) -> dict:
        params["access_token"] = self.token
        r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=30)
        return r.json()

    def _post(self, endpoint: str, data: dict) -> dict:
        data["access_token"] = self.token
        r = requests.post(f"{BASE}/{endpoint}", data=data, timeout=30)
        return r.json()

    def get_insights(self, level: str, window_days: int) -> list[dict]:
        """
        Fetch campaign/adset/ad metrics for the given window.
        level: 'campaign' | 'adset' | 'ad'
        Returns list of objects with id, name, status, spend, roas, cpa, cpm, ctr, frequency, purchases
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
        """Scale budget by pct% (e.g. 20 = +20%). Returns (old, new) in dollars."""
        old = self.get_adset_budget(adset_id)
        new = old * (1 + pct / 100)
        self.set_budget(adset_id, new)
        return old, new

    def reduce_budget(self, adset_id: str, pct: float) -> tuple[float, float]:
        """Reduce budget by pct% (e.g. 25 = -25%). Returns (old, new) in dollars."""
        old = self.get_adset_budget(adset_id)
        new = old * (1 - pct / 100)
        self.set_budget(adset_id, new)
        return old, new
