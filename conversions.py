"""
One definition of "a conversion", shared by every surface that counts one.

Two bugs live here, and both were live in production.

THE BIG ONE: this engine counted the Meta action type "purchase" and nothing
else. That is correct for the ecommerce advertiser Adstra was designed around,
and completely wrong for the businesses Reveal actually sells to. A plumber, an
auto body shop and a coffee shop generate `lead` and
`onsite_conversion.messaging_conversation_started_7d` actions and never a single
`purchase`. Their purchase count is therefore always 0, their ROAS is always
0.0, and the autopilot's kill rule ("ROAS below 0.5 and spend above $30, pause
it") matches every ad they will ever run. Pointed at a lead-gen account, the
autopilot's only possible behaviour was to switch the whole account off.

THE QUIET ONE: Meta reports the SAME conversion under several action types at
once. A single pixel purchase can appear as `purchase`, `omni_purchase` and
`offsite_conversion.fb_pixel_purchase` in one response. Summing the family
triples the count, which halves the apparent cost per result and makes a losing
ad look like a winner. So a family is reduced with max(), never sum().

Kept deliberately separate from the Meta client so the app, the engine and any
report agree by construction rather than by everyone remembering.
"""

from typing import Iterable, Optional

# Ordered widest-first inside each family. Meta's naming has drifted across API
# versions and an account can emit any subset of these for one real event.
PURCHASE_ACTIONS = (
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "onsite_web_purchase",
    "onsite_web_app_purchase",
)

LEAD_ACTIONS = (
    "lead",
    "offsite_conversion.fb_pixel_lead",
    "onsite_conversion.lead_grouped",
    "onsite_conversion.lead_form_submitted",
)

# A conversation started in Messenger, Instagram DM or WhatsApp is how a large
# share of local service businesses actually receive an enquiry. Counting it as
# a result is the difference between "this ad did nothing" and "this ad brought
# in nine people".
MESSAGE_ACTIONS = (
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.total_messaging_connection",
)

# A tap on the phone number in an ad. Never a pixel event, so it is invisible to
# anything counting pixel conversions only, and for a trades business it is
# frequently the single most common result.
CALL_ACTIONS = (
    "onsite_conversion.call_confirm",
    "click_to_call_call_confirm",
)

GOALS = ("purchase", "lead", "awareness")

# Which action families count as a result, per goal. A lead-goal account counts
# form fills, messages and calls together, because from the owner's side those
# are all the same event: someone got in touch.
_GOAL_FAMILIES: dict[str, tuple[tuple[str, ...], ...]] = {
    "purchase": (PURCHASE_ACTIONS,),
    "lead": (LEAD_ACTIONS, MESSAGE_ACTIONS, CALL_ACTIONS),
    "awareness": (),
}

# What the customer picked in the Reveal intake, mapped to a goal. The intake
# offers exactly these three (lib/fulfillment/intake.ts, key 'goal').
INTAKE_GOAL_MAP = {
    "more leads": "lead",
    "more sales": "purchase",
    "awareness": "awareness",
}


def goal_from_intake(answer: Optional[str]) -> str:
    """
    Map the Reveal intake answer to a goal.

    Defaults to 'lead', which is the safe direction: treating a purchase account
    as lead-gen under-counts revenue and makes the autopilot cautious, while
    treating a lead account as purchase-driven reports zero results forever and
    makes it pause everything.
    """
    if not answer:
        return "lead"
    return INTAKE_GOAL_MAP.get(answer.strip().lower(), "lead")


def _family_value(actions: Iterable[dict], family: tuple[str, ...]) -> float:
    """
    The value of one action family, de-duplicated.

    max() and not sum(): every member of a family can describe the same real
    event, so summing counts one purchase three times.
    """
    best = 0.0
    for a in actions or []:
        if a.get("action_type") in family:
            try:
                best = max(best, float(a.get("value", 0) or 0))
            except (TypeError, ValueError):
                continue
    return best


def count_results(actions: Iterable[dict], goal: str) -> float:
    """
    How many results this entity produced, for the given goal.

    Families are summed against each other but de-duplicated within themselves:
    a form fill and a phone call are two different people, while `lead` and
    `offsite_conversion.fb_pixel_lead` are one.
    """
    actions = list(actions or [])
    return sum(_family_value(actions, fam) for fam in _GOAL_FAMILIES.get(goal, ()))


def sum_revenue(action_values: Iterable[dict], goal: str) -> float:
    """
    Revenue attributed to this entity.

    Only purchase goals carry real revenue. A lead has no revenue on Meta's side,
    and inventing one from the customer's average job value would put a made-up
    number in front of them labelled as measured. Cost per result is the honest
    metric there.
    """
    if goal != "purchase":
        return 0.0
    return _family_value(action_values, PURCHASE_ACTIONS)


def derive(insights: dict, goal: str) -> dict:
    """
    Turn one Meta insights row into the numbers every caller should use.

    `roas` is None rather than 0.0 for non-purchase goals. That distinction is
    load-bearing: 0.0 means "measured, and it made nothing", which is what sent
    the kill rule after every lead-gen ad. None means "this account has no such
    number", and every rule must skip a None instead of comparing it.
    """
    spend = float(insights.get("spend", 0) or 0)
    results = count_results(insights.get("actions"), goal)
    revenue = sum_revenue(insights.get("action_values"), goal)

    roas: Optional[float]
    if goal == "purchase":
        roas = (revenue / spend) if spend > 0 else 0.0
    else:
        roas = None

    cost_per_result = (spend / results) if results > 0 else None

    return {
        "spend": spend,
        "results": results,
        "revenue": revenue,
        "roas": roas,
        "cost_per_result": cost_per_result,
        # ⚠️ None, never 0.0. This read `cost_per_result or 0.0`, which undid the
        # whole absent-is-not-zero fix for the one cost metric a rule can
        # actually use: `cpa` is in rules_engine.METRIC_KEYS and `results` and
        # `cost_per_result` are not. An entity that spent real money and produced
        # NOTHING has no cost per result, but reported 0.0, so the natural rule
        # "cpa less_than 40 -> scale_budget" (the direct translation of the
        # customer's target cost per customer) matched every dead ad on the
        # account and proposed raising its budget.
        "cpa": cost_per_result,
        "goal": goal,
    }


# ─────────────────────────────────────────────────────────────
# How a goal is BUILT, not just counted.
# ─────────────────────────────────────────────────────────────
# The counting half of this file was made goal-aware and the building half was
# not, so every campaign the engine published was constructed as if the customer
# ran an online store:
#
#   - the bootstrap draft hardcoded template_key "sales" / OUTCOME_SALES
#   - the ad set sent custom_event_type PURCHASE even on the "leads" template,
#     so a plumber's ad set was told to optimize for an event their pixel will
#     never fire, which is worse than no promoted_object at all
#   - every replacement ad read "Shop {business} now" with a SHOP_NOW button
#
# Keeping this beside GOALS and _GOAL_FAMILIES means the account that counts
# leads and the campaign that asks for leads cannot disagree.

_CAMPAIGN_SHAPES: dict[str, dict] = {
    "purchase": {
        "template_key": "sales",
        "objective": "OUTCOME_SALES",
        "custom_event_type": "PURCHASE",
        "cta_type": "SHOP_NOW",
        "message": "Shop {business} now",
    },
    "lead": {
        "template_key": "leads",
        "objective": "OUTCOME_LEADS",
        "custom_event_type": "LEAD",
        # LEARN_MORE and not GET_QUOTE, deliberately. GET_QUOTE reads better for
        # a trades business, but CTA validity varies by objective and placement
        # and an invalid call_to_action fails the whole ad creation, which would
        # trade a bad button for no ad at all. Revisit once there is a live
        # account to test against. Buyer-led creative replaces this copy wholesale.
        "cta_type": "LEARN_MORE",
        "message": "Request a quote from {business}",
    },
    "awareness": {
        "template_key": "awareness",
        "objective": "OUTCOME_AWARENESS",
        # No pixel event. An awareness campaign optimizing for a conversion is a
        # contradiction, and promoted_object is omitted entirely.
        "custom_event_type": None,
        "cta_type": "LEARN_MORE",
        "message": "{business}",
    },
}


def campaign_shape(goal: Optional[str]) -> dict:
    """
    Objective, optimization event and default copy for a goal.

    Defaults to 'lead' for the same reason goal_from_intake does: almost every
    business Reveal sells to is a service business, and the safe failure is
    under-reporting an ecommerce account rather than pointing a plumber's budget
    at a purchase event that never fires.
    """
    return _CAMPAIGN_SHAPES.get((goal or "").strip().lower(), _CAMPAIGN_SHAPES["lead"])
