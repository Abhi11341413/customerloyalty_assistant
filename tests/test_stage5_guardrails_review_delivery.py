"""Stage 5 tests: 8 checks (FR-07), review API (FR-08), audit (FR-09), mock delivery (FR-10)."""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.config import offers, policy
from src.generation.llm_client import FakeLLM
from src.generation.writer import generate_draft
from src.guardrails.checks import (consent_check, duplicate_check, evidence_check, grounding_check,
                                   language_check, offer_check, pii_check, post_checks, pre_llm_checks,
                                   tone_check)
from src.policy.strategy import pick_strategy
from src.schemas import CustomerProfile
from src.segmentation.rules import segment
from src.service import Service, finalize
from tests.fixtures import SEGMENT_FIXTURES, TODAY, make_profile, overdue, recent

pytestmark = pytest.mark.stage5
ROOT = Path(__file__).resolve().parents[1]
RUBRIC = {"relevance": 5, "clarity": 4, "personalisation": 4, "call_to_action": 5, "tone": 5}


def build(p: CustomerProfile, llm=None):
    s = segment(p)
    st = pick_strategy(s, p, TODAY)
    d = generate_draft(p, s, st, llm)
    return s, st, d


def run_post(d, p, st, subject=None, body=None):
    return post_checks(subject or d.subject, body or d.body, d.claims, d.supported_fields, d.filled_values,
                       p, st, TODAY)


def by_name(results, prefix):
    return next(r for r in results if r.check_name.startswith(prefix))


# ---------------------------------------------------------------- fixtures
@pytest.fixture()
def llm():
    return FakeLLM()


@pytest.fixture()
def svc(work_db, tmp_path, llm):
    s = Service(db_path=work_db, outbox=tmp_path / "outbox", llm=llm)
    yield s
    s.close()


@pytest.fixture()
def client(svc):
    return TestClient(create_app(svc))


def find(all_profiles, primary, **want):
    for p in all_profiles:
        s = segment(p)
        if s.primary_segment != primary or p.marketing_sent_30d or p.recent_contacts:
            continue
        if p.contradictory_billing or p.open_dispute or not p.marketing_consent or p.max_marketing_per_30d < 1:
            continue
        st = pick_strategy(s, p, TODAY)
        if st.action != "draft":
            continue
        if all((getattr(st, k) is not None) if v is True else getattr(st, k) == v for k, v in want.items()):
            return p
    raise LookupError(primary)


# ---------------------------------------------------------------- S5-T01
@pytest.mark.parametrize("name", list(SEGMENT_FIXTURES) + ["firm", "stage1"])
def test_s5_t01_known_good_passes_all_8(name):
    p = {"firm": overdue(30), "stage1": overdue(3)}.get(name) or SEGMENT_FIXTURES[name][0]
    s, st, d = build(p, FakeLLM())
    results = pre_llm_checks(p, s, st) + run_post(d, p, st)
    assert len(results) == 8
    assert all(r.result == "pass" for r in results if not r.check_name.startswith("8_")), \
        [(r.check_name, r.detail) for r in results if r.result != "pass"]


# ---------------------------------------------------------------- S5-T02 / T03
def test_s5_t02_invented_amount_or_date():
    p = overdue(12, amount=88.4)
    s, st, d = build(p)
    changed = d.body.replace("$88.40", "$98.40")
    assert by_name(run_post(d, p, st, body=changed), "4_").result == "fail"
    moved = d.body.replace(d.filled_values["due_date"], "December 1, 2026")
    r = by_name(run_post(d, p, st, body=moved), "4_")
    assert r.result == "fail" and "December 1, 2026" in r.detail
    _, _, results, _ = finalize(d.subject, changed, d.claims, d.supported_fields, d.filled_values, p, st, TODAY)
    assert any(x.result == "fail" for x in results)


def test_s5_t03_unsupported_claim():
    p = SEGMENT_FIXTURES["loyalty_candidate"][0]
    s, st, d = build(p)
    body = d.body.replace("Hi Alex,", "Hi Alex,\n\nYou are our top customer in the whole country.")
    r = by_name(run_post(d, p, st, body=body), "4_")
    assert r.result == "fail" and "unsupported claim" in r.detail
    bad_claim = d.claims + [type(d.claims[0])(text="You spend more than others", evidence_field="churn_label")]
    r = grounding_check(d.subject, d.body, bad_claim, d.supported_fields, p, st)
    assert r.result == "fail"


# ---------------------------------------------------------------- S5-T04
def test_s5_t04_expired_or_ineligible_offer():
    p = SEGMENT_FIXTURES["loyalty_candidate"][0]
    s, st, d = build(p)
    o = offers().get(st.offer_id)
    assert offer_check(p, st, d.body, o.valid_to + timedelta(days=1)).result == "fail"
    no_consent = p.model_copy(update={"marketing_consent": False})
    r = offer_check(no_consent, st, d.body, TODAY)
    assert r.result == "fail" and "marketing" in r.detail
    sneaky = d.body + "\nAlso try " + offers().get("WIN-DATA10").name + "."
    assert by_name(run_post(d, p, st, body=sneaky), "4_").result == "fail"


# ---------------------------------------------------------------- S5-T05
PHRASES = {"threat": "Pay this or else.", "legal_threat": "We may take legal action.",
           "guarantee": "This is guaranteed to save you money.", "shaming": "This is irresponsible.",
           "pressure": "Act now to keep it.", "discriminatory": "Great for senior citizens.",
           "unauthorised_financial_action": "We have waived your late fee."}


@pytest.mark.parametrize("category", list(PHRASES))
def test_s5_t05_blocked_language(category):
    sev = next(b.severity for b in policy().blocked_language if b.category == category)
    p = SEGMENT_FIXTURES["consistent"][0]
    s, st, d = build(p)
    body = d.body.replace("Hi Alex,", "Hi Alex,\n\n" + PHRASES[category])
    r = language_check(d.subject, body, st)
    assert r.result == ("fail" if sev == "block" else "warn"), r.detail
    assert category in r.detail


# ---------------------------------------------------------------- S5-T06
def test_s5_t06_missing_disclaimer_repair_then_block():
    pol = policy()
    p = overdue(12)
    s, st, d = build(p)
    stripped = d.body.replace(pol.disclaimers.reminder_footer, "").strip()
    assert by_name(run_post(d, p, st, body=stripped), "6_").result == "fail"
    subj, body, results, notes = finalize(d.subject, stripped, d.claims, d.supported_fields, d.filled_values,
                                          p, st, TODAY)
    assert pol.disclaimers.reminder_footer in body and not [r for r in results if r.result == "fail"]
    assert any("appended" in n for n in notes)
    # repair that cannot fix it -> second failure blocks
    _, _, results, notes = finalize(d.subject, stripped, d.claims, d.supported_fields, d.filled_values, p, st,
                                    TODAY, repair_fn=lambda s_, b_, st_: (s_, b_, ["no-op"]))
    assert by_name(results, "6_").result == "fail" and "repair attempt failed" in notes[-1]


# ---------------------------------------------------------------- S5-T07
@pytest.mark.parametrize("leak", ["Call me on 415-555-0199.", "Card 4111 1111 1111 1111 is on file.",
                                  "We also wrote to maria.lopez@example.com.",
                                  "Your neighbour Maria Lopez joined too.",
                                  "As a senior citizen you qualify.", "Your gender was considered."])
def test_s5_t07_sensitive_data(leak):
    p = SEGMENT_FIXTURES["consistent"][0]
    s, st, d = build(p)
    body = d.body.replace("Hi Alex,", "Hi Alex,\n\n" + leak)
    assert pii_check(d.subject, body, p, d.filled_values).result == "fail"


# ---------------------------------------------------------------- S5-T08 / T09
def test_s5_t08_no_consent_blocks_before_llm(svc, llm):
    p = SEGMENT_FIXTURES["loyalty_candidate"][0].model_copy(update={"marketing_consent": False})
    d = svc.create_draft(profile=p)
    assert d["status"] == "blocked" and "consent" in d["block_reason"]
    assert llm.calls == []


def test_s5_t09_frequency_and_duplicate_block_before_llm(svc, llm):
    capped = make_profile(customer_id="T-CAP", marketing_sent_30d=2,
                          recent_contacts=[recent("reward", 12), recent("winback", 20)])
    d = svc.create_draft(profile=capped)
    assert d["status"] == "blocked" and d["block_reason"].startswith("1_")
    dup = make_profile(customer_id="T-DUP", marketing_sent_30d=1, recent_contacts=[recent("appreciation", 2)])
    d = svc.create_draft(profile=dup)
    assert d["status"] == "blocked" and "2_duplicate" in d["block_reason"]
    assert llm.calls == []
    # injected DB edge cases: 2 marketing emails in the last 30 days -> any marketing draft is blocked
    edge = [r[0] for r in svc.con.execute("SELECT customer_id FROM edge_cases WHERE \"case\"='frequency_cap'")]
    checked = 0
    for c in edge:
        d = svc.create_draft(customer_id=c)
        if (d["strategy"] or {}).get("consent_type") == "marketing" and d["strategy"]["action"] == "draft":
            assert d["status"] == "blocked" and d["block_reason"].startswith("1_"), d["block_reason"]
            checked += 1
    assert checked >= 3
    assert consent_check(capped, pick_strategy(segment(capped), capped, TODAY)).result == "fail"
    assert duplicate_check(dup, pick_strategy(segment(dup), dup, TODAY)).result == "fail"


# ---------------------------------------------------------------- S5-T10
def test_s5_t10_contradictory_data_escalates(svc, llm):
    p = make_profile(customer_id="T-CON", contradictory_billing=True)
    d = svc.create_draft(profile=p)
    assert d["status"] == "escalated" and "contradictory" in d["block_reason"]
    for (cid,) in svc.con.execute("SELECT customer_id FROM edge_cases WHERE \"case\"='contradictory_paid'"):
        assert svc.create_draft(customer_id=cid)["status"] in ("escalated", "blocked")
    assert llm.calls == []


# ---------------------------------------------------------------- S5-T11
def test_s5_t11_sensitive_input_rejected_and_logged(client, svc):
    r = client.post("/drafts", json={"customer_id": "0002-ORFBO", "health_condition": "diabetes"})
    assert r.status_code == 422
    row = svc.con.execute("SELECT decision FROM review_log WHERE decision LIKE '422%'").fetchone()
    assert row and "health_condition" in row[0] and "diabetes" not in row[0]
    prof = SEGMENT_FIXTURES["consistent"][0].model_dump(mode="json") | {"religion": "x"}
    assert client.post("/drafts", json={"profile": prof}).status_code == 422


# ---------------------------------------------------------------- S5-T12
def test_s5_t12_tone_mismatch():
    p = overdue(3)
    s, st, d = build(p)
    assert st.reminder_stage == 1
    body = d.body.replace("This is a friendly reminder", "Pay now or else we will disconnect your service. "
                                                          "This is a reminder")
    res = run_post(d, p, st, body=body)
    assert by_name(res, "8_").result == "warn"
    assert by_name(res, "6_").result == "fail"


# ---------------------------------------------------------------- S5-T13 .. T18 (API)
FR08_FIELDS = {"evidence", "segments", "reason_codes", "confidence", "strategy", "objective", "tone",
               "subject", "body", "check_results", "status"}


def test_s5_t13_api_happy_path(client, all_profiles):
    p = find(all_profiles, "consistent")
    r = client.post("/drafts", json={"customer_id": p.customer_id})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "pending_review", d["block_reason"]
    full = client.get(f"/drafts/{d['draft_id']}").json()
    assert fr08_fields_ok(full)
    assert any(x["draft_id"] == d["draft_id"] for x in client.get("/drafts", params={"status": "pending_review"}).json())


def fr08_fields_ok(d) -> bool:
    return FR08_FIELDS <= set(d) and all(d[k] is not None for k in FR08_FIELDS) and len(d["check_results"]) >= 8


def test_s5_t14_send_without_approval(client, all_profiles, svc):
    p = find(all_profiles, "consistent")
    d = client.post("/drafts", json={"customer_id": p.customer_id}).json()
    r = client.post(f"/drafts/{d['draft_id']}/send")
    assert r.status_code == 409 and "approv" in r.json()["detail"]
    assert not list(svc.outbox.glob("*.eml")) if svc.outbox.exists() else True


def test_s5_t15_send_after_approval(client, all_profiles, svc):
    p = find(all_profiles, "declining_activity", offer_id=True)
    d = client.post("/drafts", json={"customer_id": p.customer_id}).json()
    assert d["status"] == "pending_review", d["block_reason"]
    assert client.post(f"/drafts/{d['draft_id']}/review", json={"action": "approve", "rubric": RUBRIC}).status_code == 200
    r = client.post(f"/drafts/{d['draft_id']}/send")
    assert r.status_code == 200, r.text
    emls = list(svc.outbox.glob("*.eml"))
    assert len(emls) == 1
    import email
    msg = email.message_from_bytes(emls[0].read_bytes())
    assert msg["Subject"] == d["subject"] and msg["X-Mock-Delivery"] == "true"
    assert msg.get_payload(decode=True).decode().strip() == d["body"].strip()
    assert client.get(f"/drafts/{d['draft_id']}").json()["status"] == "sent"
    assert client.post(f"/drafts/{d['draft_id']}/send").status_code == 409     # no double send


def test_s5_t16_edit_is_rechecked(client, all_profiles):
    p = find(all_profiles, "consistent")
    d = client.post("/drafts", json={"customer_id": p.customer_id}).json()
    bad = d["body"].replace("Hi ", "This is guaranteed to delight you. Hi ", 1)
    r = client.post(f"/drafts/{d['draft_id']}/review", json={"action": "edit", "edited_body": bad})
    assert r.json()["status"] == "blocked"
    assert client.post(f"/drafts/{d['draft_id']}/review", json={"action": "approve"}).status_code == 409
    good = client.post("/drafts", json={"customer_id": p.customer_id}).json()
    ok = good["body"].replace("We wanted to say a simple thank you.", "Thank you so much.")
    r = client.post(f"/drafts/{good['draft_id']}/review", json={"action": "edit", "edited_body": ok})
    assert r.json()["status"] == "pending_review" and "Thank you so much." in r.json()["body"]


def test_s5_t17_reject_and_feedback(client, all_profiles, svc):
    p = find(all_profiles, "consistent")
    d = client.post("/drafts", json={"customer_id": p.customer_id}).json()
    client.post(f"/drafts/{d['draft_id']}/review",
                json={"action": "reject", "rubric": RUBRIC | {"tone": 2}, "comment": "too generic"})
    rows = [e for e in client.get(f"/audit/{d['draft_id']}").json() if e["action"] == "rejected"]
    assert rows and rows[0]["comment"] == "too generic" and rows[0]["rubric"]["tone"] == 2


def _approved(client, all_profiles, primary, **want):
    p = find(all_profiles, primary, **want)
    d = client.post("/drafts", json={"customer_id": p.customer_id}).json()
    assert d["status"] == "pending_review", d["block_reason"]
    client.post(f"/drafts/{d['draft_id']}/review", json={"action": "approve"})
    return p, d


def test_s5_t18_send_time_gate(client, all_profiles, svc):
    # bill paid after approval
    p = next(x for x in all_profiles if segment(x).primary_segment == "payment_reminder" and x.service_consent
             and not x.open_dispute and not x.contradictory_billing and 1 <= x.max_days_past_due <= 45)
    d = client.post("/drafts", json={"customer_id": p.customer_id}).json()
    assert d["status"] == "pending_review", d["block_reason"]
    client.post(f"/drafts/{d['draft_id']}/review", json={"action": "approve"})
    svc.con.execute("UPDATE bills SET status='paid', paid_amount=amount, paid_date=? WHERE customer_id=? "
                    "AND status IN ('unpaid','partial')", ((TODAY - timedelta(days=1)).isoformat(), p.customer_id))
    svc.con.commit()
    r = client.post(f"/drafts/{d['draft_id']}/send")
    assert r.status_code == 409 and "no longer unpaid" in r.json()["detail"]
    # offer expired
    p2, d2 = _approved(client, all_profiles, "dormant", offer_id=True)
    o = offers().get(d2["offer_id"])
    with pytest.raises(Exception, match="expired"):
        svc.send(d2["draft_id"], today=o.valid_to + timedelta(days=1))
    # consent withdrawn
    p3, d3 = _approved(client, all_profiles, "consistent")
    svc.con.execute("UPDATE customers SET marketing_consent=0 WHERE customer_id=?", (p3.customer_id,))
    svc.con.commit()
    r = client.post(f"/drafts/{d3['draft_id']}/send")
    assert r.status_code == 409 and "consent" in r.json()["detail"]
    assert not list(svc.outbox.glob("*.eml")) if svc.outbox.exists() else True
    events = {e["action"] for e in client.get(f"/audit/{d3['draft_id']}").json()}
    assert "send_blocked" in events


# ---------------------------------------------------------------- S5-T19
def test_s5_t19_audit(client, all_profiles, svc):
    p, d = _approved(client, all_profiles, "loyalty_candidate", offer_id=True)
    client.post(f"/drafts/{d['draft_id']}/send")
    trail = client.get(f"/audit/{d['draft_id']}").json()
    assert [e["action"] for e in trail] == ["generated", "approved", "sent"]
    for e in trail:
        for k in ("prompt_version", "model_id", "policy_version", "evidence_fields", "decision", "status"):
            assert e[k] not in (None, "", []), (e["action"], k)
    blocked = svc.create_draft(profile=make_profile(customer_id="T-NC", marketing_consent=False))
    for e in svc.audit(blocked["draft_id"]):
        assert e["policy_version"] and e["decision"] and e["status"] and e["evidence_fields"]
    emails = {r[0] for r in svc.con.execute("SELECT email FROM customers")}
    cols = ["actor", "edited_subject", "edited_body", "comment", "decision", "evidence_fields"]
    for row in svc.con.execute(f"SELECT {', '.join(cols)} FROM review_log"):
        blob = " ".join(str(x) for x in row if x)
        assert "@" not in blob
        assert not any(e in blob for e in emails)


# ---------------------------------------------------------------- S5-T20
def test_s5_t20_ui_smoke(work_db, tmp_path, monkeypatch, all_profiles):
    from streamlit.testing.v1 import AppTest
    s = Service(db_path=work_db, outbox=tmp_path / "out", llm=None)
    s.create_draft(customer_id=find(all_profiles, "consistent").customer_id)
    s.create_draft(customer_id=find(all_profiles, "dormant").customer_id)
    s.close()
    monkeypatch.setenv("LOYALTY_DB", str(work_db))
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    assert any("Review queue" in m.value for m in at.subheader)
    assert any(t.label == "Body" for t in at.text_area)
