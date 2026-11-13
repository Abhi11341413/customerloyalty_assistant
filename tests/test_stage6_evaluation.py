"""Stage 6 tests: scenario file, evaluation report and thresholds, demo, README, clean-checkout readiness."""
from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import policy

pytestmark = pytest.mark.stage6
ROOT = Path(__file__).resolve().parents[1]
REFUSAL_CASES = ["missing evidence", "ineligible offer", "expired offer", "contradictory data", "sensitive field",
                 "no consent", "frequency cap", "duplicate outreach", "open dispute", "unapproved send",
                 "prompt injection"]


@pytest.fixture(scope="module")
def evaluation(tmp_path_factory, db_path):
    from src.eval.run_eval import run
    d = tmp_path_factory.mktemp("eval")
    m = run("template", out_dir=d, report=d / "report.md", results_path=d / "results.json")
    return m, (d / "report.md").read_text(encoding="utf-8")


def test_s6_t01_scenario_file():
    with open(ROOT / "data" / "test_scenarios.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 60
    need = {"scenario_id", "customer_id", "scenario_type", "expected_segments", "expected_primary",
            "expected_objective", "expected_offer_id", "expected_reminder_stage", "expected_outcome",
            "expected_reason", "notes"}
    assert need <= set(rows[0])
    types = {r["scenario_type"] for r in rows}
    assert {"consistent", "loyalty", "declining", "dormant", "overdue", "refusal"} <= types
    counts = {t: sum(r["scenario_type"] == t for r in rows) for t in types}
    assert counts["consistent"] >= 10 and counts["loyalty"] >= 10 and counts["declining"] >= 10
    assert counts["dormant"] >= 8 and counts["overdue"] >= 12
    refusals = [r for r in rows if r["expected_outcome"] in ("block", "escalate")]
    assert len(refusals) >= 10
    notes = " ".join(r["notes"].lower() for r in refusals)
    for case in REFUSAL_CASES:
        assert case in notes, case
    stages = {r["expected_reminder_stage"] for r in rows if r["scenario_type"] == "overdue"}
    assert {"1", "2", "3"} <= stages and any(r["expected_outcome"] == "escalate" for r in rows
                                            if r["scenario_type"] == "overdue")


def test_s6_t02_report_completeness(evaluation):
    _, report = evaluation
    for h in ["Classification quality", "Message groundedness", "Policy compliance", "Safety and refusal",
              "Message usefulness", "Scenario coverage", "Reviewer feedback", "Known limitations", "Operations",
              "Fairness"]:
        assert h in report, h


def test_s6_t03_thresholds(evaluation):
    m, _ = evaluation
    t = policy().targets
    assert not m["classification"]["severe_confusions"]
    assert m["classification"]["macro_f1"] >= t["macro_f1"]
    assert m["groundedness"]["grounded_rate"] >= t["grounding"]
    assert m["groundedness"]["claim_support_rate"] >= t["claim_support"]
    assert m["safety"]["must_block_recall"] >= t["must_block_recall"]
    assert m["safety"]["pii_leaks"] == 0
    assert m["safety"]["sends_without_approval"] == 0
    assert m["safety"]["over_refusal_rate"] <= t["over_refusal_max"]
    assert m["seeded_violations"]["recall"] == 1.0
    assert m["operations"]["errors"] == 0


def test_s6_t04_demo_scenarios(tmp_path, db_path):
    from src.eval.demo import run
    rows = run(out_dir=tmp_path, verbose=False)
    assert len(rows) == 7 and all(r["passed"] for r in rows), [(r["label"], r["status"]) for r in rows if not r["passed"]]
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 6


def test_s6_t05_readme_sections():
    text = (ROOT / "Readme.md").read_text(encoding="utf-8")
    for h in ["Architecture", "Data contract", "Prompt", "Structured output", "Guardrails", "Model configuration",
              "Mock delivery", "Observability", "Data sources", "Assumptions", "How to run", "How to test",
              "Known limitations"]:
        assert re.search(rf"^#+ .*{h}", text, re.M | re.I), h


def test_s6_t06_clean_checkout_has_everything():
    """Everything the code and tests need is tracked by git (the data itself is rebuilt by scripts)."""
    r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    tracked = set(r.stdout.splitlines())
    if not tracked:
        pytest.skip("not a git checkout")
    must = ["requirements.txt", "pytest.ini", "config/policy.yaml", "config/offers.yaml", "config/templates.yaml",
            "src/data/fetch_data.py", "src/data/build_tables.py", "src/service.py", "src/api/main.py",
            "app/streamlit_app.py", "data/test_scenarios.csv", "models/churn_model.joblib", "Readme.md"]
    assert not [m for m in must if m not in tracked]
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "src", "app", "tests", "config"],
                               cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not [u for u in untracked if u.endswith((".py", ".yaml", ".md"))], untracked


@pytest.mark.live
def test_s6_t06_fresh_clone_full(tmp_path):
    """Full rebuild from a fresh clone: fetch data, build tables, train, run tests (needs network, ~10 min)."""
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(ROOT), str(clone)], check=True)
    py = sys.executable
    for cmd in ([py, "src/data/fetch_data.py"], [py, "src/data/scrape_offers.py"],
                [py, "src/data/build_tables.py", "--seed", "42"], [py, "-m", "src.data.profile_data"],
                [py, "-m", "pytest", "-m", "not live", "-q", "-p", "no:cacheprovider"]):
        subprocess.run(cmd, cwd=clone, check=True, timeout=1800)
