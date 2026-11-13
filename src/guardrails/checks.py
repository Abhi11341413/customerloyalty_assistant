"""FR-07: the 8 safety and compliance checks.

Checks 1-3 run BEFORE the LLM is called; 4-8 run on the rendered draft.
Each returns a CheckResult(check_name, result pass/warn/fail, detail, action).
Check 8 (tone) can only warn.
"""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache

from src.config import offers, policy
from src.generation.renderer import REMINDER_OBJECTIVES, long_date, money
from src.policy.contact import consent_ok, duplicate_ok, frequency_ok
from src.policy.offers import REASON_TEXT, check_offer
from src.schemas import CheckResult, CustomerProfile, SegmentResult, Strategy

MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
PCT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")
DATE_RE = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|"
                     r"December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
UNSUPPORTED_CLAIM = re.compile(r"\b(top|best|most (valued|loyal|important)|number one|#1|vip|one of our (best|top|most)|"
                               r"elite|premier customer|favou?rite customer|biggest)\b[^.]*\b(customer|client|member)s?\b|"
                               r"\b(you are|you're) (our )?(top|best|most valued|number one)\b", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PROTECTED_RE = re.compile(r"\b(gender|male|female|senior citizens?|your age|elderly|married|marital|spouse|partner status|"
                          r"dependents?|children|pregnan\w*|religio\w*|ethnic\w*|race|disabilit\w*|health condition|"
                          r"nationality|sexual)\b", re.I)


def _check(name: str, ok: bool, detail: str, fail_action: str = "block") -> CheckResult:
    return CheckResult(check_name=name, result="pass" if ok else "fail", detail=detail,
                       action="none" if ok else fail_action)


# ---------------------------------------------------------------- 1-3: before the LLM
def consent_check(profile: CustomerProfile, strategy: Strategy) -> CheckResult:
    ok, why = consent_ok(profile, strategy.consent_type or "marketing")
    if not ok:
        return _check("1_consent_preferences", False, why)
    ok, why2 = frequency_ok(profile, strategy.consent_type or "marketing")
    return _check("1_consent_preferences", ok, f"{why}; {why2}")


def duplicate_check(profile: CustomerProfile, strategy: Strategy) -> CheckResult:
    ok, why = duplicate_ok(profile, strategy.message_type or "")
    return _check("2_duplicate_outreach", ok, why)


def evidence_check(profile: CustomerProfile, seg: SegmentResult, strategy: Strategy) -> CheckResult:
    name = "3_evidence"
    if seg.outcome == "insufficient_history":
        return _check(name, False, f"insufficient history: tenure {profile.tenure_months} months", "escalate")
    if profile.contradictory_billing:
        return _check(name, False, "contradictory billing record: a bill marked paid has paid_amount < amount",
                      "escalate")
    if profile.open_dispute:
        return _check(name, False, "open billing dispute: a person must handle this account", "escalate")
    if strategy.action == "escalate":
        return _check(name, False, "; ".join(strategy.reasons), "escalate")
    if strategy.action != "draft":
        return _check(name, False, "; ".join(strategy.reasons) or "nothing to send")
    missing = []
    if strategy.objective in REMINDER_OBJECTIVES:
        for f in ("amount_due", "due_date", "reminder_bill_id"):
            if getattr(profile, f) in (None, ""):
                missing.append(f)
        if profile.amount_due is not None and profile.amount_due <= 0:
            missing.append("amount_due > 0")
    if strategy.offer_id and offers().get(strategy.offer_id) is None:
        missing.append(f"offer {strategy.offer_id} not in catalogue")
    if not profile.first_name:
        missing.append("first_name")
    if missing:
        return _check(name, False, f"required evidence missing: {missing}")
    fields = sorted({rc.evidence_field for rc in seg.reason_codes})
    return _check(name, True, f"evidence complete: {fields}")


def pre_llm_checks(profile: CustomerProfile, seg: SegmentResult, strategy: Strategy) -> list[CheckResult]:
    out = [evidence_check(profile, seg, strategy)]
    if strategy.action == "draft":
        out = [consent_check(profile, strategy), duplicate_check(profile, strategy)] + out
    return out


# ---------------------------------------------------------------- 4-8: on the rendered draft
def _allowed_values(profile: CustomerProfile, strategy: Strategy) -> tuple[set[str], set[str], set[str]]:
    amounts, dates, pcts = set(), set(), set()
    if profile.amount_due is not None:
        amounts.add(money(profile.amount_due))
    if profile.due_date is not None:
        dates.add(long_date(profile.due_date))
    if strategy.offer_id:
        o = offers().get(strategy.offer_id)
        if o:
            dates.add(long_date(o.valid_to))
            for t in (o.display_value, o.terms):
                amounts |= {m.replace(" ", "") for m in MONEY_RE.findall(t)}
                pcts |= {p.replace(" ", "") for p in PCT_RE.findall(t)}
            pcts |= {p.replace(" ", "") for p in PCT_RE.findall(o.display_value)}
    return amounts, dates, pcts


def _norm_money(m: str) -> str:
    m = m.replace(" ", "")
    return m if "." in m else m  # "$10" stays "$10"


def grounding_check(subject: str, body: str, claims, supported_fields: list[str],
                    profile: CustomerProfile, strategy: Strategy) -> CheckResult:
    name = "4_grounding"
    text = subject + "\n" + body
    amounts, dates, pcts = _allowed_values(profile, strategy)
    problems = []
    for m in MONEY_RE.findall(text):
        if _norm_money(m) not in amounts:
            problems.append(f"amount {m} not in verified data")
    for d in DATE_RE.findall(text):
        if d not in dates:
            problems.append(f"date '{d}' not in verified data")
    for p in PCT_RE.findall(text):
        if p.replace(" ", "") not in pcts:
            problems.append(f"percentage {p} not in verified data")
    plan_names = _all_plan_names()
    for pn in plan_names:
        if pn != profile.plan_name and re.search(rf"\b{re.escape(pn)}\b", text):
            problems.append(f"plan '{pn}' is not the customer's plan")
    cat = offers()
    for o in cat.offers:
        if o.offer_id in text or (o.name in text and o.offer_id != strategy.offer_id):
            problems.append(f"offer '{o.name}' mentioned but not the approved offer for this draft")
    m = UNSUPPORTED_CLAIM.search(text)
    if m:
        problems.append(f"unsupported claim: '{m.group(0)}'")
    fields = set(CustomerProfile.model_fields)
    for c in claims:
        ev = c.evidence_field if hasattr(c, "evidence_field") else c.get("evidence_field")
        if ev not in supported_fields or ev not in fields | {"offer_id"}:
            problems.append(f"claim without supporting evidence: '{getattr(c, 'text', c)}' -> {ev}")
    return _check(name, not problems, "; ".join(problems) or "all amounts, dates, plans, offers and claims verified")


@lru_cache(maxsize=1)
def _all_plan_names() -> tuple[str, ...]:
    return tuple(f"{t} {c}" for t in ("Fiber", "DSL", "Voice") for c in ("Flex", "12-Month", "24-Month"))


def offer_check(profile: CustomerProfile, strategy: Strategy, body: str, today: date) -> CheckResult:
    name = "5_offer_validity"
    if not strategy.offer_id:
        named = [o.name for o in offers().offers if o.name in body]
        return _check(name, not named, f"offers mentioned without approval: {named}" if named else "no offer in this email")
    o = offers().get(strategy.offer_id)
    if o is None:
        return _check(name, False, f"offer {strategy.offer_id} does not exist")
    if strategy.objective in REMINDER_OBJECTIVES:
        return _check(name, False, "offers are never allowed in payment reminders")
    ok, reasons = check_offer(o, profile, strategy.segment or "", today)
    detail = (f"{o.offer_id} valid and eligible" if ok else
              f"{o.offer_id} not eligible: " + ", ".join(REASON_TEXT.get(r, r) for r in reasons))
    return _check(name, ok, detail)


def language_check(subject: str, body: str, strategy: Strategy) -> CheckResult:
    pol = policy()
    text = subject + "\n" + body
    blocks, warns = [], []
    for b in pol.blocked_language:
        m = re.search(b.pattern, text, re.I)
        if m:
            (blocks if b.severity == "block" else warns).append(f"{b.category}: '{m.group(0)}'")
    missing = []
    if strategy.objective in REMINDER_OBJECTIVES:
        if pol.disclaimers.reminder_footer not in body:
            missing.append("dispute/support footer")
        if pol.links.pay_link not in body:
            missing.append("pay link")
        if pol.links.dispute_link not in body:
            missing.append("dispute path")
        if pol.links.support_contact not in body:
            missing.append("support contact")
    else:
        if pol.disclaimers.unsubscribe not in body:
            missing.append("unsubscribe line")
        if strategy.offer_id:
            o = offers().get(strategy.offer_id)
            if o and o.terms not in body:
                missing.append("offer terms")
    if blocks or missing:
        detail = "; ".join(([f"blocked language - {', '.join(blocks)}"] if blocks else []) +
                           ([f"missing required content - {', '.join(missing)}"] if missing else []))
        return CheckResult(check_name="6_language_required_content", result="fail", detail=detail, action="block")
    if warns:
        return CheckResult(check_name="6_language_required_content", result="warn",
                           detail="pressure language - " + ", ".join(warns), action="warn")
    return CheckResult(check_name="6_language_required_content", result="pass",
                       detail="no blocked language; required content present")


@lru_cache(maxsize=1)
def _analyzer():
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        nlp = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
        return AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    except Exception:  # spaCy model missing -> regex-only fallback
        return None


def pii_check(subject: str, body: str, profile: CustomerProfile, filled_values: dict[str, str]) -> CheckResult:
    name = "7_sensitive_data"
    pol = policy()
    text = subject + "\n" + body
    scrub = text
    for url in (pol.links.pay_link, pol.links.dispute_link, pol.links.support_contact, pol.links.unsubscribe_link):
        scrub = scrub.replace(url, " ")
    problems = []
    if PHONE_RE.search(scrub):
        problems.append("phone number")
    for m in CARD_RE.findall(scrub):
        if len(re.sub(r"\D", "", m)) >= 13:
            problems.append("card or account number")
            break
    for m in EMAIL_RE.findall(scrub):
        problems.append(f"email address {m}")
    pm = PROTECTED_RE.search(scrub)
    if pm:
        problems.append(f"protected attribute '{pm.group(0)}'")
    an = _analyzer()
    if an is not None:
        allowed_words = {w.lower() for v in filled_values.values() for w in re.findall(r"[A-Za-z]+", v)}
        allowed_words |= {"hi", "there", "dear", "thank", "thanks"}
        for r in an.analyze(scrub, language="en", score_threshold=0.3,
                            entities=["PERSON", "PHONE_NUMBER", "CREDIT_CARD", "EMAIL_ADDRESS", "IBAN_CODE",
                                      "US_SSN", "US_BANK_NUMBER"]):
            span = scrub[r.start:r.end]
            if r.entity_type == "PERSON":
                rest = [w for w in re.findall(r"[A-Za-z]+", span) if w.lower() not in allowed_words]
                if rest and r.score >= 0.8:
                    problems.append(f"another person's name '{span.strip()}'")
            elif r.entity_type == "PHONE_NUMBER" and r.score < 0.4:
                continue
            else:
                problems.append(r.entity_type.lower().replace("_", " "))
    problems = list(dict.fromkeys(problems))
    return _check(name, not problems, "; ".join(problems) or "no sensitive data found")


TONE_LEXICON = {
    "threat": re.compile(r"\b(or else|final warning|legal|court|collections?|disconnect|terminate|suspend|penalt\w*)\b", re.I),
    "firm": re.compile(r"\b(must|required|immediately|now need|overdue|settle)\b", re.I),
    "warm": re.compile(r"\b(thank\w*|grateful|appreciate\w*|celebrate|glad|happy|missed you)\b", re.I),
    "pressure": re.compile(r"\b(act now|hurry|last chance|don't miss)\b", re.I),
}


def tone_check(draft, strategy: Strategy, llm=None) -> CheckResult:
    """Check 8: warn-only. Uses an LLM judge if one is given, else a transparent lexicon."""
    name = "8_tone"
    text = f"{draft.subject}\n{draft.body}"
    if llm is not None and hasattr(llm, "judge_tone"):
        ok, why = llm.judge_tone(text, strategy.tone)
        return CheckResult(check_name=name, result="pass" if ok else "warn", detail=why,
                           action="none" if ok else "warn")
    issues = []
    if TONE_LEXICON["threat"].search(text):
        issues.append("threatening wording for any tone")
    if strategy.objective == "courteous_reminder" and strategy.reminder_stage == 1 and \
            len(TONE_LEXICON["firm"].findall(text)) >= 3:
        issues.append("too firm for a stage-1 courteous reminder")
    if strategy.objective in ("appreciation", "reward") and not TONE_LEXICON["warm"].search(text):
        issues.append("no warm or grateful wording")
    if strategy.objective == "reengagement_offer" and TONE_LEXICON["pressure"].search(text):
        issues.append("pressure wording in a low-pressure email")
    if issues:
        return CheckResult(check_name=name, result="warn", detail=f"tone vs '{strategy.tone}': " + "; ".join(issues),
                           action="warn")
    return CheckResult(check_name=name, result="pass", detail=f"tone consistent with '{strategy.tone}'")


def post_checks(subject: str, body: str, claims, supported_fields: list[str], filled_values: dict[str, str],
                profile: CustomerProfile, strategy: Strategy, today: date, draft_for_tone=None, llm=None) -> list[CheckResult]:
    class _D:  # minimal view for the tone check
        pass
    d = draft_for_tone
    if d is None:
        d = _D()
        d.subject, d.body = subject, body
    return [grounding_check(subject, body, claims, supported_fields, profile, strategy),
            offer_check(profile, strategy, body, today),
            language_check(subject, body, strategy),
            pii_check(subject, body, profile, filled_values),
            tone_check(d, strategy, llm)]


def blocking(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.result == "fail"]
