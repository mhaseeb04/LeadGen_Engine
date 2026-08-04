"""
enricher.py — Advanced AI Enrichment Engine for B2B Leads.

Adds review-sentiment context (via DuckDuckGo search + Gemini summarisation)
on top of what scraper.py already produced. Deliberately does NOT re-audit
websites here — analyzer.py already ran a rigorous, deterministic 8-point
scored audit in Phase 1 (SSL, speed, mobile, SEO, favicon, social tags) and
assigned `strategy`/`primary_flaw`/`score`/`grade`/`report_json`. An earlier
version of this file re-checked "is this site outdated?" via a second,
fuzzy Gemini text judgment and DROPPED any lead Gemini called "modern" —
even when analyzer.py's structured score said otherwise (e.g. no SSL, not
mobile-ready). That silently threw away good website_upgrade leads and
duplicated a network fetch for no benefit. Enrichment must never drop a
website_upgrade lead based on website quality — that call already belongs
to analyzer.py.
"""

import argparse
import logging
import sys
import re
import time
import requests
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

import pandas as pd
try:
    from ddgs import DDGS  # new package name (duckduckgo_search was renamed)
except ImportError:  # pragma: no cover - fallback for older installs
    from duckduckgo_search import DDGS
from google import genai
from google.genai import types

from config import DATA_DIR, GEMINI_API_KEY, safe_str

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
GEMINI_MODEL = "gemini-2.0-flash"
RATE_LIMIT_DELAY = 4.0  # 15 requests per minute free tier

# User-Agent for web scraping
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

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
    """Search for reviews and use Gemini to summarize sentiment."""
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
    """Fetches a website and attempts to discover an email address.

    Args:
        website_url: The URL of the website to scrape.

    Returns:
        The first discovered email address, or None if not found.
    """
    if not website_url or not website_url.startswith("http"):
        return None

    try:
        response = requests.get(website_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Look for mailto links
        for link in soup.find_all("a", href=True):
            if "mailto:" in link["href"]:
                email = link["href"].split("mailto:")[1].split("?")[0]
                if "@" in email:
                    logger.info("Discovered email from mailto link: %s", email)
                    return email

        # Look for emails in text using regex
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

def batch_enrich(
    input_csv: Path,
    output_csv: Path,
    api_key: str,
) -> Path:
    """Run the enrichment pipeline on the scraped leads.

    Every lead that enters this function is kept — enrichment only adds
    columns (``website_status`` derived from Phase 1's audit, plus
    ``review_sentiment``), it never removes a lead.
    """
    logger.info("Starting enrichment on %s", input_csv)
    df = pd.read_csv(input_csv)
    
    if df.empty:
        logger.warning("Input CSV is empty. Skipping enrichment.")
        df.to_csv(output_csv, index=False)
        return output_csv

    client = setup_gemini_client(api_key)
    enriched_records = []

    # Fill NaNs
    df["website"] = df["website"].fillna("")
    df["city"] = df["city"].fillna(df["state"]).fillna("")

    total = len(df)
    for i, row in df.iterrows():
        name = row.get("name", "Unknown")
        city = row.get("city", "")

        logger.info("[%d/%d] Enriching: %s", i + 1, total, name)

        website_status = website_status_label(row)

        review_sentiment = fetch_review_sentiment(client, name, city)
        logger.info(" -> Review Sentiment: %s", review_sentiment)
        time.sleep(RATE_LIMIT_DELAY)

        # Save enriched data — created FIRST so later steps (email
        # discovery) can safely write into it. The previous version
        # referenced enriched_row inside the email-discovery block below
        # before this assignment ever ran, which raised an
        # UnboundLocalError and crashed the entire enrichment phase
        # whenever discovery actually found an email — silently stopping
        # the pipeline before email generation (Phase 3) ever ran.
        enriched_row = row.to_dict()
        enriched_row["website_status"] = website_status
        enriched_row["review_sentiment"] = review_sentiment

        # Email Discovery — only attempt this for leads that actually
        # have a real website URL (safe_str turns a NaN cell, which is
        # truthy in Python, into a clean empty string instead of being
        # passed into discover_email_from_website as a stray float).
        current_email = safe_str(row.get("email", ""))
        website_url = safe_str(row.get("website", ""))
        if not current_email and website_url:
            discovered_email = discover_email_from_website(website_url)
            if discovered_email:
                enriched_row["email"] = discovered_email
                logger.info(" -> Discovered email for %s: %s", name, discovered_email)

        enriched_records.append(enriched_row)

    logger.info("Enrichment complete. Kept all %d leads (enrichment never drops leads).", len(enriched_records))
    
    out_df = pd.DataFrame(enriched_records)
    if not out_df.empty:
        out_df.to_csv(output_csv, index=False)
    else:
        # Save empty structure
        pd.DataFrame(columns=list(df.columns) + ["website_status", "review_sentiment"]).to_csv(output_csv, index=False)
        
    return output_csv

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich leads with website auditing and review sentiment.")
    parser.add_argument("--input", type=Path, required=True, help="Input scraped CSV")
    parser.add_argument("--output", type=Path, required=True, help="Output enriched CSV")
    return parser

if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set in .env")
        sys.exit(1)
        
    try:
        result = batch_enrich(args.input, args.output, GEMINI_API_KEY)
        logger.info("Saved enriched leads to %s", result)
    except Exception as e:
        logger.exception("Enrichment failed.")
        sys.exit(1)
