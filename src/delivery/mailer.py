"""FR-10 / R4: mock delivery only. Writes an .eml file to outbox/; never opens an SMTP connection."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path


class SendBlocked(RuntimeError):
    """The send-time gate refused delivery; the message says why."""


SENDER = "Customer Team <no-reply@example.com>"


def write_eml(outbox: Path, draft_id: str, to_addr: str, subject: str, body: str) -> Path:
    if not to_addr.endswith("@example.com"):
        raise SendBlocked("mock delivery only writes to example.com test addresses")
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = SENDER
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["X-Draft-ID"] = draft_id
    msg["X-Mock-Delivery"] = "true"
    msg.set_content(body)
    path = outbox / f"{draft_id}.eml"
    path.write_bytes(bytes(msg))
    return path
