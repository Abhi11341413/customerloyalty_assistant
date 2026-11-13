"""Pydantic contracts shared by every stage (CLAUDE.md section 8.4)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SegmentName = Literal["payment_reminder", "dormant", "declining_activity", "loyalty_candidate", "consistent"]
ObjectiveName = Literal["appreciation", "reward", "reengagement_offer", "courteous_reminder", "firm_reminder"]
DraftStatus = Literal["blocked", "escalated", "pending_review", "approved", "rejected", "sent"]


class RecentContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_type: str
    days_ago: int
    offer_id: str | None = None


class CustomerProfile(BaseModel):
    """Minimal, allowlisted customer context (FR-01).

    extra="forbid": unknown or sensitive fields (gender, religion, health, ...) are rejected.
    No email address and no surname: delivery reads the address from the DB at send time.
    """
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    as_of: date
    first_name: str
    # plan / loyalty
    tenure_months: int = Field(ge=0)
    contract: str
    plan_name: str
    plan_type: str                       # Fiber / DSL / Voice
    services: list[str]
    monthly_charge: float
    payment_method: str
    loyalty_tier: str
    last_offer_accepted: str | None = None
    # consent and preferences
    marketing_consent: bool
    service_consent: bool
    max_marketing_per_30d: int = Field(ge=0)
    # activity
    usage_last3_gb: float
    usage_prev3_gb: float
    usage_change_pct: float | None      # (last3 - prev3) / prev3; None if prev3 == 0
    days_since_last_activity: int | None
    active_months_12: int
    # billing
    bills_count: int
    on_time_rate: float | None
    overdue_bill_count: int
    overdue_amount: float
    max_days_past_due: int
    open_dispute: bool
    contradictory_billing: bool
    reminder_bill_id: str | None = None  # oldest unpaid overdue bill
    amount_due: float | None = None
    due_date: date | None = None
    # engagement
    marketing_sent_30d: int
    recent_contacts: list[RecentContact] = []
    offers_sent: dict[str, int] = {}
    redeemed_offer_ids: list[str] = []
    # model output (derived)
    churn_risk: float | None = None

    @classmethod
    def evidence_fields(cls) -> list[str]:
        return list(cls.model_fields)


class ReasonCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    text: str
    evidence_field: str
    value: float | int | str | bool | None


class SegmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str
    segments: list[SegmentName]
    primary_segment: SegmentName | None
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[ReasonCode] = Field(min_length=1)
    outcome: Literal["segmented", "insufficient_history", "no_segment"] = "segmented"


class Strategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment: SegmentName | None
    objective: ObjectiveName | None
    tone: str | None
    template_id: str | None
    message_type: str | None
    consent_type: Literal["marketing", "service"] | None
    offer_id: str | None = None
    reminder_stage: int | None = None
    must_include: list[str] = []
    action: Literal["draft", "escalate", "none"] = "draft"
    reasons: list[str] = []


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    evidence_field: str


class DraftOutput(BaseModel):
    """What the LLM must return: text with {{placeholders}}, never raw facts."""
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    claims: list[Claim] = []
    cta: str


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_name: str
    result: Literal["pass", "warn", "fail"]
    detail: str = ""
    action: Literal["none", "block", "escalate", "warn"] = "none"
