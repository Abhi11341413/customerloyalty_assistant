"""FR-03: map the primary segment to one approved objective, tone and template (R1).

Deterministic: same SegmentResult + CustomerProfile + today -> same Strategy.
"""
from __future__ import annotations

from datetime import date

from src.config import policy
from src.policy.offers import choose_offer
from src.policy.reminders import reminder_stage_for_profile, stage_config
from src.schemas import CustomerProfile, SegmentResult, Strategy


def pick_strategy(seg: SegmentResult, profile: CustomerProfile, today: date | None = None) -> Strategy:
    pol = policy()
    today = today or pol.reference_date

    if seg.outcome == "insufficient_history":
        return Strategy(segment=None, objective=None, tone=None, template_id=None, message_type=None,
                        consent_type=None, action="escalate",
                        reasons=[f"insufficient history: tenure {profile.tenure_months} months "
                                 f"< {pol.segments.min_tenure_months}"])
    if not seg.segments:
        return Strategy(segment=None, objective=None, tone=None, template_id=None, message_type=None,
                        consent_type=None, action="none", reasons=["no segment matched"])

    # apply precedence again here so the policy engine never trusts the caller's ordering
    primary = next(s for s in pol.segments.precedence if s in seg.segments)
    rule = pol.strategies[primary]

    if primary == "payment_reminder":
        stage = reminder_stage_for_profile(profile)
        base = dict(segment=primary, message_type=rule.message_type, consent_type=rule.consent_type,
                    offer_id=None)
        if stage == "escalate":
            why = ("open billing dispute" if profile.open_dispute
                   else f"{profile.max_days_past_due} days past due > {pol.reminders.escalate_after_days}")
            return Strategy(**base, objective=None, tone=None, template_id=None, action="escalate",
                            reasons=[f"reminder escalated to a person: {why}"])
        if stage == "none":
            return Strategy(**base, objective=None, tone=None, template_id=None, action="none",
                            reasons=["no bill past due"])
        sc = stage_config(stage)
        return Strategy(**base, objective=sc.objective, tone=sc.tone, template_id=sc.template_id,
                        reminder_stage=stage, must_include=list(pol.reminders.must_include),
                        reasons=[f"reminder stage {stage}: {profile.max_days_past_due} days past due "
                                 f"({sc.min_days}-{sc.max_days})", "offers are never included in reminders"])

    offer, trace = choose_offer(profile, primary, today)
    must = list(pol.offer_must_include) if offer else list(pol.marketing_must_include)
    reasons = [f"segment {primary} -> {rule.objective} ({rule.tone})"] + trace
    if offer is None:
        reasons.append("no eligible offer: message without an offer")
    return Strategy(segment=primary, objective=rule.objective, tone=rule.tone, template_id=rule.template_id,
                    message_type=rule.message_type, consent_type=rule.consent_type,
                    offer_id=offer.offer_id if offer else None, must_include=must, reasons=reasons)
