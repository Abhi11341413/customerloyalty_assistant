"""Stage 2: churn-risk model trained on the real Telco churn label.

Features (no protected fields, R6): tenure, contract, services, monthly charge,
payment method, usage trend. Baseline logistic regression vs LightGBM, 80/20 split
stratified on churn, seed 42. SHAP top-3 contributions -> plain-text reasons.

    python -m src.segmentation.model          # train, print metrics, save models/churn_model.joblib
"""
from __future__ import annotations

import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.config import policy  # noqa: E402
from src.schemas import CustomerProfile  # noqa: E402

MODEL_PATH = ROOT / "models" / "churn_model.joblib"
METRICS_PATH = ROOT / "models" / "churn_model_metrics.json"

CONTRACTS = ["Month-to-month", "One year", "Two year"]
PAYMENTS = ["Electronic check", "Mailed check", "Bank transfer (automatic)", "Credit card (automatic)"]
SERVICES = ["Fiber internet", "DSL internet", "Phone line", "Multiple lines", "Online security",
            "Online backup", "Device protection", "Tech support", "Streaming TV", "Streaming movies"]
FORBIDDEN_FEATURES = {"gender", "senior_citizen", "partner", "dependents", "age", "marital_status", "location"}


def _slug(s: str) -> str:
    return s.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")


FEATURES = (["tenure_months", "monthly_charge", "n_services", "usage_change_pct"]
            + [f"contract_{_slug(c)}" for c in CONTRACTS]
            + [f"payment_{_slug(p)}" for p in PAYMENTS]
            + [f"svc_{_slug(s)}" for s in SERVICES])

# feature -> (profile evidence field, plain words)
FEATURE_TEXT = {"tenure_months": ("tenure_months", "time as a customer"),
                "monthly_charge": ("monthly_charge", "monthly charge"),
                "n_services": ("services", "number of services"),
                "usage_change_pct": ("usage_change_pct", "recent change in usage")}
FEATURE_TEXT.update({f"contract_{_slug(c)}": ("contract", f"{c} contract") for c in CONTRACTS})
FEATURE_TEXT.update({f"payment_{_slug(p)}": ("payment_method", f"pays by {p.lower()}") for p in PAYMENTS})
FEATURE_TEXT.update({f"svc_{_slug(s)}": ("services", f"has {s.lower()}") for s in SERVICES})


def features(profiles: list[CustomerProfile]) -> pd.DataFrame:
    rows = []
    for p in profiles:
        r = {"tenure_months": p.tenure_months, "monthly_charge": p.monthly_charge,
             "n_services": len(p.services),
             "usage_change_pct": p.usage_change_pct if p.usage_change_pct is not None else 0.0}
        for c in CONTRACTS:
            r[f"contract_{_slug(c)}"] = float(p.contract == c)
        for m in PAYMENTS:
            r[f"payment_{_slug(m)}"] = float(p.payment_method == m)
        for s in SERVICES:
            r[f"svc_{_slug(s)}"] = float(s in p.services)
        rows.append(r)
    return pd.DataFrame(rows, columns=FEATURES).astype(float)


def _metrics(y, prob) -> dict:
    from sklearn.metrics import f1_score, precision_score, recall_score, roc_auc_score
    pred = (prob >= 0.5).astype(int)
    return {"roc_auc": round(float(roc_auc_score(y, prob)), 4),
            "precision": round(float(precision_score(y, pred, zero_division=0)), 4),
            "recall": round(float(recall_score(y, pred, zero_division=0)), 4),
            "f1": round(float(f1_score(y, pred, zero_division=0)), 4)}


def train(db_path: Path | None = None, save: bool = True) -> dict:
    import lightgbm as lgb
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    from src.profile.assembler import DEFAULT_DB, assemble_many, connect

    pol = policy()
    con = connect(db_path or DEFAULT_DB)
    profiles = assemble_many(None, con=con, with_churn_risk=False)
    labels = dict(con.execute("SELECT customer_id, churn_label FROM customers").fetchall())
    con.close()
    X = features(profiles)
    y = np.array([int(labels[p.customer_id]) for p in profiles])
    assert not FORBIDDEN_FEATURES & set(X.columns)

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=pol.model.test_size,
                                          stratify=y, random_state=pol.seed)
    baseline = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=pol.seed))
    baseline.fit(Xtr, ytr)
    base_m = _metrics(yte, baseline.predict_proba(Xte)[:, 1])

    gbm = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=15, min_child_samples=40,
                             subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                             random_state=pol.seed, deterministic=True, verbose=-1)
    gbm.fit(Xtr, ytr)
    gbm_m = _metrics(yte, gbm.predict_proba(Xte)[:, 1])

    chosen, name = (gbm, "lightgbm") if gbm_m["roc_auc"] >= base_m["roc_auc"] else (baseline, "logistic_regression")
    version = f"churn-{name}-" + hashlib.sha256(json.dumps([base_m, gbm_m], sort_keys=True).encode()).hexdigest()[:8]
    bundle = {"model": chosen, "model_name": name, "features": FEATURES, "version": version,
              "metrics": {"chosen": name, "lightgbm": gbm_m, "logistic_regression": base_m,
                          "n_train": int(len(ytr)), "n_test": int(len(yte)),
                          "churn_rate_test": round(float(yte.mean()), 4)}}
    if save:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, MODEL_PATH)
        METRICS_PATH.write_text(json.dumps(bundle["metrics"] | {"version": version}, indent=2))
        load_bundle.cache_clear()
    return bundle


@lru_cache(maxsize=1)
def load_bundle() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    return joblib.load(MODEL_PATH)


def model_version() -> str | None:
    b = load_bundle()
    return b["version"] if b else None


def predict_risk_many(profiles: list[CustomerProfile]) -> list[float | None]:
    b = load_bundle()
    if b is None or not profiles:
        return [None] * len(profiles)
    return [float(x) for x in b["model"].predict_proba(features(profiles)[b["features"]])[:, 1]]


def predict_risk(profile: CustomerProfile) -> float | None:
    return predict_risk_many([profile])[0]


@lru_cache(maxsize=1)
def _explainer():
    import shap
    b = load_bundle()
    if b is None or b["model_name"] != "lightgbm":
        return None
    return shap.TreeExplainer(b["model"])


def explain(profile: CustomerProfile, top: int = 3) -> list[dict]:
    """Top SHAP contributions as plain words, each tied to a profile evidence field."""
    b = load_bundle()
    if b is None:
        return []
    X = features([profile])[b["features"]]
    exp = _explainer()
    if exp is not None:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sv = exp.shap_values(X)
        sv = sv[1] if isinstance(sv, list) else sv
        contrib = np.asarray(sv).reshape(-1)
    else:  # logistic regression: coefficient x standardised value
        lr = b["model"][-1]
        contrib = (b["model"][0].transform(X) * lr.coef_).reshape(-1)
    order = np.argsort(-np.abs(contrib))
    out, seen = [], set()
    for j in order:
        feat = b["features"][j]
        field, words = FEATURE_TEXT[feat]
        if X.iloc[0, j] == 0 and (feat.startswith(("contract_", "payment_", "svc_"))):
            continue  # "does not have X" is a weak, confusing reason
        if field in seen:
            continue
        seen.add(field)
        direction = "raises" if contrib[j] > 0 else "lowers"
        out.append({"feature": feat, "evidence_field": field, "contribution": round(float(contrib[j]), 4),
                    "text": f"{words[0].upper() + words[1:]} {direction} churn risk"})
        if len(out) == top:
            break
    return out


if __name__ == "__main__":
    bundle = train()
    print(json.dumps(bundle["metrics"], indent=2))
    print("saved", MODEL_PATH, bundle["version"])
