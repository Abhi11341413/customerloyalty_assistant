"""Stage 3 tests: config validation, strategy (FR-03), offers (FR-05), reminders (FR-06), contact rules."""
from __future__ import annotations

import ast
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.config import POLICY_PATH, load_offers, load_policy, offers, policy_version
from src.policy.contact import duplicate_ok, frequency_ok
from src.policy.offers import check_offer, eligible_offers
from src.policy.reminders import reminder_stage, stage_for_days
from src.policy.strategy import pick_strategy
from src.profile.assembler import build_profile_from_rows
from src.schemas import CustomerProfile, ReasonCode, SegmentResult
from src.segmentation.rules import segment
from tests.fixtures import SEGMENT_FIXTURES, TODAY, make_profile, overdue, recent

pytestmark = pytest.mark.stage3
ROOT = Path(__file__).resolve().parents[1]
LOYAL = make_profile(customer_id="T-LOY", tenure_months=36, loyalty_tier="Gold")


def _seg(*segments: str, cid: str = "T-X") -> SegmentResult:
    return SegmentResult(customer_id=cid, segments=list(segments), primary_segment=segments[0],
                         confidence=1.0, reason_codes=[ReasonCode(code="X", text="x",
                                                                  evidence_field="tenure_months", value=1)])


# ---------------------------------------------------------------- S3-T01
def test_s3_t01_config_validation(tmp_path):
    assert load_policy() and load_offers() and len(policy_version()) == 12
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    bad = dict(raw)
    bad.pop("reminders")
    p = tmp_path / "missing.yaml"
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError, match="reminders"):
        load_policy(p)
    bad = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    bad["strategies"]["consistent"]["objective"] = "upsell_everything"
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError, match="objective"):
        load_policy(p)


# ---------------------------------------------------------------- S3-T02 / T03
EXPECTED = {
    "consistent": ("appreciation", "warm, grateful"),
    "loyalty_candidate": ("reward", "warm, celebratory"),
    "declining_activity": ("reengagement_offer", "encouraging, helpful"),
    "dormant": ("reengagement_offer", "friendly, low pressure"),
    "payment_reminder": ("courteous_reminder", "clear, courteous"),   # fixture is 12 days past due -> stage 2
}


@pytest.mark.parametrize("name", list(SEGMENT_FIXTURES))
def test_s3_t02_strategy_mapping(name):
    profile, _ = SEGMENT_FIXTURES[name]
    st = pick_strategy(segment(profile), profile, TODAY)
    assert (st.objective, st.tone) == EXPECTED[name]
    assert st.segment == name and st.action == "draft"


def test_s3_t02_firm_reminder_stage3():
    p = overdue(30)
    st = pick_strategy(segment(p), p, TODAY)
    assert (st.objective, st.tone, st.reminder_stage) == ("firm_reminder", "firm but respectful", 3)


def test_s3_t03_precedence():
    p = overdue(10, days_since_last_activity=80)
    st = pick_strategy(_seg("consistent", "dormant", "payment_reminder"), p, TODAY)
    assert st.segment == "payment_reminder"
    st = pick_strategy(_seg("declining_activity", "dormant"), make_profile(), TODAY)
    assert st.segment == "dormant"
    st = pick_strategy(_seg("consistent", "loyalty_candidate"), LOYAL, TODAY)
    assert st.segment == "loyalty_candidate"


# ---------------------------------------------------------------- S3-T04 .. T06
def test_s3_t04_eligible_offer():
    st = pick_strategy(segment(LOYAL), LOYAL, TODAY)
    assert st.offer_id and st.offer_id.startswith("LOY-")
    offer = offers().get(st.offer_id)
    ok, reasons = check_offer(offer, LOYAL, "loyalty_candidate", TODAY)
    assert ok and "approved" in reasons and "marketing consent" in reasons


LOY = offers().get("LOY-DATA5")
CASES = {
    "wrong_segment": (LOY, LOYAL, "dormant", TODAY),
    "expired": (LOY, LOYAL, "loyalty_candidate", LOY.valid_to + timedelta(days=1)),
    "not_yet_valid": (LOY, LOYAL, "loyalty_candidate", LOY.valid_from - timedelta(days=1)),
    "not_approved": (offers().get("LOY-DISC10"), LOYAL, "loyalty_candidate", TODAY),
    "no_marketing_consent": (LOY, LOYAL.model_copy(update={"marketing_consent": False}), "loyalty_candidate", TODAY),
    "tenure_too_short": (LOY, LOYAL.model_copy(update={"tenure_months": 12}), "loyalty_candidate", TODAY),
    "overdue": (LOY, overdue(5, tenure_months=36), "loyalty_candidate", TODAY),
    "wrong_channel": (offers().get("ENG-SMSBOOST"), LOYAL, "declining_activity", TODAY),
    "wrong_plan_type": (offers().get("LOY-STREAM3"), LOYAL.model_copy(update={"plan_type": "Voice"}),
                        "loyalty_candidate", TODAY),
    "max_per_customer_reached": (LOY, LOYAL.model_copy(update={"offers_sent": {"LOY-DATA5": 1}}),
                                 "loyalty_candidate", TODAY),
    "already_redeemed": (LOY, LOYAL.model_copy(update={"redeemed_offer_ids": ["LOY-DATA5"]}),
                         "loyalty_candidate", TODAY),
}


@pytest.mark.parametrize("reason", list(CASES))
def test_s3_t05_ineligible_one_reason_each(reason):
    offer, profile, seg, today = CASES[reason]
    ok, reasons = check_offer(offer, profile, seg, today)
    assert not ok
    assert reasons == [reason]


def test_s3_t06_expiry_boundary():
    assert check_offer(LOY, LOYAL, "loyalty_candidate", LOY.valid_to)[0] is True
    assert check_offer(LOY, LOYAL, "loyalty_candidate", LOY.valid_to + timedelta(days=1))[0] is False
    # "valid_to = yesterday" seen from today
    assert check_offer(LOY, LOYAL, "loyalty_candidate", LOY.valid_to + timedelta(days=1))[1] == ["expired"]


# ---------------------------------------------------------------- S3-T07 .. T09
@pytest.mark.parametrize("days,expected", [(0, "none"), (1, 1), (7, 1), (8, 2), (20, 2),
                                           (21, 3), (45, 3), (46, "escalate")])
def test_s3_t07_reminder_boundaries(days, expected):
    assert stage_for_days(days) == expected
    bills = [{"due_date": TODAY - timedelta(days=days), "status": "unpaid" if days > 0 else "paid"}]
    assert reminder_stage(bills, TODAY) == expected


@pytest.mark.parametrize("days", [1, 10, 30, 60])
def test_s3_t08_open_dispute_escalates(days):
    assert stage_for_days(days, open_dispute=True) == "escalate"
    bills = [{"due_date": TODAY - timedelta(days=days), "status": "disputed"}]
    assert reminder_stage(bills, TODAY) == "escalate"
    p = overdue(days, open_dispute=True)
    st = pick_strategy(segment(p), p, TODAY)
    assert st.action == "escalate" and st.objective is None


@pytest.mark.parametrize("days", [3, 12, 30])
def test_s3_t09_reminder_content_rules(days):
    p = overdue(days, tenure_months=48, on_time_rate=0.95)
    st = pick_strategy(segment(p), p, TODAY)
    assert st.offer_id is None
    assert st.must_include == ["amount_due", "due_date", "pay_link", "support_contact", "dispute_link"]
    assert policy_version()


# ---------------------------------------------------------------- S3-T10
def test_s3_t10_frequency_and_duplicates():
    assert frequency_ok(make_profile(marketing_sent_30d=1), "marketing")[0]
    assert not frequency_ok(make_profile(marketing_sent_30d=2), "marketing")[0]        # 3rd in 30 days
    assert not frequency_ok(make_profile(marketing_sent_30d=1, max_marketing_per_30d=1), "marketing")[0]
    assert frequency_ok(make_profile(marketing_sent_30d=5), "service")[0]
    assert not duplicate_ok(make_profile(recent_contacts=[recent("appreciation", 3)]), "appreciation")[0]
    assert not duplicate_ok(make_profile(recent_contacts=[recent("appreciation", 6)]), "appreciation")[0]
    assert duplicate_ok(make_profile(recent_contacts=[recent("appreciation", 7)]), "appreciation")[0]
    assert duplicate_ok(make_profile(recent_contacts=[recent("reward", 2)]), "appreciation")[0]


# ---------------------------------------------------------------- S3-T11 .. T13
def test_s3_t11_determinism_no_ai():
    first = pick_strategy(segment(LOYAL), LOYAL, TODAY)
    for _ in range(100):
        assert pick_strategy(segment(LOYAL), LOYAL, TODAY) == first
    banned = re.compile(r"\b(anthropic|openai|lightgbm|sklearn|shap|joblib|src\.generation|src\.segmentation\.model|llm)\b")
    for f in (ROOT / "src" / "policy").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in mods if banned.search(m)], (f.name, mods)


def test_s3_t12_no_financial_actions():
    money = re.compile(r"(apply_?discount|charge_?(customer|card)?$|^charge|waive|refund|update_?bill|"
                       r"mark_?paid|set_?balance|credit_?account|adjust_?bill|write_?off)", re.I)
    found = []
    for f in (ROOT / "src").rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and money.search(n.name):
                found.append(f"{f.name}:{n.name}")
    assert not found


def test_s3_t13_protected_attributes_ignored():
    with pytest.raises(ValidationError):
        CustomerProfile(**(LOYAL.model_dump() | {"senior_citizen": True}))
    cust = {"customer_id": "T-P", "first_name": "Kim", "tenure_months": 40, "contract": "Two year",
            "plan_name": "Fiber 24-Month", "services": "Fiber internet;Phone line", "monthly_charge": 90.0,
            "payment_method": "Bank transfer (automatic)", "last_offer_accepted": None, "loyalty_tier": "Gold",
            "marketing_consent": 1, "service_consent": 1, "max_marketing_per_30d": 2}
    months = [date(2025, 11, 1)] + [date(2026, m, 1) for m in range(1, 11)]
    act = [{"month": m, "usage_gb": 40.0, "active_days": 25} for m in months]
    bills = [{"bill_id": f"B{m}", "bill_date": date(2026, m, 3), "due_date": date(2026, m, 18), "amount": 90.0,
              "paid_amount": 90.0, "paid_date": date(2026, m, 10), "status": "paid"} for m in range(1, 11)]
    outcomes = set()
    for extra in [{}, {"gender": "Female", "senior_citizen": 1, "partner": "Yes", "dependents": "No"},
                  {"gender": "Male", "senior_citizen": 0, "partner": "No", "dependents": "Yes"}]:
        p = build_profile_from_rows(cust | extra, act, bills, [], TODAY)
        s = segment(p)
        st = pick_strategy(s, p, TODAY)
        outcomes.add((tuple(s.segments), st.objective, st.offer_id, tuple(o.offer_id for o, ok, _ in
                                                                           eligible_offers(p, s.primary_segment, TODAY) if ok)))
    assert len(outcomes) == 1
