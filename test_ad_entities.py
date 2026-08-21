"""
The thread from an ad to a lead, and the registry that holds it.

Two properties here are easy to break and silent when broken:

  1. Meta's {{ad.id}} macros must reach the URL UNESCAPED. Run them through a
     URL encoder and they become %7B%7Bad.id%7D%7D, Meta stops recognising them,
     and every click arrives with the literal text instead of an id.

  2. created_by must survive reconciliation. An upsert writes every column it is
     given, so a nightly pass that always sent created_by would relabel every
     engine-published ad as an import, and the one field recording where an ad
     came from would mean nothing after a single night.
"""

import sys
import types
import unittest
from urllib.parse import parse_qs, urlsplit

WRITES: list = []
EXISTING: dict = {"rows": [], "raise": False}


class _Q:
    def __init__(self): self._t = None
    def select(self, *_a, **_k): return self
    def eq(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self
    def upsert(self, row, on_conflict=None):
        WRITES.append(row)
        return self
    def execute(self):
        if EXISTING["raise"]:
            raise RuntimeError("registry unreadable")
        return types.SimpleNamespace(data=EXISTING["rows"])


class _FakeDB:
    def table(self, _name): return _Q()


_db = types.ModuleType("db")
_db.get_db = lambda: _FakeDB()
sys.modules.setdefault("db", _db)

from ad_entities import (  # noqa: E402
    mint_utm_key, tagged_destination, upsert_entity, reconcile_entities, UTM_PREFIX,
)


class Minting(unittest.TestCase):
    def test_key_shape_and_uniqueness(self):
        keys = {mint_utm_key() for _ in range(500)}
        self.assertEqual(len(keys), 500)
        for k in list(keys)[:20]:
            self.assertTrue(k.startswith(UTM_PREFIX))
            body = k[len(UTM_PREFIX):]
            self.assertEqual(len(body), 10)
            # No characters that get confused when read off a screen.
            self.assertFalse(set(body) & set("aeiou01lo"))


class Destination(unittest.TestCase):
    def test_macros_are_literal_not_encoded(self):
        url = tagged_destination("https://acme.com/quote", "rv-abc")
        self.assertIn("fb_ad={{ad.id}}", url)
        self.assertIn("fb_adset={{adset.id}}", url)
        self.assertNotIn("%7B", url)

    def test_existing_query_is_preserved(self):
        url = tagged_destination("https://acme.com/l?ref=yelp&plan=pro", "rv-abc")
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["ref"], ["yelp"])
        self.assertEqual(q["plan"], ["pro"])
        self.assertEqual(q["utm_content"], ["rv-abc"])
        self.assertEqual(q["utm_source"], ["facebook"])

    def test_fragment_survives(self):
        self.assertTrue(tagged_destination("https://acme.com/p#book", "rv-abc").endswith("#book"))

    def test_campaign_slug_optional(self):
        self.assertNotIn("utm_campaign", tagged_destination("https://a.com", "rv-x"))
        self.assertIn("utm_campaign=summer", tagged_destination("https://a.com", "rv-x", "summer"))

    def test_empty_base_is_left_alone(self):
        self.assertEqual(tagged_destination("", "rv-abc"), "")


class Registry(unittest.TestCase):
    def setUp(self):
        WRITES.clear()
        EXISTING["rows"] = []
        EXISTING["raise"] = False

    def test_upsert_omits_nulls_so_reconcile_cannot_erase_a_hypothesis(self):
        upsert_entity("p1", "ad", "123", name="Ad One")
        row = WRITES[-1]
        self.assertEqual(row["meta_id"], "123")
        self.assertEqual(row["name"], "Ad One")
        self.assertNotIn("hypothesis_id", row)
        self.assertNotIn("utm_content", row)
        self.assertNotIn("created_by", row)

    def test_upsert_never_raises(self):
        EXISTING["raise"] = True
        upsert_entity("p1", "ad", "123")  # must not propagate

    def test_unknown_entities_are_labelled_import(self):
        n = reconcile_entities("p1", [{"id": "a1", "name": "Hand-made", "adset_id": "s1"}], [], [])
        self.assertEqual(n, 1)
        self.assertEqual(WRITES[-1]["created_by"], "import")
        self.assertEqual(WRITES[-1]["parent_meta_id"], "s1")

    def test_known_entities_keep_their_provenance(self):
        EXISTING["rows"] = [{"level": "ad", "meta_id": "a1"}]
        reconcile_entities("p1", [{"id": "a1", "name": "Engine ad"}], [], [])
        self.assertNotIn("created_by", WRITES[-1])
        self.assertEqual(WRITES[-1]["name"], "Engine ad")

    def test_entities_repeated_across_days_are_written_once(self):
        days = [{"id": "a1", "name": "Ad", "day": d} for d in ("2026-08-18", "2026-08-19", "2026-08-20")]
        n = reconcile_entities("p1", days, [], [])
        self.assertEqual(n, 1)
        self.assertEqual(len(WRITES), 1)

    def test_unreadable_registry_skips_rather_than_guesses(self):
        """Without the known set we cannot tell new from known, and guessing corrupts created_by."""
        EXISTING["raise"] = True
        self.assertEqual(reconcile_entities("p1", [{"id": "a1"}], [], []), 0)
        self.assertEqual(WRITES, [])

    def test_rows_without_an_id_are_skipped(self):
        self.assertEqual(reconcile_entities("p1", [{"name": "no id"}], [], []), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
