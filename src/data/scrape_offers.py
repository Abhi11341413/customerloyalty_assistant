"""Stage 1: collect public telecom offers -> data/raw/offers_raw.csv.

Polite scraping: robots.txt checked first, honest User-Agent, delay between requests.
Rows from data/offers_handwritten.csv are appended with source_type='hand_written'.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "raw" / "offers_raw.csv"
HAND = ROOT / "data" / "offers_handwritten.csv"
UA = "loyalty-assistant-hackathon/0.1 (student project; contact via team repo)"
DELAY_S = 3

# Fill in 2-3 public USD plan/deal pages after checking them in a browser.
SOURCE_URLS: list[str] = [
    # "https://www.example-carrier.com/plans",
]

PRICE_RE = re.compile(r"\$\s?\d+(?:\.\d{2})?(?:\s?/\s?mo(?:nth)?)?", re.I)
VALID_RE = re.compile(r"(?:for|valid(?: for)?)\s+\d+\s+(?:days?|months?)", re.I)
COLUMNS = ["offer_ref", "name", "price", "validity", "benefits",
           "source_url", "source_type", "robots_ok", "scraped_at"]


def robots_allows(url: str) -> bool:
    p = urlparse(url)
    rp = RobotFileParser(f"{p.scheme}://{p.netloc}/robots.txt")
    try:
        rp.read()
    except Exception:
        return False                       # cannot verify -> do not scrape
    return rp.can_fetch(UA, url)


def parse_offers(html: str, url: str) -> list[dict]:
    """Generic: any card-like block with a heading and a price."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[tuple, dict] = {}
    for block in soup.find_all(["article", "section", "li", "div"]):
        text = block.get_text(" ", strip=True)
        if len(text) > 600:
            continue
        price = PRICE_RE.search(text)
        head = block.find(["h2", "h3", "h4"])
        if not price or not head:
            continue
        name = head.get_text(" ", strip=True)
        benefits = "; ".join(li.get_text(" ", strip=True) for li in block.find_all("li"))[:500]
        valid = VALID_RE.search(text)
        found[(name, price.group())] = {
            "name": name, "price": price.group(), "validity": valid.group() if valid else "",
            "benefits": benefits or text[:300], "source_url": url,
        }
    return list(found.values())


def scrape() -> list[dict]:
    rows, now = [], datetime.now(timezone.utc).isoformat(timespec="seconds")
    for url in SOURCE_URLS:
        if not robots_allows(url):
            print(f"[robots ] disallowed or unreadable: {url}")
            continue
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            offers = parse_offers(r.text, url)
            print(f"[scraped] {len(offers)} offers from {url}")
            rows += [{**o, "source_type": "scraped", "robots_ok": True, "scraped_at": now} for o in offers]
        except Exception as e:
            print(f"[failed ] {url}: {e}")
        time.sleep(DELAY_S)
    return rows


def main() -> int:
    rows = scrape()
    if HAND.exists():
        hand = pd.read_csv(HAND, dtype=str).fillna("")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows += [{**r, "source_type": "hand_written", "robots_ok": False, "scraped_at": now}
                 for r in hand[["name", "price", "validity", "benefits", "source_url"]].to_dict("records")]
    df = pd.DataFrame(rows)
    if df.empty:
        print("No offers collected and no hand-written file.")
        return 1
    df = df[df["name"].astype(str).str.strip() != ""].reset_index(drop=True)
    df["offer_ref"] = [f"RAW-{i:03d}" for i in range(1, len(df) + 1)]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df[COLUMNS].to_csv(OUT, index=False)
    print(f"Wrote {len(df)} offers ({(df.source_type == 'scraped').sum()} scraped) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())