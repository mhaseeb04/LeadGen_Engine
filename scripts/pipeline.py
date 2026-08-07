"""
pipeline.py — Master orchestration script for the B2B Lead Generation Engine.

Runs all three phases sequentially:
  1. Scrape  — query OSM Overpass for businesses without websites.
  2. Enrich  — generate personalised cold emails via Gemini.
  3. Send    — deliver emails through Gmail SMTP.

Supports dry-run mode (skips sending) and comprehensive logging.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from config import (
    DATA_DIR,
    DEMO_BASE_URL,
    GEMINI_API_KEY,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    SENDER_NAME,
    US_STATES,
    validate_config,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR / "pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run_pipeline(
    state: str,
    categories: list[dict[str, str]] | None = None,
    category_ids: list[str] | None = None,
    city: str | None = None,
    query_text: str | None = None,
    base_url: str | None = None,
    dry_run: bool = False,
    subject_template: str = "Quick question for {name}",
    progress_cb: "callable | None" = None,
) -> dict[str, object]:
    """Run the full lead-generation pipeline.

    Args:
        state: US state name (e.g. ``"California"``).
        categories: Raw OSM categories to scrape. Takes precedence over
            ``category_ids``.
        category_ids: Friendly category ids selected by the user on the
            dashboard (e.g. ``["real_estate"]``).
        city: Optional city name to narrow results within the state.
        query_text: Optional free-text search narrowing business names.
        base_url: Demo site base URL.  Defaults to ``.env`` value.
        dry_run: If True, skip Phase 4 (email sending).
        subject_template: Subject line template for outbound emails.
        progress_cb: Optional callback ``fn(phase: str, message: str)``
            invoked at the start of each phase — lets the API server
            stream live status to the dashboard without polling files.

    Returns:
        Summary dict with counts and file paths.
    """

    def _progress(phase: str, message: str) -> None:
        if progress_cb:
            try:
                progress_cb(phase, message)
            except Exception:  # noqa: BLE001 — progress reporting must never break the run
                logger.debug("progress_cb raised for phase=%s", phase, exc_info=True)
    # Lazy imports to keep module-level lightweight and allow each script
    # to configure its own logging handlers.
    from scraper import scrape_leads  # noqa: WPS433
    from email_generator import batch_generate  # noqa: WPS433
    from email_sender import batch_send  # noqa: WPS433

    # IMPORTANT: do NOT default `categories` to BUSINESS_CATEGORIES here.
    # scrape_leads() resolves as `categories or resolve_category_ids(category_ids)
    # or BUSINESS_CATEGORIES` — eagerly filling `categories` with every tag
    # would silently override a user's `category_ids` selection (e.g.
    # "real_estate") with every configured category. Leave it as None
    # unless the caller explicitly passed raw OSM tags.
    base_url = base_url or DEMO_BASE_URL
    start_time = time.time()

    summary: dict[str, object] = {
        "state": state,
        "dry_run": dry_run,
        "leads_scraped": 0,
        "emails_generated": 0,
        "emails_sent": 0,
        "scrape_csv": None,
        "enriched_csv": None,
    }

    # ------------------------------------------------------------------
    # Phase 1: Scrape
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PHASE 1 — DISCOVERING leads (Google Places primary, OSM fallback)")
    logger.info("=" * 60)
    scope_note = f"in {city}" if city else "state-wide (no city given — this can be slow/unreliable, consider narrowing)"
    _progress("scrape", f"Scraping {state} {scope_note} for {', '.join(category_ids) if category_ids else 'all categories'}…")

    try:
        validate_config(require_gemini=(not dry_run), require_smtp=(not dry_run))
    except EnvironmentError:
        # Re-validate with only what Phase 1 needs (nothing beyond defaults)
        pass

    scrape_csv: Path = DATA_DIR / f"{state.lower().replace(' ', '_')}_leads.csv"
    try:
        scrape_csv = scrape_leads(
            state=state,
            categories=categories,
            category_ids=category_ids,
            city=city,
            query_text=query_text,
            output_path=scrape_csv,
            base_url=base_url,
        )
        import pandas as pd
        # Empty CSVs (zero leads) have no columns — treat as 0 rows, not a crash.
        try:
            df = pd.read_csv(scrape_csv)
        except pd.errors.EmptyDataError:
            df = pd.DataFrame()
        summary["leads_scraped"] = len(df)
        summary["scrape_csv"] = str(scrape_csv)
        logger.info("Phase 1 complete — %d leads scraped.", len(df))
    except Exception:
        logger.exception("Phase 1 FAILED.")
        return summary

    if summary["leads_scraped"] == 0:
        from config import GOOGLE_MAPS_API_KEY, is_placeholder

        # Read the scraper's sidecar diagnostics — it records the REAL
        # reason (e.g. Google's own "Places API (New) is not enabled"
        # message) instead of forcing us to guess here.
        google_error: str | None = None
        try:
            import json as _json
            meta_path = Path(str(scrape_csv)).with_suffix(".meta.json")
            if meta_path.exists():
                google_error = (_json.loads(meta_path.read_text(encoding="utf-8")) or {}).get("google_error")
        except Exception:  # noqa: BLE001 — diagnostics must never crash the pipeline
            pass

        if google_error:
            maps_hint = f" Google Places FAILED with a configuration error → {google_error}"
        elif not GOOGLE_MAPS_API_KEY or is_placeholder(GOOGLE_MAPS_API_KEY):
            maps_hint = (
                " GOOGLE_MAPS_API_KEY is missing or a placeholder. The engine fell back to "
                "OpenStreetMap which has sparse coverage for many niches. "
                "Add a real Google Maps API key to .env for production-grade results."
            )
        else:
            maps_hint = (
                " Google Places was used but returned no results. This can happen if the "
                "API key has no billing enabled, is restricted, or the category is too narrow. "
                "Check your Google Cloud Console or try a broader category."
            )
            
        empty_msg = (
            f"No businesses found for {', '.join(category_ids) if category_ids else 'the selected categories'} "
            f"in {city + ', ' if city else ''}{state}.{maps_hint}"
        )
        logger.warning(empty_msg)
        summary["warning"] = empty_msg
        _progress("scrape", empty_msg)
        _progress("done", empty_msg)
        return summary

    # ------------------------------------------------------------------
    # Phase 2: Enrich (Website Audit & Review Scraping)
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PHASE 2 — ENRICHING leads via Google Gemini & DuckDuckGo")
    logger.info("=" * 60)
    _progress("enrich", f"Enriching {summary['leads_scraped']} leads…")

    from enricher import batch_enrich
    
    enriched_csv: Path = scrape_csv.with_name(
        scrape_csv.stem + "_enriched.csv"
    )

    try:
        validate_config(require_gemini=True)
        enriched_csv = batch_enrich(
            input_csv=scrape_csv,
            output_csv=enriched_csv,
            api_key=GEMINI_API_KEY,
        )
        df = pd.read_csv(enriched_csv)
        summary["leads_enriched"] = len(df)
        summary["enriched_csv"] = str(enriched_csv)
        logger.info("Phase 2 complete — %d leads retained after enrichment.", len(df))
    except Exception:
        logger.exception("Phase 2 FAILED.")
        return summary

    if summary.get("leads_enriched", 0) == 0:
        logger.warning("No leads survived enrichment — pipeline stopping early.")
        return summary

    # ------------------------------------------------------------------
    # Phase 3: Generate emails
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PHASE 3 — GENERATING cold emails with Gemini")
    logger.info("=" * 60)
    _progress("generate", "Writing personalised cold emails…")

    email_csv: Path = enriched_csv.with_name(
        enriched_csv.stem + "_with_emails.csv"
    )

    try:
        validate_config(require_gemini=True)
        email_csv = batch_generate(
            input_csv=enriched_csv,
            output_csv=email_csv,
            api_key=GEMINI_API_KEY,
            base_url=base_url,
            subject_template=subject_template,
        )
        df = pd.read_csv(email_csv)
        generated_count = int(df["email_body"].notna().sum())
        summary["emails_generated"] = generated_count
        summary["email_csv"] = str(email_csv)
        logger.info("Phase 3 complete — %d emails generated.", generated_count)
    except Exception:
        logger.exception("Phase 3 FAILED.")
        return summary

    # ------------------------------------------------------------------
    # Phase 4: Send emails
    # ------------------------------------------------------------------
    if dry_run:
        logger.info("=" * 60)
        logger.info("PHASE 4 — SKIPPED (dry-run mode)")
        logger.info("=" * 60)
    else:
        logger.info("=" * 60)
        logger.info("PHASE 4 — SENDING emails via Gmail SMTP")
        logger.info("=" * 60)
        _progress("send", "Dispatching emails…")

        try:
            validate_config(require_smtp=True, require_demo_url=True)
            sent = batch_send(
                input_csv=email_csv,
                gmail_addr=GMAIL_ADDRESS,
                app_password=GMAIL_APP_PASSWORD,
                sender_name=SENDER_NAME,
                subject_template=subject_template,
            )
            summary["emails_sent"] = sent
            logger.info("Phase 4 complete — %d emails sent.", sent)
        except Exception:
            logger.exception("Phase 4 FAILED.")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.time() - start_time
    _progress("done", "Pipeline complete.")
    logger.info("=" * 60)
    logger.info("PIPELINE SUMMARY")
    logger.info("=" * 60)
    logger.info("  State:            %s", state)
    logger.info("  Dry run:          %s", dry_run)
    logger.info("  Leads scraped:    %s", summary["leads_scraped"])
    logger.info("  Emails generated: %s", summary["emails_generated"])
    logger.info("  Emails sent:      %s", summary["emails_sent"])
    logger.info("  Scrape CSV:       %s", summary["scrape_csv"])
    logger.info("  Enriched CSV:     %s", summary["enriched_csv"])
    logger.info("  Elapsed time:     %.1f s", elapsed)
    logger.info("=" * 60)

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the full B2B lead-generation pipeline.",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="US state name, e.g. 'California'.",
    )
    parser.add_argument(
        "--category",
        nargs=2,
        metavar=("KEY", "VALUE"),
        action="append",
        dest="categories",
        help="Raw OSM tag to query, e.g. --category amenity restaurant. "
        "Can be repeated. Overrides --category-id.",
    )
    parser.add_argument(
        "--category-id",
        action="append",
        dest="category_ids",
        help="Friendly category id from CATEGORY_CATALOG, e.g. --category-id real_estate. Can be repeated.",
    )
    parser.add_argument(
        "--city",
        default=None,
        help="Narrow results to this city within the state.",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Free-text search narrowing results to matching business names.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Demo site base URL (overrides .env).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run Phases 1 & 2 only — do NOT send emails.",
    )
    parser.add_argument(
        "--subject",
        default="Quick question for {name}",
        help="Email subject template. Use {name} as placeholder.",
    )
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    cats: list[dict[str, str]] | None = None
    if args.categories:
        cats = [{"key": k, "value": v} for k, v in args.categories]

    try:
        run_pipeline(
            state=args.state,
            categories=cats,
            category_ids=args.category_ids,
            city=args.city,
            query_text=args.query,
            base_url=args.base_url,
            dry_run=args.dry_run,
            subject_template=args.subject,
        )
    except Exception:
        logger.exception("Pipeline crashed.")
        sys.exit(1)
