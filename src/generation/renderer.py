"""Fill {{placeholders}} from verified data and append required content by code (R2).

Every value comes from the CustomerProfile (built from the DB), config/offers.yaml or
config/policy.yaml. Disclaimers, offer terms and the dispute/support footer are appended
verbatim from YAML, never written by the LLM.
"""
from __future__ import annotations

import re
from datetime import date

from src.config import offers, policy
from src.schemas import CustomerProfile, Strategy

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NAME_OK = re.compile(r"^[A-Za-z][A-Za-z \-']{0,39}$")
INJECTION_HINTS = re.compile(r"(ignore|instruction|prompt|system|assistant|offer|discount|free|%|\d|http|<|>|\{|\})", re.I)
REMINDER_OBJECTIVES = {"courteous_reminder", "firm_reminder"}


def placeholders_in(text: str) -> list[str]:
    return PLACEHOLDER_RE.findall(text)


def sanitize_first_name(name: str | None) -> tuple[str, bool]:
    """Letters, spaces, hyphens (and apostrophes), <= 40 chars, at most 3 words.
    Anything that looks like an instruction is replaced by a neutral greeting and flagged."""
    raw = (name or "").strip()
    flagged = bool(INJECTION_HINTS.search(raw)) or len(raw.split()) > 3 or len(raw) > 40
    clean = re.sub(r"[^A-Za-z \-']", "", raw).strip()
    if flagged or not clean or not NAME_OK.match(clean):
        return "there", True
    return clean, False


def money(x: float) -> str:
    return f"${x:,.2f}"


def long_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def fill_values(profile: CustomerProfile, strategy: Strategy) -> dict[str, str]:
    """placeholder -> rendered value, each traceable to a DB field or approved config."""
    pol = policy()
    name, _ = sanitize_first_name(profile.first_name)
    values = {
        "first_name": name,
        "plan_name": profile.plan_name,
        "pay_link": pol.links.pay_link,
        "dispute_link": pol.links.dispute_link,
        "support_contact": pol.links.support_contact,
    }
    if profile.amount_due is not None:
        values["amount_due"] = money(profile.amount_due)
    if profile.due_date is not None:
        values["due_date"] = long_date(profile.due_date)
    if strategy.offer_id:
        o = offers().get(strategy.offer_id)
        values.update({"offer_name": o.name, "offer_value": o.display_value, "offer_expiry": long_date(o.valid_to)})
    return values


def required_footer(strategy: Strategy) -> str:
    pol = policy()
    if strategy.objective in REMINDER_OBJECTIVES:
        return pol.disclaimers.reminder_footer
    parts = []
    if strategy.offer_id:
        o = offers().get(strategy.offer_id)
        parts.append(pol.disclaimers.offer_terms_prefix + o.terms)
    parts.append(pol.disclaimers.unsubscribe)
    return "\n\n".join(parts)


class RenderError(ValueError):
    pass


def render_text(text: str, values: dict[str, str]) -> str:
    missing = [p for p in placeholders_in(text) if p not in values]
    if missing:
        raise RenderError(f"no verified value for placeholder(s): {sorted(set(missing))}")
    return PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)


def render(subject: str, body: str, profile: CustomerProfile, strategy: Strategy) -> tuple[str, str, dict[str, str]]:
    values = fill_values(profile, strategy)
    used = {p: values[p] for p in set(placeholders_in(subject) + placeholders_in(body)) if p in values}
    r_subject = render_text(subject, values).strip()
    r_body = render_text(body, values).strip() + "\n\n" + required_footer(strategy)
    return r_subject, r_body, used


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'$.,/-]+\b", text))
