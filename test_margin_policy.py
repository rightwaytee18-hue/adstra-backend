"""
Break-even ROAS is 1 / margin, and the engine did not know it.

Every threshold in the autopilot was a fixed number applied to every business:
scale above 3.0, kill below 0.8. These tests pin the three things that were
wrong about that, all of which move real budgets in the wrong direction:

  1. On a thin margin, "scale at 3.0" funds a campaign that is losing money.
  2. On a thin margin, "kill below 0.8" never fires until the client is six
     times underwater.
  3. On a fat margin, both numbers kill campaigns that are paying.

And one thing that must NOT change: a client who has never stated a margin keeps
exactly the behaviour that shipped. A guessed margin produces a confident
threshold that is wrong in whichever direction the guess missed.

Offline by contract: no network, no key, no database.
"""

import sys
import types
import unittest

sys.modules.setdefault("requests", types.ModuleType("requests"))

TARGETS: list = []
PROJECT_ROWS: list = [{"tenant_id": "t1"}]


class _Res:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, table): self._table = table
    def select(self, *_a, **_k): return self
    def eq(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self
    def is_(self, *_a, **_k): return self
    def execute(self):
        if self._table == "projects":
            return _Res(list(PROJECT_ROWS))
        if self._table == "revenue_targets":
            return _Res(list(TARGETS))
        return _Res([])


class _Db:
    def table(self, name): return _Q(name)


db_stub = types.ModuleType("db")
db_stub.get_db = lambda: _Db()
sys.modules["db"] = db_stub

from margin_policy import (  # noqa: E402
    breakeven_roas, roas_thresholds, scale_step_pct, target_roas, _usable,
)


def set_margin(margin_bp, keep_bp=None):
    TARGETS[:] = [{"gross_margin_bp": margin_bp, "target_contribution_bp": keep_bp}]


class ArithmeticTests(unittest.TestCase):
    def test_breakeven_is_one_over_margin(self):
        self.assertAlmostEqual(breakeven_roas(2000), 5.00, places=2)
        self.assertAlmostEqual(breakeven_roas(4000), 2.50, places=2)
        self.assertAlmostEqual(breakeven_roas(7000), 1.43, places=2)
        self.assertIsNone(breakeven_roas(None))

    def test_bounds_reject_rather_than_clamp(self):
        """A 3 typed into a percent box, and a markup mistaken for a margin,
        both land low. Believing either produces a ceiling wrong by a factor."""
        self.assertFalse(_usable(None))
        self.assertFalse(_usable(0))
        self.assertFalse(_usable(300))
        self.assertFalse(_usable(10_000))
        self.assertTrue(_usable(4000))

    def test_target_roas_keeps_what_was_asked_for(self):
        # Keeping 10 of 40 points needs 1 / 0.30.
        self.assertAlmostEqual(target_roas(4000, 1000), 3.333, places=2)
        self.assertIsNone(target_roas(4000, 4000), "keeping the whole margin needs free customers")
        self.assertIsNone(target_roas(4000, 5000), "keeping more than the margin is not a target")


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        TARGETS.clear()
        PROJECT_ROWS[:] = [{"tenant_id": "t1"}]

    def test_no_margin_keeps_the_shipped_behaviour_exactly(self):
        """THE COMPATIBILITY GUARANTEE. An unmeasured client must be no worse off
        than before, and must not be given a guessed margin."""
        TARGETS[:] = []
        scale, kill, note = roas_thresholds("p1", {})
        self.assertEqual((scale, kill), (3.0, 0.8))
        self.assertIsNone(note)

    def test_thin_margin_raises_both_thresholds(self):
        set_margin(2000)
        scale, kill, note = roas_thresholds("p1", {})
        self.assertAlmostEqual(kill, 5.0, places=2, msg="break-even on 20 points is 5.0x")
        self.assertAlmostEqual(scale, 10.0, places=2, msg="scaling needs to clear break-even twice over")
        self.assertIn("Break-even", note or "")

    def test_fat_margin_lowers_both_thresholds(self):
        set_margin(7000)
        scale, kill, _ = roas_thresholds("p1", {})
        self.assertAlmostEqual(kill, 1.43, places=2)
        self.assertLess(kill, 0.8 * 2, "a fat margin must not inherit the thin-margin kill line")
        self.assertAlmostEqual(scale, 2.86, places=2)

    def test_a_contribution_target_sets_the_scale_line(self):
        set_margin(4000, keep_bp=1000)
        scale, _, _ = roas_thresholds("p1", {})
        self.assertAlmostEqual(scale, 3.333, places=2, msg="keeping 10 of 40 points")

    def test_a_stricter_operator_setting_is_respected(self):
        set_margin(7000)  # break-even 1.43
        scale, kill, _ = roas_thresholds("p1", {"kill_roas_max": 2.0, "scale_roas_min": 6.0})
        self.assertEqual(kill, 2.0, "choosing to be stricter than the arithmetic is legitimate")
        self.assertEqual(scale, 6.0)

    def test_a_kill_line_below_breakeven_is_overruled(self):
        """The one place a stored setting is overridden. Running below break-even
        is not a risk appetite, it is a loss."""
        set_margin(2000)  # break-even 5.0
        _, kill, note = roas_thresholds("p1", {"kill_roas_max": 0.8})
        self.assertAlmostEqual(kill, 5.0, places=2)
        self.assertIn("raised", (note or "").lower())

    def test_a_project_with_no_tenant_gets_the_defaults(self):
        PROJECT_ROWS[:] = [{"tenant_id": None}]
        set_margin(2000)
        self.assertEqual(roas_thresholds("p1", {})[:2], (3.0, 0.8))


class AggressionTests(unittest.TestCase):
    def test_at_or_below_breakeven_no_increase_at_all(self):
        self.assertEqual(scale_step_pct(5.0, 2000), 0.0, "exactly break-even earns nothing")
        self.assertEqual(scale_step_pct(3.0, 2000), 0.0, "below break-even earns nothing")

    def test_twice_breakeven_earns_the_cap(self):
        self.assertEqual(scale_step_pct(10.0, 2000), 20.0)
        self.assertEqual(scale_step_pct(10.0, 2000, cap_pct=10.0), 10.0, "the cap is honoured")

    def test_between_is_proportional_not_stepped(self):
        gentle = scale_step_pct(6.0, 2000)
        bolder = scale_step_pct(8.0, 2000)
        self.assertTrue(0 < gentle < bolder < 20.0, f"expected a ramp, got {gentle} then {bolder}")

    def test_the_same_roas_reads_differently_on_two_margins(self):
        """THE WHOLE POINT. ROAS 3.0 is a loss at 20 points and a win at 70."""
        self.assertEqual(scale_step_pct(3.0, 2000), 0.0)
        self.assertGreater(scale_step_pct(3.0, 7000), 0.0)

    def test_unstated_margin_keeps_the_old_full_step(self):
        self.assertEqual(scale_step_pct(3.0, None), 20.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
