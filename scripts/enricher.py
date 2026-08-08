"""
enricher.py — Advanced AI Enrichment Engine for B2B Leads (Phase 1: parallel).

Adds two columns to what scraper.py already produced:
  • ``website_status`` — a human-readable label derived purely from
    analyzer.py's Phase-1 audit (no re-fetch, no second AI judgment).
  • ``review_sentiment`` — OPTIONAL (off by default). A DuckDuckGo +
    Gemini summary of a business's online reviews. It's off by default
    because it's the single biggest time sink in this phase and nothing
    downstream currently reads it; enable with ENABLE_REVIEW_SENTIMENT=1.

It also opportunistically discovers a contact email from a lead's own
website (pure HTTP, no rate limit).

What changed vs the old version
-------------------------------
The old loop processed leads strictly one-at-a-time with a hard
``sleep(4)`` after every lead — ~6-9 s/lead, so a 60-lead run spent
6-9 minutes here alone. This version:

  1. Runs leads through a ``ThreadPoolExecutor`` (config.ENRICH_MAX_WORKERS).
  2. Replaces the blanket per-lead sleep with a shared token-bucket
     RateLimiter that ONLY paces the actual Gemini calls (used only when
     review sentiment is enabled) — HTTP work runs fully in parallel.
  3. Makes the expensive, unused review-sentiment step opt-in.

Guarantees kept identical to before:
  • Enrichment NEVER drops a lead — it only adds columns.
  • ``safe_str`` handles NaN cells so no ``tel:nan``-style corruption.
  • ``enriched_row`` is built before any write into it (the old
    UnboundLocalError path stays fixed).
  • Output column set is unchanged (plus the two enrichment columns).
"""

import argparse
import concurrent.futures
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS  # new package name (duckduckgo_search was renamed)
except ImportError:  # pragma: no cover - fallback for older installs
    from duckduckgo_search import DDGS

from google import genai
from google.genai import types

from config import (
    ENABLE_REVIEW_SENTIMENT,
    ENRICH_MAX_WORKERS,
    GEMINI_API_KEY,
    GEMINI_RPM,
    safe_str,
)
from ratelimit import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
GEMINI_MODEL = "gemini-2.0-flash"

# User-Agent for web scraping
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Shared limiter paces ONLY real Gemini calls (review sentiment). HTTP
# work (email discovery) isn't rate-limited and runs fully in parallel.
_GEMINI_LIMITER = RateLimiter(rpm=GEMINI_RPM)


def setup_gemini_client(api_key: str) -> genai.Client:
    """Initialize the Google GenAI client."""
    return genai.Client(api_key=api_key)


def website_status_label(row: "pd.Series") -> str:
    """Build a human-readable website status from analyzer.py's already
    computed columns, instead of re-fetching and re-judging the site.
    """
    strategy = safe_str(row.get("strategy", ""))
    if strategy != "website_upgrade":
        return "No Website"

    grade = row.get("grade", "")
    score = row.get("score", "")
    primary_flaw = safe_str(row.get("primary_flaw", ""))

    if primary_flaw == "protected_asset":
        return "Protected/could not fully audit"
    if grade == "" or pd.isna(grade):
        return "Website present (audit incomplete)"

    label = f"Grade {grade} ({score}/100)"
    if primary_flaw:
        label += f" — {primary_flaw}"
    return label


def fetch_review_sentiment(client: genai.Client, business_name: str, city: str) -> str:
    """Search for reviews and use Gemini to summarize sentiment.

    Only called when ENABLE_REVIEW_SENTIMENT is on. The Gemini call is
    paced by the shared token-bucket limiter so parallel workers still
    respect the account's RPM limit.
    """
    query = f"{business_name} {city} reviews"
    try:
        results = DDGS().text(query, max_results=3)
        if not results:
            return "No review data found online."
        snippets = "\n".join([f"- {r.get('title')}: {r.get('body')}" for r in results])
    except Exception as e:
        logger.error("DuckDuckGo search error: %s", e)
        return "Unable to fetch reviews."

    sys_prompt = (
        "You are an analyst. Given these search snippets about a local business, "
        "extract the overall sentiment (e.g. 4.5 stars) and one specific praise or complaint. "
        "Write it as a 1-2 sentence summary. Keep it conversational."
    )

    try:
        _GEMINI_LIMITER.acquire()  # pace only the actual API call
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"Snippets for {business_name} in {city}:\n{snippets}",
            config=types.GenerateContentConfig(
                system_instruction=sys_prompt,
                temperature=0.3,
                max_output_tokens=150,
            ),
        )
        return response.text.strip() if response.text else "Mixed reviews."
    except Exception as e:
        logger.error("Gemini API error during review analysis: %s", e)
        return "Mixed reviews."


def discover_email_from_website(website_url: str) -> Optional[str]:
    """Fetch a website and attempt to discover a contact email address."""
    if not website_url or not website_url.startswith("http"):
        return None

    try:
        response = requests.get(website_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Look for mailto links first (highest signal)
        for link in soup.find_all("a", href=True):
            if "mailto:" in link["href"]:
                email = link["href"].split("mailto:")[1].split("?")[0]
                if "@" in email:
                    logger.info("Discovered email from mailto link: %s", email)
                    return email

        # Fall back to a regex sweep of visible text
        email_pattern = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
        for text_node in soup.find_all(string=True):
            emails = email_pattern.findall(text_node)
            if emails:
                logger.info("Discovered email from text: %s", emails[0])
                return emails[0]

    except requests.exceptions.RequestException as e:
        logger.warning("Could not fetch website %s for email discovery: %s", website_url, e)
    except Exception as e:
        logger.warning("Error during email discovery from %s: %s", website_url, e)

    return None


def _enrich_one(
    row: dict[str, Any],
    client: "genai.Client | None",
    enable_reviews: bool,
) -> dict[str, Any]:
    """Enrich a single lead. Pure function over one row -> one row so it's
    safe to run across a thread pool. Never drops the lead; on any error
    it still returns a fully-formed row with sensible defaults.
    """
    name = safe_str(row.get("name", "Unknown"), default="Unknown")
    city = safe_str(row.get("city", ""))

    # Build the output row FIRST so later writes are always safe.
    enriched_row = dict(row)
    enriched_row["website_status"] = website_status_label(pd.Series(row))

    # Review sentiment — opt-in only.
    if enable_reviews and client is not None:
        enriched_row["review_sentiment"] = fetch_review_sentiment(client, name, city)
    else:
        enriched_row["review_sentiment"] = ""

    # Email discovery — only for leads that already have a real website
    # URL and no email yet. Pure HTTP, no rate limit.
    current_email = safe_str(row.get("email", ""))
    website_url = safe_str(row.get("website", ""))
    if not current_email and website_url:
        discovered = discover_email_from_website(website_url)
        if discovered:
            enriched_row["email"] = discovered
            logger.info(" -> Discovered email for %s: %s", name, discovered)

    return enriched_row


def batch_enrich(
    input_csv: Path,
    output_csv: Path,
    api_key: str,
    enable_reviews: "bool | None" = None,
    max_workers: "int | None" = None,
) -> Path:
    """Run enrichment across all scraped leads, in parallel.

    Every input lead appears in the output — enrichment only adds columns.
    """
    enable_reviews = ENABLE_REVIEW_SENTIMENT if enable_reviews is None else enable_reviews
    max_workers = max_workers or ENRICH_MAX_WORKERS

    logger.info("Starting enrichment on %s (reviews=%s, workers=%d)",
                input_csv, enable_reviews, max_workers)
    df = pd.read_csv(input_csv)

    if df.empty:
        logger.warning("Input CSV is empty. Skipping enrichment.")
        df.to_csv(output_csv, index=False)
        return output_csv

    # Normalise the two columns the enrichers read.
    df["website"] = df["website"].fillna("")
    df["city"] = df["city"].fillna(df["state"]).fillna("")

    client = setup_gemini_client(api_key) if enable_reviews else None
    rows = df.to_dict("records")
    total = len(rows)

    counter_lock = threading.Lock()
    done = 0

    def _work(row: dict[str, Any]) -> dict[str, Any]:
        nonlocal done
        result = _enrich_one(row, client, enable_reviews)
        with counter_lock:
            done += 1
            logger.info("[%d/%d] Enriched: %s", done, total,
                        safe_str(row.get("name", "Unknown")))
        return result

    # executor.map preserves input order in the returned list, so output
    # row order matches input even though work runs concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        enriched_records = list(executor.map(_work, rows))

    logger.info("Enrichment complete. Kept all %d leads (enrichment never drops leads).",
                len(enriched_records))

    out_df = pd.DataFrame(enriched_records)
    if out_df.empty:
        out_df = pd.DataFrame(columns=list(df.columns) + ["website_status", "review_sentiment"])
    out_df.to_csv(output_csv, index=False)
    return output_csv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich leads with website status and (optional) review sentiment.")
    parser.add_argument("--input", type=Path, required=True, help="Input scraped CSV")
    parser.add_argument("--output", type=Path, required=True, help="Output enriched CSV")
    parser.add_argument("--reviews", action="store_true", help="Enable review-sentiment lookup (slower).")
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if not GEMINI_API_KEY and args.reviews:
        logger.error("GEMINI_API_KEY is not set in .env (required with --reviews)")
        sys.exit(1)

    try:
        result = batch_enrich(
            args.input, args.output, GEMINI_API_KEY,
            enable_reviews=args.reviews or None,
        )
        logger.info("Saved enriched leads to %s", result)
    except Exception:
        logger.exception("Enrichment failed.")
        sys.exit(1)
