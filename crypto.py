"""
Decrypts the Meta access token that Reveal encrypted.

⚠️ THIS MUST STAY BYTE-COMPATIBLE WITH reveal/lib/ads/meta/oauth.ts.
Same algorithm (AES-256-GCM), same layout (iv.tag.body, each base64url, joined
by dots), same key (COMMERCE_ENCRYPTION_KEY, 32 bytes as hex or base64).
Changing one side alone locks every customer out of their own ad account.

Why encrypted at all: the token can create campaigns and spend money on the
customer's ad account, and it used to sit in plaintext in
projects.meta_access_token, readable by anyone with dashboard access, while the
App Review data-handling answer claimed it was encrypted at rest.
"""

import base64
import binascii
import logging
import os
import re
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_HEX_KEY = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


class TokenCryptoError(Exception):
    """Refusing to produce a token. Never falls back to plaintext."""


def _b64url_decode(s: str) -> bytes:
    """base64url without padding, which is what Node's base64url emits."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key() -> bytes:
    raw = (os.environ.get("COMMERCE_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise TokenCryptoError(
            "COMMERCE_ENCRYPTION_KEY is not set; cannot decrypt Meta tokens"
        )
    try:
        if _HEX_KEY.match(raw):
            key = bytes.fromhex(raw)
        else:
            # ⚠️ Must accept exactly what Node's Buffer.from(raw, 'base64')
            # accepts, or the two sides disagree about the KEY and every decrypt
            # fails. Node tolerates base64url alphabet (- and _) and missing
            # padding; Python's b64decode raises on missing padding and silently
            # DISCARDS - and _ rather than translating them, which yields a
            # different, shorter key from the same string. Translate first, then
            # pad, so a rotation to a base64url value cannot lock every customer
            # out of their own ad account.
            norm = raw.replace("-", "+").replace("_", "/")
            key = base64.b64decode(norm + "=" * (-len(norm) % 4))
    except (ValueError, binascii.Error) as e:
        raise TokenCryptoError(f"COMMERCE_ENCRYPTION_KEY is not valid hex or base64: {e}")
    if len(key) != 32:
        raise TokenCryptoError(
            f"COMMERCE_ENCRYPTION_KEY must decode to 32 bytes, got {len(key)}"
        )
    return key


def decrypt_token(payload: str) -> str:
    """iv.tag.body -> the token. Raises rather than returning something wrong."""
    parts = (payload or "").split(".")
    if len(parts) != 3:
        raise TokenCryptoError("token payload is not in iv.tag.body form")
    iv_p, tag_p, body_p = parts
    try:
        iv, tag, body = _b64url_decode(iv_p), _b64url_decode(tag_p), _b64url_decode(body_p)
    except (ValueError, binascii.Error) as e:
        raise TokenCryptoError(f"token payload is not valid base64url: {e}")

    try:
        # Python's AESGCM expects the tag appended to the ciphertext, while Node
        # exposes it separately via getAuthTag(). Concatenating here is what
        # makes the two libraries agree.
        return AESGCM(_key()).decrypt(iv, body + tag, None).decode("utf-8")
    except Exception as e:
        # A failed auth tag means tampering or a rotated key, never a retry.
        raise TokenCryptoError(f"token decrypt failed: {e}")


def token_for_project(project: Optional[dict]) -> Optional[str]:
    """
    The usable Meta token for a project, including an AGENCY-level credential.

    Resolution order, additive so nothing that works today regresses:

      1. projects.meta_access_token_enc   this client's own OAuth token
      2. projects.ad_credential_id        the agency credential covering many
                                          clients at once
      3. projects.meta_access_token       legacy plaintext, pre-2026-08-16

    The client's own token wins deliberately. A business that connected its own
    ad account has given consent directly, and an agency credential arriving
    later must not quietly start acting on that consent instead.

    ⚠️ CALLERS MUST SELECT `ad_credential_id`. Two loaders in this codebase read
    a NAMED column list rather than `*` (rules_engine and the daily loop), and a
    column that is not selected reads as None with no error. That is exactly how
    user_id ended up null on every queued action for months. If the column is
    absent this falls through to the legacy branch and the project looks
    disconnected, so the failure is at least visible rather than wrong.

    Returns None when nothing is stored. Raises when something IS stored and
    cannot be decrypted, because treating a corrupt token as "not connected"
    shows the customer a setup screen forever with no explanation.
    """
    if not project:
        return None

    enc = project.get("meta_access_token_enc")
    if enc:
        return decrypt_token(enc)

    credential_id = project.get("ad_credential_id")
    if credential_id:
        # Imported here rather than at module scope so this file stays importable
        # without supabase installed, which is what lets the crypto round trip be
        # tested on its own.
        from db import get_db

        resp = (
            get_db()
            .table("ad_platform_credentials")
            .select("id,token_enc,revoked_at,expires_at")
            .eq("id", credential_id)
            .maybe_single()
            .execute()
        )
        row = resp.data if resp else None
        if not row:
            logger.error(
                "[crypto] project %s points at credential %s, which does not exist",
                project.get("id"), credential_id,
            )
        elif row.get("revoked_at"):
            # Fail loudly rather than falling through to a legacy token. A
            # revoked credential means someone deliberately cut access, and
            # quietly reaching for an older token would undo that decision.
            raise TokenCryptoError(
                f"agency credential {credential_id} was revoked at {row['revoked_at']}"
            )
        else:
            return decrypt_token(row["token_enc"])

    legacy = project.get("meta_access_token")
    if legacy:
        logger.warning(
            "[crypto] project %s is still on a plaintext token; it will be "
            "encrypted when they next reconnect",
            project.get("id"),
        )
        return legacy

    return None
