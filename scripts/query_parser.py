"""
query_parser.py — Natural-language campaign parsing ("Copilot"-style input).

Turns a free-text request like:

    "scrape real estate businesses in Miami"
    "find slow-website beauty salons around Austin, Texas"
    "restaurants in Chicago"

into structured campaign parameters:

    {"state": "Florida", "city": "Miami", "category_ids": ["real_estate"],
     "query": "", "confidence": "high", "source": "keyword"}

Two layers, tried in order:

  1. KEYWORD LAYER (instant, free, offline). Matches category synonyms,
     state names, and a built-in city→state map covering the ~200 largest
     US cities. Handles the overwhelming majority of real queries with
     zero latency and zero API cost — critical because parsing runs on
     every keystroke-submit and must not burn Gemini quota.

  2. GEMINI LAYER (only when the keyword layer is not confident). Sends
     the query plus the allowed state/category vocabularies and asks for
     strict JSON. Output is VALIDATED against the real vocab — the model
     can only choose from what actually exists, so a hallucinated
     category can never reach the scraper.

Both layers return the same shape, so the caller never cares which one
answered. If neither can parse, we return needs={...} telling the UI
exactly which fields to ask the user for — graceful, never a dead end.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from config import CATEGORY_CATALOG, GEMINI_API_KEY, GEMINI_MODEL, US_STATES

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Vocabulary: category synonyms
# --------------------------------------------------------------------------- #
# Maps spoken-language words to category ids. Order matters only for
# readability; matching checks every entry.
CATEGORY_SYNONYMS: dict[str, list[str]] = {
    "real_estate": ["real estate", "realtor", "realty", "property", "properties",
                     "estate agent", "housing", "homes for sale", "brokerage"],
    "restaurants_cafes": ["restaurant", "cafe", "café", "coffee shop", "diner",
                           "eatery", "bistro", "food place", "pizzeria", "bakery"],
    "bars_nightlife": ["bar", "bars", "pub", "nightclub", "night club", "lounge",
                        "nightlife", "brewery", "taproom"],
    "beauty_salons": ["salon", "salons", "beauty", "hair", "barber", "barbershop",
                       "nail", "nails", "spa", "spas"],
    "health_medical": ["doctor", "clinic", "medical", "dentist", "dental",
                        "physician", "healthcare", "health care", "chiropractor",
                        "pharmacy", "therapist"],
    "veterinary": ["vet", "vets", "veterinary", "veterinarian", "animal clinic",
                    "pet clinic", "animal hospital"],
    "automotive": ["auto repair", "car repair", "mechanic", "automotive",
                    "auto shop", "car dealer", "dealership", "tire shop",
                    "body shop", "oil change"],
    "legal_professional": ["lawyer", "attorney", "law firm", "legal", "accountant",
                            "accounting", "cpa", "notary", "consulting firm",
                            "insurance agency"],
    "retail_shopping": ["retail", "boutique", "shopping", "clothing store",
                         "furniture store", "electronics store", "gift shop",
                         "retail store", "shops and stores"],
    "fitness_wellness": ["gym", "gyms", "fitness", "yoga", "pilates", "crossfit",
                          "wellness", "massage", "personal trainer"],
    "construction_trades": ["construction", "contractor", "plumber", "plumbing",
                             "electrician", "roofing", "roofer", "hvac", "handyman",
                             "landscaping", "builder", "remodeling"],
    "hospitality": ["hotel", "hotels", "motel", "inn", "hospitality", "resort",
                     "bed and breakfast", "b&b", "lodging"],
    "financial_services": ["bank", "banks", "insurance", "insurance agency", "financial advisor",
                            "tax advisor", "accountant", "accounting firm", "credit union"],
    "pet_services": ["pet grooming", "dog grooming", "pet boarding", "kennel", "pet store", "pet sitting"],
    "home_services": ["laundry", "dry cleaning", "dry cleaner", "cleaning service", "house cleaning",
                       "maid service", "storage unit", "self storage"],
    "events_photography": ["photographer", "photography", "photo studio", "event planner",
                            "florist", "flower shop", "event venue"],
    "education_childcare": ["daycare", "childcare", "preschool", "tutoring", "driving school",
                             "education center", "learning center"],
    "travel_transport": ["travel agency", "travel agent", "car rental", "taxi service", "limo service"],
    "specialty_trades": ["locksmith", "car wash", "tattoo shop", "tattoo parlor"],
    "funeral_services": ["funeral home", "funeral director", "mortuary", "cremation"],
    "printing_signage": ["print shop", "printing", "sign maker", "signage", "copy shop"],
    "entertainment": ["bowling alley", "arcade", "escape room", "entertainment venue"],
}

# --------------------------------------------------------------------------- #
# Vocabulary: major US cities → state (top metros; keyword layer only —
# the Gemini layer knows every city). Keys lowercase.
# --------------------------------------------------------------------------- #
CITY_TO_STATE: dict[str, str] = {
    # FL
    "miami": "Florida", "orlando": "Florida", "tampa": "Florida",
    "jacksonville": "Florida", "tallahassee": "Florida", "st. petersburg": "Florida",
    "st petersburg": "Florida", "fort lauderdale": "Florida", "sarasota": "Florida",
    # TX
    "houston": "Texas", "austin": "Texas", "dallas": "Texas",
    "san antonio": "Texas", "fort worth": "Texas", "el paso": "Texas", "plano": "Texas",
    # CA
    "los angeles": "California", "san francisco": "California", "san diego": "California",
    "san jose": "California", "sacramento": "California", "fresno": "California",
    "oakland": "California", "long beach": "California",
    # NY
    "new york": "New York", "new york city": "New York", "nyc": "New York",
    "buffalo": "New York", "rochester": "New York", "albany": "New York",
    # IL / other majors
    "chicago": "Illinois", "springfield": "Illinois",
    "phoenix": "Arizona", "tucson": "Arizona", "scottsdale": "Arizona", "mesa": "Arizona",
    "philadelphia": "Pennsylvania", "pittsburgh": "Pennsylvania",
    "columbus": "Ohio", "cleveland": "Ohio", "cincinnati": "Ohio",
    "charlotte": "North Carolina", "raleigh": "North Carolina", "durham": "North Carolina",
    "indianapolis": "Indiana", "seattle": "Washington", "spokane": "Washington",
    "denver": "Colorado", "colorado springs": "Colorado", "boulder": "Colorado",
    "washington": "Virginia",  # ambiguous; DC not a state — nearest sensible default
    "boston": "Massachusetts", "worcester": "Massachusetts",
    "nashville": "Tennessee", "memphis": "Tennessee", "knoxville": "Tennessee",
    "detroit": "Michigan", "grand rapids": "Michigan",
    "portland": "Oregon", "salem": "Oregon",
    "las vegas": "Nevada", "reno": "Nevada", "henderson": "Nevada",
    "louisville": "Kentucky", "lexington": "Kentucky",
    "baltimore": "Maryland", "annapolis": "Maryland",
    "milwaukee": "Wisconsin", "madison": "Wisconsin",
    "albuquerque": "New Mexico", "santa fe": "New Mexico",
    "kansas city": "Missouri", "st. louis": "Missouri", "st louis": "Missouri",
    "atlanta": "Georgia", "savannah": "Georgia", "augusta": "Georgia",
    "virginia beach": "Virginia", "richmond": "Virginia", "norfolk": "Virginia",
    "omaha": "Nebraska", "lincoln": "Nebraska",
    "minneapolis": "Minnesota", "st. paul": "Minnesota", "st paul": "Minnesota",
    "tulsa": "Oklahoma", "oklahoma city": "Oklahoma",
    "new orleans": "Louisiana", "baton rouge": "Louisiana",
    "wichita": "Kansas", "topeka": "Kansas",
    "anchorage": "Alaska", "honolulu": "Hawaii", "boise": "Idaho",
    "salt lake city": "Utah", "provo": "Utah",
    "birmingham": "Alabama", "montgomery": "Alabama", "tuscaloosa": "Alabama",
    "little rock": "Arkansas", "charleston": "South Carolina",
    "columbia": "South Carolina", "jackson": "Mississippi",
    "des moines": "Iowa", "cedar rapids": "Iowa",
    "hartford": "Connecticut", "providence": "Rhode Island",
    "manchester": "New Hampshire", "burlington": "Vermont",
    "cheyenne": "Wyoming", "billings": "Montana", "fargo": "North Dakota",
    "sioux falls": "South Dakota", "wilmington": "Delaware", "newark": "New Jersey",
    "jersey city": "New Jersey", "trenton": "New Jersey",
}

_STATES_LOWER = {s.lower(): s for s in US_STATES}
_VALID_CATEGORY_IDS = {c["id"] for c in CATEGORY_CATALOG}

# Merge in the comprehensive city dataset (us_cities.py) so AI Fill
# recognises far more cities than the hand-curated map alone. Curated
# entries win on conflict (they're hand-verified for ambiguous names).
try:
    from us_cities import CITIES_BY_STATE
    for _state, _cities in CITIES_BY_STATE.items():
        for _city in _cities:
            CITY_TO_STATE.setdefault(_city.lower(), _state)
except ImportError:
    pass


# --------------------------------------------------------------------------- #
# Layer 1 — keyword parsing (instant, free)
# --------------------------------------------------------------------------- #
def parse_keyword(query: str) -> dict[str, Any]:
    """Deterministic parse. Returns the standard result dict; confidence
    'high' only when we found BOTH a category and a location.
    """
    q = " " + query.lower().strip() + " "
    result: dict[str, Any] = {"state": None, "city": None, "category_ids": [],
                              "query": "", "confidence": "low", "source": "keyword"}

    # Categories: match any synonym as a whole-word-ish substring.
    for cat_id, synonyms in CATEGORY_SYNONYMS.items():
        for syn in synonyms:
            if re.search(rf"(?<![a-z]){re.escape(syn)}s?(?![a-z])", q):
                if cat_id not in result["category_ids"]:
                    result["category_ids"].append(cat_id)
                break

    # State: explicit state name anywhere in the query wins.
    for low, proper in _STATES_LOWER.items():
        if re.search(rf"(?<![a-z]){re.escape(low)}(?![a-z])", q):
            result["state"] = proper
            break

    # City: longest match first so "new york city" beats "york".
    for city in sorted(CITY_TO_STATE, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(city)}(?![a-z])", q):
            result["city"] = city.title().replace("St ", "St. ")
            # Infer the state from the city if none was stated explicitly.
            if not result["state"]:
                result["state"] = CITY_TO_STATE[city]
            break

    # "in <Word>" fallback: catches cities not in our map, e.g. "in Pueblo".
    if not result["city"]:
        m = re.search(r"\b(?:in|near|around|at)\s+([a-z][a-z .]{2,30}?)(?:\s*,\s*([a-z ]+))?\s*$", q.strip())
        if m:
            candidate = m.group(1).strip().title()
            # Don't mistake a state name for a city.
            if candidate.lower() not in _STATES_LOWER:
                result["city"] = candidate
            if m.group(2):
                maybe_state = m.group(2).strip().lower()
                if maybe_state in _STATES_LOWER:
                    result["state"] = _STATES_LOWER[maybe_state]

    have_cat = bool(result["category_ids"])
    have_loc = bool(result["state"])
    result["confidence"] = "high" if (have_cat and have_loc) else ("medium" if (have_cat or have_loc) else "low")
    return result


# --------------------------------------------------------------------------- #
# Layer 2 — Gemini parsing (complex phrasing only)
# --------------------------------------------------------------------------- #
def parse_gemini(query: str) -> dict[str, Any] | None:
    """LLM parse for phrasing the keyword layer can't handle. Returns the
    standard dict or None on any failure (caller falls back gracefully).
    Every field is validated against the real vocabularies.
    """
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        sys_prompt = (
            "Extract US lead-scraping parameters from the user's request. "
            "Respond with ONLY a JSON object, no markdown fences, shaped: "
            '{"state": "<full state name or null>", "city": "<city or null>", '
            '"category_ids": ["<id>", ...]}. '
            f"state MUST be one of: {sorted(US_STATES)}. "
            f"category_ids MUST be from: {sorted(_VALID_CATEGORY_IDS)}. "
            "If a city is named, infer its state. Use null for anything not determinable."
        )
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=query,
            config=types.GenerateContentConfig(
                system_instruction=sys_prompt, temperature=0.0, max_output_tokens=200,
            ),
        )
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)

        # STRICT validation — a hallucinated value can never pass through.
        state = data.get("state")
        state = state if state in US_STATES else None
        cats = [c for c in (data.get("category_ids") or []) if c in _VALID_CATEGORY_IDS]
        city = (data.get("city") or "").strip() or None
        if city and len(city) > 40:
            city = None

        conf = "high" if (state and cats) else ("medium" if (state or cats) else "low")
        return {"state": state, "city": city, "category_ids": cats,
                "query": "", "confidence": conf, "source": "gemini"}
    except Exception:  # noqa: BLE001 — any failure just means "layer unavailable"
        logger.warning("Gemini query parse failed; using keyword result.", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def parse_campaign_query(query: str) -> dict[str, Any]:
    """Parse a natural-language campaign request.

    Strategy: keyword layer first (instant). If it's fully confident,
    done — no API call at all. Otherwise try Gemini and take its answer
    when it improves on the keyword result; merge so each field keeps the
    best available value. Always includes ``needs`` listing anything
    still missing so the UI can prompt precisely.
    """
    query = (query or "").strip()
    if not query:
        return {"state": None, "city": None, "category_ids": [], "query": "",
                "confidence": "low", "source": "none",
                "needs": ["state", "category_ids"]}

    kw = parse_keyword(query)
    result = kw
    if kw["confidence"] != "high":
        llm = parse_gemini(query)
        if llm:
            # Merge: prefer LLM values, keep keyword values it missed.
            merged = {
                "state": llm["state"] or kw["state"],
                "city": llm["city"] or kw["city"],
                "category_ids": llm["category_ids"] or kw["category_ids"],
                "query": "", "source": llm["source"],
            }
            merged["confidence"] = ("high" if (merged["state"] and merged["category_ids"])
                                     else ("medium" if (merged["state"] or merged["category_ids"]) else "low"))
            result = merged

    needs = []
    if not result["state"]:
        needs.append("state")
    if not result["category_ids"]:
        needs.append("category_ids")
    result["needs"] = needs
    return result
