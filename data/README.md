# Data

Everything in `data/raw/` is rebuilt by `python src/data/fetch_data.py` (not committed).
Profiling: `python -m src.data.profile_data` → `data/profile_summary.csv`;
scores: `notebooks/01_profile.ipynb` → `data/candidate_scores.csv`.

The seven signals every source is judged against: **ACT** activity over time, **LOY** loyalty,
**PLAN** active plan, **PAY** timely payment, **ENG** engagement / communication history,
**OFFER** approved campaign data, **CONS** consent and preferences.

## Sources

| ID | Dataset | Route | Fetched from | File | Rows | Signals |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | IBM Telco Customer Churn | R1 portal | Hugging Face `scikit-learn/churn-prediction` (Kaggle `blastchar/telco-customer-churn` first if a key exists; OpenML 42178 rejected automatically, it lacks `customerID`) | `raw/telco/telco_churn.csv` | 7,043 | LOY, PLAN, PAY (method only) |
| D2 | Maven Telecom Customer Churn | R1 portal | manual download, not fetched | `raw/maven/` | – | LOY, PLAN, OFFER |
| D3 | Orange Telecom churn | R1 portal | BigML S3 (403 since 2026) → OpenML 40701 | `raw/orange/orange_bigml.csv` | 5,000 | ACT (minutes), PLAN |
| D4 | Orange Belgium churn uplift | R1 portal | OpenML 45580 | `raw/orange_uplift/orange_uplift.csv` | 11,896 | anonymised PCs only |
| D5 | UCI Online Retail II | R1 portal | UCI zip (ucimlrepo cannot import id 502) | `raw/retail/online_retail_ii.csv` | 1,067,371 | ACT, LOY (retail) |
| D6 | Olist Brazilian E-Commerce | R1 portal | Kaggle (needs key) – not fetched | `raw/olist/` | – | ACT, ENG (reviews) |
| D7 | UCI Default of Credit Card Clients | R1 portal | `ucimlrepo` id 350 | `raw/credit/credit_default.csv` | 30,000 | PAY |
| D8 | TRAI telecom subscription reports | R2 government | manual PDF – not fetched | `raw/trai/` | – | calibration only |
| R3 | Offers catalogue | R3 scraping / hand-written | `scrape_offers.py` + `data/offers_handwritten.csv` | `raw/offers_raw.csv` | 15 | OFFER |
| D9 | Our synthetic generator | R4 synthetic | `src/data/build_tables.py` (stage 2) | `processed/loyalty.db` | – | ACT, ENG, CONS, edge cases |

Column names were normalised on save (whitespace stripped, inner spaces → `_`, credit `X1..X23`
→ the UCI names `LIMIT_BAL, PAY_0..PAY_6, BILL_AMT1..6, PAY_AMT1..6`). The original → new
mapping is in `raw/_column_map.json`.

## Licences

| ID | Licence | Note |
| --- | --- | --- |
| D1 | IBM sample data set, redistributed on Kaggle / Hugging Face for learning | OK for a hackathon prototype |
| D2 | Maven Analytics free data playground | OK for learning; manual download terms |
| D3 | Public on OpenML; original licence unclear | unclear → reject |
| D4 | OpenML, research use; anonymised | redistribution terms unclear → reject |
| D5 | CC BY 4.0 (UCI) | OK with attribution |
| D6 | CC BY-NC-SA 4.0 | non-commercial → reject for a company prototype |
| D7 | CC BY 4.0 (UCI) | OK with attribution |
| D8 | Indian government publication | aggregate figures only |
| R3 | Hand-written by the team | ours |

## Scores

Each candidate scored 1–5 on coverage of the seven signals (weight 30), real labels (15),
size and quality (15), domain fit (15), licence (10), effort (15). Total = weighted sum ÷ 5 (max 100).

| ID | Coverage | Labels | Size/quality | Domain fit | Licence | Effort | Total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | 3 | 5 | 4 | 5 | 4 | 5 | **83** |
| D2 | 4 | 5 | 4 | 5 | 4 | 2 | 80 |
| D7 | 2 | 5 | 5 | 2 | 5 | 4 | 70 |
| D3 | 2 | 5 | 3 | 4 | 2 | 4 | 64 |
| D5 | 2 | 1 | 5 | 3 | 5 | 3 | 58 |
| D4 | 2 | 4 | 4 | 3 | 1 | 3 | 56 |
| D6 | 3 | 2 | 4 | 2 | 1 | 2 | 50 |
| R3 | 1 | 1 | 2 | 5 | 4 | 4 | 50 |
| D8 | 1 | 1 | 2 | 3 | 4 | 2 | 38 |

Profiling highlights (`profile_summary.csv`): D1 has 11 blank `TotalCharges` (all tenure 0),
churn 26.5%; D7 clients with at least one late month = 33.6%; D5 has 34,335 duplicate rows and
negative quantities (returns); D4 is 180 anonymised columns with a 3.4% positive label.

## Chosen combination

**D1 (IBM Telco) as the customer base + D7 (payment-delay patterns, borrowed) + R3 offers
(hand-written, reviewed into `config/offers.yaml`) + D9 synthetic generator** for activity,
consent, contact history and edge cases. D2 would replace D1 if downloaded (it is the same
7,043 IBM customers with offer and status columns). D5 is kept as the retail variant; D8 for
calibration only.

## Rejected

- **D3 Orange telecom** – overlaps D1 (same kind of churn table, no customer IDs, plans or
  bills), unclear licence, and its original download link is dead.
- **D4 Orange Belgium uplift** – 180 anonymised principal components: nothing interpretable
  for reason codes, and redistribution terms are unclear. Future work for uplift modelling.
- **D6 Olist** – non-commercial licence and 8 tables to join; e-commerce, not telecom.
- **D8 TRAI** – aggregate PDFs, no customer rows; useful only to sanity-check rates.
- **Live scraping** – carrier plan pages render with JavaScript and/or disallow bots in
  `robots.txt`, so the scraper's `SOURCE_URLS` is left empty and all 15 offers are
  **hand-written** (`source_type = hand_written`), modelled on common US telecom promotions.

## Sensitive columns

Found by the profiler (`sensitive_cols`) and handled under rule R6:

| Source | Columns | Treatment |
| --- | --- | --- |
| D1 | gender, SeniorCitizen, Partner, Dependents | moved to `fairness_attributes`; never features, never in prompts |
| D1 | PhoneService | false positive (a service flag), kept |
| D3 | state, area_code, phone_number | location / identifier → not used |
| D5 | Country | location → not used for treatment |
| D7 | SEX, MARRIAGE, AGE (and EDUCATION) | dropped; only repayment status and amounts are borrowed |

## Assumptions

- Currency is **USD** (the Telco base) everywhere; hand-written offers are in USD.
- Random seed **42** everywhere.
- Telco has no dates, bills, activity, consent or contact history: these are synthesised in
  stage 2 (lineage `synthetic`), anchored on the real monthly charge and churn label.
- Payment behaviour is **borrowed** from D7: clients are grouped into repayment profiles and
  telecom customers are assigned a profile through the tilt table in `config/policy.yaml`.
- First names and emails are fake (Faker, `example.com`).
- Offers are hand-written and must be approved in `config/offers.yaml` before use.
- Thresholds (dormant 60 days, decline 30%, reminder stages, caps) live in `config/policy.yaml`.
