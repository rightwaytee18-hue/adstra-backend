"""
The agency autonomy ceiling, and the DRY_RUN hole it sits next to.

Mason's instruction for Optic, in writing on 2026-08-22: the system may Collect,
Diagnose, Flag and Recommend, and must not autonomously change budgets, pause
ads, launch campaigns or change targeting. Execution is Human Review, then
Approval, then the Meta change.

Two separate things had to be true for that to hold and neither was:

  1. approval_mode is set by the CLIENT in their own portal, so an Optic client
     could switch on full autonomy themselves and Optic would never know.
  2. AUTOPILOT_DRY_RUN, the global flag whose docstring promised "no Meta call
     that changes anything", covered the budget writes and not the ad pause or
     the replacement-ad creation.

Runs without network or database. `db` is stubbed before import.
"""

import re
import sys
import types
import unittest
from typing import Optional

# ─── Stub `db` before autonomy imports it ────────────────────────────────────
TENANT_ROW: Optional[dict] = {}
AGENCY_ROW: Optional[dict] = {}
RAISE_ON_READ = False


class _Resp:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, table):
        self.table = table

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        if RAISE_ON_READ:
            raise RuntimeError("supabase is having a moment")
        return _Resp(TENANT_ROW if self.table == "tenants" else AGENCY_ROW)


class _Db:
    def table(self, name):
        return _Q(name)


_stub = types.ModuleType("db")
_stub.get_db = lambda: _Db()  # type: ignore[attr-defined]
sys.modules["db"] = _stub

import autonomy  # noqa: E402


class TestAgencyMode(unittest.TestCase):
    def setUp(self):
        global TENANT_ROW, AGENCY_ROW, RAISE_ON_READ
        TENANT_ROW = {"agency_id": "agency-1"}
        AGENCY_ROW = {"autonomy_mode": "observe"}
        RAISE_ON_READ = False

    def test_reads_the_agency_setting(self):
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p", "tenant_id": "t"}), "observe")
        global AGENCY_ROW
        AGENCY_ROW = {"autonomy_mode": "execute"}
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p", "tenant_id": "t"}), "execute")

    def test_a_project_with_no_tenant_keeps_todays_behaviour(self):
        """
        Adstra-era, user-owned projects belong to no agency, so there is no
        agency to impose a ceiling. Not an error, and must not be treated as one:
        failing these closed would switch off every legacy customer's autopilot.
        """
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p"}), "execute")
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p", "tenant_id": None}), "execute")

    def test_a_tenant_outside_any_agency_keeps_todays_behaviour(self):
        global TENANT_ROW
        TENANT_ROW = {"agency_id": None}
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p", "tenant_id": "t"}), "execute")

    def test_fails_closed_when_the_database_errors(self):
        """
        ⚠️ A read failure costs one run of optimization. Failing open costs a
        client relationship, because it lets the engine pause ads against a
        written instruction.
        """
        global RAISE_ON_READ
        RAISE_ON_READ = True
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p", "tenant_id": "t"}), "observe")

    def test_fails_closed_on_a_value_this_code_does_not_know(self):
        """A mode added to the database later must not read as permission."""
        global AGENCY_ROW
        AGENCY_ROW = {"autonomy_mode": "full-send"}
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p", "tenant_id": "t"}), "observe")
        AGENCY_ROW = {}
        self.assertEqual(autonomy.agency_mode_for_project({"id": "p", "tenant_id": "t"}), "observe")

    def test_no_project_at_all(self):
        self.assertEqual(autonomy.agency_mode_for_project(None), "observe")


class TestCeiling(unittest.TestCase):
    """
    ⚠️ A CEILING CAN ONLY EVER REDUCE. If any of these ever returns 'auto' from a
    client who did not ask for it, an agency setting has started widening a
    client's exposure instead of narrowing it.
    """

    def test_observe_and_prepare_never_execute(self):
        for agency_mode in ("observe", "prepare"):
            for client_mode in ("auto", "manual", None, "", "AUTO"):
                self.assertEqual(
                    autonomy.effective_approval_mode(agency_mode, client_mode),
                    "manual",
                    f"{agency_mode} + {client_mode!r} must not execute",
                )

    def test_execute_honours_the_client(self):
        self.assertEqual(autonomy.effective_approval_mode("execute", "auto"), "auto")
        self.assertEqual(autonomy.effective_approval_mode("execute", "manual"), "manual")

    def test_execute_never_invents_consent(self):
        """An agency set to execute must not turn a client's 'manual' into auto."""
        for client_mode in ("manual", None, "", "whatever"):
            self.assertNotEqual(autonomy.effective_approval_mode("execute", client_mode), "auto")


class TestEveryMetaWriteIsBehindDryRun(unittest.TestCase):
    """
    ⚠️ THIS IS THE TEST THE MISSING GUARD WOULD HAVE FAILED.

    DRY_RUN's docstring promised "no Meta call that changes anything" while the
    ad-pause and replacement-ad paths ran regardless. A flag that is true in
    three places out of four is worse than no flag, because it is the one thing
    somebody sets before pointing this engine at a real account.

    Static, on the source, because the alternative is a live Meta account.
    """

    #: Every call in full_autopilot_engine that changes something on Meta.
    MUTATORS = (r"meta\.pause\(", r"meta\.set_budget\(", r"_upload_and_create_ad\(\s*$")

    def test_every_mutating_call_sits_under_a_dry_run_branch(self):
        with open("full_autopilot_engine.py") as f:
            src = f.read().split("\n")

        # ⚠️ The whole file, not one function. The first draft of this test
        # scanned from `def run_full_daily` to the next module-level `def`, which
        # looked right and covered half the call sites: the budget writes live in
        # _execute_budget_action, further down the file, so the two calls that
        # WERE already guarded were the two it checked. It passed while proving
        # nothing about the two that were not.
        #
        # Nothing in this module may write to Meta unguarded. The human-approval
        # path is deliberately elsewhere: a person clicking Approve is exactly the
        # flow Mason asked for and must still reach Meta.
        found = []
        for i, line in enumerate(src):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("def ") or "DRY RUN would" in line:
                continue
            if not any(re.search(pat, line) for pat in self.MUTATORS):
                continue
            found.append(i + 1)
            window = "\n".join(src[max(0, i - 8):i])
            self.assertIn(
                "if DRY_RUN:",
                window,
                f"full_autopilot_engine.py:{i + 1} changes something on Meta with no DRY_RUN "
                f"branch above it:\n    {stripped}",
            )

        # ⚠️ A test that finds nothing passes. If a refactor renames these calls
        # this has to fail loudly rather than quietly guard an empty set.
        self.assertGreaterEqual(
            len(found), 4,
            f"expected at least 4 mutating Meta calls, found {found}. Either a call site was "
            f"renamed and MUTATORS is now stale, or one was removed.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
