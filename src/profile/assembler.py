"""FR-01: build a minimal CustomerProfile from permitted data, as of a given date.

Only allowlisted fields are read. Bills, payments, activity and contacts after `as_of`
are ignored (point in time, S2-T15). Protected attributes are never read (R6).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.config import policy
from src.schemas import CustomerProfile, RecentContact

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "processed" / "loyalty.db"

CUSTOMER_FIELDS = ["customer_id", "first_name", "tenure_months", "contract", "plan_name", "services",
                   "monthly_charge", "payment_method", "last_offer_accepted", "loyalty_tier",
                   "marketing_consent", "service_consent", "max_marketing_per_30d"]
BILL_FIELDS = ["bill_id", "bill_date", "due_date", "amount", "paid_amount", "paid_date", "status"]
CONTACT_FIELDS = ["sent_at", "message_type", "offer_id", "outcome"]


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _d(v) -> date | None:
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _records(rows) -> list[dict]:
    return rows.to_dict("records") if isinstance(rows, pd.DataFrame) else list(rows)


def build_profile_from_rows(customer: dict[str, Any], activity, bills, contacts, as_of: date,
                            churn_risk: float | None = None,
                            build_date: date | None = None) -> CustomerProfile:
    """Pure function: rows in, profile out. Used by the DB path and by tests with inline data.

    activity / bills / contacts may be DataFrames or lists of dicts (ISO date strings or dates).
    """
    pol = policy()
    build_date = build_date or pol.reference_date
    cid = customer["customer_id"]

    # tenure is recorded at build time; roll it back for an earlier as_of
    tenure = max(int(customer["tenure_months"]) - max(0, _months_between(as_of, build_date)), 0)

    # ---- activity: full months that ended before as_of
    act = sorted(((_d(r["month"]), float(r["usage_gb"]), int(r["active_days"])) for r in _records(activity)),
                 key=lambda t: t[0])
    act = [a for a in act if _month_end(a[0]) < as_of]
    usage = [a[1] for a in act]
    last3 = round(sum(usage[-3:]), 2)
    prev3 = round(sum(usage[-6:-3]), 2)
    change = round((last3 - prev3) / prev3, 4) if prev3 > 0 else None
    active = [a for a in act if a[2] > 0]
    days_since = (as_of - _month_end(active[-1][0])).days if active else None

    # ---- bills as of the date (payments after as_of are unknown at that date)
    bl = []
    for r in _records(bills):
        bdate = _d(r["bill_date"])
        if bdate > as_of:
            continue
        due, paid_d = _d(r["due_date"]), _d(r.get("paid_date"))
        known = paid_d is not None and paid_d <= as_of
        status = r["status"] if (known or r["status"] == "disputed") else "unpaid"
        paid_amt = r.get("paid_amount")
        bl.append({"bill_id": r.get("bill_id"), "due": due, "paid_d": paid_d if known else None,
                   "amount": float(r["amount"]),
                   "paid": float(paid_amt) if known and paid_amt is not None else 0.0,
                   "status": status})
    due_passed = [x for x in bl if x["due"] < as_of]
    on_time = [x for x in due_passed if x["status"] == "paid" and x["paid_d"] <= x["due"]]
    on_time_rate = round(len(on_time) / len(due_passed), 4) if due_passed else None
    od = sorted((x for x in due_passed if x["status"] in ("unpaid", "partial", "disputed")),
                key=lambda x: x["due"])
    overdue_amount = round(sum(x["amount"] - x["paid"] for x in od), 2)
    max_dpd = max(((as_of - x["due"]).days for x in od), default=0)
    open_dispute = any(x["status"] == "disputed" for x in bl)
    contradictory = any(x["status"] == "paid" and x["paid"] + 0.005 < x["amount"] for x in bl)
    reminder = od[0] if od else None

    # ---- contacts up to as_of
    ch = []
    for r in _records(contacts):
        sent = _d(r["sent_at"])
        if sent <= as_of:
            oid = r.get("offer_id")
            ch.append({"type": r["message_type"], "days": (as_of - sent).days,
                       "offer": oid if isinstance(oid, str) and oid else None, "outcome": r.get("outcome")})
    mkt_types = set(pol.contact.marketing_message_types)
    recent = sorted((c for c in ch if c["days"] <= 30), key=lambda c: c["days"])
    offers_sent: dict[str, int] = {}
    for c in ch:
        if c["offer"]:
            offers_sent[c["offer"]] = offers_sent.get(c["offer"], 0) + 1
    redeemed = sorted({c["offer"] for c in ch if c["outcome"] == "redeemed" and c["offer"]})
    marketing_consent = bool(customer["marketing_consent"]) and \
        not any(c["outcome"] == "unsubscribed" for c in ch)

    return CustomerProfile(
        customer_id=cid,
        as_of=as_of,
        first_name=str(customer["first_name"]),
        tenure_months=tenure,
        contract=str(customer["contract"]),
        plan_name=str(customer["plan_name"]),
        plan_type=str(customer["plan_name"]).split(" ")[0],
        services=[s for s in str(customer["services"] or "").split(";") if s],
        monthly_charge=float(customer["monthly_charge"]),
        payment_method=str(customer["payment_method"]),
        loyalty_tier=str(customer["loyalty_tier"]),
        last_offer_accepted=customer.get("last_offer_accepted") or None,
        marketing_consent=marketing_consent,
        service_consent=bool(customer["service_consent"]),
        max_marketing_per_30d=int(customer["max_marketing_per_30d"]),
        usage_last3_gb=last3,
        usage_prev3_gb=prev3,
        usage_change_pct=change,
        days_since_last_activity=days_since,
        active_months_12=len(active),
        bills_count=len(bl),
        on_time_rate=on_time_rate,
        overdue_bill_count=len(od),
        overdue_amount=overdue_amount,
        max_days_past_due=int(max_dpd),
        open_dispute=open_dispute,
        contradictory_billing=contradictory,
        reminder_bill_id=reminder["bill_id"] if reminder else None,
        amount_due=round(reminder["amount"] - reminder["paid"], 2) if reminder else None,
        due_date=reminder["due"] if reminder else None,
        marketing_sent_30d=sum(1 for c in recent if c["type"] in mkt_types),
        recent_contacts=[RecentContact(message_type=c["type"], days_ago=c["days"], offer_id=c["offer"])
                         for c in recent],
        offers_sent=offers_sent,
        redeemed_offer_ids=redeemed,
        churn_risk=None if churn_risk is None else round(float(churn_risk), 4),
    )


def load_rows(con: sqlite3.Connection, customer_id: str):
    cust = con.execute(f"SELECT {', '.join(CUSTOMER_FIELDS)} FROM customers WHERE customer_id = ?",
                       (customer_id,)).fetchone()
    if cust is None:
        raise KeyError(f"unknown customer_id {customer_id}")
    act = [dict(zip(["month", "usage_gb", "active_days"], r)) for r in con.execute(
        "SELECT month, usage_gb, active_days FROM monthly_activity WHERE customer_id = ?", (customer_id,))]
    bills = [dict(zip(BILL_FIELDS, r)) for r in con.execute(
        f"SELECT {', '.join(BILL_FIELDS)} FROM bills WHERE customer_id = ?", (customer_id,))]
    contacts = [dict(zip(CONTACT_FIELDS, r)) for r in con.execute(
        f"SELECT {', '.join(CONTACT_FIELDS)} FROM contact_history WHERE customer_id = ?", (customer_id,))]
    return dict(cust), act, bills, contacts


def load_all_rows(con: sqlite3.Connection):
    """Read every table once and group by customer (fast path for evaluation and tests)."""
    cust = {r[0]: dict(zip(CUSTOMER_FIELDS, r)) for r in
            con.execute(f"SELECT {', '.join(CUSTOMER_FIELDS)} FROM customers")}
    groups: dict[str, dict[str, list]] = {c: {"act": [], "bills": [], "contacts": []} for c in cust}
    for r in con.execute("SELECT customer_id, month, usage_gb, active_days FROM monthly_activity"):
        groups[r[0]]["act"].append({"month": r[1], "usage_gb": r[2], "active_days": r[3]})
    for r in con.execute(f"SELECT customer_id, {', '.join(BILL_FIELDS)} FROM bills"):
        groups[r[0]]["bills"].append(dict(zip(BILL_FIELDS, r[1:])))
    for r in con.execute(f"SELECT customer_id, {', '.join(CONTACT_FIELDS)} FROM contact_history"):
        groups[r[0]]["contacts"].append(dict(zip(CONTACT_FIELDS, r[1:])))
    return cust, groups


def assemble_profile(customer_id: str, as_of: date | None = None, con: sqlite3.Connection | None = None,
                     with_churn_risk: bool = True) -> CustomerProfile:
    as_of = as_of or policy().reference_date
    own = con is None
    con = con or connect()
    try:
        cust, act, bills, contacts = load_rows(con, customer_id)
        profile = build_profile_from_rows(cust, act, bills, contacts, as_of)
        if with_churn_risk:
            from src.segmentation.model import predict_risk  # lazy: no model load when unused
            risk = predict_risk(profile)
            if risk is not None:
                profile = profile.model_copy(update={"churn_risk": round(risk, 4)})
        return profile
    finally:
        if own:
            con.close()


def all_customer_ids(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT customer_id FROM customers ORDER BY customer_id")]


def assemble_many(ids: Iterable[str] | None = None, as_of: date | None = None,
                  con: sqlite3.Connection | None = None, with_churn_risk: bool = True) -> list[CustomerProfile]:
    own = con is None
    con = con or connect()
    try:
        as_of = as_of or policy().reference_date
        cust, groups = load_all_rows(con)
        ids = list(ids) if ids is not None else sorted(cust)
        profiles = [build_profile_from_rows(cust[i], groups[i]["act"], groups[i]["bills"],
                                            groups[i]["contacts"], as_of) for i in ids]
        if with_churn_risk:
            from src.segmentation.model import predict_risk_many
            risks = predict_risk_many(profiles)
            profiles = [p.model_copy(update={"churn_risk": None if r is None else round(r, 4)})
                        for p, r in zip(profiles, risks)]
        return profiles
    finally:
        if own:
            con.close()
