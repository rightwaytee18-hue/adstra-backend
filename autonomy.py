"""
Who is allowed to change a live ad account without a human, and who is not.

⚠️ THE INSTRUCTION THIS ENFORCES IS IN WRITING FROM A CLIENT.

Mason (Optic), 2026-08-22, on what the system may do in phase 1: it may Collect,
Diagnose, Flag and Recommend. Execution is Human Review, then Approval, then the
Meta change. It must NOT autonomously change budgets, pause ads, adsets or
campaigns, launch campaigns, or change targeting, placements or offers. In his
words: "automate information gathering, calculation, pattern detection and
recommendation first; automate consequential decisions last."

Nothing enforced that before this module. `autopilot_settings.approval_mode`
defaults to 'manual', which is safe on arrival, but it is flipped to 'auto' by
the CLIENT'S OWN portal toggle (reveal: app/app/ads/page.tsx and
app/api/portal/ads/settings/route.ts) and by the portal assistant
(lib/portal/assistantTools.ts). So one of Optic's clients could switch on full
autonomy from their own portal and Optic would have no say in it at all.

The modes, on `agencies.autonomy_mode`:

  observe   Recommend only. Never change anything on Meta. Mason's phase 1.
  prepare   Prepare the change and wait for a human. Mason's phase 2.
  execute   Honour whatever the client chose in their portal. Today's behaviour.

⚠️ A CEILING, NEVER AN OVERRIDE. This can only ever make the engine do LESS than
approval_mode already allows, never more, so an agency row can never widen a
client's exposure past what that client chose for themselves.

⚠️ observe AND prepare ARE THE SAME INSTRUCTION TO THIS ENGINE, and that is
stated here rather than left to be discovered. Both mean "never touch Meta
without a human", which is one behaviour. They differ in what the console does
with the queue afterwards, not in what the engine does with it. Anyone adding a
third behaviour here should add it deliberately rather than assuming it already
exists.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

OBSERVE = "observe"
PREPARE = "prepare"
EXECUTE = "execute"

MODES = (OBSERVE, PREPARE, EXECUTE)


def agency_mode_for_project(project: Optional[dict]) -> str:
    """
    The autonomy ceiling for this project's agency.

    ⚠️ CALLERS MUST SELECT `tenant_id`. Two loaders in this codebase read a NAMED
    column list rather than select("*"), and a column that is not selected reads
    as None with no error. That is how user_id stayed null on every queued action
    for months and how ad_credential_id had to be retrofitted into rules_engine.
    An absent tenant_id here reads as "no agency", which fails OPEN, so the
    column being missing is the one case worth stating twice.

    ⚠️ FAILS CLOSED ON AN ERROR, DELIBERATELY. A database hiccup returns
    'observe', which costs one run of optimization and is visible in the logs. The
    alternative is that a transient error lets the engine pause a client's ads
    against a written instruction, which costs the relationship. A project with no
    tenant at all is a different case, not an error: those are Adstra-era,
    user-owned projects that belong to no agency and have no agency to be
    constrained by, so they keep today's behaviour.
    """
    if not project:
        return OBSERVE

    tenant_id = project.get("tenant_id")
    if not tenant_id:
        # No agency exists to impose a ceiling. Not an error.
        return EXECUTE

    try:
        from db import get_db

        db = get_db()
        tenant = (
            db.table("tenants").select("agency_id").eq("id", tenant_id).maybe_single().execute()
        )
        agency_id = (tenant.data or {}).get("agency_id") if tenant else None
        if not agency_id:
            # A tenant outside any agency, i.e. one of Reveal's own direct
            # clients from before the agency layer. Same reasoning as above.
            return EXECUTE

        agency = (
            db.table("agencies")
            .select("autonomy_mode")
            .eq("id", agency_id)
            .maybe_single()
            .execute()
        )
        mode = (agency.data or {}).get("autonomy_mode") if agency else None
        if mode in MODES:
            return mode
        # A value the database allowed but this code does not know. Treat an
        # unrecognised ceiling as the most restrictive one rather than ignoring it.
        logger.warning(
            f"[autonomy] agency {agency_id} has autonomy_mode={mode!r}, which is not one of "
            f"{MODES}. Falling back to {OBSERVE}."
        )
        return OBSERVE
    except Exception as e:
        logger.error(
            f"[autonomy] Could not resolve the autonomy ceiling for project "
            f"{project.get('id')}: {e}. Falling back to {OBSERVE}, so this run "
            f"recommends and changes nothing."
        )
        return OBSERVE


def effective_approval_mode(agency_mode: str, approval_mode: Optional[str]) -> str:
    """
    What the engine should actually do, given both answers.

    Returns 'auto' or 'manual', which is exactly what every existing call site
    already understands, so the gate is one value assignment rather than a new
    branch in five places.

    'auto' is only ever returned when the agency permits execution AND the client
    asked for it. Every other combination is 'manual', which queues the action for
    a human. That is the ceiling: this function can turn auto into manual and can
    never turn manual into auto.
    """
    mode = (approval_mode or "manual").strip().lower()
    if agency_mode == EXECUTE:
        return mode
    return "manual"
