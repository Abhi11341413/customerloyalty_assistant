"""FR-06: billing-reminder controls from verified billing status (R1).

days past due 1-7 -> stage 1, 8-20 -> stage 2, 21-45 -> stage 3, > 45 -> escalate;
an open dispute always escalates. Boundaries come from config/policy.yaml.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Literal

from src.config import policy
from src.schemas import CustomerProfile

StageResult = int | Literal["escalate", "none"]


def stage_for_days(days_past_due: int, open_dispute: bool = False) -> StageResult:
    r = policy().reminders
    if days_past_due <= 0:
        return "escalate" if open_dispute else "none"
    if open_dispute:
        return "escalate"
    if days_past_due > r.escalate_after_days:
        return "escalate"
    for s in r.stages:
        if s.min_days <= days_past_due <= s.max_days:
            return s.stage
    return "escalate"   # a gap in the configured stages is never auto-drafted


def reminder_stage(bills: Iterable[dict], today: date, disputes: Iterable[dict] | None = None) -> StageResult:
    """bills: dicts with due_date, status ('paid' | 'partial' | 'unpaid' | 'disputed')."""
    bills = list(bills)
    open_dispute = any(b["status"] == "disputed" for b in bills) or bool(list(disputes or []))
    overdue = [b for b in bills if b["status"] in ("unpaid", "partial", "disputed")
               and _as_date(b["due_date"]) < today]
    days = max(((today - _as_date(b["due_date"])).days for b in overdue), default=0)
    return stage_for_days(days, open_dispute)


def reminder_stage_for_profile(profile: CustomerProfile) -> StageResult:
    return stage_for_days(profile.max_days_past_due, profile.open_dispute)


def stage_config(stage: int):
    return next(s for s in policy().reminders.stages if s.stage == stage)


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
