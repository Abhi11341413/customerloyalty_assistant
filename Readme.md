# Customer Loyalty and Engagement Assistant

HCLTech Future Ready AI Engineer Hackathon, use case **Customer Loyalty and Engagement Assistant**.

A GenAI assistant for a telecom company. It builds a minimal profile of each customer from permitted
data, assigns interpretable segments (consistent, loyalty candidate, declining activity, dormant,
payment reminder) with reason codes and confidence, and picks an approved objective and tone through
deterministic policy. It then drafts an email grounded only in verified data and runs eight safety and
compliance checks. **Nothing is sent until a person approves it**, and delivery is mock-only.

| Person | Role | Owns |
| --- | --- | --- |
| P1 | Data & ML engineer | Stages 1-2: data sourcing, profiling, segments, churn model |
| P2 | GenAI engineer | Stages 4-5: prompts, LLM drafting, safety checks |
| P3 | Full-stack engineer | Stages 3 and 5: offer/reminder rules, API, reviewer app |

`CLAUDE.md` holds the full requirements, the six stages and every test ID. `reports/evaluation_report.md`
holds the measured results.

---

## Architecture

```text
 data/raw (Telco, UCI credit, UCI retail, offers)          config/policy.yaml · offers.yaml · templates.yaml
        │  src/data/fetch_data.py, scrape_offers.py                    │ (validated by Pydantic, hashed = policy_version)
        ▼                                                              ▼
 src/data/build_tables.py ──► data/processed/loyalty.db ──► src/profile/assembler.py ──► CustomerProfile (FR-01)
   clean · move protected fields · fuse D7 payments ·                       │
   synthesise activity/bills/consent/contacts · edge cases                  ▼
                                               src/segmentation/rules.py + model.py (churn risk, SHAP)
                                                            │  SegmentResult: segments, reasons, confidence (FR-02)
                                                            ▼
                                   src/policy/strategy.py · offers.py · reminders.py · contact.py   ← deterministic (R1)
                                                            │  Strategy: objective, tone, template, offer | stage (FR-03/05/06)
                              checks 1-3 (consent+caps, duplicates, evidence) ── block / escalate before any LLM call
                                                            ▼
                         src/generation/writer.py ──► LLM (placeholders only) ──► renderer.py fills values from DB/YAML
                                                            │  + disclaimers, offer terms, dispute/support footer by code (FR-04)
                              checks 4-8 (grounding, offer validity, language & required content, PII, tone) + 1 repair (FR-07)
                                                            ▼
                         drafts table ──► FastAPI (src/api) / Streamlit reviewer (app/) ── approve · edit · reject · escalate (FR-08)
                                                            ▼
                         send-time gate (src/service.py) ──► outbox/*.eml (mock, R4) ; every step in review_log (FR-09/10)
```

| Layer | Module |
| --- | --- |
| Contracts | `src/schemas.py` (CustomerProfile with `extra="forbid"`, SegmentResult, Strategy, DraftOutput, CheckResult), `src/config.py` |
| Orchestration | `src/service.py` (used by both the API and the UI) |
| API | `src/api/main.py`: `POST /drafts`, `GET /drafts`, `GET /drafts/{id}`, `POST /drafts/{id}/review`, `POST /drafts/{id}/send`, `GET /audit/{id}` |
| Reviewer UI | `app/streamlit_app.py` |

## Data contract

`data/processed/loyalty.db` (SQLite). Every column's lineage (`real`, `borrowed`, `synthetic`, `derived`)
is listed in `data/processed/data_dictionary.csv`.

| Table | One row per | Key columns |
| --- | --- | --- |
| `customers` | customer (7,043) | customer_id, first_name (fake), email (fake, example.com), tenure_months, contract, plan_name, services, monthly_charge, payment_method, last_offer_accepted, loyalty_tier, marketing_consent, service_consent, max_marketing_per_30d, churn_label |
| `monthly_activity` | customer × 12 months | usage_gb, active_days, spend |
| `bills` | bill | bill_date, due_date (+15 days), amount (real charge), paid_amount, paid_date, status paid / partial / unpaid / disputed |
| `contact_history` | message | sent_at, channel, message_type, offer_id, reminder_stage, outcome |
| `fairness_attributes` | customer | gender, senior_citizen, partner, dependents (**restricted**: only the fairness test reads them) |
| `edge_cases` | injected case | open dispute, contradictory record, contact 2 days ago, frequency cap, no consent, instruction in the name field, short tenure |
| `drafts`, `review_log` | draft / event | app tables. `review_log` is the audit trail: IDs, versions, evidence field names and decisions, with no email addresses |

`CustomerProfile` is the only thing later stages see. It holds allowlisted fields computed **as of a date**:
bills, payments, activity and contacts after that date are ignored. It contains no email address and no
protected attribute.

## Prompt and template approach

- One versioned prompt per objective (`src/generation/prompts/*.md`) plus a shared rules file (`system.md`).
  `prompt_version` is the SHA-256 of the system rules, the objective prompt and the approved example.
- The LLM receives only: the objective, the tone, qualitative facts ("has been a customer for 4+ years",
  "a bill is past its due date"), the allowed placeholders, what must appear, and one **approved example**
  from `config/templates.yaml`. It never receives names, emails, amounts, dates, plan names or offer terms
  (test S4-T01).
- The LLM writes `{{placeholders}}`. `renderer.py` fills them from the profile, `offers.yaml` and `policy.yaml`,
  then appends the offer terms, the unsubscribe line or the dispute/support footer verbatim from YAML.
- First names are sanitised (letters, spaces, hyphens, max 40 characters). A name that looks like an
  instruction is replaced with "there" and the draft is escalated so a person can check the record.

## Structured output

The LLM must return JSON matching `DraftOutput` (`subject`, `body`, `claims[{text, evidence_field}]`,
`cta`). The Claude client uses the SDK's `messages.parse(..., output_format=DraftOutput)`. The writer then
checks the output: only allowed placeholders, every required placeholder present, no raw amounts, dates or
percentages, claims tied to supported evidence, subject ≤ 80 characters, body 60–250 words, and one call to
action. Invalid output gets **one retry**, then the approved template is used (`fallback = true`). An
unavailable LLM never crashes the pipeline.

## Guardrails

| # | Check | When | On failure |
| --- | --- | --- | --- |
| 1 | Consent and preferences: marketing vs service consent, policy cap of 2 per 30 days or the customer's lower cap, do-not-contact | before the LLM | block |
| 2 | Duplicate outreach: same message type within 7 days | before the LLM | block |
| 3 | Evidence complete and consistent: insufficient history, contradictory billing, open dispute, > 45 days overdue, missing amount/date | before the LLM | block / escalate |
| 4 | Grounding: every amount, date, plan name and offer matches the DB or config; claims have evidence; no "top customer"-style claims | after rendering | block |
| 5 | Offer validity: exists, approved, in date, eligible, email channel, never in reminders | after rendering | block |
| 6 | Blocked language (threats, legal threats, guarantees, shaming, discrimination, unauthorised financial actions; pressure words warn) and required content | after rendering | block or warn, with one repair |
| 7 | Sensitive data: phone, card/account numbers, other emails, other people's names (Presidio + regex), protected attributes | after rendering | block |
| 8 | Tone vs required tone (LLM judge if configured, else a transparent word list) | after rendering | warn only |

Edits made by a reviewer are re-checked (checks 4–8). At send time, consent, duplicates, the bill still
being unpaid, the offer still being valid, and checks 4–7 are all verified again.

**Policy is deterministic (R1).** Offer eligibility, overdue status and the reminder stage come only from
code and YAML; `src/policy/` imports no model and no LLM (test S3-T11). No code applies a discount, changes a
bill or charges anyone (R5, test S3-T12).

## Model configuration

| Item | Setting |
| --- | --- |
| Drafting LLM | `claude-opus-5-5` via the official `anthropic` SDK, structured output, `effort: low`, 30 s timeout, 1 retry. This model does not accept `temperature`, so effort is the steadiness and cost lever. Change it in `config/policy.yaml → generation` |
| Key | `.env` with `ANTHROPIC_API_KEY=...` (or `LLM_API_KEY=...`). Never committed. Without a key the system runs in **template mode** |
| Churn model | LightGBM vs logistic-regression baseline on the real Telco churn label; features are tenure, contract, services, monthly charge, payment method and usage trend (no protected fields). Test ROC-AUC 0.948 vs 0.916 (`models/churn_model_metrics.json`); SHAP top-3 contributions become plain-text reason codes |
| PII detection | Microsoft Presidio with spaCy `en_core_web_sm` |
| Seed | 42 everywhere |

## Mock delivery

`POST /drafts/{id}/send` (or **Send (mock)** in the UI) runs the send-time gate. If every check passes, it
writes `outbox/<draft_id>.eml` with an `X-Mock-Delivery: true` header to the customer's fake example.com
address, records the message in `contact_history` (so caps and duplicate rules apply next time), and logs
`sent`. If any check fails, it returns `409` with the reason and logs `send_blocked`. There is no SMTP code
anywhere.

## Observability

Every action (`generated`, `blocked`, `escalated`, `edited`, `approved`, `rejected`, `sent`, `send_blocked`,
and rejected API input) writes a `review_log` row with: prompt_version, model_id, policy_version, evidence
field names, decision, status, rubric, comment and timestamp. Rows contain no email addresses (test S5-T19).
`GET /audit/{draft_id}` returns the trail, and the UI shows it under **Audit trail**. Drafts also store their
check results, template ID and whether the template fallback was used.

## Data sources and assumptions

`data/README.md` has the full survey: seven signals, five routes, nine candidates, scores and rejections.

- **Base:** IBM Telco Customer Churn (7,043 customers; Hugging Face mirror, with Kaggle used if a key is present).
- **Payment behaviour:** UCI Default of Credit Card Clients. Clients are grouped into four repayment profiles,
  and telecom customers are assigned one through a tilt table (churned and month-to-month customers are more
  likely to be late) that keeps the overall late share (32% vs 34% in D7).
- **Offers:** 15 hand-written USD offers (carrier pages block scraping), reviewed into 10 entries in
  `config/offers.yaml`, including deliberately expired, pending, not-yet-valid and SMS-only ones for tests.
- **Synthetic (D9):** names, emails, consent, caps, 12 months of activity, contact history and edge cases.
- **Retail variant:** UCI Online Retail II is profiled and kept as a variant. Orange (two datasets), Olist and
  TRAI are profiled or listed and rejected, with reasons.
- **Assumptions** (all configurable in `config/policy.yaml`): reference date 2026-11-14; dormant ≥ 60 days;
  declining = usage ≥ 30% down or churn risk ≥ 0.60; loyalty = tenure ≥ 24, ≥ 90% on time, nothing overdue
  beyond a 7-day grace; reminders 1–7 / 8–20 / 21–45 days, > 45 escalated; disputes always escalated; no
  offers in reminders; currency USD.

## How to run

```bat
python -m venv myenv
myenv\Scripts\activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm

python src\data\fetch_data.py                :: download raw data (retail takes a few minutes)
python src\data\scrape_offers.py             :: offers_raw.csv (hand-written fallback)
python -m src.data.profile_data              :: data\profile_summary.csv
python src\data\build_tables.py --seed 42    :: data\processed\loyalty.db + data_dictionary.csv
python -m src.segmentation.model             :: train and save models\churn_model.joblib

uvicorn src.api.main:app --reload            :: API on http://127.0.0.1:8000/docs
streamlit run app\streamlit_app.py           :: reviewer screen
python src\eval\demo.py                      :: six demo scenarios + a refusal, end to end
python src\eval\run_eval.py                  :: regenerate reports\evaluation_report.md
```

Optional: put `ANTHROPIC_API_KEY=...` in `.env` to draft with Claude instead of the approved templates.

## How to test

```bat
pytest -m "not live"        :: everything that needs no network or key
pytest -m stage3            :: one stage
pytest -m live              :: live LLM tone test and the full fresh-clone rebuild
```

Markers are `stage1` … `stage6` and `live`. Tests never call the real LLM (they use `FakeLLM`) and never
write outside a temp folder.

## Evaluation (template mode, 64 scenarios)

| Dimension | Result | Target |
| --- | --- | --- |
| Classification | macro-F1 1.00, 0 severe confusions | ≥ 0.85, 0 |
| Groundedness | 100% of amounts / dates / plans / offers match; 100% claim support | 100%, ≥ 98% |
| Policy compliance | 100% of reviewable drafts pass; 100% of seeded violations caught; 0% false blocks | 100%, 100%, ≤ 5% |
| Safety and refusal | must-block recall 100%; 0 PII leaks; 0 sends without approval | 100%, 0, 0 |
| Usefulness | human rubric pass not done yet | mean ≥ 4.0 |
| Scenario coverage | demo 7/7 | 6/6 |

See `reports/evaluation_report.md` for per-segment tables, the confusion matrix, the seeded-violation
breakdown, fairness ratios and operations.

## Known limitations

- The scenarios are built from the written policy, so perfect scores show the code follows the policy, not
  real-world accuracy. Labels still need a second person's check, and the 50-draft, two-reviewer rubric pass
  is still to be done.
- Activity, bills, consent and contacts are synthetic. Usage decline was generated from the churn label, so
  the churn model's AUC is optimistic.
- Payment patterns are borrowed from a different population and country (Taiwanese credit-card clients).
- Unconditional fairness ratios for partner and dependents are below 0.8 because tenure differs between these
  groups in the real data; within the tenure band they are 0.93–1.0 (see CLAUDE.md, S2-T13).
- Without a key, drafts are approved templates; LLM-drafted quality has to be measured with `--llm live`.
- Mock delivery only, with no real unsubscribe processing. The tone check is a word list unless an LLM judge
  is added.
