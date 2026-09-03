"""
The buyer psychology a creative is supposed to be built from.

Pillar 4 of the demand-to-revenue deck says every creative pulls from Pillar 1:
the ideal customer, the pain, the fear, the objection, the awareness stage, and
the customer's own words. None of that ever reached this engine. _build_brand_context
composed its prompt from the business name, two brand colours and a product
list, and the autopilot's replacement path passed one fixed sentence:

    "Fresh high-converting creative optimized for performance. Bold hook, clear CTA."

So every ad this engine has ever been asked to make was a brand asset, not an
argument aimed at a person.

⚠️ THIS MODULE OWNS NO TAXONOMY, ON PURPOSE. The fourteen levers, the Pillar 1
slots that feed each one, and the awareness stages each works at live in
reveal/lib/creative/angles.ts and are pinned by reveal/scripts/check-creative.ts.
Reveal renders the brief into ad_hypotheses.brief; this file reads that string
and does not compose one. A second copy of the vocabulary in Python would drift
inside a release, and the drift would be invisible — the ads would still
generate, they would simply stop being the ads the hypothesis describes.
"""

import logging
from typing import Optional

from db import get_db

logger = logging.getLogger(__name__)


def _tenant_of(project_id: str) -> Optional[str]:
    """The Reveal tenant a project belongs to, if it has one."""
    db = get_db()
    res = db.table("projects").select("tenant_id").eq("id", project_id).limit(1).execute()
    rows = res.data or []
    return rows[0].get("tenant_id") if rows else None


def next_hypothesis(project_id: str) -> Optional[dict]:
    """
    The hypothesis the next creative for this project should carry.

    Returns {"id", "brief", "statement", "angle", "awareness"} or None.

    Selection is deliberately simple and deliberately SPREADING: among the
    hypotheses that are live and carry a rendered brief, prefer the one with the
    fewest ads already built on it.

    That single rule is what makes the deck's central question answerable —
    "which buyer hypothesis performs best and produces the highest quality
    lead?" cannot be answered if the engine keeps rebuilding the first idea it
    found. Spreading is what produces arms to compare.

    Returns None rather than guessing when the client has no live hypothesis.
    The caller falls back to its old behaviour: a client whose buyers have never
    been read still gets an ad, it is just honestly a brand ad rather than one
    pretending to be evidence-led.
    """
    tenant_id = _tenant_of(project_id)
    if not tenant_id:
        return None

    db = get_db()
    try:
        res = (
            db.table("ad_hypotheses")
            .select("id,angle,awareness,statement,brief,status")
            .eq("tenant_id", tenant_id)
            .eq("status", "live")
            .is_("retired_at", "null")
            .not_.is_("brief", "null")
            .limit(100)
            .execute()
        )
    except Exception as e:  # pragma: no cover - network
        logger.warning(f"[creative_brief] could not read hypotheses: {e}")
        return None

    rows = [r for r in (res.data or []) if (r.get("brief") or "").strip()]
    if not rows:
        return None

    # How many ads each one already carries. A hypothesis with no ads sorts
    # first, so a freshly promoted idea gets tested rather than starved.
    counts: dict[str, int] = {}
    try:
        used = (
            db.table("ad_entities")
            .select("hypothesis_id")
            .eq("project_id", project_id)
            .eq("level", "ad")
            .is_("archived_at", "null")
            .limit(1000)
            .execute()
        )
        for row in used.data or []:
            hid = row.get("hypothesis_id")
            if hid:
                counts[hid] = counts.get(hid, 0) + 1
    except Exception as e:  # pragma: no cover - network
        # Not fatal: with no counts every hypothesis ties at zero and the first
        # one wins, which is still better than no brief at all.
        logger.warning(f"[creative_brief] could not count ads per hypothesis: {e}")

    rows.sort(key=lambda r: (counts.get(r["id"], 0), r.get("statement") or ""))
    chosen = rows[0]
    logger.info(
        f"[creative_brief] project {project_id}: chose hypothesis {chosen['id']} "
        f"({chosen.get('angle')} / {chosen.get('awareness')}), "
        f"{counts.get(chosen['id'], 0) } existing ad(s)"
    )
    return chosen


def direction_for(project_id: str, fallback: str) -> tuple[str, Optional[str]]:
    """
    (creative direction, hypothesis_id) for the next creative on this project.

    Falls back to `fallback` and a null hypothesis when the client has no live
    hypothesis with a brief. The null is as important as the string: it travels
    onto the ad_entities row, so an ad built without evidence is recorded as
    such rather than being quietly attributed to whichever idea was nearest.
    """
    h = next_hypothesis(project_id)
    if not h:
        return fallback, None
    return h["brief"], h["id"]
