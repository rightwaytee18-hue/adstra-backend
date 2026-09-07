"""
What shape publish_for_project actually builds on Meta.

Nothing covered this function before, and it is the one that spends money. The
specific thing under test is that segmenting by vertical produces ONE campaign
with an ad set each, not three campaigns: three campaigns split the budget three
ways in advance and ask Meta to learn each vertical from a third of the data,
which defeats the entire point of segmenting.

No network. MetaClient is replaced with a recorder, so what is asserted is the
sequence of calls the publisher would have made.
"""
import sys
import types
import unittest
from unittest.mock import patch

# ⚠️ The db module is stubbed, not the supabase package under it.
#
# ad_entities does `from db import get_db`, and db.py annotates a module-level
# `Client | None`, which this machine's system python (below 3.10) cannot even
# evaluate. Stubbing supabase therefore is not enough; the import still dies on
# the annotation. Standing in for `db` itself skips both problems, and it is
# honest for this file: nothing here touches a database. Same root cause as
# test_autopilot.py failing on a missing dotenv, which is not the runtime's
# problem either.
_db = types.ModuleType("db")
_db.get_db = lambda *a, **k: None  # type: ignore[attr-defined]
sys.modules.setdefault("db", _db)


class FakeMeta:
    """Records what the publisher asked Meta to build."""

    def __init__(self, *_a, **_k):
        self.campaigns: list[dict] = []
        self.adsets: list[dict] = []
        self.ads: list[dict] = []

    def create_campaign(self, **kw):
        self.campaigns.append(kw)
        return f"camp_{len(self.campaigns)}"

    def create_adset(self, **kw):
        self.adsets.append(kw)
        return f"adset_{len(self.adsets)}"

    def upload_image_from_url(self, *_a, **_k):
        return "imagehash"

    def create_ad_creative(self, **_kw):
        return "creative_1"

    def create_ad(self, **kw):
        self.ads.append(kw)
        return f"ad_{len(self.ads)}"


PROJECT = {
    "id": "p1", "ad_account_id": "act_1", "facebook_page_id": "page_1",
    "pixel_id": "pix_1", "website": "https://example.com",
}


def creative(n: str) -> dict:
    return {"image_url": f"https://img/{n}.jpg", "headline": n, "message": n, "description": n}


class CampaignShape(unittest.TestCase):
    def _publish(self, draft):
        import campaign_builder as cb
        fake = FakeMeta()
        with patch.object(cb, "_load_project", return_value=PROJECT), \
             patch.object(cb, "token_for_project", return_value="tok"), \
             patch.object(cb, "MetaClient", lambda *a, **k: fake), \
             patch.object(cb, "upsert_entity", lambda *a, **k: None), \
             patch.object(cb, "_save_published", lambda *a, **k: None):
            result = cb.publish_for_project("p1", draft)
        return result, fake

    def test_no_adsets_key_behaves_exactly_as_before(self):
        """Every existing caller passes creatives and no ad sets. That path must not move."""
        result, fake = self._publish({"creatives": [creative("a"), creative("b")]})
        self.assertEqual(len(fake.campaigns), 1)
        self.assertEqual(len(fake.adsets), 1, "one ad set when none were asked for")
        self.assertEqual(len(fake.ads), 2, "both creatives became ads")
        self.assertEqual(result["adset_id"], "adset_1", "the old single-value key still answers")

    def test_three_verticals_make_one_campaign_with_three_adsets(self):
        result, fake = self._publish({
            "name": "Reveal - Trades",
            "adsets": [
                {"name": "HVAC", "creatives": [creative("hvac")]},
                {"name": "Plumbing", "creatives": [creative("plumbing")]},
                {"name": "Electrical", "creatives": [creative("electrical")]},
            ],
        })
        self.assertEqual(len(fake.campaigns), 1, "ONE campaign, so the budget is shared")
        self.assertEqual(len(fake.adsets), 3)
        self.assertEqual([a["name"] for a in fake.adsets], ["HVAC", "Plumbing", "Electrical"])
        self.assertEqual(len(result["adset_ids"]), 3)
        # Every ad set is under the same campaign, which is the whole claim.
        self.assertEqual({a["campaign_id"] for a in fake.adsets}, {"camp_1"})

    def test_each_creative_lands_in_its_own_vertical(self):
        """A plumbing ad in the HVAC ad set makes every per-vertical number a lie."""
        _, fake = self._publish({
            "adsets": [
                {"name": "HVAC", "creatives": [creative("hvac")]},
                {"name": "Plumbing", "creatives": [creative("plumbing1"), creative("plumbing2")]},
            ],
        })
        by_adset: dict[str, int] = {}
        for ad in fake.ads:
            by_adset[ad["adset_id"]] = by_adset.get(ad["adset_id"], 0) + 1
        self.assertEqual(by_adset, {"adset_1": 1, "adset_2": 2})

    def test_an_existing_campaign_is_reused_not_duplicated(self):
        """Adding a fourth vertical later must not create a second campaign."""
        result, fake = self._publish({
            "campaign_id": "camp_existing",
            "adsets": [{"name": "Roofing", "creatives": [creative("roofing")]}],
        })
        self.assertEqual(len(fake.campaigns), 0, "no new campaign when one was named")
        self.assertEqual(result["campaign_id"], "camp_existing")
        self.assertEqual(fake.adsets[0]["campaign_id"], "camp_existing")

    def test_one_failing_vertical_does_not_take_the_others_down(self):
        import campaign_builder as cb

        class HalfBroken(FakeMeta):
            def create_adset(self, **kw):
                if kw.get("name") == "Plumbing":
                    raise cb.MetaAPIError("targeting rejected")
                return super().create_adset(**kw)

        fake = HalfBroken()
        with patch.object(cb, "_load_project", return_value=PROJECT), \
             patch.object(cb, "token_for_project", return_value="tok"), \
             patch.object(cb, "MetaClient", lambda *a, **k: fake), \
             patch.object(cb, "upsert_entity", lambda *a, **k: None), \
             patch.object(cb, "_save_published", lambda *a, **k: None):
            result = cb.publish_for_project("p1", {
                "adsets": [
                    {"name": "HVAC", "creatives": [creative("hvac")]},
                    {"name": "Plumbing", "creatives": [creative("plumbing")]},
                    {"name": "Electrical", "creatives": [creative("electrical")]},
                ],
            })
        self.assertEqual(len(result["adset_ids"]), 2, "the two good verticals still shipped")
        self.assertTrue(result["ok"], "a partial launch is still a launch")
        self.assertTrue(any("Plumbing" in e for e in result["errors"]), "and it says which one failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
