"""
What ROAS actually means for THIS client.

The autopilot has always compared ROAS against fixed numbers: scale above 3.0,
kill below 0.8. Those are not thresholds, they are guesses about a business
nobody asked. Break-even ROAS is 1 / gross margin:

    margin 20%  ->  break-even 5.00
    margin 40%  ->  break-even 2.50
    margin 70%  ->  break-even 1.43

So on a twenty-point margin the shipped default said "put more money behind
this" at ROAS 3.0, which is forty cents lost on every dollar of revenue, and
left the campaign running all the way down to 0.8, six times past the point the
client starts paying for the privilege of having customers. On a seventy-point
margin the same numbers killed campaigns that were making money.

⚠️ THIS MODULE OWNS NO ARITHMETIC THE PRODUCT ALSO OWNS. The definitions live in
reveal/lib/revenue/margin.ts and are pinned by reveal/scripts/check-scaling.ts.
The three formulas below are deliberately trivial restatements of that file, and
they are duplicated rather than imported for the same reason creative_brief.py
reads a rendered string: this is a separate service in a separate language. Keep
them identical, and if they must change, change the TypeScript first.

⚠️ MARGIN, NOT MARKUP. Basis points. 4000 is forty cents kept on a dollar of
revenue after the cost of delivering the job, before advertising.
"""

import logging
from typing import Optional

from db import get_db

logger = logging.getLogger(__name__)

BP = 10_000
MIN_MARGIN_BP = 500
MAX_MARGIN_BP = 9_500


def _usable(margin_bp: Optional[int]) -> bool:
    """Mirrors isUsableMargin in reveal/lib/revenue/margin.ts, including the
    bounds. Rejects rather than clamps: a 3 typed into a percent box and a markup
    mistaken for a margin both land low, and quietly believing either produces a
    confident affordability ceiling that is wrong by a factor."""
    return margin_bp is not None and MIN_MARGIN_BP <= margin_bp <= MAX_MARGIN_BP


def breakeven_roas(margin_bp: Optional[int]) -> Optional[float]:
    """The ROAS at which this client exactly breaks even. None if unstated."""
    if margin_bp is None or not _usable(margin_bp):
        return None
    return BP / float(margin_bp)


def target_roas(margin_bp: Optional[int], keep_bp: Optional[int]) -> Optional[float]:
    """
    ROAS needed to keep `keep_bp` of revenue after paying for ads.

    Solving contribution - spend = keep * revenue gives 1 / (margin - keep).
    Wanting to keep the whole margin is asking for free customers, so that
    returns None rather than reporting an infinite target as a real one.
    """
    if margin_bp is None or not _usable(margin_bp):
        return None
    if keep_bp is None or keep_bp < 0 or keep_bp >= margin_bp:
        return None
    return BP / float(margin_bp - keep_bp)


def margin_for_project(project_id: str) -> tuple[Optional[int], Optional[int]]:
    """
    (gross_margin_bp, target_contribution_bp) for the client behind a project.

    Both None when the project has no tenant, no live revenue target, or nobody
    has stated a margin. None is a real answer and the caller must keep its old
    behaviour rather than assuming one.
    """
    db = get_db()
    try:
        proj = db.table("projects").select("tenant_id").eq("id", project_id).limit(1).execute()
        rows = proj.data or []
        tenant_id = rows[0].get("tenant_id") if rows else None
        if not tenant_id:
            return None, None

        res = (
            db.table("revenue_targets")
            .select("gross_margin_bp,target_contribution_bp")
            .eq("tenant_id", tenant_id)
            .is_("retired_at", "null")
            .limit(1)
            .execute()
        )
        t = (res.data or [None])[0]
        if not t:
            return None, None
        return t.get("gross_margin_bp"), t.get("target_contribution_bp")
    except Exception as e:  # pragma: no cover - network
        logger.warning(f"[margin_policy] could not read margin for project {project_id}: {e}")
        return None, None


def roas_thresholds(project_id: str, settings: dict) -> tuple[float, float, Optional[str]]:
    """
    (scale_roas_min, kill_roas_max, note) for this project.

    Precedence, and the asymmetry in it is deliberate:

      * No margin stated -> the shipped defaults, unchanged. Guessing a margin
        would produce a confident threshold that is wrong in whichever direction
        the guess missed, and it decides how a real budget moves.

      * Margin stated, no operator override -> derived. Kill at break-even.
        Scale at the ROAS that keeps the target contribution, or at twice
        break-even when no contribution target is set, which keeps roughly half
        the margin after ads.

      * Operator override ABOVE break-even -> respected. Choosing to be stricter
        than the arithmetic is a legitimate business decision.

      * ⚠️ Operator override BELOW break-even -> RAISED to break-even, loudly.
        This is the one place a stored setting is overruled, and the reason is
        that running below break-even is not a preference, it is a loss. A kill
        threshold of 0.8 on a twenty-point margin does not express a risk
        appetite; it expresses not knowing that break-even is 5.0.
    """
    default_scale = float(settings.get("scale_roas_min") or 3.0)
    default_kill = float(settings.get("kill_roas_max") or 0.8)

    margin_bp, keep_bp = margin_for_project(project_id)
    be = breakeven_roas(margin_bp)
    if be is None or margin_bp is None:
        return default_scale, default_kill, None

    scale = target_roas(margin_bp, keep_bp) or (be * 2.0)
    kill = be

    note = (
        f"Break-even is {be:.2f}x at a {margin_bp / 100:.0f} point margin, "
        f"so this account is judged against {kill:.2f}x and {scale:.2f}x."
    )

    # An explicitly set threshold is honoured only while it is at least as
    # strict as the arithmetic.
    if settings.get("scale_roas_min") is not None and default_scale >= be:
        scale = max(default_scale, be)
    if settings.get("kill_roas_max") is not None:
        if default_kill >= be:
            kill = default_kill
        else:
            logger.warning(
                f"[margin_policy] project {project_id}: stored kill_roas_max "
                f"{default_kill} is below break-even {be:.2f} at a "
                f"{margin_bp / 100:.0f} point margin. Raising to break-even; "
                f"below it every additional sale loses money."
            )
            note += " A stored kill threshold below break-even was raised to it."

    return scale, kill, note


def scale_step_pct(roas: Optional[float], margin_bp: Optional[int], cap_pct: float = 20.0) -> float:
    """
    How hard the budget may be pushed, as a percentage of current spend.

    Aggression is a property of how far above break-even the campaign is, not of
    the ROAS number itself. Twice break-even earns the full cap; anything at or
    below break-even earns nothing. Linear between, because a step function makes
    the budget lurch on a rounding change and an operator watching that stops
    trusting it.

    Falls back to the cap when the margin is unstated, which is the behaviour
    that shipped, so an unmeasured client is no worse off than before.
    """
    be = breakeven_roas(margin_bp)
    if be is None or roas is None:
        return cap_pct
    if roas <= be:
        return 0.0
    headroom = min(1.0, (roas - be) / be)
    return round(cap_pct * headroom, 1)
