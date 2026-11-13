"""Stage 1 tests: raw data present, schemas, offers, profile summary, docs, hygiene."""
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
pytestmark = pytest.mark.stage1

FILES = {
    "telco": RAW / "telco" / "telco_churn.csv",
    "credit": RAW / "credit" / "credit_default.csv",
    "retail": RAW / "retail" / "online_retail_ii.csv",
    "offers": RAW / "offers_raw.csv",
}
IMPORT_NAMES = {"beautifulsoup4": "bs4", "scikit-learn": "sklearn", "PyYAML": "yaml",
                "Faker": "faker", "jupyter": "jupyter_core", "python-dotenv": "dotenv",
                "presidio-analyzer": "presidio_analyzer", "SQLAlchemy": "sqlalchemy"}


def _requirements():
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            yield re.split(r"[<>=!~\[; ]", line)[0]


@pytest.mark.parametrize("pkg", list(_requirements()))
def test_s1_t01_imports(pkg):
    importlib.import_module(IMPORT_NAMES.get(pkg, pkg.replace("-", "_").lower()))


@pytest.mark.parametrize("key", FILES)
def test_s1_t02_raw_files_exist(key):
    assert FILES[key].is_file() and FILES[key].stat().st_size > 0, f"missing {FILES[key]}"


def test_s1_t03_telco_schema():
    df = pd.read_csv(FILES["telco"])
    assert len(df) == 7043
    assert df["customerID"].is_unique
    assert df["Churn"].nunique() == 2
    s = df["TotalCharges"].astype(str).str.strip()
    blank = s.isin(["", "nan"])
    bad = pd.to_numeric(s.where(~blank), errors="coerce").isna() & ~blank
    assert not bad.any()


def test_s1_t04_credit_schema():
    df = pd.read_csv(FILES["credit"])
    assert len(df) == 30000
    need = ["PAY_0", "PAY_2", "PAY_3", "PAY_4", "PAY_5", "PAY_6"] \
        + [f"BILL_AMT{i}" for i in range(1, 7)] + [f"PAY_AMT{i}" for i in range(1, 7)]
    assert not set(need) - set(df.columns)


def test_s1_t05_retail_schema():
    cols = ["Invoice", "Customer_ID", "InvoiceDate", "Quantity", "Price"]
    df = pd.read_csv(FILES["retail"], usecols=cols, low_memory=False)
    assert len(df) > 1_000_000


def test_s1_t06_offers_file():
    df = pd.read_csv(FILES["offers"])
    assert len(df) >= 15
    assert {"name", "price", "benefits", "source_url"} <= set(df.columns)
    assert df["name"].astype(str).str.strip().ne("").all()
    ok = df["robots_ok"].astype(str).str.lower().eq("true") | df["source_type"].eq("hand_written")
    assert ok.all()


def test_s1_t07_profile_summary():
    from src.data.fetch_data import SOURCES
    df = pd.read_csv(ROOT / "data" / "profile_summary.csv")
    need = {"dataset_id", "rows", "cols", "pct_missing", "dup_rows", "label_balance", "sensitive_cols"}
    assert need <= set(df.columns)
    fetched = {s.id for s in SOURCES if s.exists()}
    assert set(df["dataset_id"]) == fetched
    assert df["dataset_id"].is_unique
    assert len(fetched) >= 6, "definition of done: >= 6 candidates fetched"


def test_s1_t08_no_secrets_committed():
    r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, "not a git repository - run git init"
    bad = [f for f in r.stdout.splitlines()
           if Path(f).name in {"kaggle.json", ".env", "access_token"} or f.endswith(".key")
           or (f.startswith("data/raw/") and f != "data/raw/.gitkeep")]
    assert not bad, bad


def test_s1_t09_data_readme():
    text = (ROOT / "data" / "README.md").read_text(encoding="utf-8")
    for h in ["Sources", "Licences", "Scores", "Chosen combination", "Rejected",
              "Sensitive columns", "Assumptions"]:
        assert re.search(rf"^#+\s*{h}", text, re.M | re.I), f"missing heading {h}"


def test_s1_t10_fetch_check_idempotent():
    def snap():
        return {p: p.stat().st_mtime_ns for p in RAW.rglob("*") if p.is_file()}
    before = snap()
    r = subprocess.run([sys.executable, "src/data/fetch_data.py", "--check"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert snap() == before
