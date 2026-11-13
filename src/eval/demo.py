"""Stage 6 demo: six scenarios end to end on real (synthetic-augmented) customers from the DB.

    python src/eval/demo.py            # uses a temp copy of the DB; writes .eml files to a temp outbox

consistent, loyalty reward, declining, inactive (dormant), overdue stage 1 and stage 3, and one refusal.
Each approved draft is sent through the send-time gate to the mock outbox.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.delivery.mailer import SendBlocked  # noqa: E402
from src.policy.strategy import pick_strategy  # noqa: E402
from src.profile.assembler import DEFAULT_DB, assemble_many, connect  # noqa: E402
from src.segmentation.rules import segment  # noqa: E402

DEMOS = [  # (label, primary segment, extra condition on the strategy, expected outcome)
    ("Consistent customer -> appreciation", "consistent", lambda st: st.offer_id is not None, "sent"),
    ("Loyalty candidate -> reward", "loyalty_candidate", lambda st: st.offer_id is not None, "sent"),
    ("Declining activity -> re-engagement offer", "declining_activity", lambda st: st.offer_id is not None, "sent"),
    ("Inactive (dormant) -> win-back", "dormant", lambda st: st.offer_id is not None, "sent"),
    ("Overdue, stage 1 -> courteous reminder", "payment_reminder", lambda st: st.reminder_stage == 1, "sent"),
    ("Overdue, stage 3 -> firm but respectful", "payment_reminder", lambda st: st.reminder_stage == 3, "sent"),
]


def pick_customers(con) -> dict[str, str]:
    profiles = assemble_many(None, con=con)
    chosen: dict[str, str] = {}
    for label, primary, cond, _ in DEMOS:
        for p in profiles:
            if p.customer_id in chosen.values() or p.recent_contacts or p.marketing_sent_30d:
                continue
            if not (p.marketing_consent and p.service_consent and p.max_marketing_per_30d >= 1):
                continue
            if p.open_dispute or p.contradictory_billing or p.first_name.startswith("Ignore"):
                continue
            s = segment(p)
            if s.primary_segment != primary:
                continue
            st = pick_strategy(s, p, p.as_of)
            if st.action == "draft" and cond(st):
                chosen[label] = p.customer_id
                break
    refusal = con.execute("SELECT customer_id FROM edge_cases WHERE \"case\" = 'open_dispute' "
                          "ORDER BY customer_id LIMIT 1").fetchone()[0]
    chosen["Refusal: open billing dispute -> escalate, nothing drafted"] = refusal
    return chosen


def run(out_dir: Path | None = None, llm=None, verbose: bool = True) -> list[dict]:
    from src.service import Service
    work = Path(out_dir or tempfile.mkdtemp(prefix="demo_"))
    work.mkdir(parents=True, exist_ok=True)
    db = work / "demo.db"
    shutil.copy(DEFAULT_DB, db)
    svc = Service(db_path=db, outbox=work / "outbox", llm=llm)
    con = connect(db)
    customers = pick_customers(con)
    con.close()
    expected = {d[0]: d[3] for d in DEMOS}
    expected["Refusal: open billing dispute -> escalate, nothing drafted"] = "escalated"
    rows = []
    for label, cid in customers.items():
        d = svc.create_draft(customer_id=cid, actor="demo")
        final, eml = d["status"], None
        if d["status"] == "pending_review":
            svc.review(d["draft_id"], "approve", "demo-reviewer",
                       rubric={"relevance": 5, "clarity": 5, "personalisation": 4, "call_to_action": 5, "tone": 5})
            try:
                out = svc.send(d["draft_id"], actor="demo-reviewer")
                final, eml = out["status"], out["eml_path"]
            except SendBlocked as e:
                final = f"send blocked: {e}"
        rows.append({"label": label, "customer_id": cid, "segments": d["segments"], "objective": d["objective"],
                     "offer_id": d["offer_id"], "reminder_stage": d["reminder_stage"], "status": final,
                     "reason": d["block_reason"], "subject": d["subject"], "eml": eml,
                     "passed": final == expected[label]})
    svc.close()
    if verbose:
        for r in rows:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{mark}] {r['label']}\n       customer {r['customer_id']} | segments {r['segments']} | "
                  f"objective {r['objective']} | offer {r['offer_id']} | stage {r['reminder_stage']}")
            print(f"       subject: {r['subject']}" if r["subject"] else f"       reason: {r['reason']}")
            print(f"       -> {r['status']}" + (f"  ({r['eml']})" if r["eml"] else ""))
        print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} demo scenarios passed. Outbox: {work / 'outbox'}")
    return rows


if __name__ == "__main__":
    from src.generation.llm_client import get_llm
    results = run(llm=get_llm())
    sys.exit(0 if all(r["passed"] for r in results) else 1)
