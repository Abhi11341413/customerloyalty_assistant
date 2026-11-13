"""Consent, frequency cap and duplicate-outreach rules (used by guardrail checks 1 and 2)."""
from __future__ import annotations

from src.config import policy
from src.schemas import CustomerProfile


def consent_ok(profile: CustomerProfile, consent_type: str) -> tuple[bool, str]:
    if consent_type == "marketing":
        if not profile.marketing_consent:
            return False, "no marketing consent"
        if profile.max_marketing_per_30d == 0:
            return False, "customer opted out of marketing emails (cap 0)"
        return True, "marketing consent given"
    if not profile.service_consent:
        return False, "no service-message consent"
    return True, "service consent given"


def frequency_ok(profile: CustomerProfile, consent_type: str) -> tuple[bool, str]:
    """Marketing cap: policy cap or the customer's own lower cap, per 30 days."""
    if consent_type != "marketing":
        return True, "service messages are not frequency-capped"
    cap = min(policy().contact.marketing_cap_per_30d, profile.max_marketing_per_30d)
    sent = profile.marketing_sent_30d
    if sent >= cap:
        return False, f"frequency cap reached: {sent} marketing emails in 30 days (cap {cap})"
    return True, f"{sent} of {cap} marketing emails used in 30 days"


def duplicate_ok(profile: CustomerProfile, message_type: str) -> tuple[bool, str]:
    window = policy().contact.duplicate_window_days
    hits = [c for c in profile.recent_contacts if c.message_type == message_type and c.days_ago < window]
    if hits:
        return False, f"duplicate outreach: '{message_type}' sent {min(c.days_ago for c in hits)} days ago (window {window} days)"
    return True, f"no '{message_type}' message in the last {window} days"
