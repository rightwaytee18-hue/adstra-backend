"""
Pillar 4's promise, and the two ways it silently reverts.

The deck says every creative pulls from Pillar 1. For the whole life of this
engine it did not: _build_brand_context composed the prompt from a business
name, two hex codes and a product list, and the autopilot's replacement path
passed one fixed sentence on every account.

Both failures are invisible. The ads still generate and still look good; they
are just no longer arguments aimed at a person. So the properties worth pinning
are not "does it work" but "is the buyer still in the prompt, and is the ad
still traceable back to the idea it was arguing for".

Offline by contract: no network, no key, no database.
"""

import sys
import types
import unittest


# ── Stubs, installed before the modules under test are imported ──────────────
sys.modules.setdefault("requests", types.ModuleType("requests"))

HYPOTHESES: list = []
AD_ROWS: list = []
PROJECT_ROWS: list = [{"tenant_id": "t1"}]


class _Res:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, table): self._table = table
    def select(self, *_a, **_k): return self
    def eq(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self
    def is_(self, *_a, **_k): return self

    @property
    def not_(self): return self

    def execute(self):
        if self._table == "projects":
            return _Res(list(PROJECT_ROWS))
        if self._table == "ad_hypotheses":
            return _Res(list(HYPOTHESES))
        if self._table == "ad_entities":
            return _Res(list(AD_ROWS))
        return _Res([])


class _Db:
    def table(self, name): return _Q(name)


db_stub = types.ModuleType("db")
db_stub.get_db = lambda: _Db()
sys.modules["db"] = db_stub

from creative_brief import direction_for, next_hypothesis  # noqa: E402


def _h(hid, angle="pain", brief="BRIEF", statement="s", status="live"):
    return {
        "id": hid, "angle": angle, "awareness": "problem_aware",
        "statement": statement, "brief": brief, "status": status,
    }


class DirectionTests(unittest.TestCase):
    def setUp(self):
        HYPOTHESES.clear()
        AD_ROWS.clear()
        PROJECT_ROWS[:] = [{"tenant_id": "t1"}]

    def test_no_hypothesis_falls_back_and_reports_no_evidence(self):
        """
        The null id matters as much as the fallback string. An ad built without
        evidence must be RECORDED as unevidenced, not attributed to whichever
        idea happened to be nearest, or the hypothesis ledger scores an ad that
        never argued for it.
        """
        brief, hid = direction_for("p1", "FALLBACK")
        self.assertEqual(brief, "FALLBACK")
        self.assertIsNone(hid)

    def test_live_hypothesis_supplies_the_brief(self):
        HYPOTHESES.append(_h("h1", brief="AUDIENCE: a problem aware buyer."))
        brief, hid = direction_for("p1", "FALLBACK")
        self.assertEqual(brief, "AUDIENCE: a problem aware buyer.")
        self.assertEqual(hid, "h1")

    def test_a_hypothesis_without_a_brief_is_not_used(self):
        """
        A row written before ad_hypotheses.brief existed has none. Using it
        would put an empty psychology section in the prompt, which reads to the
        model as "no constraints" rather than as "fall back to the brand ad".
        """
        HYPOTHESES.append(_h("h1", brief=""))
        HYPOTHESES.append(_h("h2", brief="   "))
        brief, hid = direction_for("p1", "FALLBACK")
        self.assertEqual(brief, "FALLBACK")
        self.assertIsNone(hid)

    def test_it_spreads_across_hypotheses_rather_than_repeating_one(self):
        """
        THE PROPERTY THE WHOLE TEST LOOP RESTS ON. "Which buyer hypothesis
        performs best" is unanswerable if the engine keeps rebuilding the first
        idea it finds, because there is never a second arm to compare.
        """
        HYPOTHESES.extend([_h("h1", angle="pain"), _h("h2", angle="fear")])
        AD_ROWS.extend([{"hypothesis_id": "h1"}, {"hypothesis_id": "h1"}])
        chosen = next_hypothesis("p1")
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen["id"], "h2", "the untested hypothesis must go next")

    def test_a_project_with_no_tenant_has_no_hypotheses(self):
        """A legacy project predating the Reveal bridge must not borrow another
        client's buyer psychology."""
        PROJECT_ROWS[:] = [{"tenant_id": None}]
        HYPOTHESES.append(_h("h1"))
        self.assertIsNone(next_hypothesis("p1"))


class PromptShapeTests(unittest.TestCase):
    """
    The brief has to reach the model, and reach it FIRST.

    A model reads the top of a prompt as the assignment and the rest as
    constraints. When the prompt opened with "premium brand aesthetic" and two
    hex codes, every ad on every account came out a handsome product shot no
    matter what the client's buyers had said.
    """

    def _prompt_for(self, brief):
        from creative_engine import NanaBanana

        captured = {}

        class Fake(NanaBanana):
            def __init__(self): pass
            # Signature mirrors the real _call, so a change to it breaks this
            # fake loudly instead of letting the test pass against a method
            # that no longer exists.
            def _call(self, contents, use_pro=False):
                captured["prompt"] = contents[0]
                return None
            def _extract_and_upload(self, *_a, **_k):
                return (None, None)

        Fake().generate_fresh(
            {"id": "p1", "business_name": "Ridgeline Roofing", "brand_colors": ["#123456"]},
            "ignored direction",
            "roof replacement",
            brief=brief,
        )
        return captured["prompt"]

    def test_the_buyer_comes_before_the_brand(self):
        brief = 'VERBATIM CUSTOMER QUOTE, DO NOT PARAPHRASE: "the roof leaked again"'
        prompt = self._prompt_for(brief)
        self.assertIn(brief, prompt)
        self.assertLess(
            prompt.index(brief),
            prompt.index("Ridgeline Roofing"),
            "the psychology must be stated before the brand identity",
        )

    def test_the_verbatim_instruction_survives_into_the_prompt(self):
        brief = 'VERBATIM CUSTOMER QUOTE, DO NOT PARAPHRASE: "we waited three weeks"'
        self.assertIn("DO NOT PARAPHRASE", self._prompt_for(brief))

    def test_without_a_brief_it_is_honestly_a_brand_ad(self):
        """No hypothesis is not an error. It is a brand ad, and it must not
        pretend to carry evidence it does not have."""
        prompt = self._prompt_for("")
        self.assertIn("Creative direction: ignored direction", prompt)
        self.assertNotIn("DO NOT PARAPHRASE", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
