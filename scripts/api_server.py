"""
email_generator.py — Gemini-powered cold email generator (Phase 1.2).

Two entry points:
  • generate_single() — build ONE email for one lead. Used by the
    dashboard's on-demand endpoint so Gemini is only called for leads the
    operator actually reviews (a handful), instead of all ~60 up front.
    This is what ended the 429 storm: the old flow generated every lead's
    email inside the campaign job, hammering the free tier and blocking
    the whole job (and therefore the leads table) for 20+ minutes.
  • batch_generate() — still available for CLI / bulk runs, now with the
    same rate limiter + exponential backoff.

Reliability changes vs the old version:
  • Model + thinking are config-driven (default gemini-2.0-flash, no
    thinking) — far higher free-tier limits than gemini-2.5-flash.
  • A shared token-bucket RateLimiter paces calls to the real RPM instead
    of a blanket sleep after every call.
  • 429s use bounded exponential backoff (few short waits) instead of a
    flat 60 s × 3, and a final failure returns a clear signal instead of
    hanging.
"""

import argparse
import logging
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from google import genai
from google.genai import types

from config import (
    DATA_DIR,
    DEMO_BASE_URL,
    EMAIL_MAX_WORKERS,
    EMAIL_THINKING_BUDGET,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_RPM,
    safe_str,
    validate_config,
)
from ratelimit import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CHECKPOINT_EVERY: int = 10
MAX_ATTEMPTS: int = 4
BASE_BACKOFF: float = 5.0   # seconds; grows 5 → 10 → 20 (capped)
MAX_BACKOFF: float = 30.0

# One shared limiter for all generation, so batch + on-demand together
# never exceed the account's RPM.
_GEMINI_LIMITER = RateLimiter(rpm=GEMINI_RPM)


class EmailGenerationError(RuntimeError):
    """Raised when generation fails after all retries — lets the API
    surface a clear message to the dashboard instead of hanging.
    """


def create_client(api_key: str) -> genai.Client:
    client = genai.Client(api_key=api_key)
    logger.info("Gemini client initialised (model: %s).", GEMINI_MODEL)
    return client


def _build_prompt(
    business_name: str,
    city: str,
    demo_url: str,
    strategy: str,
    primary_flaw: str,
    report_json: str,
) -> str:
    """Assemble the system prompt for one lead (dual-branch logic)."""
    issues_summary = primary_flaw
    if report_json:
        try:
            import json as _json
            report = _json.loads(report_json)
            failing = [c["title"] for c in report.get("checks", []) if c.get("status") == "fail"]
            if failing:
                issues_summary = "; ".join(failing[:2])
        except Exception:  # noqa: BLE001
            pass

    if strategy == "website_upgrade" and issues_summary and issues_summary != "protected_asset":
        return (
            "You are Haseeb Baig, founder of Pulsfi Marketing Agency. Do not sound like a generic marketer. "
            f"HOOK: Tell them you audited their site and found these specific issues: '{issues_summary}'. "
            f"VALUE: Focus strictly on how a modern, fast website will get {business_name} more customers in {city}. "
            "Frame the Vercel link as a free, pre-built gift. Explain briefly that these issues are costing them leads, so you coded a lightning-fast premium prototype to fix them. "
            f"HARD CTA: Give them this link ({demo_url}) and tell them to click 'Book Your Onboarding Call' so you can initiate deployment. "
            "TONE: 3 sentences. Direct, authoritative, urgent. No subject line. Start casually. Under 120 words."
        )
    if strategy == "website_upgrade":
        return (
            "You are Haseeb Baig, founder of Pulsfi Marketing Agency. Do not sound like a generic marketer. "
            "HOOK: Tell them you audited their site and noticed the architecture is severely outdated. "
            f"VALUE: Focus strictly on how a modern, fast website will get {business_name} more customers in {city}. "
            "Frame the Vercel link as a free, pre-built gift. Explain briefly that outdated frameworks leak traffic, so you coded a modern premium prototype for them. "
            f"HARD CTA: Give them this link ({demo_url}) and tell them to click 'Book Your Onboarding Call' so you can initiate deployment. "
            "TONE: 3 sentences. Direct, authoritative, urgent. No subject line. Start casually. Under 120 words."
        )
    return (
        "You are Haseeb Baig, founder of Pulsfi Marketing Agency. Do not sound like a generic marketer. "
        "HOOK: Tell them you were looking for them online but realized they have zero digital footprint (no website). "
        f"VALUE: Focus strictly on how a modern, fast website will get {business_name} more customers in {city}. "
        "Frame the Vercel link as a free, pre-built gift. Explain briefly that operating without a modern storefront is bleeding revenue, so you coded a premium prototype for them. "
        f"HARD CTA: Give them this link ({demo_url}) and tell them to click 'Book Your Onboarding Call' so you can initiate deployment. "
        "TONE: 3 sentences. Direct, authoritative, urgent. No subject line. Start casually. Under 120 words."
    )


def _generate_config(system_prompt: str) -> types.GenerateContentConfig:
    """Build the generation config, only attaching a thinking budget if
    one is actually configured (0 = off → faster, cheaper, fewer 429s).
    """
    kwargs: dict[str, Any] = dict(
        system_instruction=system_prompt,
        temperature=0.85,
        max_output_tokens=1024,
    )
    if EMAIL_THINKING_BUDGET > 0:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=EMAIL_THINKING_BUDGET)
    return types.GenerateContentConfig(**kwargs)


def generate_email(
    client: genai.Client,
    business_name: str,
    city: str,
    demo_url: str,
    strategy: str = "no_website",
    primary_flaw: str = "",
    report_json: str = "",
) -> str:
    """Generate a single cold-email body, paced + retried.

    Raises EmailGenerationError if it can't succeed within MAX_ATTEMPTS.
    """
    system_prompt = _build_prompt(business_name, city, demo_url, strategy, primary_flaw, report_json)
    user_prompt = (
        f"Business Name: {business_name}\nLocation: {city}\nDemo Site URL: {demo_url}\n\nDraft the cold email."
    )

    last_err: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            _GEMINI_LIMITER.acquire()  # respect account RPM across all threads
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=_generate_config(system_prompt),
            )
            body = response.text.strip() if response.text else ""
            if body:
                return body
            last_err = EmailGenerationError("Empty response from model")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            msg = str(exc).lower()
            is_rate = "429" in msg or "resource_exhausted" in msg or "quota" in msg
            if attempt < MAX_ATTEMPTS - 1:
                wait = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
                logger.warning(
                    "Email gen %s (attempt %d/%d) — backing off %.0fs.",
                    "rate-limited" if is_rate else f"error: {exc}",
                    attempt + 1, MAX_ATTEMPTS, wait,
                )
                time.sleep(wait)
            else:
                break

    raise EmailGenerationError(
        f"Failed to generate email for '{business_name}' after {MAX_ATTEMPTS} attempts: {last_err}"
    )


def generate_single(
    client: genai.Client,
    lead: dict[str, Any],
    base_url: str | None = None,
    subject_template: str = "Quick question for {name}",
) -> dict[str, str]:
    """On-demand single-lead generation for the dashboard endpoint.

    Accepts a lead dict (as the dashboard/CSV holds it) and returns
    ``{"subject": ..., "body": ...}``. Reuses the demo_url already built
    by the scraper (with phone + audit token) when present.
    """
    biz = safe_str(lead.get("name", "Business"), default="Business")
    city = safe_str(lead.get("city", "your city"), default="your city")
    strategy = str(lead.get("strategy", "no_website"))
    primary_flaw = safe_str(lead.get("primary_flaw", ""))
    report_json = safe_str(lead.get("report_json", ""))

    demo_url = safe_str(lead.get("demo_url", ""))
    if not demo_url:
        target_base = base_url or DEMO_BASE_URL
        params = urllib.parse.urlencode({"biz": biz, "city": city})
        demo_url = f"{target_base.rstrip('/')}/?{params}"

    body = generate_email(client, biz, city, demo_url, strategy, primary_flaw, report_json)
    subject = subject_template.replace("{name}", biz)
    return {"subject": subject, "body": body}


def batch_generate(
    input_csv: Path,
    output_csv: Path,
    api_key: str | None = None,
    base_url: str | None = None,
    subject_template: str = "Quick question for {name}",
) -> Path:
    """Bulk-generate emails for every lead in a CSV (CLI / power use).

    Kept for completeness; the dashboard now uses on-demand generation
    instead so it no longer blocks the leads table or burns quota on
    leads that will never be contacted.
    """
    api_key = api_key or GEMINI_API_KEY
    validate_config(require_gemini=True)
    client = create_client(api_key)

    df = pd.read_csv(input_csv)
    logger.info("Loaded %d leads from %s.", len(df), input_csv)

    if "email_body" not in df.columns:
        df["email_body"] = None
    if "email_subject" not in df.columns:
        df["email_subject"] = None

    generated = 0
    for idx in range(len(df)):
        row = df.iloc[idx]
        if pd.notna(row.get("email_body")) and str(row["email_body"]).strip():
            continue  # resume: skip already-generated rows wherever they are

        lead = row.to_dict()
        try:
            result = generate_single(client, lead, base_url, subject_template)
            df.at[idx, "email_body"] = result["body"]
            df.at[idx, "email_subject"] = result["subject"]
            generated += 1
            logger.info("[%d/%d] Email generated for '%s'.", idx + 1, len(df),
                        safe_str(lead.get("name", "Business")))
        except EmailGenerationError as exc:
            logger.error("%s", exc)
            df.at[idx, "email_body"] = ""  # leave empty; operator can regenerate

        if generated and generated % CHECKPOINT_EVERY == 0:
            df.to_csv(output_csv, index=False)
            logger.info("Checkpoint saved (%d emails).", generated)

    df.to_csv(output_csv, index=False)
    logger.info("Batch complete — %d emails generated -> %s", generated, output_csv)
    return output_csv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate cold emails for scraped leads using Gemini.")
    parser.add_argument("--input", type=Path, required=True, help="Input CSV of scraped leads.")
    parser.add_argument("--output", type=Path, default=None, help="Output CSV (default: <input>_with_emails.csv).")
    parser.add_argument("--api-key", default=None, help="Gemini API key (overrides .env).")
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    inp: Path = args.input
    out: Path = args.output or inp.with_name(inp.stem + "_with_emails" + inp.suffix)
    try:
        batch_generate(input_csv=inp, output_csv=out, api_key=args.api_key)
    except Exception:
        logger.exception("Email generation failed.")
        sys.exit(1)
