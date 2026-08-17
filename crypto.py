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
        key = bytes.fromhex(raw) if _HEX_KEY.match(raw) else base64.b64decode(raw)
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


def token_for(project: dict) -> Optional[str]:
    """
    The usable Meta token for a project row, encrypted first.

    Falls back to the legacy plaintext column ONLY when there is no encrypted
    value, so an account connected before 2026-08-16 keeps working until it
    reconnects. A row carrying both is treated as encrypted: the callback nulls
    the plaintext column on every reconnect, so a leftover value there is stale
    by definition.

    Returns None rather than raising when nothing is stored, because "not
    connected yet" is an ordinary state that every caller already handles. A
    decrypt that FAILS does raise, because silently treating a corrupt token as
    "not connected" would show the customer a setup screen forever with no
    explanation.
    """
    enc = project.get("meta_access_token_enc")
    if enc:
        return decrypt_token(enc)

    legacy = project.get("meta_access_token")
    if legacy:
        logger.warning(
            "[crypto] project %s is still on a plaintext token; it will be "
            "encrypted when they next reconnect",
            project.get("id"),
        )
        return legacy

    return None
