"""Stage 1: same 8 checks for every fetched candidate -> data/profile_summary.csv.
Checks: shape, types, missing, duplicates, ranges, time coverage, labels, sensitive columns.
Run:  python -m src.data.profile_data
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.data.fetch_data import SOURCES, Source  # noqa: E402

OUT = ROOT / "data" / "profile_summary.csv"

CONFIG: dict[str, dict] = {
    "D1": {"label": "Churn", "time": None},
    "D2": {"label": "Customer_Status", "time": None, "file": "telecom_customer_churn.csv"},
    "D3": {"label": "class", "time": None},
    "D4": {"label": "y", "time": None},
    "D5": {"label": None, "time": "InvoiceDate"},
    "D6": {"label": None, "time": "order_purchase_timestamp", "file": "olist_orders_dataset.csv"},
    "D7": {"label": "default_payment_next_month", "time": None},
    "R3": {"label": "source_type", "time": "scraped_at"},
}

SENSITIVE_TOKENS = {
    "gender", "sex", "senior", "age", "birth", "dob", "partner", "marriage", "married",
    "marital", "dependents", "dependent", "country", "city", "state", "zip", "postal",
    "area", "region", "latitude", "longitude", "lat", "lon", "religion", "race",
    "ethnicity", "email", "phone",
}


def _tokens(col: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", str(col))}


def sensitive_columns(cols) -> list[str]:
    return [c for c in cols if _tokens(c) & SENSITIVE_TOKENS]


def load(s: Source) -> pd.DataFrame:
    p, cfg = s.path, CONFIG.get(s.id, {})
    if p.is_dir():
        p = p / cfg["file"] if "file" in cfg else sorted(p.glob("*.csv"))[0]
    return pd.read_csv(p, low_memory=False)


def profile_frame(df: pd.DataFrame, sid: str, name: str,
                  label: str | None, time_col: str | None) -> dict:
    n_rows, n_cols = df.shape
    missing = int(df.isna().sum().sum())
    for c in df.columns:                       # blank strings count as missing (TotalCharges)
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
            missing += int((df[c].astype(str).str.strip() == "").sum())
    num = df.select_dtypes("number")
    t_min = t_max = None
    if time_col and time_col in df.columns:
        t = pd.to_datetime(df[time_col], errors="coerce")
        t_min, t_max = t.min(), t.max()
    balance = None
    if label and label in df.columns:
        balance = json.dumps(df[label].astype(str).value_counts(normalize=True).round(3).to_dict())
    return {
        "dataset_id": sid, "name": name,
        "rows": n_rows, "cols": n_cols,                                         # 1 shape
        "dtypes": json.dumps(df.dtypes.astype(str).value_counts().to_dict()),  # 2 types
        "pct_missing": round(100 * missing / max(n_rows * n_cols, 1), 3),       # 3 missing
        "dup_rows": int(df.duplicated().sum()),                                 # 4 duplicates
        "negative_cols": ";".join(c for c in num.columns if (num[c] < 0).any()),  # 5 ranges
        "time_min": t_min, "time_max": t_max,                                   # 6 time
        "label_col": label, "label_balance": balance,                           # 7 labels
        "sensitive_cols": ";".join(sensitive_columns(df.columns)),              # 8 sensitive
        "notes": "",
    }


def profile_pdf_folder(s: Source) -> dict:
    import pdfplumber
    rows = cols = pages = 0
    for pdf_path in s.path.glob("*.pdf"):
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                pages += 1
                for table in page.extract_tables():
                    rows += len(table)
                    cols = max(cols, max((len(r) for r in table), default=0))
    return {"dataset_id": s.id, "name": s.name, "rows": rows, "cols": cols, "dtypes": None,
            "pct_missing": None, "dup_rows": None, "negative_cols": "", "time_min": None,
            "time_max": None, "label_col": None, "label_balance": None, "sensitive_cols": "",
            "notes": f"PDF, {pages} pages; calibration only"}


def build_profile_summary(out: Path = OUT) -> pd.DataFrame:
    records = []
    for s in SOURCES:
        if not s.exists():
            print(f"skip {s.id} (not fetched)")
            continue
        print(f"profiling {s.id} {s.name}")
        if s.id == "D8":
            records.append(profile_pdf_folder(s))
        else:
            cfg = CONFIG.get(s.id, {})
            records.append(profile_frame(load(s), s.id, s.name, cfg.get("label"), cfg.get("time")))
    summary = pd.DataFrame(records)
    summary.to_csv(out, index=False)
    print(f"wrote {out}")
    return summary


if __name__ == "__main__":
    build_profile_summary()