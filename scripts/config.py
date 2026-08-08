"""
config.py — Central configuration for the B2B Lead Generation Engine.

Loads environment variables from .env, defines US state mappings,
business categories for OpenStreetMap queries, and API/SMTP settings.
"""

import math
import os
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def safe_str(value: Any, default: str = "") -> str:
    """Coerce a value to a string, treating pandas/NaN/None as ``default``.

    Plain ``str(x.get(k, "") or "")`` looks safe but isn't: after a CSV
    round-trip, an empty cell comes back as ``float('nan')``, which is
    *truthy* in Python — so ``nan or ""`` evaluates to ``nan`` and
    ``str(...)`` on it produces the literal text ``"nan"`` (visibly
    broken in emails/URLs, e.g. a phone link becoming ``tel:nan``).
    Use this helper anywhere a CSV-sourced value needs to become a clean
    string.
    """
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    text = str(value).strip()
    return default if text.lower() == "nan" else text


def is_placeholder(value: str | None) -> bool:
    """Return True if a config value is empty or an obvious template
    placeholder (e.g. ``your_api_key_here``) rather than a real secret.

    scraper.py and pipeline.py rely on this to decide whether the Google
    Places key is genuinely configured — treating a copied-over template
    value as "configured" would silently produce REQUEST_DENIED runs.
    """
    if not value:
        return True
    lowered = value.strip().lower()
    if not lowered:
        return True
    placeholder_markers = (
        "your_api", "your-api", "api_key_here", "replace-with",
        "changeme", "change_me", "xxxx", "todo", "placeholder", "example",
    )
    return any(marker in lowered for marker in placeholder_markers)

# ---------------------------------------------------------------------------
# Load .env from the project root (one level above /scripts).
# Also try scripts/.env so a key placed next to the scripts still works.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_ENV_PATH_SCRIPTS = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)
load_dotenv(dotenv_path=_ENV_PATH_SCRIPTS, override=False)  # root wins if both exist

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory constants
# ---------------------------------------------------------------------------
DATA_DIR: Path = _PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# US States → ISO 3166-2 codes
# ---------------------------------------------------------------------------
US_STATES: dict[str, str] = {
    "Alabama": "US-AL",
    "Alaska": "US-AK",
    "Arizona": "US-AZ",
    "Arkansas": "US-AR",
    "California": "US-CA",
    "Colorado": "US-CO",
    "Connecticut": "US-CT",
    "Delaware": "US-DE",
    "Florida": "US-FL",
    "Georgia": "US-GA",
    "Hawaii": "US-HI",
    "Idaho": "US-ID",
    "Illinois": "US-IL",
    "Indiana": "US-IN",
    "Iowa": "US-IA",
    "Kansas": "US-KS",
    "Kentucky": "US-KY",
    "Louisiana": "US-LA",
    "Maine": "US-ME",
    "Maryland": "US-MD",
    "Massachusetts": "US-MA",
    "Michigan": "US-MI",
    "Minnesota": "US-MN",
    "Mississippi": "US-MS",
    "Missouri": "US-MO",
    "Montana": "US-MT",
    "Nebraska": "US-NE",
    "Nevada": "US-NV",
    "New Hampshire": "US-NH",
    "New Jersey": "US-NJ",
    "New Mexico": "US-NM",
    "New York": "US-NY",
    "North Carolina": "US-NC",
    "North Dakota": "US-ND",
    "Ohio": "US-OH",
    "Oklahoma": "US-OK",
    "Oregon": "US-OR",
    "Pennsylvania": "US-PA",
    "Rhode Island": "US-RI",
    "South Carolina": "US-SC",
    "South Dakota": "US-SD",
    "Tennessee": "US-TN",
    "Texas": "US-TX",
    "Utah": "US-UT",
    "Vermont": "US-VT",
    "Virginia": "US-VA",
    "Washington": "US-WA",
    "West Virginia": "US-WV",
    "Wisconsin": "US-WI",
    "Wyoming": "US-WY",
}

# ---------------------------------------------------------------------------
# Business categories (OSM key/value tags), grouped into human-friendly
# groups so the dashboard can present a "Real Estate", "Restaurants &
# Cafes", etc. picker instead of raw OSM tag pairs. Each entry in
# CATEGORY_CATALOG is what the *user* selects; it fans out into one or
# more OSM key/value pairs for the Overpass query.
# ---------------------------------------------------------------------------
CATEGORY_CATALOG: list[dict[str, Any]] = [
    {"id": "real_estate", "label": "Real Estate", "icon": "🏠",
     "tags": [{"key": "office", "value": "estate_agent"}, {"key": "shop", "value": "real_estate"}]},
    {"id": "restaurants_cafes", "label": "Restaurants & Cafes", "icon": "🍽️",
     "tags": [{"key": "amenity", "value": "restaurant"}, {"key": "amenity", "value": "cafe"},
              {"key": "amenity", "value": "fast_food"}]},
    {"id": "bars_nightlife", "label": "Bars & Nightlife", "icon": "🍸",
     "tags": [{"key": "amenity", "value": "bar"}, {"key": "amenity", "value": "pub"}]},
    {"id": "beauty_salons", "label": "Beauty & Salons", "icon": "💇",
     "tags": [{"key": "shop", "value": "beauty"}, {"key": "shop", "value": "hairdresser"}]},
    {"id": "health_medical", "label": "Health & Medical", "icon": "🩺",
     "tags": [{"key": "amenity", "value": "dentist"}, {"key": "amenity", "value": "clinic"},
              {"key": "amenity", "value": "pharmacy"}, {"key": "amenity", "value": "doctors"}]},
    {"id": "veterinary", "label": "Veterinary", "icon": "🐾",
     "tags": [{"key": "amenity", "value": "veterinary"}]},
    {"id": "automotive", "label": "Automotive & Repair", "icon": "🔧",
     "tags": [{"key": "shop", "value": "car_repair"}, {"key": "shop", "value": "car"},
              {"key": "shop", "value": "tyres"}]},
    {"id": "legal_professional", "label": "Legal & Professional Services", "icon": "⚖️",
     "tags": [{"key": "office", "value": "lawyer"}, {"key": "office", "value": "accountant"},
              {"key": "office", "value": "insurance"}]},
    {"id": "retail_shopping", "label": "Retail & Shopping", "icon": "🛍️",
     "tags": [{"key": "shop", "value": "bakery"}, {"key": "shop", "value": "florist"},
              {"key": "shop", "value": "clothes"}]},
    {"id": "fitness_wellness", "label": "Fitness & Wellness", "icon": "💪",
     "tags": [{"key": "leisure", "value": "fitness_centre"}, {"key": "shop", "value": "massage"}]},
    {"id": "construction_trades", "label": "Construction & Trades", "icon": "🏗️",
     "tags": [{"key": "office", "value": "construction_company"}, {"key": "shop", "value": "hardware"}]},
    {"id": "hospitality", "label": "Hotels & Hospitality", "icon": "🏨",
     "tags": [{"key": "tourism", "value": "hotel"}, {"key": "tourism", "value": "guest_house"}]},
]

# Flat list of every {key, value} OSM tag pair across all categories, kept
# for backward compatibility with call sites expecting the old
# ``BUSINESS_CATEGORIES`` shape (e.g. "scrape everything").
BUSINESS_CATEGORIES: list[dict[str, str]] = [
    tag for cat in CATEGORY_CATALOG for tag in cat["tags"]
]


def resolve_category_ids(category_ids: list[str] | None) -> list[dict[str, str]]:
    """Turn user-facing category ids (e.g. ``["real_estate"]``) into the
    flat list of OSM ``{key, value}`` tag dicts Overpass needs.

    Unknown ids are ignored. An empty/None list resolves to every
    configured category (i.e. "scrape everything").
    """
    if not category_ids:
        return BUSINESS_CATEGORIES
    by_id = {cat["id"]: cat["tags"] for cat in CATEGORY_CATALOG}
    tags: list[dict[str, str]] = []
    for cid in category_ids:
        tags.extend(by_id.get(cid, []))
    return tags or BUSINESS_CATEGORIES


# ---------------------------------------------------------------------------
# Endpoints & Limits
# ---------------------------------------------------------------------------
# Multiple public Overpass mirrors — the scraper rotates through these on
# failure/timeout/rate-limit instead of hammering (and depending on) one
# single instance.
OVERPASS_MIRRORS: list[str] = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_URL: str = OVERPASS_MIRRORS[0]  # kept for legacy direct references

# ---------------------------------------------------------------------------
# Gemini (google-genai SDK)
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
# Default to gemini-2.0-flash for outreach copy: it has FAR higher free-tier
# limits (≈15 RPM / 1500 RPD) than gemini-2.5-flash (≈10 RPM / 250 RPD), and
# a 120-word cold email needs no reasoning budget. The old 2.5-flash default
# is exactly what produced the 429 storm in Phase 3. Override via .env if you
# have a paid tier and prefer 2.5.
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# ---------------------------------------------------------------------------
# Performance tuning (Phase 1) — all overridable via .env so you can dial
# these up on a paid Gemini tier without touching code.
# ---------------------------------------------------------------------------
def _int_env(name: str, default: int) -> int:
    """Read an int from the environment, falling back safely on bad input."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    """Read a boolean-ish env var ('1', 'true', 'yes', 'on')."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Gemini free tier is ~15 requests/minute; paid tiers are far higher. This
# single number paces BOTH enrichment and email generation via a shared
# token-bucket limiter, so raising it here speeds up everything at once.
GEMINI_RPM: int = _int_env("GEMINI_RPM", 15)

# Leads cache (Module 1.3). Cached discovery results are reused for
# repeat/overlapping searches. TTL bounds how long an audit is trusted.
LEADS_CACHE_ENABLED: bool = _bool_env("LEADS_CACHE_ENABLED", True)
LEADS_CACHE_TTL_DAYS: int = _int_env("LEADS_CACHE_TTL_DAYS", 30)

# Thinking budget for email generation. 0 = off (fastest, cheapest, and
# plenty for short cold-email copy). Only relevant on models that support
# thinking (e.g. gemini-2.5-*).
EMAIL_THINKING_BUDGET: int = _int_env("EMAIL_THINKING_BUDGET", 0)

# Concurrency for the enrichment phase (mostly HTTP work — safe to run
# several at once). Email generation uses its own worker count since it's
# gated harder by the Gemini RPM limit.
ENRICH_MAX_WORKERS: int = _int_env("ENRICH_MAX_WORKERS", 8)
EMAIL_MAX_WORKERS: int = _int_env("EMAIL_MAX_WORKERS", 4)

# Review-sentiment lookup (DuckDuckGo search + a Gemini summary per lead)
# is the single biggest time sink in enrichment — and the result isn't
# consumed anywhere downstream (not in emails, not on the dashboard). It's
# therefore OFF by default. Flip ENABLE_REVIEW_SENTIMENT=1 in .env to
# re-enable it if you later surface it in the UI.
ENABLE_REVIEW_SENTIMENT: bool = _bool_env("ENABLE_REVIEW_SENTIMENT", False)

# ---------------------------------------------------------------------------
# Google Maps Platform (Places API) — primary lead discovery source
# ---------------------------------------------------------------------------
GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")

# ---------------------------------------------------------------------------
# SMTP / Gmail
# ---------------------------------------------------------------------------
GMAIL_ADDRESS: str = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "")
SMTP_HOST: str = "smtp.gmail.com"
SMTP_PORT: int = 587
SENDER_NAME: str = os.getenv("SENDER_NAME", "LeadGen Agency")

# Where "hot lead" engagement alerts (someone opened their personalised
# demo page) get sent. Defaults to your own sending address so no extra
# setup is required — override in .env if you want alerts routed
# somewhere else (e.g. a Slack-forwarding inbox).
NOTIFY_EMAIL: str = os.getenv("NOTIFY_EMAIL", "") or GMAIL_ADDRESS

# ---------------------------------------------------------------------------
# Demo site base URL
# ---------------------------------------------------------------------------
DEMO_BASE_URL: str = os.getenv("VERCEL_BASE_URL", "https://REPLACE-WITH-YOUR-VERCEL-URL.example")


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------
def validate_config(
    require_gemini: bool = False,
    require_smtp: bool = False,
    require_demo_url: bool = False,
    require_maps: bool = False,
) -> bool:
    """Validate that required configuration values are present.

    Args:
        require_gemini: If True, check that GEMINI_API_KEY is set.
        require_smtp: If True, check that Gmail SMTP credentials are set.
        require_demo_url: If True, treat an unconfigured/placeholder
            VERCEL_BASE_URL as a hard error rather than a warning. Use
            this specifically before actually sending emails — a wrong
            demo link isn't recoverable once sent, unlike a scrape or a
            dry-run where it's just a warning to fix before going live.
        require_maps: If True, require GOOGLE_MAPS_API_KEY (Places API).

    Returns:
        True if all required config values are present.

    Raises:
        EnvironmentError: If any required value is missing.
    """
    errors: list[str] = []

    if require_gemini and not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY is not set. Add it to your .env file.")

    if require_maps and not GOOGLE_MAPS_API_KEY:
        errors.append(
            "GOOGLE_MAPS_API_KEY is not set. Add your Google Maps Platform "
            "(Places API) key to .env for fast, accurate lead discovery."
        )

    if require_smtp:
        if not GMAIL_ADDRESS:
            errors.append("GMAIL_ADDRESS is not set. Add it to your .env file.")
        if not GMAIL_APP_PASSWORD:
            errors.append("GMAIL_APP_PASSWORD is not set. Add it to your .env file.")

    demo_url_is_placeholder = not DEMO_BASE_URL or DEMO_BASE_URL == "https://REPLACE-WITH-YOUR-VERCEL-URL.example"
    if demo_url_is_placeholder:
        msg = (
            "VERCEL_BASE_URL is not set — every demo_url generated will point to "
            "a placeholder, non-functional address. Leads will NOT see a working "
            "personalised page. Set VERCEL_BASE_URL in your .env file to your real "
            "deployed demo-site URL."
        )
        if require_demo_url:
            errors.append(msg)
        else:
            logger.warning(msg)

    if not GOOGLE_MAPS_API_KEY and not require_maps:
        logger.warning(
            "GOOGLE_MAPS_API_KEY is not set — scraper will fall back to slower, "
            "less complete OpenStreetMap/Overpass. For enterprise-grade results "
            "add your Google Maps Platform Places API key to .env."
        )

    if errors:
        for err in errors:
            logger.error(err)
        raise EnvironmentError(
            "Missing required configuration:\n  • " + "\n  • ".join(errors)
        )

    logger.info("Configuration validated successfully.")
    return True
