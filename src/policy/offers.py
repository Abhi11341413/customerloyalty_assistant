"""FR-05: deterministic offer and reward eligibility (R1). No model, no LLM.

Each offer is checked against every rule; an ineligible offer carries the exact list
of failed rule codes, an eligible one the list of rules it passed.
"""
from __future__ import annotations

from datetime import date

from src.config import Offer, offers, policy
from src.schemas import CustomerProfile

# rule code -> human text (used by the reviewer UI and the evaluation report)
REASON_TEXT = {
    "not_approved": "offer is not approved",
    "wrong_segment": "offer is not for this segment",
    "not_yet_valid": "offer is not valid yet",
    "expired": "offer has expired",
    "wrong_channel": "offer is not available by email",
    "no_marketing_consent": "customer has not agreed to marketing",
    "tenure_too_short": "customer tenure is below the offer minimum",
    "overdue": "customer has an overdue bill",
    "wrong_plan_type": "offer does not apply to the customer's plan",
    "max_per_customer_reached": "customer already received this offer the maximum number of times",
    "already_redeemed": "customer already redeemed this offer",
}


def check_offer(offer: Offer, profile: CustomerProfile, segment: str, today: date,
                channel: str = "email") -> tuple[bool, list[str]]:
    e = offer.eligibility
    failed: list[str] = []
    passed: list[str] = []

    def rule(ok: bool, code: str, ok_text: str) -> None:
        (passed.append(ok_text) if ok else failed.append(code))

    rule(offer.approval_status == "approved", "not_approved", "approved")
    rule(segment in offer.for_segments, "wrong_segment", f"for segment {segment}")
    rule(offer.valid_from <= today, "not_yet_valid", f"valid from {offer.valid_from}")
    rule(today <= offer.valid_to, "expired", f"valid until {offer.valid_to}")
    rule(offer.channel == channel, "wrong_channel", f"channel {channel}")
    rule((not e.needs_marketing_consent) or profile.marketing_consent, "no_marketing_consent", "marketing consent")
    rule(profile.tenure_months >= e.min_tenure_months, "tenure_too_short",
         f"tenure {profile.tenure_months} >= {e.min_tenure_months}")
    rule(profile.max_days_past_due <= e.max_days_overdue, "overdue",
         f"days overdue {profile.max_days_past_due} <= {e.max_days_overdue}")
    rule("any" in e.plan_types or profile.plan_type in e.plan_types, "wrong_plan_type",
         f"plan type {profile.plan_type}")
    rule(profile.offers_sent.get(offer.offer_id, 0) < offer.max_per_customer, "max_per_customer_reached",
         f"sent {profile.offers_sent.get(offer.offer_id, 0)} < {offer.max_per_customer}")
    rule(offer.offer_id not in profile.redeemed_offer_ids, "already_redeemed", "not redeemed before")
    return (not failed), (failed if failed else passed)


def eligible_offers(profile: CustomerProfile, segment: str, today: date | None = None,
                    channel: str = "email") -> list[tuple[Offer, bool, list[str]]]:
    today = today or policy().reference_date
    return [(o, *check_offer(o, profile, segment, today, channel)) for o in offers().offers]


def choose_offer(profile: CustomerProfile, segment: str, today: date | None = None) -> tuple[Offer | None, list[str]]:
    """First eligible offer of an allowed type for the segment (catalogue order), plus a trace."""
    allowed = set(policy().strategies[segment].allowed_offer_types)
    trace = []
    for offer, ok, reasons in eligible_offers(profile, segment, today):
        if offer.offer_type not in allowed:
            continue
        trace.append(f"{offer.offer_id}: {'eligible' if ok else 'not eligible - ' + ', '.join(reasons)}")
        if ok:
            return offer, trace
    return None, trace
