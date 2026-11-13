"""Stage 2 tests: tables, lineage, fusion, profiles (FR-01), segments (FR-02), churn model, fairness."""
from __future__ import annotations

import random
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from src.data.build_tables import TABLE_COLUMNS, build, table_hashes, write_db
from src.profile.assembler import build_profile_from_rows
from src.schemas import CustomerProfile
from src.segmentation.rules import segment, segment_with_model
from tests.fixtures import INSUFFICIENT, SEGMENT_FIXTURES, make_profile, overdue

pytestmark = pytest.mark.stage2
ROOT = Path(__file__).resolve().parents[1]
CORE = ["customers", "monthly_activity", "bills", "contact_history"]
SENSITIVE = re.compile(r"gender|senior|partner|dependent|\bage\b|marital|marriage|location|country|state|zip|city")


def _cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def test_s2_t01_tables_and_columns(con):
    for t in CORE:
        assert _cols(con, t) == TABLE_COLUMNS[t], t
    assert _cols(con, "customers") == ["customer_id", "first_name", "email", "tenure_months", "contract",
                                       "plan_name", "services", "monthly_charge", "payment_method",
                                       "last_offer_accepted", "loyalty_tier", "marketing_consent",
                                       "service_consent", "max_marketing_per_30d", "churn_label"]


def test_s2_t02_row_counts(con):
    telco = pd.read_csv(ROOT / "data" / "raw" / "telco" / "telco_churn.csv")
    n = con.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    assert n == telco["customerID"].nunique()
    assert con.execute("SELECT COUNT(*) FROM monthly_activity").fetchone()[0] == n * 12
    no_bill = con.execute("SELECT COUNT(*) FROM customers c WHERE NOT EXISTS "
                          "(SELECT 1 FROM bills b WHERE b.customer_id = c.customer_id)").fetchone()[0]
    assert no_bill == 0


def test_s2_t03_referential_integrity(con):
    for t in ["monthly_activity", "bills", "contact_history", "fairness_attributes", "edge_cases"]:
        orphans = con.execute(f"SELECT COUNT(*) FROM {t} x LEFT JOIN customers c "
                              f"ON c.customer_id = x.customer_id WHERE c.customer_id IS NULL").fetchone()[0]
        assert orphans == 0, t
    for t, k in [("bills", "bill_id"), ("contact_history", "contact_id"), ("customers", "customer_id")]:
        assert con.execute(f"SELECT COUNT(*) - COUNT(DISTINCT {k}) FROM {t}").fetchone()[0] == 0


def test_s2_t04_reproducible(tmp_path):
    h = []
    for k in range(2):
        p = tmp_path / f"b{k}.db"
        write_db(build(seed=42, sample=300), p)
        h.append(table_hashes(p))
    assert h[0] == h[1]


def test_s2_t05_lineage(con):
    dd = pd.read_csv(ROOT / "data" / "processed" / "data_dictionary.csv")
    assert set(dd["lineage"]) <= {"real", "borrowed", "synthetic", "derived"}
    have = set(zip(dd["table"], dd["column"]))
    for t in TABLE_COLUMNS:
        for c in _cols(con, t):
            assert (t, c) in have, f"{t}.{c} missing from data_dictionary.csv"


def test_s2_t06_no_sensitive_fields(con):
    from src.segmentation.model import FEATURES
    assert not [c for c in _cols(con, "customers") if SENSITIVE.search(c.lower())]
    assert not [f for f in CustomerProfile.model_fields if SENSITIVE.search(f)]
    assert not [f for f in FEATURES if SENSITIVE.search(f)]


def test_s2_t07_fusion_sanity(con, today):
    credit = pd.read_csv(ROOT / "data" / "raw" / "credit" / "credit_default.csv")
    d7_late = (credit[["PAY_0", "PAY_2", "PAY_3", "PAY_4", "PAY_5", "PAY_6"]] >= 1).any(axis=1).mean()
    b = pd.read_sql("SELECT customer_id, due_date, paid_date, status FROM bills", con)
    due, paid = pd.to_datetime(b.due_date), pd.to_datetime(b.paid_date)
    late = (paid > due) | ((b.status != "paid") & (due < pd.Timestamp(today)))
    any_late = late.groupby(b.customer_id).any()
    assert abs(any_late.mean() - d7_late) <= 0.05
    c = pd.read_sql("SELECT customer_id, churn_label FROM customers", con).set_index("customer_id")
    telco = pd.read_csv(ROOT / "data" / "raw" / "telco" / "telco_churn.csv")
    assert abs(c.churn_label.mean() - (telco.Churn == "Yes").mean()) < 1e-9
    r = any_late.reindex(c.index).astype(int).corr(c.churn_label.astype(int))
    assert 0 < r < 0.9


@pytest.mark.parametrize("name", list(SEGMENT_FIXTURES))
def test_s2_t08_rule_segments(name):
    profile, expected = SEGMENT_FIXTURES[name]
    res = segment(profile)
    assert res.segments == expected
    assert res.primary_segment == expected[0]


def test_s2_t08_insufficient_history():
    res = segment(INSUFFICIENT)
    assert res.outcome == "insufficient_history" and res.segments == [] and res.primary_segment is None


def test_s2_t09_multilabel_precedence():
    """A loyal customer 5 days overdue gets both labels; primary = payment_reminder.
    Loyalty tolerates a bill inside the grace window (segments.loyalty_max_days_past_due)."""
    res = segment(overdue(5, tenure_months=48, on_time_rate=0.95))
    assert {"loyalty_candidate", "payment_reminder"} <= set(res.segments)
    assert res.primary_segment == "payment_reminder"
    # beyond the grace window the customer is no longer a loyalty candidate
    res = segment(overdue(30, tenure_months=48, on_time_rate=0.95))
    assert "loyalty_candidate" not in res.segments and res.primary_segment == "payment_reminder"


def test_s2_t10_result_contract(all_profiles):
    fields = set(CustomerProfile.model_fields)
    for p in all_profiles:
        r = segment(p)
        assert 0.0 <= r.confidence <= 1.0
        assert r.reason_codes
        assert all(rc.evidence_field in fields for rc in r.reason_codes)


def test_s2_t11_model_quality(pol):
    from src.segmentation.model import load_bundle, predict_risk
    b = load_bundle()
    assert b is not None, "train with: python -m src.segmentation.model"
    m = b["metrics"]
    assert m[m["chosen"]]["roc_auc"] >= m["logistic_regression"]["roc_auc"] - pol.model.auc_tolerance_vs_baseline
    assert m[m["chosen"]]["roc_auc"] >= pol.model.min_auc
    assert 0.0 <= predict_risk(make_profile()) <= 1.0


def test_s2_t12_reason_code_fidelity(all_profiles):
    rnd = random.Random(42)
    sample = rnd.sample(all_profiles, 100)
    sample += [p for p in all_profiles if p.churn_risk and p.churn_risk >= 0.6][:20]
    for p in sample:
        for rc in segment_with_model(p).reason_codes:
            v = getattr(p, rc.evidence_field)
            v = ";".join(v) if isinstance(v, list) else v
            assert rc.value == v, (p.customer_id, rc.code, rc.value, v)


def _di(df: pd.DataFrame, flag: str, attr: str) -> float:
    rates = df.groupby(attr)[flag].mean()
    return float(rates.min() / rates.max()) if rates.max() > 0 else 1.0


def test_s2_t13_fairness(con, all_profiles, pol):
    """Disparate impact within the legitimate eligibility band (tenure >= loyalty minimum).

    Unconditional rates differ by partner/dependents because tenure itself differs in the real
    Telco data; tenure is a permitted business criterion. See CLAUDE.md S2-T13 note.
    """
    fa = pd.read_sql("SELECT * FROM fairness_attributes", con).set_index("customer_id")
    rows = []
    offers_mod = None
    try:
        from src.policy import offers as offers_mod  # stage 3
    except ImportError:
        pass
    for p in all_profiles:
        r = segment(p)
        row = {"customer_id": p.customer_id, "loyal": "loyalty_candidate" in r.segments,
               "band": p.tenure_months >= pol.segments.loyalty_min_tenure_months}
        if offers_mod is not None and r.primary_segment and r.primary_segment != "payment_reminder":
            row["offer_eligible"] = any(e for _, e, _ in offers_mod.eligible_offers(p, r.primary_segment, p.as_of))
        else:
            row["offer_eligible"] = False
        rows.append(row)
    df = pd.DataFrame(rows).set_index("customer_id").join(fa)
    band = df[df.band]
    for attr in ["gender", "senior_citizen", "partner", "dependents"]:
        for flag in ["loyal", "offer_eligible"]:
            di = _di(band, flag, attr)
            assert 0.8 <= di <= 1.25, (attr, flag, di)


def test_s2_t14_profile_contract(all_profiles):
    assert len(all_profiles) > 7000
    data = all_profiles[0].model_dump()
    data["religion"] = "x"
    with pytest.raises(ValidationError):
        CustomerProfile(**data)
    data.pop("religion")
    data["gender"] = "Female"
    with pytest.raises(ValidationError):
        CustomerProfile(**data)


def test_s2_t15_point_in_time():
    as_of = date(2026, 6, 30)
    cust = {"customer_id": "T-PIT", "first_name": "Sam", "tenure_months": 30, "contract": "One year",
            "plan_name": "DSL 12-Month", "services": "DSL internet", "monthly_charge": 50.0,
            "payment_method": "Mailed check", "last_offer_accepted": None, "loyalty_tier": "Gold",
            "marketing_consent": 1, "service_consent": 1, "max_marketing_per_30d": 2}
    months = [date(2025, 11, 1)] + [date(2026, m, 1) for m in range(1, 11)] + [date(2025, 12, 1)]
    activity = [{"month": m, "usage_gb": 10.0 if m < date(2026, 6, 1) else 999.0, "active_days": 20} for m in months]
    bills = [
        {"bill_id": "B1", "bill_date": date(2026, 5, 1), "due_date": date(2026, 5, 16), "amount": 50.0,
         "paid_amount": 50.0, "paid_date": date(2026, 5, 10), "status": "paid"},
        {"bill_id": "B2", "bill_date": date(2026, 6, 1), "due_date": date(2026, 6, 16), "amount": 50.0,
         "paid_amount": 50.0, "paid_date": date(2026, 7, 20), "status": "paid"},   # paid after as_of
        {"bill_id": "B3", "bill_date": date(2026, 8, 1), "due_date": date(2026, 8, 16), "amount": 50.0,
         "paid_amount": 0.0, "paid_date": None, "status": "unpaid"},                # issued after as_of
    ]
    contacts = [{"sent_at": date(2026, 7, 1), "message_type": "reward", "offer_id": "LOY-DATA5",
                 "outcome": "redeemed"}]
    p = build_profile_from_rows(cust, activity, bills, contacts, as_of)
    assert p.bills_count == 2                       # B3 ignored
    assert p.overdue_bill_count == 1 and p.reminder_bill_id == "B2"   # B2 unpaid as of 30 June
    assert p.max_days_past_due == (as_of - date(2026, 6, 16)).days
    assert p.usage_last3_gb == 30.0                 # June (999) is not a finished month yet
    assert p.redeemed_offer_ids == [] and p.offers_sent == {}
    assert p.tenure_months == 30 - 5                # rolled back from the build date (Nov 2026)
