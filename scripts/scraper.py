"""
scraper.py — Enterprise lead discovery for the B2B Lead Generation Engine.

Primary path: Google Places API (Text Search + Details) for fast, accurate,
high-coverage results on hotels, restaurants, professional services, etc.

Fallback path: OpenStreetMap / Overpass (when no Google Maps key is configured
or Places returns empty). The dual-branch WebsiteAnalyzer still runs on every
lead so strategy / score / grade / report_json stay identical downstream.
"""

import argparse
import logging
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from config import (
    BUSINESS_CATEGORIES,
    CATEGORY_CATALOG,
    DATA_DIR,
    DEMO_BASE_URL,
    GOOGLE_MAPS_API_KEY,
    OVERPASS_MIRRORS,
    OVERPASS_URL,
    US_STATES,
    resolve_category_ids,
    safe_str,
)

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
MAX_RETRIES: int = 2
RETRY_BACKOFF: float = 6.0   # seconds
REQUEST_DELAY: float = 2.0   # polite delay between Overpass calls

# Google Places (legacy Text Search) — fast, accurate, enterprise-grade
PLACES_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
PLACES_MAX_PAGES = 3          # 20 results per page → up to 60 leads per query
PLACES_PAGE_DELAY = 2.1       # Google requires ~2s before using next_page_token

# Human-readable search terms for each category_id (used by Google Places)
CATEGORY_SEARCH_TERMS: dict[str, list[str]] = {
    "real_estate": ["real estate agency", "realtor", "estate agent"],
    "restaurants_cafes": ["restaurant", "cafe", "coffee shop"],
    "bars_nightlife": ["bar", "pub", "nightclub"],
    "beauty_salons": ["beauty salon", "hair salon", "barber"],
    "health_medical": ["dentist", "medical clinic", "doctor", "pharmacy"],
    "veterinary": ["veterinary clinic", "animal hospital", "vet"],
    "automotive": ["auto repair", "car dealership", "tire shop"],
    "legal_professional": ["lawyer", "attorney", "accountant", "insurance agency"],
    "retail_shopping": ["bakery", "florist", "clothing store"],
    "fitness_wellness": ["gym", "fitness center", "yoga studio", "massage"],
    "construction_trades": ["general contractor", "construction company", "hardware store"],
    "hospitality": ["hotel", "motel", "guest house", "inn"],
}


# ---------------------------------------------------------------------------
# Google Places API — primary discovery engine
# ---------------------------------------------------------------------------
def _places_search_terms(category_ids: list[str] | None, query_text: str | None) -> list[str]:
    """Build a short list of high-signal Text Search queries."""
    terms: list[str] = []
    if query_text and query_text.strip():
        terms.append(query_text.strip())
    if category_ids:
        for cid in category_ids:
            terms.extend(CATEGORY_SEARCH_TERMS.get(cid, [cid.replace("_", " ")]))
    if not terms:
        # Fallback: one generic term per known category so "all categories"
        # still returns useful results instead of an empty run.
        for cid, tlist in CATEGORY_SEARCH_TERMS.items():
            terms.append(tlist[0])
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def _places_text_search(
    query: str,
    api_key: str,
    max_pages: int = PLACES_MAX_PAGES,
) -> list[dict[str, Any]]:
    """Run Google Places Text Search with pagination. Returns raw result dicts."""
    results: list[dict[str, Any]] = []
    params: dict[str, str] = {
        "query": query,
        "key": api_key,
        "type": "",  # leave empty; query already carries the intent
    }
    page = 0
    next_token: str | None = None

    while page < max_pages:
        if next_token:
            time.sleep(PLACES_PAGE_DELAY)
            params = {"pagetoken": next_token, "key": api_key}

        try:
            resp = requests.get(PLACES_TEXTSEARCH_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.error("Places Text Search failed for %r: %s", query, exc)
            break

        status = data.get("status", "")
        if status not in ("OK", "ZERO_RESULTS"):
            logger.warning(
                "Places Text Search status=%s for %r — %s",
                status, query, data.get("error_message", ""),
            )
            if status in ("OVER_QUERY_LIMIT", "REQUEST_DENIED"):
                break
            # For other transient statuses continue / stop gracefully
            break

        page_results = data.get("results", [])
        results.extend(page_results)
        logger.info(
            "Places page %d for %r → %d results (running total %d)",
            page + 1, query, len(page_results), len(results),
        )

        next_token = data.get("next_page_token")
        if not next_token or not page_results:
            break
        page += 1

    return results


def _places_details(place_id: str, api_key: str) -> dict[str, Any]:
    """Fetch website + phone for a place_id (cheap Details call)."""
    params = {
        "place_id": place_id,
        "fields": "name,formatted_phone_number,international_phone_number,website,formatted_address,geometry,business_status,types",
        "key": api_key,
    }
    try:
        resp = requests.get(PLACES_DETAILS_URL, params=params, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "OK":
            return data.get("result", {})
    except requests.RequestException as exc:
        logger.debug("Places Details failed for %s: %s", place_id, exc)
    return {}


def scrape_google_places(
    state: str,
    city: str | None,
    category_ids: list[str] | None,
    query_text: str | None,
    api_key: str,
    max_results: int = 60,
) -> list[dict[str, Any]]:
    """Discover leads via Google Places Text Search + Details.

    Returns records in the same shape the rest of the pipeline expects:
    name, phone, email, website, street, city, state, postcode, category,
    lat, lon, place_id (extra).
    """
    location_part = f"{city}, {state}" if city else state
    search_terms = _places_search_terms(category_ids, query_text)

    # Limit concurrent term fan-out so we stay under typical free-tier
    # quotas while still covering multi-category campaigns.
    terms_to_run = search_terms[:4] if len(search_terms) > 4 else search_terms
    logger.info(
        "Google Places discovery: location=%r terms=%s",
        location_part, terms_to_run,
    )

    seen_place_ids: set[str] = set()
    records: list[dict[str, Any]] = []

    for term in terms_to_run:
        if len(records) >= max_results:
            break
        full_query = f"{term} in {location_part}"
        raw_results = _places_text_search(full_query, api_key)

        for item in raw_results:
            if len(records) >= max_results:
                break
            place_id = item.get("place_id") or ""
            if not place_id or place_id in seen_place_ids:
                continue
            seen_place_ids.add(place_id)

            # Prefer data already present on the Text Search payload;
            # only hit Details when website or phone is missing.
            name = (item.get("name") or "").strip()
            if not name:
                continue

            website = (item.get("website") or "").strip()
            phone = (
                item.get("formatted_phone_number")
                or item.get("international_phone_number")
                or ""
            ).strip()
            address = (item.get("formatted_address") or "").strip()
            geometry = item.get("geometry", {}).get("location", {})
            lat = geometry.get("lat")
            lon = geometry.get("lng")
            types = item.get("types") or []

            if not website or not phone:
                details = _places_details(place_id, api_key)
                if details:
                    website = website or (details.get("website") or "").strip()
                    phone = phone or (
                        details.get("formatted_phone_number")
                        or details.get("international_phone_number")
                        or ""
                    ).strip()
                    address = address or (details.get("formatted_address") or "").strip()
                    if lat is None or lon is None:
                        geom = details.get("geometry", {}).get("location", {})
                        lat = geom.get("lat")
                        lon = geom.get("lng")
                    types = types or details.get("types") or []

            # Skip permanently closed / non-operational businesses
            status = item.get("business_status") or "OPERATIONAL"
            if status and status.upper() not in ("OPERATIONAL", "OPEN"):
                continue

            # Reject dead leads (no contact path at all)
            if not phone and not website:
                continue

            # Best-effort city extraction from formatted address
            city_guess = city or ""
            if not city_guess and address:
                parts = [p.strip() for p in address.split(",")]
                if len(parts) >= 3:
                    city_guess = parts[-3] if len(parts) >= 4 else parts[-2]

            # Map Google types → a readable category label
            category_label = "business"
            if category_ids:
                # Prefer the first user-selected category for labeling
                for cat in CATEGORY_CATALOG:
                    if cat["id"] in category_ids:
                        category_label = cat["label"]
                        break
            elif types:
                category_label = types[0].replace("_", " ")

            records.append({
                "osm_id": "",               # not from OSM
                "osm_type": "google_places",
                "place_id": place_id,
                "name": name,
                "phone": phone,
                "email": "",                # Google never returns email
                "website": website,
                "street": address,
                "city": city_guess or "City Not Listed",
                "state": state,
                "postcode": "",
                "category": category_label,
                "lat": lat,
                "lon": lon,
            })

    logger.info(
        "Google Places returned %d unique, contactable leads for %s.",
        len(records), location_part,
    )
    return records


# ---------------------------------------------------------------------------
# Overpass query builder
# ---------------------------------------------------------------------------
NOMINATIM_URL: str = "https://nominatim.openstreetmap.org/search"
DEFAULT_CITY_RADIUS_KM: float = 15.0


def geocode_city(city: str, state: str) -> tuple[float, float] | None:
    """Resolve a city name to (lat, lon) via OSM's Nominatim geocoder.

    This is the fix for the failure mode where city-scoped Overpass
    queries return zero results: matching by administrative boundary
    name (``area["name"="City"]["boundary"="administrative"]``) requires
    OSM to have a mapped boundary *polygon* for that exact place, at
    exactly the right admin_level. Huge numbers of real places —
    unincorporated communities, suburbs, anywhere colloquially called a
    "city" that isn't one administratively — have no such polygon, so
    that query silently returns nothing. Geocoding to a point and
    searching by radius has no such dependency.

    Nominatim's usage policy caps at ~1 request/second and requires an
    identifiable User-Agent — this function is called at most once per
    scrape run, well within that.

    Args:
        city: City name as typed by the user.
        state: Full state name, e.g. ``"California"`` (helps disambiguate
            same-named cities in different states).

    Returns:
        ``(lat, lon)`` tuple, or ``None`` if geocoding fails or finds
        nothing — callers should fall back gracefully, not raise.
    """
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": f"{city}, {state}, USA", "format": "json", "limit": 1},
            headers={"User-Agent": "PulsfiLeadGenBot/1.0 (+https://pulsfi.com; contact: hello@pulsfi.com)"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            logger.warning("Nominatim found no match for '%s, %s' — falling back to boundary-name search.", city, state)
            return None
        lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
        logger.info("Geocoded '%s, %s' -> (%.4f, %.4f).", city, state, lat, lon)
        return (lat, lon)
    except Exception as exc:  # noqa: BLE001 — geocoding failure must never crash the scrape
        logger.warning("Geocoding '%s, %s' failed (%s) — falling back to boundary-name search.", city, state, exc)
        return None


def build_overpass_query(
    state_iso: str,
    categories: list[dict[str, str]],
    city: str | None = None,
    city_coords: tuple[float, float] | None = None,
    radius_km: float = DEFAULT_CITY_RADIUS_KM,
    timeout: int | None = None,
) -> str:
    """Build an Overpass QL query string for the given state/city and categories.

    IMPORTANT: when ``city_coords`` is provided (the normal path — see
    :func:`geocode_city`), the query searches a radius around that point
    instead of matching an administrative boundary by name. This is the
    robust path: it works for any place Nominatim can locate, regardless
    of whether OSM has a mapped boundary polygon for it. The boundary-name
    match (``city`` without ``city_coords``) is kept only as a fallback
    for when geocoding itself fails, and is known to return zero results
    for many real places — see :func:`geocode_city`'s docstring.

    Args:
        state_iso: ISO 3166-2 code, e.g. ``"US-CA"``.
        categories: List of dicts with ``key`` and ``value`` OSM tags.
        city: City name, used only for the boundary-match fallback path
            when ``city_coords`` is not given.
        city_coords: ``(lat, lon)`` from :func:`geocode_city`. When given,
            takes precedence over ``city`` and searches by radius.
        radius_km: Search radius around ``city_coords``, in kilometers.
        timeout: Overpass query timeout in seconds. Defaults to a shorter
            window for city-scoped queries (they're small and should
            resolve quickly) and a longer one for state-wide queries
            (inherently a bigger job).

    Returns:
        A complete Overpass QL query string.
    """
    if timeout is None:
        timeout = 60 if (city or city_coords) else 180

    lines: list[str] = [f"[out:json][timeout:{timeout}];"]

    if city_coords:
        lat, lon = city_coords
        radius_m = int(radius_km * 1000)
        lines.append("(")
        for cat in categories:
            lines.append(
                f'  nwr["{cat["key"]}"="{cat["value"]}"]["name"](around:{radius_m},{lat},{lon});'
            )
        lines.append(");")
        lines.append("out center tags;")
        return "\n".join(lines)

    if city:
        # Fallback path only — see docstring. Requires OSM to have a
        # mapped administrative boundary for this exact name/admin_level.
        safe_city = city.replace('"', '\\"')
        lines.append(
            f'area["name"="{safe_city}"]["boundary"="administrative"]'
            f'["admin_level"~"^(8|9|10)$"]->.searchArea;'
        )
    else:
        lines.append(f'area["ISO3166-2"="{state_iso}"]->.searchArea;')

    lines.append("(")
    for cat in categories:
        lines.append(
            f'  nwr["{cat["key"]}"="{cat["value"]}"]["name"](area.searchArea);'
        )
    lines.append(");")
    lines.append("out center tags;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Overpass API caller
# ---------------------------------------------------------------------------
def query_overpass(query: str, mirrors: list[str] | None = None) -> dict[str, Any]:
    """Send a POST request to an Overpass API mirror and return the JSON body.

    Rotates through :data:`config.OVERPASS_MIRRORS` — each mirror gets
    :data:`MAX_RETRIES` attempts with exponential back-off before the
    scraper falls through to the next one. This means a single mirror
    being down, rate-limiting us, or timing out no longer kills the run.

    The HTTP-level timeout is derived from the query's own
    ``[timeout:N]`` Overpass QL directive (plus a buffer) rather than a
    fixed constant, so a small city-scoped query fails over to the next
    mirror quickly instead of hanging for minutes.

    Args:
        query: The Overpass QL query string.
        mirrors: Ordered list of Overpass endpoint URLs to try. Defaults
            to :data:`config.OVERPASS_MIRRORS`.

    Returns:
        Parsed JSON response as a dictionary.

    Raises:
        requests.HTTPError: If every mirror is exhausted without success.
    """
    mirrors = mirrors or OVERPASS_MIRRORS or [OVERPASS_URL]
    last_exc: Exception | None = None

    # Derive the HTTP timeout from the query's own internal [timeout:N]
    # directive so we don't wait far longer than the server itself will.
    http_timeout = 200
    match = re.search(r"\[timeout:(\d+)\]", query)
    if match:
        http_timeout = int(match.group(1)) + 20  # small buffer for network/response overhead

    for mirror_idx, mirror_url in enumerate(mirrors, start=1):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    "Overpass mirror %d/%d (%s) — attempt %d/%d …",
                    mirror_idx, len(mirrors), mirror_url, attempt, MAX_RETRIES,
                )
                resp = requests.post(
                    mirror_url,
                    data={"data": query},
                    timeout=http_timeout,
                    headers={
                        # Overpass's usage policy explicitly asks for a
                        # clear, identifiable User-Agent rather than a
                        # browser-spoofed one — some mirrors' abuse
                        # detection actually penalizes generic/faked
                        # Mozilla strings from what is obviously a script.
                        "User-Agent": "PulsfiLeadGenBot/1.0 (+https://pulsfi.com; contact: hello@pulsfi.com)",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip, deflate",
                    },
                )
                # Overpass returns 429/504 under load and sometimes 400 for
                # a mirror-specific quirk — none of these mean the query
                # itself is bad, so we don't hard-fail on the first mirror.
                if not resp.ok:
                    logger.error("Overpass HTTP %d from %s: %.200s", resp.status_code, mirror_url, resp.text)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                logger.info("Overpass returned %d elements from %s.", len(data.get("elements", [])), mirror_url)
                return data

            except requests.HTTPError as exc:
                last_exc = exc
                status = exc.response.status_code if exc.response is not None else 0
                if status in (429, 502, 503, 504) and attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * attempt
                    logger.warning("HTTP %d from %s — retrying in %.0f s …", status, mirror_url, wait)
                    time.sleep(wait)
                else:
                    logger.warning("Mirror %s failed (HTTP %s) — rotating to next mirror.", mirror_url, status)
                    break  # move on to the next mirror

            except requests.RequestException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * attempt
                    logger.warning("Request error (%s) on %s — retrying in %.0f s …", exc, mirror_url, wait)
                    time.sleep(wait)
                else:
                    logger.warning("Mirror %s exhausted retries — rotating to next mirror.", mirror_url)
                    break

    logger.error("All %d Overpass mirrors failed.", len(mirrors))
    if last_exc:
        raise last_exc
    raise RuntimeError("Exhausted all Overpass mirrors without a successful response.")


# ---------------------------------------------------------------------------
# Element parser
# ---------------------------------------------------------------------------
def parse_elements(
    data: dict[str, Any],
    categories: list[dict[str, str]],
    state: str,
    city_filter: str | None = None,
    query_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Extract business records from raw Overpass JSON.

    Args:
        data: Raw Overpass API response.
        categories: Requested OSM tag categories.
        state: The targeted state name.
        city_filter: If set, only keep leads whose ``addr:city`` matches
            this value (case-insensitive substring match).
        query_filter: If set, only keep leads whose business name contains
            this free-text query (case-insensitive substring match) — this
            is what a typed dashboard search query narrows down.

    Returns:
        List of flat dicts suitable for creating a DataFrame.
    """
    records: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for el in data.get("elements", []):
        tags: dict[str, str] = el.get("tags", {})
        name = tags.get("name", "").strip()
        if not name:
            continue

        # Coordinates: nodes have lat/lon directly; ways/relations use "center"
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")

        if lat is None or lon is None:
            continue

        phone = (tags.get("phone") or tags.get("contact:phone") or tags.get("mobile") or "").strip()
        website = (tags.get("website") or tags.get("contact:website", "")).strip()

        # Reject dead leads
        if not phone and not website:
            continue

        # Phone Normalization for Deduplication Hash
        phone_normalized = re.sub(r'\D', '', phone)
        city = tags.get("addr:city", "").strip()

        # City / free-text query narrowing (user-supplied filters)
        if city_filter and city_filter.strip().lower() not in city.lower():
            continue
        if query_filter and query_filter.strip().lower() not in name.lower():
            continue

        # Hash based deduplication
        hash_key = (
            f"{name.lower()}|{phone_normalized}"
            if phone_normalized
            else f"{name.lower()}|{city.lower()}"
        )
        if hash_key in seen_hashes:
            continue
        seen_hashes.add(hash_key)

        # Strict category matching
        assigned_category = _detect_category(tags, categories)
        if assigned_category == "other":
            continue

        records.append(
            {
                "osm_id": el.get("id"),
                "osm_type": el.get("type"),
                "name": name,
                "phone": phone,
                "email": (tags.get("email") or tags.get("contact:email", "")).strip(),
                "website": website,
                "street": tags.get("addr:street", "").strip(),
                "city": city or "City Not Listed",
                "state": state,
                "postcode": tags.get("addr:postcode", "").strip(),
                "category": assigned_category,
                "lat": lat,
                "lon": lon,
            }
        )
    logger.info("Parsed %d named, unique business records matching requested categories.", len(records))
    return records


def _detect_category(tags: dict[str, str], requested_categories: list[dict[str, str]]) -> str:
    """Return the exact requested category if matched, else 'other'."""
    for cat in requested_categories:
        if tags.get(cat["key"]) == cat["value"]:
            return f"{cat['key']}={cat['value']}"
    return "other"


# ---------------------------------------------------------------------------
# Filters & enrichment
# ---------------------------------------------------------------------------
# (Removed filter_no_website function entirely for dual-branch architecture)


def _category_type_label(category: str) -> str:
    """Turn an OSM ``key=value`` category string into a display label."""
    if not category or "=" not in category:
        return "business"
    return category.split("=", 1)[1].replace("_", " ")


def generate_demo_urls(df: pd.DataFrame, base_url: str) -> pd.DataFrame:
    """Add a ``demo_url`` column with personalised demo site links.

    IMPORTANT: ``demo-site/index.html`` loads ``js/script.js`` (NOT
    ``js/dynamic.js`` — that file is orphaned dead code from an earlier
    iteration and is never `<script src>`-included anywhere). The live
    ``script.js`` reads ``biz``, ``city`` and ``cat`` query params, so
    those are the names that matter here. We additionally send ``phone``
    and a compact base64 ``report`` token, both consumed by the audit
    section added to ``script.js``.

    Args:
        df: DataFrame with ``name``, ``city``, ``phone``, ``category`` columns.
        base_url: Base URL of the deployed demo site.

    Returns:
        DataFrame with the new ``demo_url`` column.
    """
    import base64
    import json as _json

    def _build(row: pd.Series) -> str:
        query: dict[str, str] = {
            "biz": safe_str(row.get("name", "")),
            "city": safe_str(row.get("city", "")),
            "cat": safe_str(row.get("category", "")),
            "phone": safe_str(row.get("phone", "")),
        }

        if str(row.get("strategy", "")) == "website_upgrade" and row.get("report_json"):
            try:
                report = _json.loads(row["report_json"])
                compact = {
                    "score": report.get("score"),
                    "grade": report.get("grade"),
                    "issues": [i["title"] for i in report.get("checks", []) if i.get("status") != "pass"][:4],
                }
                token = base64.urlsafe_b64encode(_json.dumps(compact).encode()).decode()
                query["report"] = token
            except Exception:  # noqa: BLE001 — never let report encoding break URL generation
                pass

        params = urllib.parse.urlencode(query)
        return f"{base_url.rstrip('/')}/?{params}"

    df = df.copy()
    df["demo_url"] = df.apply(_build, axis=1)
    logger.info("Generated demo URLs for %d leads.", len(df))
    return df


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------
def scrape_leads(
    state: str,
    categories: list[dict[str, str]] | None = None,
    category_ids: list[str] | None = None,
    city: str | None = None,
    query_text: str | None = None,
    output_path: Path | None = None,
    base_url: str | None = None,
) -> Path:
    """End-to-end scraping pipeline: discover → analyze → CSV.

    Discovery order (enterprise-ready):
      1. Google Places API (when GOOGLE_MAPS_API_KEY is set) — fast,
         accurate, high coverage for hotels, restaurants, etc.
      2. OpenStreetMap / Overpass fallback when no Maps key is present
         or Places returns nothing.

    Args:
        state: US state name (e.g. ``"California"``).
        categories: Raw OSM tag categories (fallback path only).
        category_ids: Friendly category ids from the dashboard
            (e.g. ``["hospitality", "restaurants_cafes"]``).
        city: Optional city name — strongly recommended for speed & relevance.
        query_text: Optional free-text narrowing (business name keywords).
        output_path: Where to save the CSV.
        base_url: Demo site base URL.

    Returns:
        Path to the saved CSV file.
    """
    if state not in US_STATES:
        raise ValueError(
            f"Unknown state '{state}'. Choose from: {', '.join(sorted(US_STATES))}"
        )

    state_iso = US_STATES[state]
    categories = categories or resolve_category_ids(category_ids) or BUSINESS_CATEGORIES
    base_url = base_url or DEMO_BASE_URL
    output_path = output_path or DATA_DIR / f"{state.lower().replace(' ', '_')}_leads.csv"

    logger.info(
        "=== Starting scrape for %s (%s) | city=%s | query=%r | categories=%s ===",
        state, state_iso, city or "any", query_text or "",
        category_ids or "all",
    )

    records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # PRIMARY: Google Places (enterprise path)
    # ------------------------------------------------------------------
    if GOOGLE_MAPS_API_KEY:
        logger.info("Using Google Places API for lead discovery (key present).")
        try:
            records = scrape_google_places(
                state=state,
                city=city,
                category_ids=category_ids,
                query_text=query_text,
                api_key=GOOGLE_MAPS_API_KEY,
                max_results=60,
            )
        except Exception:
            logger.exception("Google Places discovery failed — will try OSM fallback.")
            records = []
    else:
        logger.warning(
            "GOOGLE_MAPS_API_KEY not set — falling back to OpenStreetMap/Overpass "
            "(slower, incomplete coverage for many niches)."
        )

    # ------------------------------------------------------------------
    # FALLBACK: OpenStreetMap / Overpass
    # ------------------------------------------------------------------
    if not records:
        logger.info("Running OpenStreetMap/Overpass discovery…")
        city_coords: tuple[float, float] | None = None
        if city:
            city_coords = geocode_city(city, state)

        query = build_overpass_query(
            state_iso, categories, city=city, city_coords=city_coords
        )
        logger.debug("Overpass query:\n%s", query)
        try:
            data = query_overpass(query)
            time.sleep(REQUEST_DELAY)
            records = parse_elements(
                data,
                categories,
                state,
                city_filter=None if city_coords else city,
                query_filter=query_text,
            )
        except Exception:
            logger.exception("Overpass discovery also failed.")
            records = []

        if city and not records:
            if city_coords:
                logger.warning(
                    "Geocoded '%s' but found 0 matching businesses within %.0f km. "
                    "OSM coverage is sparse in many areas — set GOOGLE_MAPS_API_KEY "
                    "for reliable results.",
                    city, DEFAULT_CITY_RADIUS_KM,
                )
            else:
                logger.warning(
                    "Could not geocode '%s' and boundary fallback returned nothing. "
                    "Check spelling or use a larger nearby city.",
                    city,
                )

    if not records:
        logger.warning("No records found for %s. Saving empty CSV.", state)
        # Header-only CSV so pandas.read_csv never raises EmptyDataError
        empty_cols = [
            "osm_id", "osm_type", "place_id", "name", "phone", "email",
            "website", "street", "city", "state", "postcode", "category",
            "lat", "lon", "strategy", "primary_flaw", "flaw_count",
            "score", "grade", "report_json", "demo_url",
        ]
        pd.DataFrame(columns=empty_cols).to_csv(output_path, index=False)
        return output_path

    df = pd.DataFrame(records)

    # ------------------------------------------------------------------
    # Dual-Branch Asynchronous Website Analyzer (unchanged)
    # ------------------------------------------------------------------
    import concurrent.futures
    from analyzer import WebsiteAnalyzer

    logger.info("Starting asynchronous website analysis for %d leads…", len(df))
    analyzer = WebsiteAnalyzer()

    def process_row(row: dict[str, Any]) -> dict[str, Any]:
        import json as _json

        website = str(row.get("website", "")).strip()
        if not website:
            row["strategy"] = "no_website"
            row["primary_flaw"] = ""
            row["flaw_count"] = 0
            row["score"] = ""
            row["grade"] = ""
            row["report_json"] = ""
            return row

        res = analyzer.analyze(website)
        row["strategy"] = "website_upgrade"
        row["primary_flaw"] = res["primary_flaw"]
        row["flaw_count"] = res["flaw_count"]
        row["score"] = res.get("score", "")
        row["grade"] = res.get("grade", "")
        row["report_json"] = _json.dumps(res)
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        enriched_records = list(executor.map(process_row, df.to_dict("records")))

    df = pd.DataFrame(enriched_records)

    # Generate demo URLs & save
    df = generate_demo_urls(df, base_url)
    df.to_csv(output_path, index=False)
    logger.info("Saved %d leads → %s", len(df), output_path)

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover B2B leads (Google Places primary, OSM fallback).",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="US state name, e.g. 'California'.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV path (default: data/<state>_leads.csv).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Demo site base URL (overrides .env).",
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
        help="Friendly category id from CATEGORY_CATALOG, e.g. --category-id real_estate. "
        "Can be repeated. Defaults to all configured categories.",
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
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    cats: list[dict[str, str]] | None = None
    if args.categories:
        cats = [{"key": k, "value": v} for k, v in args.categories]

    try:
        result_path = scrape_leads(
            state=args.state,
            categories=cats,
            category_ids=args.category_ids,
            city=args.city,
            query_text=args.query,
            output_path=args.output,
            base_url=args.base_url,
        )
        logger.info("Done. Output: %s", result_path)
    except Exception:
        logger.exception("Scraper failed.")
        sys.exit(1)
