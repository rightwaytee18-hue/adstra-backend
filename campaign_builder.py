"""
Campaign Builder — orchestrates Meta campaign creation for Adstra.

Flow: preflight_for_project → publish_for_project

Both functions load the project from Supabase via service-role key (db.py),
so they work regardless of the user's JWT state.
"""

import logging
import re
from typing import Optional
from ad_entities import mint_utm_key, tagged_destination, upsert_entity
from conversions import GOALS, campaign_shape
from crypto import token_for_project
from db import get_db
from meta_client import MetaClient, MetaAPIError

logger = logging.getLogger(__name__)

# Map template key to (objective, optimization_goal)
TEMPLATE_MAP = {
    "sales":     ("OUTCOME_SALES",     "OFFSITE_CONVERSIONS"),
    "leads":     ("OUTCOME_LEADS",     "LEAD_GENERATION"),
    "traffic":   ("OUTCOME_TRAFFIC",   "LINK_CLICKS"),
    "awareness": ("OUTCOME_AWARENESS", "REACH"),
}

# Which pixel event each template asks Meta to OPTIMIZE for.
#
# This was the literal "PURCHASE" for both the sales and the leads template, so
# a service business's ad set was told to chase an event their pixel will never
# fire. That is worse than sending no promoted_object at all: Meta optimizes
# toward a signal it never receives, so delivery never leaves the learning phase.
#
# Derived from conversions.campaign_shape rather than written out again, so the
# side that COUNTS a result and the side that ASKS for one cannot drift.
TEMPLATE_EVENT = {
    shape["template_key"]: shape["custom_event_type"]
    for shape in (campaign_shape(goal) for goal in GOALS)
}

DEFAULT_ATTRIBUTION = [
    {"event_type": "CLICK_THROUGH",     "window_days": 7},
    {"event_type": "VIEW_THROUGH",      "window_days": 1},
    {"event_type": "ENGAGED_VIDEO_VIEW","window_days": 1},
]


def _load_project(project_id: str) -> dict:
    db = get_db()
    resp = db.table("projects").select("*").eq("id", project_id).maybe_single().execute()
    if not resp or not resp.data:
        raise ValueError(f"Project {project_id} not found")
    return resp.data


def preflight_for_project(project_id: str, draft: dict) -> dict:
    """Run validation reads only. Returns {ok, steps[]}."""
    project = _load_project(project_id)
    token = token_for_project(project)
    account = project.get("ad_account_id")
    page_id = project.get("facebook_page_id")
    pixel_id = project.get("pixel_id")

    if not token or not account:
        return {
            "ok": False,
            "steps": [{"step": "token", "ok": False, "detail": "Your Facebook account is not connected yet."}],
        }

    template_key = draft.get("template_key", "sales")
    needs_pixel = template_key in ("sales", "leads")

    client = MetaClient(token, account, page_id=page_id)
    steps = client.validate(pixel_id=pixel_id if needs_pixel else None)

    ok = all(s["ok"] for s in steps)
    return {"ok": ok, "steps": steps}


def publish_for_project(project_id: str, draft: dict) -> dict:
    """
    Full publish flow: campaign → adset → image upload → creative → ad.
    Returns {
        ok: bool,
        campaign_id?: str,
        adset_id?: str,
        ad_ids: list[str],
        steps: list[{step, status, detail}],
        errors: list[str],
        fatal?: str
    }
    """
    project = _load_project(project_id)
    token = token_for_project(project)
    account = project.get("ad_account_id")
    page_id = project.get("facebook_page_id")
    pixel_id = project.get("pixel_id")

    if not token or not account:
        return {"ok": False, "fatal": "Meta not connected.", "steps": [], "errors": [], "ad_ids": []}

    # Resolve template
    template_key = draft.get("template_key", "sales")
    objective, optimization_goal = TEMPLATE_MAP.get(template_key, TEMPLATE_MAP["sales"])

    campaign_name = draft.get("name") or f"Reveal - {template_key.capitalize()} Campaign"
    budget_mode = draft.get("budget_mode", "cbo")
    daily_budget_cents = draft.get("daily_budget_cents")
    bid_strategy = draft.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP")
    special_ad_categories = draft.get("special_ad_categories", [])
    countries = draft.get("countries", ["US"])
    age_min = draft.get("age_min", 18)
    age_max = draft.get("age_max", 65)
    gender = draft.get("gender", "all")
    interests = draft.get("interests", [])
    link_url = draft.get("link_url") or project.get("store_url") or project.get("website", "")
    utm_params = draft.get("utm_params", "")
    cta_type = draft.get("cta_type", "LEARN_MORE")
    creatives_data = draft.get("creatives", [])

    steps = []
    errors = []
    ad_ids = []
    campaign_id = None
    adset_id = None

    client = MetaClient(token, account, page_id=page_id)

    # Build destination URL.
    #
    # Any hand-supplied utm_params are folded in first, then each creative gets
    # its OWN minted key appended inside the loop below. One destination shared
    # by every ad in the campaign would make every lead trace back to "this
    # campaign" and no further, which is the resolution the platform already
    # gives us for free and is not enough to score a creative.
    destination = link_url
    if utm_params:
        sep = "&" if "?" in destination else "?"
        destination = f"{destination}{sep}{utm_params}"

    # Slug for utm_campaign. Readable in the customer's own analytics, which is
    # where they will go looking when they ask where a lead came from.
    campaign_slug = re.sub(r"[^a-z0-9]+", "-", (campaign_name or "").lower()).strip("-")[:40]

    # Build targeting
    targeting: dict = {
        "geo_locations": {"countries": countries},
        "age_min": age_min,
        "age_max": age_max,
        "targeting_automation": {"advantage_audience": 1},
    }
    if gender == "male":
        targeting["genders"] = [1]
    elif gender == "female":
        targeting["genders"] = [2]
    if interests:
        targeting["interests"] = interests

    # Ad sets to build.
    #
    # ⚠️ ONE CAMPAIGN, MANY AD SETS, AND THE BUDGET STAYS ON THE CAMPAIGN.
    # Running three verticals as three CAMPAIGNS splits the budget three ways up
    # front and asks Meta to learn each one from a third of the data. With the
    # budget on one campaign, spend moves to whichever vertical is answering,
    # which is the entire reason to segment by vertical instead of guessing which
    # one works before any of them has run.
    #
    # A draft with no "adsets" key behaves exactly as it always did: one ad set
    # carrying every creative. Every existing caller is unchanged.
    adset_specs = draft.get("adsets")
    if not isinstance(adset_specs, list) or not adset_specs:
        adset_specs = [{"name": draft.get("adset_name") or campaign_name, "creatives": creatives_data}]

    # --- Step 1: Create campaign (or attach to one already made) ---
    #
    # An existing campaign_id lets a second call add another vertical to a
    # campaign that is already running, instead of the only shape this function
    # used to have, which was a brand new campaign every single time.
    existing_campaign = (draft.get("campaign_id") or "").strip() or None
    if existing_campaign:
        campaign_id = existing_campaign
        steps.append({"step": "campaign", "status": "ok", "detail": f"reused {campaign_id}"})
    else:
        try:
            cbo_budget = daily_budget_cents if budget_mode == "cbo" else None
            campaign_id = client.create_campaign(
                name=campaign_name,
                objective=objective,
                special_ad_categories=special_ad_categories,
                daily_budget_cents=cbo_budget,
                bid_strategy=bid_strategy,
            )
            steps.append({"step": "campaign", "status": "ok", "detail": campaign_id})
            logger.info(f"[campaign_builder] Created campaign {campaign_id} for project {project_id}")
            upsert_entity(project_id, "campaign", campaign_id, name=campaign_name, status="PAUSED")
        except MetaAPIError as e:
            steps.append({"step": "campaign", "status": "error", "detail": str(e)})
            return {"ok": False, "fatal": str(e), "campaign_id": None, "adset_id": None,
                    "adset_ids": [], "ad_ids": [], "steps": steps, "errors": [str(e)]}

    adset_ids: list[str] = []
    ad_num = 0

    for si, spec in enumerate(adset_specs):
        spec_name = spec.get("name") or f"{campaign_name} - Ad Set {si + 1}"

        # Per-ad-set targeting. Only the keys a vertical actually varies are
        # overridden; everything else stays the campaign's own targeting, so an
        # ad set cannot silently widen the geography or the age range.
        spec_targeting = dict(targeting)
        if spec.get("interests"):
            spec_targeting["interests"] = spec["interests"]

        # --- Step 2: Create ad set ---
        try:
            promoted_object = None
            event_type = TEMPLATE_EVENT.get(template_key)
            if event_type and pixel_id:
                promoted_object = {"pixel_id": pixel_id, "custom_event_type": event_type}

            abo_budget = daily_budget_cents if budget_mode == "abo" else None
            bid_amount = draft.get("target_cpa_cents") if bid_strategy == "COST_CAP" else None

            adset_id = client.create_adset(
                campaign_id=campaign_id,
                name=spec_name,
                optimization_goal=optimization_goal,
                targeting=spec_targeting,
                attribution_spec=DEFAULT_ATTRIBUTION,
                promoted_object=promoted_object,
                daily_budget_cents=abo_budget,
                bid_strategy=bid_strategy,
                bid_amount_cents=bid_amount,
            )
            steps.append({"step": f"adset_{si + 1}", "status": "ok", "detail": adset_id})
            logger.info(f"[campaign_builder] Created adset {adset_id} ({spec_name})")
            adset_ids.append(adset_id)
            upsert_entity(project_id, "adset", adset_id, parent_meta_id=campaign_id,
                          name=spec_name, status="PAUSED")
        except MetaAPIError as e:
            # One vertical failing must not take the others down with it. The
            # campaign and every ad set already built stay, and the error is
            # reported against the ad set that caused it.
            steps.append({"step": f"adset_{si + 1}", "status": "error", "detail": str(e)})
            errors.append(f"Ad set {spec_name} failed: {e}")
            logger.warning(f"[campaign_builder] adset {spec_name} failed: {e}")
            continue

        # --- Steps 3-5: Per creative: upload image → creative → ad ---
        for creative in (spec.get("creatives") or []):
            image_url = creative.get("image_url", "")
            headline = creative.get("headline", "")
            message = creative.get("message", "")
            description = creative.get("description", "")
            ad_num += 1

            # Minted BEFORE the creative, because the creative carries the URL and
            # the ad id does not exist until after it. This key is the only
            # identifier we control, and it is written onto the ad_entities row
            # below alongside the ad id Meta hands back, which joins the two.
            utm_content = mint_utm_key()
            ad_destination = tagged_destination(destination, utm_content, campaign_slug)

            try:
                image_hash = client.upload_image_from_url(image_url, filename=f"adstra_creative_{ad_num}.jpg")
                steps.append({"step": f"image_{ad_num}", "status": "ok", "detail": image_hash[:16] + "…"})
            except (MetaAPIError, Exception) as e:
                err = f"Ad {ad_num} image upload failed: {e}"
                steps.append({"step": f"image_{ad_num}", "status": "error", "detail": str(e)})
                errors.append(err)
                logger.warning(f"[campaign_builder] {err}")
                continue

            try:
                creative_id = client.create_ad_creative(
                    name=f"Creative {ad_num} - {headline[:40]}",
                    image_hash=image_hash,
                    link=ad_destination,
                    message=message,
                    headline=headline,
                    description=description or None,
                    cta_type=cta_type,
                )
                steps.append({"step": f"creative_{ad_num}", "status": "ok", "detail": creative_id})
            except MetaAPIError as e:
                err = f"Ad {ad_num} creative creation failed: {e}"
                steps.append({"step": f"creative_{ad_num}", "status": "error", "detail": str(e)})
                errors.append(err)
                logger.warning(f"[campaign_builder] {err}")
                continue

            try:
                ad_id = client.create_ad(
                    name=f"{spec_name} - Ad {ad_num}",
                    adset_id=adset_id,
                    creative_id=creative_id,
                )
                steps.append({"step": f"ad_{ad_num}", "status": "ok", "detail": ad_id})
                ad_ids.append(ad_id)
                logger.info(f"[campaign_builder] Created ad {ad_id}")
                upsert_entity(
                    project_id, "ad", ad_id,
                    parent_meta_id=adset_id,
                    name=f"{spec_name} - Ad {ad_num}",
                    status="PAUSED",
                    meta_creative_id=creative_id,
                    meta_image_hash=image_hash,
                    creative_generation_id=creative.get("creative_generation_id"),
                    hypothesis_id=creative.get("hypothesis_id"),
                    utm_content=utm_content,
                )
            except MetaAPIError as e:
                err = f"Ad {ad_num} creation failed: {e}"
                steps.append({"step": f"ad_{ad_num}", "status": "error", "detail": str(e)})
                errors.append(err)
                logger.warning(f"[campaign_builder] {err}")

    # Save record regardless of partial failure
    _save_published(project_id, draft, campaign_id, adset_ids, ad_ids, steps, errors)

    ok = len(ad_ids) > 0
    return {
        "ok": ok,
        "campaign_id": campaign_id,
        # Kept for every existing caller, which reads one ad set and knows
        # nothing about verticals. adset_ids is the honest answer now.
        "adset_id": adset_ids[0] if adset_ids else None,
        "adset_ids": adset_ids,
        "ad_ids": ad_ids,
        "steps": steps,
        "errors": errors,
    }


def _save_published(
    project_id: str,
    draft: dict,
    campaign_id: Optional[str],
    adset_ids: list[str],
    ad_ids: list[str],
    steps: list[dict],
    errors: list[str],
) -> None:
    """Write a published_campaigns row. Best-effort — never raises."""
    try:
        project = _load_project(project_id)
        db = get_db()
        db.table("published_campaigns").insert({
            "project_id": project_id,
            "user_id": project["user_id"],
            "meta_campaign_id": campaign_id or "",
            "meta_adset_ids": adset_ids,
            "meta_ad_ids": ad_ids,
            "name": draft.get("name"),
            "objective": draft.get("objective"),
            "daily_budget_cents": draft.get("daily_budget_cents"),
            "publish_status": "paused",
            "publish_result": {"steps": steps, "errors": errors},
        }).execute()
    except Exception as e:
        logger.error(f"[campaign_builder] Failed to save published_campaigns record: {e}")
