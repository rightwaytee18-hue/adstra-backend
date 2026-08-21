"""
Is the agency credential still good?

One token now covers every client an agency runs, and that concentration cuts
both ways. It removes eleven OAuth dances from an operator's life, and it means a
single revoke, password change or 60-day expiry takes twelve businesses off the
air on the same morning.

Nothing else in this system would notice. The daily loop only touches projects
whose autopilot is enabled, it treats a Meta failure as non-fatal by design, and
a marketer reading a dashboard cannot tell "no spend because nothing ran" from
"no spend because the token died".

So this asks the cheapest possible question, once per agency per day:
GET /me/adaccounts?limit=1. One call. It is not measuring anything, it is
checking that the key still turns.
"""

import logging
from datetime import datetime, timezone

import requests

from crypto import TokenCryptoError, decrypt_token
from db import get_db
from meta_client import API_VERSION

logger = logging.getLogger(__name__)

GRAPH = f"https://graph.facebook.com/{API_VERSION}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_credential(cred: dict) -> dict:
    """Verify one credential. Returns the fields to write back."""
    try:
        token = decrypt_token(cred["token_enc"])
    except TokenCryptoError as e:
        # A decrypt failure is not a Meta problem. It means COMMERCE_ENCRYPTION_KEY
        # differs between the half that encrypted and the half reading, which is
        # the one failure mode that breaks EVERY credential at once, so say so
        # plainly rather than reporting it as an expired token.
        return {"last_error": f"decrypt failed (check COMMERCE_ENCRYPTION_KEY): {e}"}

    try:
        resp = requests.get(
            f"{GRAPH}/me/adaccounts",
            params={"limit": 1, "access_token": token},
            timeout=20,
        )
        body = resp.json() if resp.content else {}
    except Exception as e:
        # A network blip is not a dead token, and recording it as one would send
        # an operator to re-authorise something that was never broken. Leave
        # last_verified_at alone so the previous good check still stands.
        logger.warning("[cred-health] %s: transport error, not marking bad: %s", cred["id"], e)
        return {}

    err = body.get("error") or {}
    if err:
        return {"last_error": f"{err.get('type', 'OAuthException')}: {err.get('message', 'unknown')}"}

    return {"last_verified_at": _now(), "last_error": None}


def run_all() -> dict:
    """Check every live credential. Never raises: this is a monitor."""
    summary = {"checked": 0, "ok": 0, "failing": 0}
    try:
        resp = (
            get_db()
            .table("ad_platform_credentials")
            .select("id,agency_id,token_enc,expires_at")
            .is_("revoked_at", "null")
            .execute()
        )
        creds = resp.data or []
    except Exception as e:
        logger.error("[cred-health] could not list credentials: %s", e)
        return summary

    for cred in creds:
        summary["checked"] += 1
        updates = check_credential(cred)
        if not updates:
            continue
        if updates.get("last_error"):
            summary["failing"] += 1
            logger.error(
                "[cred-health] agency %s credential is FAILING: %s",
                cred.get("agency_id"), updates["last_error"],
            )
        else:
            summary["ok"] += 1

        updates["updated_at"] = _now()
        try:
            get_db().table("ad_platform_credentials").update(updates).eq("id", cred["id"]).execute()
        except Exception as e:
            logger.error("[cred-health] %s: write-back failed: %s", cred["id"], e)

    logger.info("[cred-health] %s", summary)
    return summary
