"""FR-04: grounded message generation.

The LLM sees only the objective, tone, qualitative facts, the allowed placeholders and
the approved example. It never sees names, emails, amounts, dates, plan names or offer
terms (R2). Output must match DraftOutput; invalid output gets one retry, then the
approved template is used (fallback=True).
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from src.config import ROOT, policy
from src.generation.llm_client import LLMClient, LLMError, example_json
from src.generation.renderer import (REMINDER_OBJECTIVES, RenderError, placeholders_in, render,
                                     sanitize_first_name, word_count)
from src.schemas import Claim, CustomerProfile, DraftOutput, SegmentResult, Strategy

PROMPTS = Path(__file__).resolve().parent / "prompts"
TEMPLATES_PATH = ROOT / "config" / "templates.yaml"
OFFER_PH = {"offer_name", "offer_value", "offer_expiry"}
RAW_FACT = re.compile(r"(\$\s?\d|\d+(\.\d+)?\s?%|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b|\b(19|20)\d{2}\b|"
                      r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d)")

# reason code -> (evidence field, segment it belongs to, qualitative fact for the LLM)
FACTS = {
    "LONG_TENURE": ("tenure_months", "loyalty_candidate", None),
    "PAYS_ON_TIME": ("on_time_rate", None, "pays bills on time"),
    "NOTHING_SERIOUSLY_OVERDUE": ("max_days_past_due", "loyalty_candidate", "account is in good standing"),
    "STABLE_USAGE": ("usage_change_pct", "consistent", "uses the service steadily"),
    "USAGE_DECLINE": ("usage_change_pct", "declining_activity", "has been using the service less in recent months"),
    "NO_RECENT_ACTIVITY": ("days_since_last_activity", "dormant", "has not used the service for a while"),
    "BILL_OVERDUE": ("due_date", "payment_reminder", "a bill is past its due date (use {{due_date}})"),
    "AMOUNT_OVERDUE": ("amount_due", "payment_reminder", "there is an unpaid balance (use {{amount_due}})"),
}


class GeneratedDraft(BaseModel):
    subject: str                 # rendered
    body: str                    # rendered, with required footer
    raw_subject: str             # with placeholders
    raw_body: str
    claims: list[Claim]
    cta: str
    filled_values: dict[str, str]
    supported_fields: list[str]
    template_id: str
    prompt_version: str
    model_id: str
    fallback: bool
    fallback_reason: str | None = None
    name_flagged: bool = False
    llm_calls: int = 0


@lru_cache(maxsize=1)
def templates() -> dict[str, dict]:
    return yaml.safe_load(TEMPLATES_PATH.read_text(encoding="utf-8"))["templates"]


def template_draft(template_id: str, has_offer: bool) -> DraftOutput:
    t = templates()[template_id]
    parts = [t["body"].strip()]
    if has_offer and t.get("offer_paragraph"):
        parts.append(t["offer_paragraph"].strip())
    parts.append(t["closing"].strip())
    cta = t["cta"] if (has_offer or t["objective"] in REMINDER_OBJECTIVES) else t.get("cta_no_offer", t["cta"])
    return DraftOutput(subject=t["subject"], body="\n\n".join(parts),
                       claims=[Claim(**c) for c in t.get("claims", [])], cta=cta)


def facts_for(profile: CustomerProfile, seg: SegmentResult, strategy: Strategy) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rc in seg.reason_codes:
        if rc.code not in FACTS:
            continue          # churn score and model drivers are never shown to the writer
        field, owner, text = FACTS[rc.code]
        if owner not in (None, strategy.segment):
            continue
        if rc.code == "PAYS_ON_TIME" and strategy.segment == "payment_reminder":
            continue
        if rc.code == "LONG_TENURE":
            years = profile.tenure_months // 12
            text = f"has been a customer for {years}+ years" if years >= 2 else "has been a customer for over a year"
        if (field, text) not in out:
            out.append((field, text))
    out.append(("plan_name", "the plan is referred to as {{plan_name}}"))
    if strategy.offer_id:
        out.append(("offer_id", "an approved offer is available: refer to it only as {{offer_name}}, "
                                "{{offer_value}} and {{offer_expiry}}; its terms are added by code"))
    return out


def prompt_template_text(strategy: Strategy) -> str:
    system = (PROMPTS / "system.md").read_text(encoding="utf-8")
    objective = (PROMPTS / f"{strategy.objective}.md").read_text(encoding="utf-8")
    example = template_draft(strategy.template_id, bool(strategy.offer_id))
    return system + "\n---\n" + objective + "\n---\n" + example.model_dump_json()


def prompt_version_for(strategy: Strategy) -> str:
    return hashlib.sha256(prompt_template_text(strategy).encode("utf-8")).hexdigest()[:12]


def build_prompt(profile: CustomerProfile, seg: SegmentResult, strategy: Strategy) -> tuple[str, str]:
    g = policy().generation
    system = (PROMPTS / "system.md").read_text(encoding="utf-8")
    objective = (PROMPTS / f"{strategy.objective}.md").read_text(encoding="utf-8")
    ex = template_draft(strategy.template_id, bool(strategy.offer_id))
    allowed = [p for p in g.allowed_placeholders
               if not (p in OFFER_PH and not strategy.offer_id)
               and not (p in {"amount_due", "due_date", "pay_link"} and strategy.objective not in REMINDER_OBJECTIVES)]
    facts = "\n".join(f"- [{f}] {t}" for f, t in facts_for(profile, seg, strategy))
    must = [f"{{{{{m}}}}}" for m in strategy.must_include if m in ("amount_due", "due_date", "pay_link",
                                                                  "offer_name", "offer_value", "offer_expiry")]
    user = (f"<task>\n{objective.strip()}\n</task>\n\n"
            f"<context>\nTemplate: {strategy.template_id}\nTone: {strategy.tone}\n"
            f"Facts (evidence field in brackets):\n{facts}\n"
            f"Allowed placeholders: {', '.join('{{' + p + '}}' for p in allowed)}\n"
            f"Must appear in the body: {', '.join(must) if must else 'nothing extra'}\n</context>\n\n"
            f"<approved_example>\n{example_json(ex.subject, ex.body, [c.model_dump() for c in ex.claims], ex.cta)}\n"
            f"</approved_example>\n\n"
            "Write a new email for this customer in the same spirit as the approved example. "
            "You may reword it, but keep every rule.")
    return system, user


def validate_output(d: DraftOutput, profile: CustomerProfile, strategy: Strategy,
                    supported: set[str]) -> list[str]:
    g = policy().generation
    problems: list[str] = []
    text = d.subject + "\n" + d.body
    used = set(placeholders_in(text))
    unknown = used - set(g.allowed_placeholders)
    if unknown:
        problems.append(f"unknown placeholders: {sorted(unknown)}")
    if "{{" in PLACEHOLDER_STRIP.sub("", text) or "}}" in PLACEHOLDER_STRIP.sub("", text):
        problems.append("malformed placeholder")
    if strategy.objective in REMINDER_OBJECTIVES:
        for p in ("amount_due", "due_date", "pay_link"):
            if f"{{{{{p}}}}}" not in d.body:
                problems.append(f"missing {{{{{p}}}}}")
        if d.body.count("{{pay_link}}") != 1:
            problems.append("pay link must appear exactly once")
        if used & OFFER_PH:
            problems.append("reminders must not mention offers")
    elif strategy.offer_id:
        for p in sorted(OFFER_PH):
            if f"{{{{{p}}}}}" not in d.body:
                problems.append(f"missing {{{{{p}}}}}")
    elif used & OFFER_PH:
        problems.append("offer placeholders used without an approved offer")
    if RAW_FACT.search(PLACEHOLDER_STRIP.sub("", text)):
        problems.append("raw amount, percentage or date written instead of a placeholder")
    bad_claims = [c.evidence_field for c in d.claims if c.evidence_field not in supported]
    if bad_claims:
        problems.append(f"claims cite unsupported evidence: {bad_claims}")
    if not d.cta.strip() or len(d.cta) > 120 or d.cta.count(". ") > 0:
        problems.append("exactly one short call to action required")
    if not problems:
        try:
            subj, body, _ = render(d.subject, d.body, profile, strategy)
        except RenderError as e:
            return [str(e)]
        if len(subj) > g.subject_max_chars:
            problems.append(f"subject longer than {g.subject_max_chars} characters")
        n = word_count(body)
        if not g.body_min_words <= n <= g.body_max_words:
            problems.append(f"body has {n} words (allowed {g.body_min_words}-{g.body_max_words})")
    return problems


PLACEHOLDER_STRIP = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}")


def generate_draft(profile: CustomerProfile, seg: SegmentResult, strategy: Strategy,
                   llm: LLMClient | None) -> GeneratedDraft:
    if strategy.action != "draft" or not strategy.template_id:
        raise ValueError(f"strategy does not allow a draft: {strategy.action}")
    g = policy().generation
    _, name_flagged = sanitize_first_name(profile.first_name)
    supported = {f for f, _ in facts_for(profile, seg, strategy)} | {"first_name", "plan_name"}
    supported |= {c.evidence_field for c in template_draft(strategy.template_id, bool(strategy.offer_id)).claims}
    pv = prompt_version_for(strategy)

    draft: DraftOutput | None = None
    reason = None
    calls = 0
    if llm is None:
        reason = "no LLM configured"
    else:
        system, user = build_prompt(profile, seg, strategy)
        for attempt in range(1 + g.max_retries):
            calls += 1
            try:
                raw = llm.complete(system, user)
                candidate = DraftOutput.model_validate_json(raw)
            except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as e:
                reason = f"attempt {attempt + 1}: {type(e).__name__}: {str(e)[:160]}"
                continue
            problems = validate_output(candidate, profile, strategy, supported)
            if not problems:
                draft = candidate
                reason = None
                break
            reason = f"attempt {attempt + 1}: " + "; ".join(problems)

    fallback = draft is None
    if fallback:
        draft = template_draft(strategy.template_id, bool(strategy.offer_id))
    subject, body, used = render(draft.subject, draft.body, profile, strategy)
    return GeneratedDraft(subject=subject, body=body, raw_subject=draft.subject, raw_body=draft.body,
                          claims=draft.claims, cta=draft.cta, filled_values=used,
                          supported_fields=sorted(supported), template_id=strategy.template_id,
                          prompt_version=pv, model_id="template" if (fallback and llm is None) else
                          (llm.model_id if llm else "template"),
                          fallback=fallback, fallback_reason=reason, name_flagged=name_flagged, llm_calls=calls)
