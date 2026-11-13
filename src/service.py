"""Orchestration shared by the API and the reviewer UI.

create_draft: profile -> segments -> strategy -> checks 1-3 -> LLM draft -> checks 4-8 (+1 repair)
review:       approve / edit (re-checked) / reject / escalate, with rubric and comment
send:         send-time gate -> mock .eml in outbox/ (R3, R4)
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Callable

from src.audit import store
from src.config import ROOT, policy, policy_version
from src.delivery.mailer import SendBlocked, write_eml
from src.generation.llm_client import LLMClient, get_llm
from src.generation.writer import generate_draft
from src.guardrails.checks import (blocking, consent_check, duplicate_check, post_checks, pre_llm_checks)
from src.guardrails.repair import repair as default_repair
from src.policy.offers import check_offer, REASON_TEXT
from src.policy.strategy import pick_strategy
from src.profile.assembler import assemble_profile, connect
from src.schemas import Claim, CheckResult, CustomerProfile, SegmentResult, Strategy
from src.config import offers

DEFAULT_DB = ROOT / "data" / "processed" / "loyalty.db"
DEFAULT_OUTBOX = ROOT / "outbox"
_UNSET = object()


class InvalidTransition(ValueError):
    pass


def finalize(subject: str, body: str, claims, supported, filled, profile: CustomerProfile, strategy: Strategy,
             today: date, llm=None, allow_repair: bool = True,
             repair_fn: Callable = default_repair) -> tuple[str, str, list[CheckResult], list[str]]:
    """Run checks 4-8; on a fixable failure (check 6 only) try one repair, then re-check."""
    results = post_checks(subject, body, claims, supported, filled, profile, strategy, today, llm=llm)
    notes: list[str] = []
    fails = blocking(results)
    if allow_repair and fails and all(r.check_name.startswith("6_") for r in fails):
        subject, body, notes = repair_fn(subject, body, strategy)
        results = post_checks(subject, body, claims, supported, filled, profile, strategy, today, llm=llm)
        if blocking(results):
            notes.append("repair attempt failed - draft blocked")
    return subject, body, results, notes


class Service:
    def __init__(self, db_path: Path | str | None = None, outbox: Path | str | None = None,
                 llm: LLMClient | None | object = _UNSET, today: date | None = None):
        self.db_path = Path(db_path or os.getenv("LOYALTY_DB", DEFAULT_DB))
        self.outbox = Path(outbox or os.getenv("OUTBOX_DIR", DEFAULT_OUTBOX))
        self.llm = get_llm() if llm is _UNSET else llm
        self.today = today or policy().reference_date
        self.con = connect(self.db_path)
        store.init_app_tables(self.con)
        self.lock = threading.RLock()

    # ------------------------------------------------------------ helpers
    def _segment(self, profile: CustomerProfile) -> SegmentResult:
        from src.segmentation.rules import segment_with_model
        return segment_with_model(profile)

    def _profile(self, customer_id: str, as_of: date | None = None) -> CustomerProfile:
        return assemble_profile(customer_id, as_of or self.today, self.con)

    # ------------------------------------------------------------ create
    def create_draft(self, customer_id: str | None = None, profile: CustomerProfile | None = None,
                     actor: str = "system", run_id: str | None = None) -> dict:
        with self.lock:
            if profile is None:
                profile = self._profile(customer_id)
            elif profile.churn_risk is None:
                from src.segmentation.model import predict_risk
                r = predict_risk(profile)
                profile = profile.model_copy(update={"churn_risk": None if r is None else round(r, 4)})
            seg = self._segment(profile)
            strategy = pick_strategy(seg, profile, self.today)
            rec = {
                "run_id": run_id, "customer_id": profile.customer_id, "as_of": profile.as_of.isoformat(),
                "segments": seg.segments, "primary_segment": seg.primary_segment, "confidence": seg.confidence,
                "reason_codes": [rc.model_dump() for rc in seg.reason_codes],
                "objective": strategy.objective, "tone": strategy.tone, "offer_id": strategy.offer_id,
                "reminder_stage": strategy.reminder_stage, "template_id": strategy.template_id,
                "policy_version": policy_version(), "strategy": strategy.model_dump(),
                "evidence": profile.model_dump(mode="json"), "model_id": None, "prompt_version": None,
            }
            pre = pre_llm_checks(profile, seg, strategy)
            fails = blocking(pre)
            if fails:
                escalate = any(r.action == "escalate" for r in fails)
                rec.update(status="escalated" if escalate else "blocked",
                           block_reason="; ".join(f"{r.check_name}: {r.detail}" for r in fails),
                           check_results=[r.model_dump() for r in pre])
                rec["draft_id"] = store.save_draft(self.con, rec)
                store.log_event(self.con, rec, "escalated" if escalate else "blocked", actor,
                                decision=rec["block_reason"], status=rec["status"])
                return store.get_draft(self.con, rec["draft_id"])

            gen = generate_draft(profile, seg, strategy, self.llm)
            subject, body, results, notes = finalize(gen.subject, gen.body, gen.claims, gen.supported_fields,
                                                     gen.filled_values, profile, strategy, self.today)
            all_results = pre + results
            fails = blocking(results)
            rec.update(subject=subject, body=body, raw_subject=gen.raw_subject, raw_body=gen.raw_body,
                       claims=[c.model_dump() for c in gen.claims], filled_values=gen.filled_values,
                       supported_fields=gen.supported_fields, template_id=gen.template_id,
                       prompt_version=gen.prompt_version, model_id=gen.model_id, fallback=gen.fallback,
                       check_results=[r.model_dump() for r in all_results],
                       status="blocked" if fails else "pending_review",
                       block_reason="; ".join(f"{r.check_name}: {r.detail}" for r in fails) or None)
            if gen.name_flagged:
                rec["check_results"].append(CheckResult(check_name="input_name_sanitised", result="warn",
                                                        detail="first name looked like an instruction; greeting "
                                                               "replaced with a neutral one", action="warn").model_dump())
            if notes:
                rec["check_results"].append(CheckResult(check_name="repair", result="warn",
                                                        detail="; ".join(notes), action="warn").model_dump())
            rec["draft_id"] = store.save_draft(self.con, rec)
            store.log_event(self.con, rec, "generated", actor,
                            decision="fallback template" if gen.fallback else "llm draft", status=rec["status"])
            if fails:
                store.log_event(self.con, rec, "blocked", actor, decision=rec["block_reason"], status="blocked")
            return store.get_draft(self.con, rec["draft_id"])

    # ------------------------------------------------------------ review
    def review(self, draft_id: str, action: str, reviewer: str = "reviewer", edited_subject: str | None = None,
               edited_body: str | None = None, rubric: dict | None = None, comment: str | None = None) -> dict:
        with self.lock:
            d = store.get_draft(self.con, draft_id)
            if d is None:
                raise KeyError(draft_id)
            status = d["status"]
            if action == "approve":
                if status != "pending_review":
                    raise InvalidTransition(f"cannot approve a draft with status '{status}'")
                store.update_draft(self.con, draft_id, status="approved")
                d["status"] = "approved"
                store.log_event(self.con, d, "approved", reviewer, decision="approved", status="approved",
                                rubric=rubric, comment=comment)
            elif action == "edit":
                if status not in ("pending_review",):
                    raise InvalidTransition(f"cannot edit a draft with status '{status}'")
                subject = edited_subject if edited_subject is not None else d["subject"]
                body = edited_body if edited_body is not None else d["body"]
                profile = CustomerProfile(**d["evidence"])
                strategy = Strategy(**d["strategy"])
                claims = [Claim(**c) for c in d["claims"] or []]
                _, _, results, _ = finalize(subject, body, claims, d["supported_fields"] or [],
                                            d["filled_values"] or {}, profile, strategy, self.today,
                                            allow_repair=False)
                fails = blocking(results)
                new_status = "blocked" if fails else "pending_review"
                reason = "; ".join(f"{r.check_name}: {r.detail}" for r in fails) or None
                pre = [r for r in d["check_results"] if r["check_name"][:2] in ("1_", "2_", "3_")]
                store.update_draft(self.con, draft_id, subject=subject, body=body, status=new_status,
                                   block_reason=reason, check_results=pre + [r.model_dump() for r in results])
                d.update(status=new_status)
                store.log_event(self.con, d, "edited", reviewer, decision="edit re-checked: " +
                                ("blocked" if fails else "passed"), status=new_status,
                                edited_subject=subject, edited_body=body, rubric=rubric, comment=comment)
                if fails:
                    store.log_event(self.con, d, "blocked", "system", decision=reason, status="blocked")
            elif action == "reject":
                if status in ("sent", "rejected"):
                    raise InvalidTransition(f"cannot reject a draft with status '{status}'")
                store.update_draft(self.con, draft_id, status="rejected")
                d["status"] = "rejected"
                store.log_event(self.con, d, "rejected", reviewer, decision="rejected", status="rejected",
                                rubric=rubric, comment=comment)
            elif action == "escalate":
                if status == "sent":
                    raise InvalidTransition("cannot escalate a sent draft")
                store.update_draft(self.con, draft_id, status="escalated")
                d["status"] = "escalated"
                store.log_event(self.con, d, "escalated", reviewer, decision="escalated by reviewer",
                                status="escalated", rubric=rubric, comment=comment)
            else:
                raise InvalidTransition(f"unknown action '{action}'")
            return store.get_draft(self.con, draft_id)

    # ------------------------------------------------------------ send
    def send_gate(self, d: dict, today: date) -> list[str]:
        """Re-verify everything at send time. Returns the reasons to refuse (empty = OK)."""
        if d["status"] != "approved":
            return [f"draft is not approved (status '{d['status']}'); human approval is required"]
        strategy = Strategy(**d["strategy"])
        try:
            fresh = self._profile(d["customer_id"], as_of=today)
        except KeyError:
            return ["customer is not in the database; consent and billing cannot be verified at send time"]
        reasons = []
        for r in (consent_check(fresh, strategy), duplicate_check(fresh, strategy)):
            if r.result == "fail":
                reasons.append(r.detail)
        if strategy.reminder_stage is not None:
            stored = d["evidence"]
            if fresh.reminder_bill_id != stored.get("reminder_bill_id") or fresh.amount_due != stored.get("amount_due"):
                reasons.append("bill is no longer unpaid as drafted (paid or changed since approval)")
            if fresh.open_dispute:
                reasons.append("a billing dispute was opened since approval")
        if strategy.offer_id:
            o = offers().get(strategy.offer_id)
            ok, why = check_offer(o, fresh, strategy.segment or "", today) if o else (False, ["missing"])
            if not ok:
                reasons.append("offer no longer valid: " + ", ".join(REASON_TEXT.get(w, w) for w in why))
        claims = [Claim(**c) for c in d["claims"] or []]
        results = post_checks(d["subject"], d["body"], claims, d["supported_fields"] or [], d["filled_values"] or {},
                              fresh, strategy, today)
        reasons += [f"{r.check_name}: {r.detail}" for r in blocking(results)]
        return reasons

    def send(self, draft_id: str, actor: str = "reviewer", today: date | None = None) -> dict:
        with self.lock:
            today = today or self.today
            d = store.get_draft(self.con, draft_id)
            if d is None:
                raise KeyError(draft_id)
            reasons = self.send_gate(d, today)
            if reasons:
                store.log_event(self.con, d, "send_blocked", actor, decision="; ".join(reasons), status=d["status"])
                raise SendBlocked("; ".join(reasons))
            email = self.con.execute("SELECT email FROM customers WHERE customer_id = ?",
                                     (d["customer_id"],)).fetchone()[0]
            path = write_eml(self.outbox, d["draft_id"], email, d["subject"], d["body"])
            strategy = d["strategy"]
            n = self.con.execute("SELECT COUNT(*) FROM contact_history").fetchone()[0]
            self.con.execute("INSERT INTO contact_history (contact_id, customer_id, sent_at, channel, message_type, "
                             "offer_id, reminder_stage, outcome) VALUES (?,?,?,?,?,?,?,?)",
                             (f"C-APP-{n + 1:07d}", d["customer_id"], today.isoformat(), "email",
                              strategy["message_type"], strategy.get("offer_id"), strategy.get("reminder_stage"),
                              "delivered"))
            store.update_draft(self.con, draft_id, status="sent")
            d["status"] = "sent"
            store.log_event(self.con, d, "sent", actor, decision=f"mock delivery: {path.name}", status="sent")
            return store.get_draft(self.con, draft_id) | {"eml_path": str(path)}

    # ------------------------------------------------------------ reads
    def get(self, draft_id: str) -> dict:
        d = store.get_draft(self.con, draft_id)
        if d is None:
            raise KeyError(draft_id)
        return d

    def list(self, status: str | None = None, segment: str | None = None) -> list[dict]:
        return store.list_drafts(self.con, status, segment)

    def audit(self, draft_id: str) -> list[dict]:
        return store.audit_trail(self.con, draft_id)

    def log_rejected_input(self, detail: str) -> None:
        store.log_event(self.con, None, "blocked", "api", decision=f"422 input rejected: {detail}",
                        status="blocked")

    def close(self) -> None:
        self.con.close()
