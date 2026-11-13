"""App tables (drafts, review_log) and the audit trail (FR-09).

review_log stores IDs, versions, evidence field NAMES and decisions - never email
addresses or full names (R10). Draft text is stored in `drafts` for review/delivery.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

DRAFT_COLUMNS = ["draft_id", "run_id", "customer_id", "as_of", "segments", "primary_segment", "confidence",
                 "reason_codes", "objective", "tone", "offer_id", "reminder_stage", "template_id",
                 "prompt_version", "model_id", "policy_version", "subject", "body", "raw_subject", "raw_body",
                 "claims", "filled_values", "supported_fields", "check_results", "strategy", "evidence",
                 "fallback", "status", "block_reason", "created_at", "updated_at"]
JSON_COLS = {"segments", "reason_codes", "claims", "filled_values", "supported_fields", "check_results",
             "strategy", "evidence"}
LOG_COLUMNS = ["event_id", "draft_id", "actor", "action", "edited_subject", "edited_body", "rubric", "comment",
               "prompt_version", "model_id", "policy_version", "evidence_fields", "decision", "status", "timestamp"]
ACTIONS = {"generated", "blocked", "approved", "edited", "rejected", "escalated", "sent", "send_blocked"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_app_tables(con: sqlite3.Connection) -> None:
    con.execute(f"""CREATE TABLE IF NOT EXISTS drafts ({', '.join(
        c + (' TEXT PRIMARY KEY' if c == 'draft_id' else '') for c in DRAFT_COLUMNS)})""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS review_log ({', '.join(
        c + (' TEXT PRIMARY KEY' if c == 'event_id' else '') for c in LOG_COLUMNS)})""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_log_draft ON review_log(draft_id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_draft_status ON drafts(status)")
    con.commit()


def _enc(k: str, v: Any) -> Any:
    if k in JSON_COLS:
        return json.dumps(v, default=str)
    if isinstance(v, bool):
        return int(v)
    return v


def _dec(row: sqlite3.Row | tuple, cols: list[str]) -> dict:
    d = dict(zip(cols, row))
    for k in JSON_COLS & set(d):
        d[k] = json.loads(d[k]) if d[k] else None
    if "fallback" in d and d["fallback"] is not None:
        d["fallback"] = bool(d["fallback"])
    for k in ("rubric", "evidence_fields"):
        if k in d and d[k]:
            d[k] = json.loads(d[k])
    return d


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def save_draft(con: sqlite3.Connection, draft: dict) -> str:
    draft = {c: draft.get(c) for c in DRAFT_COLUMNS}
    draft["draft_id"] = draft["draft_id"] or new_id("D")
    draft["created_at"] = draft["created_at"] or now()
    draft["updated_at"] = now()
    con.execute(f"INSERT INTO drafts ({', '.join(DRAFT_COLUMNS)}) VALUES ({', '.join('?' * len(DRAFT_COLUMNS))})",
                [_enc(c, draft[c]) for c in DRAFT_COLUMNS])
    con.commit()
    return draft["draft_id"]


def update_draft(con: sqlite3.Connection, draft_id: str, **fields) -> None:
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE drafts SET {sets} WHERE draft_id = ?", [_enc(k, v) for k, v in fields.items()] + [draft_id])
    con.commit()


def get_draft(con: sqlite3.Connection, draft_id: str) -> dict | None:
    row = con.execute(f"SELECT {', '.join(DRAFT_COLUMNS)} FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    return _dec(row, DRAFT_COLUMNS) if row else None


def list_drafts(con: sqlite3.Connection, status: str | None = None, segment: str | None = None) -> list[dict]:
    q = f"SELECT {', '.join(DRAFT_COLUMNS)} FROM drafts"
    cond, args = [], []
    if status:
        cond.append("status = ?")
        args.append(status)
    if segment:
        cond.append("primary_segment = ?")
        args.append(segment)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY created_at, draft_id"
    return [_dec(r, DRAFT_COLUMNS) for r in con.execute(q, args)]


def log_event(con: sqlite3.Connection, draft: dict | None, action: str, actor: str = "system",
              decision: str = "", status: str | None = None, edited_subject: str | None = None,
              edited_body: str | None = None, rubric: dict | None = None, comment: str | None = None) -> str:
    assert action in ACTIONS, action
    draft = draft or {}
    ev_fields = sorted({rc["evidence_field"] for rc in (draft.get("reason_codes") or [])})
    row = {"event_id": new_id("E"), "draft_id": draft.get("draft_id"), "actor": actor, "action": action,
           "edited_subject": edited_subject, "edited_body": edited_body,
           "rubric": json.dumps(rubric) if rubric else None, "comment": comment,
           "prompt_version": draft.get("prompt_version"), "model_id": draft.get("model_id"),
           "policy_version": draft.get("policy_version"), "evidence_fields": json.dumps(ev_fields),
           "decision": decision, "status": status or draft.get("status"), "timestamp": now()}
    con.execute(f"INSERT INTO review_log ({', '.join(LOG_COLUMNS)}) VALUES ({', '.join('?' * len(LOG_COLUMNS))})",
                [row[c] for c in LOG_COLUMNS])
    con.commit()
    return row["event_id"]


def audit_trail(con: sqlite3.Connection, draft_id: str) -> list[dict]:
    rows = con.execute(f"SELECT {', '.join(LOG_COLUMNS)} FROM review_log WHERE draft_id = ? "
                       "ORDER BY timestamp, rowid", (draft_id,))
    return [_dec(r, LOG_COLUMNS) for r in rows]
