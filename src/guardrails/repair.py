"""One repair attempt for fixable failures (missing disclaimer, banned phrase)."""
from __future__ import annotations

import re

from src.config import policy
from src.generation.renderer import required_footer
from src.schemas import Strategy

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def repair(subject: str, body: str, strategy: Strategy) -> tuple[str, str, list[str]]:
    notes: list[str] = []
    patterns = [re.compile(b.pattern, re.I) for b in policy().blocked_language if b.severity == "block"]
    lines = []
    for para in body.split("\n"):
        kept = []
        for sent in SENTENCE_SPLIT.split(para):
            if any(p.search(sent) for p in patterns):
                notes.append(f"removed sentence: '{sent.strip()[:80]}'")
                continue
            kept.append(sent)
        lines.append(" ".join(kept))
    body = "\n".join(lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if any(p.search(subject) for p in patterns):
        notes.append("subject contained blocked language")   # not repairable: subject is short, re-write needed
    footer = required_footer(strategy)
    if footer not in body:
        body = body + "\n\n" + footer
        notes.append("appended required footer/disclaimers from policy.yaml")
    return subject, body, notes
