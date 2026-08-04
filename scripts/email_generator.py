"""
email_generator.py — Gemini-powered cold email generator.

Uses the google-genai SDK to craft hyper-short, personalised cold emails
for each scraped lead.  Supports batch processing with checkpointing,
resume, and rate-limit handling.
"""

import argparse
import logging
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import pandas as pd
from google import genai
from google.genai import types

from config import DATA_DIR, DEMO_BASE_URL, GEMINI_API_KEY, GEMINI_MODEL, safe_str, validate_config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RPM_DELAY: float = 4.0        # seconds between calls (strict limit per plan)
CHECKPOINT_EVERY: int = 10    # save progress every N records
RATE_LIMIT_WAIT: float = 60.0 # seconds to wait on HTTP 429


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------
def create_client(api_key: str) -> genai.Client:
    """Create and return a google-genai Client.

    Args:
        api_key: Gemini API key.

    Returns:
        Initialised :class:`genai.Client`.
    """
    client = genai.Client(api_key=api_key)
    logger.info("Gemini client initialised (model: %s).", GEMINI_MODEL)
    return client


# ---------------------------------------------------------------------------
# Single email generator
# ---------------------------------------------------------------------------
def generate_email(
    client: genai.Client,
    business_name: str,
    city: str,
    demo_url: str,
    strategy: str = "no_website",
    primary_flaw: str = "",
    report_json: str = "",
) -> str:
    """Generate a cold email body for a single lead using dual-branch logic.

    Args:
        client: Initialised Gemini client.
        business_name: Name of the target business.
        city: City where the business is located.
        demo_url: Personalised demo site link.
        strategy: Branch A ('no_website') or Branch B ('website_upgrade').
        primary_flaw: Audit flaw from WebsiteAnalyzer (legacy/short form).
        report_json: Full structured report JSON from WebsiteAnalyzer, used
            to cite up to two concrete, specific findings instead of one
            generic flaw string.

    Returns:
        The generated email body as a plain string.
    """
    issues_summary = primary_flaw
    if report_json:
        try:
            import json as _json
            report = _json.loads(report_json)
            failing = [c["title"] for c in report.get("checks", []) if c.get("status") == "fail"]
            if failing:
                issues_summary = "; ".join(failing[:2])
        except Exception:  # noqa: BLE001 — fall back to primary_flaw silently
            pass

    if strategy == "website_upgrade":
        if issues_summary and issues_summary != "protected_asset":
            system_prompt = (
                "You are Haseeb Baig, founder of Pulsfi Marketing Agency. Do not sound like a generic marketer. "
                f"HOOK: Tell them you audited their site and found these specific issues: '{issues_summary}'. "
                f"VALUE: Do not list all our services immediately. Focus strictly on how a modern, fast website will get {business_name} more customers in {city}. "
                "Frame the Vercel link as a free, pre-built gift to demonstrate our value. Explain briefly that these issues are currently costing them leads, so you went ahead and coded a lightning-fast, premium prototype to fix them. "
                f"HARD CTA: Give them this link ({demo_url}) and tell them to click 'Book Your Onboarding Call' so you can initiate the deployment. "
                "TONE: 3 sentences. Direct, authoritative, urgent (e.g., 'before you archive this'). No subject line. Start casually. Keep the entire email under 120 words."
            )
        else:
            system_prompt = (
                "You are Haseeb Baig, founder of Pulsfi Marketing Agency. Do not sound like a generic marketer. "
                "HOOK: Tell them you audited their site and noticed the architecture is severely outdated. "
                f"VALUE: Do not list all our services immediately. Focus strictly on how a modern, fast website will get {business_name} more customers in {city}. "
                "Frame the Vercel link as a free, pre-built gift to demonstrate our value. Explain briefly that outdated frameworks leak traffic, so you went ahead and coded a modern, premium prototype for them. "
                f"HARD CTA: Give them this link ({demo_url}) and tell them to click 'Book Your Onboarding Call' so you can initiate the deployment. "
                "TONE: 3 sentences. Direct, authoritative, urgent. No subject line. Start casually. Keep the entire email under 120 words."
            )
    else:
        system_prompt = (
            "You are Haseeb Baig, founder of Pulsfi Marketing Agency. Do not sound like a generic marketer. "
            "HOOK: Tell them you were looking for them online but realized they have zero digital footprint (no website). "
            f"VALUE: Do not list all our services immediately. Focus strictly on how a modern, fast website will get {business_name} more customers in {city}. "
            "Frame the Vercel link as a free, pre-built gift to demonstrate our value. Explain briefly that operating without a modern storefront is bleeding revenue, so you went ahead and coded a premium prototype for them. "
            f"HARD CTA: Give them this link ({demo_url}) and tell them to click 'Book Your Onboarding Call' so you can initiate the deployment. "
            "TONE: 3 sentences. Direct, authoritative, urgent. No subject line. Start casually. Keep the entire email under 120 words."
        )

    user_prompt = (
        f"Business Name: {business_name}\n"
        f"Location: {city}\n"
        f"Demo Site URL: {demo_url}\n\n"
        "Draft the cold email."
    )

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.85,
            max_output_tokens=1024,
            thinking_config=types.ThinkingConfig(thinking_budget=256),
        ),
    )

    body: str = response.text.strip() if response.text else ""
    logger.debug("Generated email for '%s' (%d chars).", business_name, len(body))
    return body


# ---------------------------------------------------------------------------
# Batch generator
# ---------------------------------------------------------------------------
def batch_generate(
    input_csv: Path,
    output_csv: Path,
    api_key: str | None = None,
    base_url: str | None = None,
    subject_template: str = "Quick question for {name}",
) -> Path:
    """Process a CSV of leads, generate an email for each, and save results.

    Features:
    - Resumes from an existing ``output_csv`` (skips already-generated rows).
    - Checkpoints every :data:`CHECKPOINT_EVERY` records.
    - Handles HTTP 429 rate-limit errors with a 60 s cool-down.

    Args:
        input_csv: Path to scraped leads CSV (must have ``name``, ``city``,
            ``demo_url`` columns).
        output_csv: Destination CSV (will include ``email_body`` and
            ``email_subject`` columns — the latter so the dashboard's
            triage view can show/edit the real subject before send, not a
            generic placeholder).
        api_key: Gemini API key.  Falls back to :data:`config.GEMINI_API_KEY`.
        base_url: Demo base URL. Only used as a fallback if a lead's
            ``demo_url`` is missing — normally the URL scraper.py already
            built (with phone + audit report token) is reused as-is.
        subject_template: Subject line template. ``{name}`` is replaced
            with the business name — kept identical to email_sender.py's
            default so the subject shown in triage matches what's sent.

    Returns:
        Path to the output CSV.
    """
    api_key = api_key or GEMINI_API_KEY
    validate_config(require_gemini=True)
    client = create_client(api_key)

    df = pd.read_csv(input_csv)
    logger.info("Loaded %d leads from %s.", len(df), input_csv)

    # ---- Resume support ----
    start_idx = 0
    if output_csv.exists():
        existing = pd.read_csv(output_csv)
        done_count = existing["email_body"].notna().sum()
        if done_count > 0:
            logger.info(
                "Resuming — %d emails already generated in %s.",
                done_count,
                output_csv,
            )
            # Merge existing email bodies back
            df = df.copy()
            if "email_body" not in df.columns:
                df["email_body"] = None
            df.update(existing[["email_body"]])
            start_idx = int(done_count)
    else:
        df["email_body"] = None

    if "email_subject" not in df.columns:
        df["email_subject"] = None

    # ---- Generate emails ----
    generated = 0
    for idx in range(start_idx, len(df)):
        row = df.iloc[idx]

        # Skip if already populated (safety net)
        if pd.notna(row.get("email_body")) and str(row["email_body"]).strip():
            continue

        biz_name: str = safe_str(row.get("name", "Business"), default="Business")
        city: str = safe_str(row.get("city", "your city"), default="your city")
        cat: str = safe_str(row.get("category", ""), default="Service Provider")
        strategy: str = str(row.get("strategy", "no_website"))
        primary_flaw: str = safe_str(row.get("primary_flaw", ""))
        report_json: str = safe_str(row.get("report_json", ""))

        # Reuse the demo_url scraper.py already built (includes phone +
        # the audit report token) — rebuilding it here would silently
        # drop those params and the emailed link would lose the
        # personalised "Your Free Website Audit" section on the landing
        # page. Only fall back to building a bare URL if it's missing.
        demo_url = safe_str(row.get("demo_url", ""))
        if not demo_url:
            target_base = base_url or DEMO_BASE_URL
            params = urllib.parse.urlencode({"biz": biz_name, "city": city, "cat": cat})
            demo_url = f"{target_base.rstrip('/')}/?{params}"

        # Attempt generation with rate-limit retry
        for attempt in range(3):
            try:
                body = generate_email(client, biz_name, city, demo_url, strategy, primary_flaw, report_json)
                df.at[idx, "email_body"] = body
                df.at[idx, "email_subject"] = subject_template.replace("{name}", biz_name)
                generated += 1
                logger.info(
                    "[%d/%d] Email generated for '%s'.",
                    idx + 1,
                    len(df),
                    biz_name,
                )
                break

            except Exception as exc:
                exc_str = str(exc).lower()
                if "429" in exc_str or "resource_exhausted" in exc_str:
                    logger.warning(
                        "Rate limited — waiting %.0f s before retry …",
                        RATE_LIMIT_WAIT,
                    )
                    time.sleep(RATE_LIMIT_WAIT)
                else:
                    logger.error(
                        "Error generating email for '%s': %s", biz_name, exc
                    )
                    df.at[idx, "email_body"] = ""
                    break

        # Checkpoint
        if generated % CHECKPOINT_EVERY == 0 and generated > 0:
            df.to_csv(output_csv, index=False)
            logger.info("Checkpoint saved (%d emails so far).", generated)

        # Rate-limit delay
        time.sleep(RPM_DELAY)

    # Final save
    df.to_csv(output_csv, index=False)
    logger.info(
        "Batch complete — %d new emails generated. Saved -> %s",
        generated,
        output_csv,
    )
    return output_csv


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate cold emails for scraped leads using Gemini.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Input CSV of scraped leads.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV with email bodies (default: <input>_with_emails.csv).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Gemini API key (overrides .env).",
    )
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    inp: Path = args.input
    out: Path = args.output or inp.with_name(
        inp.stem + "_with_emails" + inp.suffix
    )

    try:
        batch_generate(input_csv=inp, output_csv=out, api_key=args.api_key)
    except Exception:
        logger.exception("Email generation failed.")
        sys.exit(1)
