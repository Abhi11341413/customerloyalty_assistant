"""Reviewer screen (FR-08): queue -> customer card -> draft editor -> 8 checks -> decision.

    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import html
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.delivery.mailer import SendBlocked  # noqa: E402
from src.service import InvalidTransition, Service  # noqa: E402

st.set_page_config(page_title="Loyalty Assistant - Reviewer", layout="wide")


@st.cache_resource
def get_service(db: str, outbox: str) -> Service:
    return Service(db_path=db, outbox=outbox)


svc = get_service(os.getenv("LOYALTY_DB", str(ROOT / "data" / "processed" / "loyalty.db")),
                  os.getenv("OUTBOX_DIR", str(ROOT / "outbox")))
SEGMENTS = ["", "consistent", "loyalty_candidate", "declining_activity", "dormant", "payment_reminder"]
STATUSES = ["pending_review", "approved", "blocked", "escalated", "rejected", "sent", ""]
ICON = {"pass": "🟢", "warn": "🟠", "fail": "🔴"}

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("New draft")
    cid = st.text_input("Customer ID", placeholder="e.g. 0002-ORFBO")
    if st.button("Generate draft", disabled=not cid):
        try:
            d = svc.create_draft(customer_id=cid.strip(), actor="reviewer-ui")
            st.session_state["selected"] = d["draft_id"]
            st.success(f"{d['draft_id']}: {d['status']}")
        except KeyError:
            st.error("Unknown customer ID")
    st.divider()
    st.header("Queue filter")
    status = st.selectbox("Status", STATUSES, index=0, format_func=lambda s: s or "all")
    segment = st.selectbox("Segment", SEGMENTS, format_func=lambda s: s or "all")
    st.caption(f"Model: {getattr(svc.llm, 'model_id', 'template only (no LLM key)')}")

# ---------------------------------------------------------------- queue
st.title("Customer Loyalty and Engagement Assistant")
st.caption("Nothing is sent without approval. Delivery is mock-only (.eml files in outbox/).")
queue = svc.list(status or None, segment or None)
if not queue:
    st.info("No drafts match this filter. Generate one from the sidebar.")
    st.stop()

qdf = pd.DataFrame([{"draft_id": d["draft_id"], "customer": d["customer_id"], "segment": d["primary_segment"],
                     "objective": d["objective"], "offer": d["offer_id"], "status": d["status"]} for d in queue])
st.subheader(f"Review queue ({len(queue)})")
st.dataframe(qdf, hide_index=True, use_container_width=True)
ids = qdf["draft_id"].tolist()
default = ids.index(st.session_state["selected"]) if st.session_state.get("selected") in ids else 0
draft_id = st.selectbox("Open draft", ids, index=default)
d = svc.get(draft_id)
ev = d["evidence"] or {}

# ---------------------------------------------------------------- customer card + strategy
left, right = st.columns([1, 1])
with left:
    st.subheader("Customer card")
    facts = {
        "Customer": d["customer_id"], "Plan": ev.get("plan_name"), "Tenure (months)": ev.get("tenure_months"),
        "Loyalty tier": ev.get("loyalty_tier"), "On-time rate": ev.get("on_time_rate"),
        "Usage change (3 m)": ev.get("usage_change_pct"), "Days since activity": ev.get("days_since_last_activity"),
        "Overdue amount": ev.get("overdue_amount"), "Max days past due": ev.get("max_days_past_due"),
        "Marketing consent": ev.get("marketing_consent"), "Service consent": ev.get("service_consent"),
        "Churn risk": ev.get("churn_risk"),
    }
    st.table(pd.DataFrame({"value": [str(v) for v in facts.values()]}, index=list(facts)))
    st.markdown(f"**Segments:** {', '.join(d['segments'] or []) or 'none'}  \n"
                f"**Primary:** {d['primary_segment']}  \n**Confidence:** {d['confidence']}")
    st.markdown("**Reason codes**")
    st.dataframe(pd.DataFrame(d["reason_codes"] or []), hide_index=True, use_container_width=True)
with right:
    stg = d["strategy"] or {}
    st.subheader("Strategy")
    st.markdown(f"**Objective:** {d['objective']}  \n**Tone:** {d['tone']}  \n**Offer:** {d['offer_id'] or '-'}  \n"
                f"**Reminder stage:** {d['reminder_stage'] or '-'}  \n**Template:** {d['template_id']}  \n"
                f"**Prompt version:** {d['prompt_version']}  \n**Model:** {d['model_id']}  \n"
                f"**Policy version:** {d['policy_version']}")
    for r in stg.get("reasons", []):
        st.caption(f"• {r}")
    st.subheader("Checks")
    for r in d["check_results"] or []:
        st.markdown(f"{ICON[r['result']]} **{r['check_name']}** - {r['detail']}")
    if d["status"] in ("blocked", "escalated"):
        st.error(f"{d['status'].upper()}: {d['block_reason']}")

# ---------------------------------------------------------------- draft editor
st.subheader(f"Draft  ·  status: {d['status']}")
if d["subject"]:
    preview = html.escape(d["body"])
    for v in sorted((d["filled_values"] or {}).values(), key=len, reverse=True):
        preview = preview.replace(html.escape(v), f"<mark>{html.escape(v)}</mark>")
    with st.expander("Preview (verified values highlighted)", expanded=True):
        st.markdown(f"**{html.escape(d['subject'])}**<br><br>" + preview.replace("\n", "<br>"),
                    unsafe_allow_html=True)
    editable = d["status"] == "pending_review"
    subject = st.text_input("Subject", d["subject"], disabled=not editable)
    body = st.text_area("Body", d["body"], height=320, disabled=not editable)

    st.markdown("**Rubric (1-5)**")
    cols = st.columns(5)
    names = ["relevance", "clarity", "personalisation", "call_to_action", "tone"]
    rubric = {n: cols[i].slider(n.replace("_", " "), 1, 5, 4, key=f"r_{n}_{draft_id}") for i, n in enumerate(names)}
    comment = st.text_input("Comment", key=f"c_{draft_id}")

    b1, b2, b3, b4, b5 = st.columns(5)

    def act(action: str, **kw):
        try:
            svc.review(draft_id, action, "reviewer-ui", rubric=rubric, comment=comment or None, **kw)
            st.session_state["selected"] = draft_id
            st.rerun()
        except InvalidTransition as e:
            st.error(str(e))

    if b1.button("Approve", disabled=d["status"] != "pending_review"):
        act("approve")
    if b2.button("Save edit (re-check)", disabled=not editable):
        act("edit", edited_subject=subject, edited_body=body)
    if b3.button("Reject"):
        act("reject")
    if b4.button("Escalate"):
        act("escalate")
    if b5.button("Send (mock)", disabled=d["status"] != "approved"):
        try:
            out = svc.send(draft_id, actor="reviewer-ui")
            st.success(f"Written to {out['eml_path']}")
        except SendBlocked as e:
            st.error(f"Send blocked: {e}")

with st.expander("Audit trail"):
    st.dataframe(pd.DataFrame(svc.audit(draft_id)), hide_index=True, use_container_width=True)
