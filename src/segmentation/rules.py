"""FR-02: transparent, rule-based segmentation with reason codes and confidence.

Every threshold comes from config/policy.yaml (section 9 of CLAUDE.md). Multi-label:
a customer gets every segment whose rule fires; the primary segment follows the
configured precedence. Confidence: facts -> 1.0; threshold rules -> 0.6 at the
threshold rising linearly to 1.0 as the value moves further past it.
"""
from __future__ import annotations

from src.config import policy
from src.schemas import CustomerProfile, ReasonCode, SegmentResult


def _scaled(distance: float, scale: float) -> float:
    """0.6 exactly at the threshold, 1.0 once `distance` reaches `scale`."""
    if scale <= 0:
        return 1.0
    return round(min(1.0, 0.6 + 0.4 * max(0.0, distance) / scale), 4)


def segment(profile: CustomerProfile, churn_reasons: list[dict] | None = None) -> SegmentResult:
    t = policy().segments
    p = profile
    reasons: dict[str, list[ReasonCode]] = {}
    conf: dict[str, float] = {}

    # ---- insufficient history: no segment, escalate
    if p.tenure_months < t.min_tenure_months:
        return SegmentResult(
            customer_id=p.customer_id, segments=[], primary_segment=None, confidence=1.0,
            outcome="insufficient_history",
            reason_codes=[ReasonCode(code="INSUFFICIENT_HISTORY",
                                     text=f"Customer for {p.tenure_months} months; at least "
                                          f"{t.min_tenure_months} months are needed",
                                     evidence_field="tenure_months", value=p.tenure_months)])

    # ---- payment reminder: an unpaid bill past its due date (fact)
    if p.overdue_bill_count > 0:
        reasons["payment_reminder"] = [
            ReasonCode(code="BILL_OVERDUE", text=f"Oldest unpaid bill is {p.max_days_past_due} days past due",
                       evidence_field="max_days_past_due", value=p.max_days_past_due),
            ReasonCode(code="AMOUNT_OVERDUE", text="Overdue balance on the account",
                       evidence_field="overdue_amount", value=p.overdue_amount)]
        conf["payment_reminder"] = 1.0

    # ---- dormant: no activity for >= N days
    dormant = p.days_since_last_activity is None or p.days_since_last_activity >= t.dormant_days_inactive
    if dormant:
        d = p.days_since_last_activity
        reasons["dormant"] = [ReasonCode(
            code="NO_RECENT_ACTIVITY",
            text=("No recorded activity" if d is None else f"No activity for {d} days"),
            evidence_field="days_since_last_activity", value=d)]
        conf["dormant"] = 1.0 if d is None else _scaled(d - t.dormant_days_inactive, t.dormant_days_inactive)

    # ---- declining activity: usage drop >= X, or churn risk >= Y
    if not dormant:
        dec = []
        c_drop = c_risk = 0.0
        if p.usage_change_pct is not None and p.usage_change_pct <= -t.declining_usage_drop:
            dec.append(ReasonCode(code="USAGE_DECLINE",
                                  text=f"Usage in the last 3 months is {abs(p.usage_change_pct):.0%} below the 3 months before",
                                  evidence_field="usage_change_pct", value=p.usage_change_pct))
            c_drop = _scaled(-p.usage_change_pct - t.declining_usage_drop, t.declining_usage_drop)
        if p.churn_risk is not None and p.churn_risk >= t.declining_churn_risk:
            dec.append(ReasonCode(code="HIGH_CHURN_RISK", text=f"Churn-risk score {p.churn_risk:.2f}",
                                  evidence_field="churn_risk", value=p.churn_risk))
            c_risk = _scaled(p.churn_risk - t.declining_churn_risk, 1 - t.declining_churn_risk)
            for r in (churn_reasons or [])[:3]:
                val = getattr(p, r["evidence_field"])
                dec.append(ReasonCode(code="CHURN_DRIVER", text=r["text"], evidence_field=r["evidence_field"],
                                      value=";".join(val) if isinstance(val, list) else val))
        if dec:
            reasons["declining_activity"] = dec
            conf["declining_activity"] = max(c_drop, c_risk)

    good_payer = p.on_time_rate is not None and p.on_time_rate >= t.on_time_rate_min
    within_grace = p.max_days_past_due <= t.loyalty_max_days_past_due

    # ---- loyalty candidate: long tenure, pays on time, nothing overdue beyond the grace window
    if p.tenure_months >= t.loyalty_min_tenure_months and good_payer and within_grace:
        reasons["loyalty_candidate"] = [
            ReasonCode(code="LONG_TENURE", text=f"Customer for {p.tenure_months} months",
                       evidence_field="tenure_months", value=p.tenure_months),
            ReasonCode(code="PAYS_ON_TIME", text=f"{p.on_time_rate:.0%} of bills paid on time",
                       evidence_field="on_time_rate", value=p.on_time_rate),
            ReasonCode(code="NOTHING_SERIOUSLY_OVERDUE",
                       text=("No overdue bills" if p.overdue_bill_count == 0 else
                             f"Only {p.max_days_past_due} days overdue (within the grace window)"),
                       evidence_field="max_days_past_due", value=p.max_days_past_due)]
        conf["loyalty_candidate"] = min(
            _scaled(p.tenure_months - t.loyalty_min_tenure_months, t.loyalty_min_tenure_months),
            _scaled(p.on_time_rate - t.on_time_rate_min, 1 - t.on_time_rate_min))

    # ---- consistent: active, pays on time, usage stable within +/- band
    if (not dormant and good_payer and p.usage_change_pct is not None
            and abs(p.usage_change_pct) <= t.consistent_usage_band):
        reasons["consistent"] = [
            ReasonCode(code="STABLE_USAGE", text=f"Usage changed by {p.usage_change_pct:+.0%} over the last 3 months",
                       evidence_field="usage_change_pct", value=p.usage_change_pct),
            ReasonCode(code="PAYS_ON_TIME", text=f"{p.on_time_rate:.0%} of bills paid on time",
                       evidence_field="on_time_rate", value=p.on_time_rate)]
        conf["consistent"] = _scaled(t.consistent_usage_band - abs(p.usage_change_pct), t.consistent_usage_band)

    segs = [s for s in t.precedence if s in reasons]
    if not segs:
        return SegmentResult(
            customer_id=p.customer_id, segments=[], primary_segment=None, confidence=1.0, outcome="no_segment",
            reason_codes=[ReasonCode(code="NO_SEGMENT_MATCH",
                                     text="No segment rule matched (e.g. usage growing or mixed payment record)",
                                     evidence_field="usage_change_pct", value=p.usage_change_pct)])
    primary = segs[0]
    ordered = reasons[primary] + [r for s in segs[1:] for r in reasons[s]]
    return SegmentResult(customer_id=p.customer_id, segments=segs, primary_segment=primary,
                         confidence=conf[primary], reason_codes=ordered)


def segment_with_model(profile: CustomerProfile) -> SegmentResult:
    """Segment and, when churn risk drives `declining_activity`, attach SHAP reasons."""
    t = policy().segments
    churn_reasons = None
    if profile.churn_risk is not None and profile.churn_risk >= t.declining_churn_risk:
        from src.segmentation.model import explain
        churn_reasons = explain(profile)
    return segment(profile, churn_reasons)
