"""
What is actually running on Meta, and how a lead finds its way back to it.

THE PROBLEM THIS SOLVES. Impressions and clicks exist only inside Meta, reported
per entity per day and never per person. The grain of the platform side is
(ad, day). The only per-person thread from a click to a lead is the URL. So if
the URL does not carry an identifier, there is no way to say which creative
produced which customer, and every claim about cost per qualified lead is a guess.

THE ORDERING PROBLEM. The obvious identifier is Meta's ad id, and it does not
exist yet: an ad is created FROM a creative, and the creative already contains the
destination URL. By the time there is an ad id, the URL is frozen. So the key is
MINTED HERE, locally, before the creative is built, stamped into the URL, and
written onto the ad_entities row alongside the ad id Meta returns.

⚠️ AGGREGATE EACH SIDE BEFORE JOINING THEM. Spend lives per (ad, day) and
outcomes live per lead, so joining the two row to row multiplies spend by the
number of leads: one ad with $120 of spend and 2 leads reports $240 and a cost
per lead of $120 instead of $60. Verified against this database on 2026-08-21,
where the naive join returned exactly that. Sum spend per entity in one CTE,
count outcomes per utm_content in another, then join the two aggregates. This is
the single easiest way to produce a confident wrong number here, and every
metric the hypothesis ledger scores is downstream of it.

TWO THREADS, DELIBERATELY. Alongside our key the URL also carries Meta's own
dynamic macros, {{ad.id}} and {{adset.id}}, which Meta substitutes at click time.
One thread we control and one we do not means a broken thread is DETECTABLE:
if utm_content arrives without fb_ad, or the two disagree, something is wrong with
the link, and that is a fixable state rather than a silent hole in the numbers.
"""

import logging
import secrets
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from db import get_db

logger = logging.getLogger(__name__)

# No vowels (so it cannot spell anything) and no 0/1/o/l/i (so it survives being
# read aloud off a screen or retyped out of a spreadsheet).
_ALPHABET = "bcdfghjkmnpqrstvwxyz23456789"
_KEY_LEN = 10

UTM_PREFIX = "rv-"


def mint_utm_key() -> str:
    """A fresh join key. ~28^10 of space, so collisions are not a design concern."""
    return UTM_PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(_KEY_LEN))


def tagged_destination(base_url: str, utm_content: str, campaign_slug: str = "") -> str:
    """
    The ad's destination URL, carrying both threads.

    Existing query parameters on the customer's own landing page are preserved,
    because a funnel URL frequently already carries something the page needs, and
    a builder that rebuilt the query string from scratch would silently break it.

    Meta's macros are appended as literal text: urlencode would escape the braces
    and Meta would stop recognising them, so they are joined by hand and never
    passed through the encoder.
    """
    if not base_url:
        return base_url

    parts = urlsplit(base_url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    existing.update({
        "utm_source": "facebook",
        "utm_medium": "paid",
        "utm_content": utm_content,
    })
    if campaign_slug:
        existing["utm_campaign"] = campaign_slug

    query = urlencode(existing)
    # Substituted by Meta at click time, not by us.
    query = f"{query}&fb_ad={{{{ad.id}}}}&fb_adset={{{{adset.id}}}}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def upsert_entity(
    project_id: str,
    level: str,
    meta_id: str,
    *,
    parent_meta_id: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
    meta_creative_id: Optional[str] = None,
    meta_image_hash: Optional[str] = None,
    creative_generation_id: Optional[str] = None,
    hypothesis_id: Optional[str] = None,
    utm_content: Optional[str] = None,
    created_by: Optional[str] = None,
) -> None:
    """
    Register one campaign, ad set or ad. Idempotent on (project, level, meta_id).

    ⚠️ NEVER RAISES. This is a bookkeeping write that runs immediately after money
    has already been committed on Meta. Letting it fail a publish would turn a
    successful campaign into a reported error and invite someone to retry it, and
    a retried publish is a duplicated campaign spending real money.

    Only non-null fields are written, so the reconciliation pass in snapshots.py
    can refresh a name or a status without erasing the hypothesis link that
    publish_for_project set. That asymmetry is the whole reason this takes
    keyword arguments rather than a dict.
    """
    row = {
        "project_id": project_id,
        "level": level,
        "meta_id": str(meta_id),
        "last_seen_at": "now()",
    }
    # Sent only on the path that KNOWS the provenance. An upsert writes every
    # column it is given, so a reconciliation pass that always sent created_by
    # would relabel every engine-published ad as an import the first night it ran.
    if created_by is not None:
        row["created_by"] = created_by
    for key, value in (
        ("parent_meta_id", parent_meta_id),
        ("name", name),
        ("status", status),
        ("meta_creative_id", meta_creative_id),
        ("meta_image_hash", meta_image_hash),
        ("creative_generation_id", creative_generation_id),
        ("hypothesis_id", hypothesis_id),
        ("utm_content", utm_content),
    ):
        if value is not None:
            row[key] = value

    try:
        get_db().table("ad_entities").upsert(
            row, on_conflict="project_id,level,meta_id"
        ).execute()
    except Exception as e:
        logger.error(
            "[ad_entities] %s %s/%s registry write failed (non-fatal): %s",
            project_id, level, meta_id, e,
        )


def reconcile_entities(project_id: str, ads, adsets, campaigns) -> int:
    """
    Register whatever the daily snapshot just saw. Returns rows touched.

    This is the half of the registry that does not come from publish_for_project,
    and it matters more than it looks: an operator will absolutely build an ad by
    hand in Ads Manager, and an ad the registry has never heard of is an ad whose
    spend can never be attributed to anything.

    Reads existing ids ONCE and splits the work, rather than upserting everything
    with created_by set. An upsert writes every column it is given, so a blanket
    pass would rewrite created_by to 'import' on ads this engine published, and
    the one field that says where an ad came from would mean nothing after a
    single night.

    Entities repeat once per day in a per-day snapshot, so they are deduped first.
    """
    seen: dict[tuple[str, str], dict] = {}
    for level, rows in (("campaign", campaigns), ("adset", adsets), ("ad", ads)):
        for r in rows or []:
            meta_id = r.get("id")
            if not meta_id:
                continue
            # Later days win, so name and status reflect the most recent state.
            seen[(level, str(meta_id))] = {
                "name": r.get("name"),
                "status": r.get("status"),
                "parent_meta_id": r.get("adset_id") or r.get("campaign_id"),
            }

    if not seen:
        return 0

    known: set[tuple[str, str]] = set()
    try:
        resp = (
            get_db()
            .table("ad_entities")
            .select("level,meta_id")
            .eq("project_id", project_id)
            .limit(5000)
            .execute()
        )
        for row in (resp.data or []):
            known.add((row["level"], str(row["meta_id"])))
    except Exception as e:
        # Without the existing set we cannot tell new from known, and guessing
        # would corrupt created_by on every engine-published ad. Skipping a
        # night of reconciliation is the cheaper mistake.
        logger.error("[ad_entities] %s: could not read registry, skipping reconcile: %s", project_id, e)
        return 0

    for (level, meta_id), fields in seen.items():
        upsert_entity(
            project_id, level, meta_id,
            created_by=None if (level, meta_id) in known else "import",
            **fields,
        )
    return len(seen)
