"""Stage 1: download every candidate dataset into data/raw/.

    python src/data/fetch_data.py            download what is missing
    python src/data/fetch_data.py --force    re-download everything
    python src/data/fetch_data.py --check    verify files only, download nothing
    python src/data/fetch_data.py --only D1 D7
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
COLMAP_FILE = RAW / "_column_map.json"
UA = {"User-Agent": "loyalty-assistant-hackathon/0.1 (student project)"}


# ---------- helpers ----------
def _save_colmap(dataset: str, mapping: dict[str, str]) -> None:
    data = json.loads(COLMAP_FILE.read_text()) if COLMAP_FILE.exists() else {}
    data[dataset] = mapping
    COLMAP_FILE.write_text(json.dumps(data, indent=2))


def normalise_columns(df: pd.DataFrame, dataset: str, renames: dict | None = None) -> pd.DataFrame:
    """Strip whitespace, spaces -> underscores; keep original names in _column_map.json."""
    renames = renames or {}
    mapping = {str(c): str(renames.get(c, c)).strip().replace(" ", "_") for c in df.columns}
    _save_colmap(dataset, mapping)
    return df.set_axis(list(mapping.values()), axis=1)


def _save(df: pd.DataFrame, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest, index=False)


def _get(url: str, timeout: int = 300) -> bytes:
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.content


def _kaggle_download(slug: str, folder: Path) -> None:
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
        api = KaggleApi()
        api.authenticate()
    except SystemExit as e:  # kaggle calls sys.exit() when it can't authenticate
        raise RuntimeError("Kaggle authentication failed - check ~/.kaggle/access_token") from e
    folder.mkdir(parents=True, exist_ok=True)
    api.dataset_download_files(slug, path=str(folder), unzip=True)


# ---------- D1 IBM Telco (base) ----------
TELCO_REQUIRED = {"customerID", "Churn", "TotalCharges", "tenure"}


def _telco_kaggle() -> pd.DataFrame:
    tmp = RAW / "telco" / "_tmp"
    try:
        _kaggle_download("blastchar/telco-customer-churn", tmp)
        return pd.read_csv(next(tmp.glob("*.csv")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _telco_hf() -> pd.DataFrame:
    from datasets import load_dataset
    return load_dataset("scikit-learn/churn-prediction", split="train").to_pandas()


def _telco_openml() -> pd.DataFrame:
    from sklearn.datasets import fetch_openml
    return fetch_openml(data_id=42178, as_frame=True, parser="auto").frame


def fetch_telco(dest: Path) -> None:
    errors = []
    for label, fn in [("kaggle", _telco_kaggle), ("huggingface", _telco_hf), ("openml", _telco_openml)]:
        try:
            df = fn()
            df.columns = [str(c).strip() for c in df.columns]
            missing = TELCO_REQUIRED - set(df.columns)
            if missing or len(df) != 7043:
                raise ValueError(f"shape {df.shape}, missing columns {missing}")
            _save(normalise_columns(df, "telco"), dest)
            print(f"    telco fetched from {label}")
            return
        except Exception as e:  # try the next route
            errors.append(f"{label}: {e}")
    raise RuntimeError("all telco routes failed: " + " | ".join(errors))


# ---------- D7 UCI credit-card default (payment behaviour) ----------
CREDIT_NAMES = (
    ["LIMIT_BAL", "SEX", "EDUCATION", "MARRIAGE", "AGE",
     "PAY_0", "PAY_2", "PAY_3", "PAY_4", "PAY_5", "PAY_6"]
    + [f"BILL_AMT{i}" for i in range(1, 7)]
    + [f"PAY_AMT{i}" for i in range(1, 7)]
)


def fetch_credit(dest: Path) -> None:
    from ucimlrepo import fetch_ucirepo
    ds = fetch_ucirepo(id=350)
    X, y = ds.data.features, ds.data.targets
    df = pd.concat([p for p in (ds.data.ids, X, y) if p is not None], axis=1)
    renames = {}
    if list(X.columns) == [f"X{i}" for i in range(1, 24)]:
        renames.update(dict(zip(X.columns, CREDIT_NAMES)))
    renames.update({c: "default_payment_next_month" for c in y.columns})
    _save(normalise_columns(df, "credit", renames), dest)


# ---------- D5 UCI Online Retail II (retail variant) ----------
RETAIL_ZIP = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"


def _xlsx_from_zip(blob: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            if n.endswith(".xlsx"):
                return z.read(n)
        inner = next(n for n in z.namelist() if n.endswith(".zip"))
        return _xlsx_from_zip(z.read(inner))


def fetch_retail(dest: Path) -> None:
    try:
        from ucimlrepo import fetch_ucirepo
        df = fetch_ucirepo(id=502).data.original
        if df is None or len(df) < 1_000_000:
            raise ValueError("incomplete")
    except Exception as e:
        print(f"    ucimlrepo failed ({e}); downloading zip - Excel parse takes several minutes")
        sheets = pd.read_excel(io.BytesIO(_xlsx_from_zip(_get(RETAIL_ZIP, 900))),
                               sheet_name=None, dtype={"Invoice": str, "StockCode": str})
        df = pd.concat(sheets.values(), ignore_index=True)
    _save(normalise_columns(df, "retail"), dest)


# ---------- D3 Orange BigML (profiled, then rejected) ----------
ORANGE = {"train": "https://bml-data.s3.amazonaws.com/churn-bigml-80.csv",
          "test": "https://bml-data.s3.amazonaws.com/churn-bigml-20.csv"}


def fetch_orange(dest: Path) -> None:
    """BigML S3 split first; it returns 403 since 2026, so fall back to OpenML 40701
    (the same 5,000-customer Orange/MLC++ telecom churn table)."""
    try:
        parts = [pd.read_csv(io.BytesIO(_get(u))).assign(split=k) for k, u in ORANGE.items()]
        df = pd.concat(parts, ignore_index=True)
    except Exception as e:
        print(f"    BigML S3 failed ({e}); falling back to OpenML 40701")
        from sklearn.datasets import fetch_openml
        df = fetch_openml(data_id=40701, as_frame=True, parser="liac-arff").frame
    _save(normalise_columns(df, "orange"), dest)


# ---------- D4 Orange Belgium uplift (licence reject; profile only) ----------
def fetch_orange_uplift(dest: Path) -> None:
    from sklearn.datasets import fetch_openml
    _save(normalise_columns(fetch_openml(data_id=45580, as_frame=True, parser="auto").frame,
                            "orange_uplift"), dest)


# ---------- D6 Olist (reject; profile only, needs Kaggle key) ----------
def fetch_olist(dest: Path) -> None:
    _kaggle_download("olistbr/brazilian-ecommerce", dest)


# ---------- registry ----------
@dataclass
class Source:
    id: str
    name: str
    path: Path                               # file, or folder for multi-file sources
    fetch: Callable[[Path], None] | None     # None = manual / produced by another script
    required: bool                           # part of the chosen combination
    note: str = ""

    def exists(self) -> bool:
        p = self.path
        if p.is_dir():
            return any(f.is_file() and f.stat().st_size > 0 and f.name != ".gitkeep" for f in p.iterdir())
        return p.is_file() and p.stat().st_size > 0


SOURCES: list[Source] = [
    Source("D1", "IBM Telco Customer Churn", RAW / "telco" / "telco_churn.csv", fetch_telco, True),
    Source("D2", "Maven Telecom Churn", RAW / "maven", None, False,
           "manual: download from Maven Data Playground into data/raw/maven/"),
    Source("D3", "Orange Telecom (BigML)", RAW / "orange" / "orange_bigml.csv", fetch_orange, False),
    Source("D4", "Orange Belgium uplift", RAW / "orange_uplift" / "orange_uplift.csv", fetch_orange_uplift, False),
    Source("D5", "UCI Online Retail II", RAW / "retail" / "online_retail_ii.csv", fetch_retail, True),
    Source("D6", "Olist e-commerce", RAW / "olist", fetch_olist, False),
    Source("D7", "UCI Default of Credit Card Clients", RAW / "credit" / "credit_default.csv", fetch_credit, True),
    Source("D8", "TRAI subscription reports", RAW / "trai", None, False,
           "manual: save 1-2 TRAI PDFs from trai.gov.in into data/raw/trai/"),
    Source("R3", "Scraped public offers", RAW / "offers_raw.csv", None, True,
           "run: python src/data/scrape_offers.py"),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args(argv)

    failed: list[str] = []
    for s in [s for s in SOURCES if not args.only or s.id in args.only]:
        present = s.exists()
        if args.check:
            print(f"[{'OK' if present else 'MISSING':7}] {s.id:3} {s.name}")
            if s.required and not present:
                failed.append(s.id)
            continue
        if present and not args.force:
            print(f"[skip   ] {s.id} already present")
            continue
        if s.fetch is None:
            print(f"[manual ] {s.id} {s.note}")
            continue
        print(f"[fetch  ] {s.id} {s.name} ...")
        try:
            RAW.mkdir(parents=True, exist_ok=True)
            s.fetch(s.path)
            print(f"[done   ] {s.id}")
        except Exception as e:
            print(f"[FAILED ] {s.id}: {e}")
            if s.required:
                failed.append(s.id)

    if failed:
        print("Required sources missing/failed:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())