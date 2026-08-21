"""
Which Meta token a project actually uses.

This decides whose ad account gets charged, so the order is not a preference:

  1. the client's OWN encrypted token       explicit, direct consent
  2. the AGENCY credential                  one token covering many clients
  3. the legacy plaintext column            pre-2026-08-16 accounts

A client that connected its own ad account has consented directly, and an agency
credential arriving later must not quietly start acting on that consent instead.

Runs without network or database. `db` is stubbed before import.
"""

import base64
import os
import sys
import types
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes(range(32))
os.environ["COMMERCE_ENCRYPTION_KEY"] = KEY.hex()

CREDENTIAL_ROW: dict = {}


class _Q:
    def __init__(self, store): self.store = store
    def select(self, *_a, **_k): return self
    def eq(self, *_a, **_k): return self
    def maybe_single(self): return self
    def execute(self): return types.SimpleNamespace(data=self.store.get("row"))


class _FakeDB:
    def __init__(self, store): self.store = store
    def table(self, name):
        self.store["table"] = name
        return _Q(self.store)


_db = types.ModuleType("db")
_db.get_db = lambda: _FakeDB(CREDENTIAL_ROW)
sys.modules.setdefault("db", _db)

from crypto import TokenCryptoError, token_for_project  # noqa: E402


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def encrypt(plaintext: str) -> str:
    """Produce the iv.tag.body form reveal/lib/ads/meta/oauth.ts writes."""
    iv = b"\x00" * 12
    ct = AESGCM(KEY).encrypt(iv, plaintext.encode(), None)
    body, tag = ct[:-16], ct[-16:]
    return f"{b64u(iv)}.{b64u(tag)}.{b64u(body)}"


class TokenResolution(unittest.TestCase):
    def setUp(self):
        CREDENTIAL_ROW.clear()

    def test_round_trip_matches_the_node_format(self):
        self.assertEqual(token_for_project({"meta_access_token_enc": encrypt("EAAG123")}), "EAAG123")

    def test_client_token_beats_agency_credential(self):
        CREDENTIAL_ROW["row"] = {"token_enc": encrypt("AGENCY"), "revoked_at": None}
        got = token_for_project({
            "id": "p1",
            "meta_access_token_enc": encrypt("CLIENT"),
            "ad_credential_id": "c1",
        })
        self.assertEqual(got, "CLIENT")

    def test_agency_credential_used_when_client_has_none(self):
        CREDENTIAL_ROW["row"] = {"token_enc": encrypt("AGENCY"), "revoked_at": None}
        self.assertEqual(token_for_project({"id": "p1", "ad_credential_id": "c1"}), "AGENCY")

    def test_agency_credential_beats_legacy_plaintext(self):
        """Order matters: a revoked-then-restored agency must not fall back in time."""
        CREDENTIAL_ROW["row"] = {"token_enc": encrypt("AGENCY"), "revoked_at": None}
        got = token_for_project({
            "id": "p1", "ad_credential_id": "c1", "meta_access_token": "OLDPLAINTEXT",
        })
        self.assertEqual(got, "AGENCY")

    def test_revoked_credential_raises_rather_than_falling_back(self):
        """Revoking is a deliberate cut. Reaching for an older token would undo it."""
        CREDENTIAL_ROW["row"] = {"token_enc": encrypt("AGENCY"), "revoked_at": "2026-08-20T00:00:00Z"}
        with self.assertRaises(TokenCryptoError):
            token_for_project({"id": "p1", "ad_credential_id": "c1", "meta_access_token": "OLD"})

    def test_missing_credential_row_falls_through_visibly(self):
        CREDENTIAL_ROW["row"] = None
        self.assertEqual(
            token_for_project({"id": "p1", "ad_credential_id": "gone", "meta_access_token": "OLD"}),
            "OLD",
        )

    def test_unconnected_project_is_none_not_an_error(self):
        self.assertIsNone(token_for_project({"id": "p1"}))
        self.assertIsNone(token_for_project(None))

    def test_corrupt_token_raises_rather_than_looking_disconnected(self):
        with self.assertRaises(TokenCryptoError):
            token_for_project({"id": "p1", "meta_access_token_enc": "not.a.token"})

    def test_column_not_selected_looks_disconnected_not_wrong(self):
        """
        The rules_engine trap. A named column list that omits ad_credential_id
        makes it read as absent, and the project must then look DISCONNECTED
        rather than silently resolving some other token.
        """
        CREDENTIAL_ROW["row"] = {"token_enc": encrypt("AGENCY"), "revoked_at": None}
        self.assertIsNone(token_for_project({"id": "p1"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
