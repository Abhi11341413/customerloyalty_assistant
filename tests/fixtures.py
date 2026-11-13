"""Fixture builders shared by every stage's tests (no DB, no network)."""
from __future__ import annotations

from datetime import date, timedelta

from src.config import policy
from src.schemas import CustomerProfile, RecentContact

TODAY: date = policy().reference_date


def make_profile(**overrides) -> CustomerProfile:
    """A clean, active, on-time Fiber customer. Override fields to build each case."""
    base = dict(
        customer_id="T-0001", as_of=TODAY, first_name="Alex",
        tenure_months=18, contract="One year", plan_name="Fiber 12-Month", plan_type="Fiber",
        services=["Fiber internet", "Phone line", "Streaming TV"], monthly_charge=70.35,
        payment_method="Credit card (automatic)", loyalty_tier="Silver", last_offer_accepted=None,
        marketing_consent=True, service_consent=True, max_marketing_per_30d=2,
        usage_last3_gb=120.0, usage_prev3_gb=118.0, usage_change_pct=0.0169,
        days_since_last_activity=14, active_months_12=12,
        bills_count=12, on_time_rate=1.0, overdue_bill_count=0, overdue_amount=0.0,
        max_days_past_due=0, open_dispute=False, contradictory_billing=False,
        reminder_bill_id=None, amount_due=None, due_date=None,
        marketing_sent_30d=0, recent_contacts=[], offers_sent={}, redeemed_offer_ids=[],
        churn_risk=0.10,
    )
    base.update(overrides)
    return CustomerProfile(**base)


def overdue(days: int, amount: float = 70.35, **kw) -> CustomerProfile:
    """A customer whose oldest unpaid bill is `days` past due."""
    return make_profile(customer_id=kw.pop("customer_id", f"T-OD{days}"), overdue_bill_count=1,
                        overdue_amount=amount, max_days_past_due=days, reminder_bill_id="B-T1",
                        amount_due=amount, due_date=TODAY - timedelta(days=days),
                        on_time_rate=kw.pop("on_time_rate", 0.9), **kw)


SEGMENT_FIXTURES = {
    "consistent": (make_profile(customer_id="T-CONS"), ["consistent"]),
    "loyalty_candidate": (make_profile(customer_id="T-LOY", tenure_months=36, loyalty_tier="Gold"),
                          ["loyalty_candidate", "consistent"]),
    "declining_activity": (make_profile(customer_id="T-DEC", usage_last3_gb=70.0, usage_change_pct=-0.4068),
                           ["declining_activity"]),
    "dormant": (make_profile(customer_id="T-DOR", days_since_last_activity=75, active_months_12=9,
                             usage_last3_gb=0.0, usage_change_pct=-1.0), ["dormant"]),
    "payment_reminder": (overdue(12, customer_id="T-PAY"), ["payment_reminder", "consistent"]),
}

INSUFFICIENT = make_profile(customer_id="T-NEW", tenure_months=2, bills_count=2, active_months_12=2)


def recent(message_type: str, days_ago: int, offer_id: str | None = None) -> RecentContact:
    return RecentContact(message_type=message_type, days_ago=days_ago, offer_id=offer_id)
