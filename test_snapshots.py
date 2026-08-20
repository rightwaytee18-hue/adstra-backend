"""
Regression test for the day-labelling bug in write_daily_snapshot.

The bug: get_insights asks Meta for a time_range with no time_increment, so Meta
returns ONE row per entity covering the whole trailing window. write_daily_snapshot
stamped every one of those rows with today's date, and the portal sums ~30 of them.
A 7-day window therefore reported roughly 7x the spend the customer was charged.

These tests pin the two properties that make that impossible to reintroduce:
  1. a row lands on the day Meta reported, never on today's date
  2. rows arriving with no `day` are dropped and counted, never silently dated

Runs without network or database. `db` is stubbed before import.
"""

import sys
import types
import unittest


# ── Stub `db` so snapshots can be imported without supabase installed ────────
class _FakeExec:
    def __init__(self, sink):
        self.sink = sink

    def execute(self):
        return types.SimpleNamespace(data=self.sink)


class _FakeTable:
    def __init__(self, store):
        self.store = store

    def upsert(self, rows, on_conflict=None):
        self.store["rows"] = rows
        self.store["on_conflict"] = on_conflict
        return _FakeExec(rows)


class _FakeDB:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        self.store["table"] = name
        return _FakeTable(self.store)


STORE: dict = {}
_db_mod = types.ModuleType("db")
_db_mod.get_db = lambda: _FakeDB(STORE)
sys.modules.setdefault("db", _db_mod)

from snapshots import write_daily_snapshot  # noqa: E402


def _ad(ad_id, day, spend, results=0.0, impressions=100, clicks=10):
    return {
        "id": ad_id,
        "name": f"Ad {ad_id}",
        "status": "ACTIVE",
        "adset_id": "as1",
        "day": day,
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "results": results,
        "revenue": 0.0,
    }


class DayLabelling(unittest.TestCase):
    def setUp(self):
        STORE.clear()

    def test_rows_land_on_their_own_day(self):
        ads = [
            _ad("a1", "2026-08-17", 10.00),
            _ad("a1", "2026-08-18", 20.00),
            _ad("a1", "2026-08-19", 30.00),
        ]
        n = write_daily_snapshot("p1", ads, [], [])

        rows = STORE["rows"]
        ad_rows = {r["day"]: r for r in rows if r["entity_level"] == "ad"}

        self.assertEqual(set(ad_rows), {"2026-08-17", "2026-08-18", "2026-08-19"})
        self.assertEqual(ad_rows["2026-08-17"]["spend_cents"], 1000)
        self.assertEqual(ad_rows["2026-08-18"]["spend_cents"], 2000)
        self.assertEqual(ad_rows["2026-08-19"]["spend_cents"], 3000)
        # 3 ad rows + 3 account rows
        self.assertEqual(n, 6)

    def test_account_row_is_per_day_and_summed_from_ads(self):
        ads = [
            _ad("a1", "2026-08-18", 20.00),
            _ad("a2", "2026-08-18", 5.50),
            _ad("a1", "2026-08-19", 30.00),
        ]
        write_daily_snapshot("p1", ads, [], [])

        acct = {r["day"]: r for r in STORE["rows"] if r["entity_level"] == "account"}
        self.assertEqual(set(acct), {"2026-08-18", "2026-08-19"})
        self.assertEqual(acct["2026-08-18"]["spend_cents"], 2550)
        self.assertEqual(acct["2026-08-19"]["spend_cents"], 3000)

    def test_total_across_days_equals_real_spend(self):
        """The property the bug violated: summing the account rows is the truth."""
        ads = [_ad("a1", f"2026-08-{d:02d}", 10.00) for d in range(13, 20)]
        write_daily_snapshot("p1", ads, [], [])

        total = sum(
            r["spend_cents"] for r in STORE["rows"] if r["entity_level"] == "account"
        )
        self.assertEqual(total, 7000)  # 7 days x $10.00, not 7 x 7 x $10.00

    def test_undated_rows_are_dropped_not_dated_from_the_clock(self):
        stale = _ad("a1", "2026-08-18", 10.00)
        del stale["day"]
        write_daily_snapshot("p1", [stale], [], [])
        self.assertEqual(STORE.get("rows"), None)  # nothing written at all

    def test_upsert_key_still_includes_day(self):
        write_daily_snapshot("p1", [_ad("a1", "2026-08-18", 1.0)], [], [])
        self.assertEqual(
            STORE["on_conflict"], "project_id,day,entity_level,entity_id"
        )

    def test_cents_conversion_has_no_float_drift(self):
        write_daily_snapshot("p1", [_ad("a1", "2026-08-18", 12.34)], [], [])
        ad = next(r for r in STORE["rows"] if r["entity_level"] == "ad")
        self.assertEqual(ad["spend_cents"], 1234)


if __name__ == "__main__":
    unittest.main(verbosity=2)
