# Customer Loyalty and Engagement Assistant

HCLTech Future Ready AI Engineer Hackathon, use case: Customer Loyalty and Engagement Assistant.

A GenAI assistant that segments retail/telecom customers (consistent, loyalty candidate,
declining activity, dormant, payment reminder), drafts the right email for each segment
from verified data, runs policy and safety checks, and sends nothing until a person approves it.

## Team

| Person | Role | Owns |
| --- | --- | --- |
| P1 | Data & ML engineer | Stage 1–2: data sourcing, profiling, segments, churn model |
| P2 | GenAI engineer | Stage 4–5: prompts, LLM drafting, safety checks |
| P3 | Full-stack engineer | Stage 3 and 5: offer/reminder rules, API, reviewer app |

## Stages

1. Get the data
2. Build profiles and segments
3. Offer and reminder rules
4. Write the email (LLM)
5. Check and review (guardrails + reviewer app + mock send)
6. Evaluate and demo

## Project layout

```text
data/raw/          downloaded files (not committed; rebuilt by src/fetch_data.py)
data/processed/    cleaned tables (not committed)
notebooks/         exploration and profiling notebooks
src/               Python code
requirements.txt   Python libraries
```

## Setup (Windows)

```bat
python -m venv myenv
myenv\Scripts\activate
pip install -r requirements.txt
```

Your prompt must start with `(myenv)` before you run `pip install`.

For the telecom dataset, put your Kaggle API key at `C:\Users\<you>\.kaggle\kaggle.json`.
Never commit it.

## Data sources (Stage 1)

| Id | Dataset | Use |
| --- | --- | --- |
| D1/D2 | IBM Telco Customer Churn / Maven Telecom Churn | Customer base: plans, tenure, charges, churn |
| D7 | UCI Default of Credit Card Clients | Real late-payment patterns for bills |
| D5 | UCI Online Retail II | Retail variant |
| R3 | Scraped public offer pages | Offer catalogue |
| D9 | Our synthetic generator | Consent, contact history, edge cases |

Every column in the final tables is tagged `real`, `borrowed` or `synthetic` in the data dictionary.
