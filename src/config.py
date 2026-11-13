"""Load config/policy.yaml and config/offers.yaml through Pydantic so a bad file fails at start-up.

policy_version = first 12 hex chars of SHA-256 over both files (recorded in every draft and audit row).
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "policy.yaml"
OFFERS_PATH = ROOT / "config" / "offers.yaml"

Objective = Literal["appreciation", "reward", "reengagement_offer", "courteous_reminder", "firm_reminder"]
SEGMENTS = ("payment_reminder", "dormant", "declining_activity", "loyalty_candidate", "consistent")
Segment = Literal["payment_reminder", "dormant", "declining_activity", "loyalty_candidate", "consistent"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SegmentThresholds(_Strict):
    min_tenure_months: int
    dormant_days_inactive: int
    declining_usage_drop: float
    declining_churn_risk: float
    loyalty_min_tenure_months: int
    loyalty_max_days_past_due: int
    on_time_rate_min: float
    consistent_usage_band: float
    precedence: list[Segment]

    @field_validator("precedence")
    @classmethod
    def _all_segments(cls, v):
        if sorted(v) != sorted(SEGMENTS):
            raise ValueError(f"precedence must list each of {SEGMENTS} exactly once")
        return v


class StrategyRule(_Strict):
    objective: Objective | Literal["by_reminder_stage"]
    tone: str
    template_id: str
    allowed_offer_types: list[str]
    consent_type: Literal["marketing", "service"]
    message_type: str


class ReminderStage(_Strict):
    stage: int
    min_days: int
    max_days: int
    objective: Objective
    tone: str
    template_id: str


class Reminders(_Strict):
    stages: list[ReminderStage]
    escalate_after_days: int
    open_dispute: Literal["escalate"]
    must_include: list[str]


class Contact(_Strict):
    marketing_cap_per_30d: int
    duplicate_window_days: int
    marketing_message_types: list[str]
    service_message_types: list[str]


class Links(_Strict):
    pay_link: str
    dispute_link: str
    support_contact: str
    unsubscribe_link: str


class Disclaimers(_Strict):
    unsubscribe: str
    reminder_footer: str
    offer_terms_prefix: str


class BlockedPattern(_Strict):
    pattern: str
    category: str
    severity: Literal["block", "warn"]

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v):
        re.compile(v)
        return v


class Generation(_Strict):
    model_id: str
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    timeout_s: int
    max_retries: int
    subject_max_chars: int
    body_min_words: int
    body_max_words: int
    allowed_placeholders: list[str]


class Fusion(_Strict):
    payment_profiles: dict[str, str]
    late_tilt: dict[str, float]


class ModelCfg(_Strict):
    min_auc: float
    auc_tolerance_vs_baseline: float
    test_size: float


class Policy(_Strict):
    version_label: str
    reference_date: date
    seed: int
    currency: str
    data: dict[str, int]
    segments: SegmentThresholds
    strategies: dict[Segment, StrategyRule]
    reminders: Reminders
    offer_must_include: list[str]
    marketing_must_include: list[str]
    contact: Contact
    links: Links
    disclaimers: Disclaimers
    blocked_language: list[BlockedPattern]
    generation: Generation
    fusion: Fusion
    model: ModelCfg
    targets: dict[str, float]

    @model_validator(mode="after")
    def _every_segment_has_strategy(self):
        missing = set(SEGMENTS) - set(self.strategies)
        if missing:
            raise ValueError(f"strategies missing for segments: {sorted(missing)}")
        return self


class Eligibility(_Strict):
    min_tenure_months: int
    max_days_overdue: int
    needs_marketing_consent: bool
    plan_types: list[str]


class Offer(_Strict):
    offer_id: str
    name: str
    offer_type: Literal["perk", "reward", "engagement", "winback"]
    value: float
    unit: str
    terms: str
    valid_from: date
    valid_to: date
    channel: Literal["email", "sms", "push"]
    for_segments: list[Segment]
    eligibility: Eligibility
    max_per_customer: int
    approval_status: Literal["approved", "pending", "rejected"]
    source_url: str

    @property
    def display_value(self) -> str:
        v = int(self.value) if float(self.value).is_integer() else self.value
        if self.unit.startswith("%"):
            return f"{v}{self.unit}"
        if self.unit.startswith("USD"):
            return f"${v}{self.unit[3:]}"
        return f"{v} {self.unit}"


class OfferCatalogue(_Strict):
    offers: list[Offer]

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [o.offer_id for o in self.offers]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate offer_id in offers.yaml")
        return self

    def get(self, offer_id: str) -> Offer | None:
        return next((o for o in self.offers if o.offer_id == offer_id), None)


def load_policy(path: Path = POLICY_PATH) -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_offers(path: Path = OFFERS_PATH) -> OfferCatalogue:
    return OfferCatalogue.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def compute_policy_version(policy_path: Path = POLICY_PATH, offers_path: Path = OFFERS_PATH) -> str:
    h = hashlib.sha256()
    for p in (policy_path, offers_path):
        h.update(Path(p).read_bytes())
    return h.hexdigest()[:12]


@lru_cache(maxsize=1)
def policy() -> Policy:
    return load_policy()


@lru_cache(maxsize=1)
def offers() -> OfferCatalogue:
    return load_offers()


@lru_cache(maxsize=1)
def policy_version() -> str:
    return compute_policy_version()


__all__ = ["Policy", "Offer", "OfferCatalogue", "policy", "offers", "policy_version",
           "load_policy", "load_offers", "compute_policy_version", "SEGMENTS", "Field"]
