"""
leads_cache.py — SQLite write-through cache for discovered leads.

The "queue indexing" layer. Discovery (scrape + website audit) is by far
the slowest part of a campaign — dozens of HTTP calls per run. But the
same area gets searched over and over (re-running Virginia Beach /
Beauty & Salons, or overlapping category sweeps). Re-scraping identical
ground is pure waste.

This module caches every scraped lead in SQLite, keyed by ``place_id``
(a business's stable global identity) and indexed by
``(state, city, category)`` so a repeat or overlapping search is a single
indexed SELECT — milliseconds — instead of a fresh multi-minute scrape.

Design choices (deliberate, for speed + correctness):
  • place_id is the natural primary key: the same business found via two
    overlapping searches is stored once, not duplicated.
  • A composite index on (state, city, category) turns "give me every
    cached lead for this search" into an O(log n) index range scan.
  • TTL (config.LEADS_CACHE_TTL_DAYS): a website audit goes stale — a
    business that had no site last month may have one now — so cached
    rows older than the TTL are ignored and refreshed on next scrape.
  • Write-through: after a live scrape we upsert the results, so the
    cache warms itself with zero extra work from callers.
  • The full lead row is stored as JSON in one column, so the cache never
    needs a schema change when the scraper's columns evolve — only the
    handful of indexed/queried fields are promoted to real columns.

Public API (all any caller needs):
    init_cache()                      — create table + indexes (idempotent)
    get_cached(state, city, cat_ids)  — -> DataFrame | None  (None = miss)
    put_cached(df, state, city, cat_ids) -> int  (rows upserted)
    cache_stats()                     — small dict for a dashboard badge
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable

import pandas as pd

from config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH: Path = DATA_DIR / "leads_cache.db"

# How long a cached lead is trusted before it must be re-scraped. A
# website audit is a point-in-time fact; a month is a sensible default.
try:
    from config import LEADS_CACHE_TTL_DAYS as _TTL_DAYS
except ImportError:  # config predates this knob → safe default
    _TTL_DAYS = 30
TTL_SECONDS: float = float(_TTL_DAYS) * 86400.0

# Columns promoted to real, indexed columns for fast lookup + dedup.
# Everything else in a lead row rides along in the JSON ``payload``.
_PROMOTED = ("place_id", "name", "state", "city", "category")


# --------------------------------------------------------------------------- #
# Connection helper
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    """One short-lived WAL connection per call — the simplest pattern that
    is safe across the API's request + background threads.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA synchronous=NORMAL;")  # fast + safe enough for a cache
    return conn


# --------------------------------------------------------------------------- #
# Normalisation — the key to overlapping searches hitting the same bucket
# --------------------------------------------------------------------------- #
def _norm(value: str | None) -> str:
    """Normalise a key part so 'Virginia Beach', ' virginia beach ' and
    'VIRGINIA BEACH' all map to the same cache bucket.
    """
    return (value or "").strip().lower()


def _category_keys(category_ids: Iterable[str] | None) -> list[str]:
    """A search may span several categories; we cache per-category so an
    overlapping search that shares even one category reuses those rows.
    An empty/None selection means "all" — represented by the single
    sentinel key ``"*"``.
    """
    ids = [c for c in (category_ids or []) if c]
    return sorted({_norm(c) for c in ids}) or ["*"]


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_cache() -> None:
    """Create the table + indexes if absent. Idempotent; call on startup."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads_cache (
                place_id   TEXT NOT NULL,
                category   TEXT NOT NULL,
                name       TEXT,
                state      TEXT NOT NULL,
                city       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                cached_at  REAL NOT NULL,
                PRIMARY KEY (place_id, category)
            )
            """
        )
        # THE index that makes repeat/overlapping lookups instant.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_scope "
            "ON leads_cache (state, city, category, cached_at)"
        )
    logger.info("Leads cache ready at %s (TTL %.0f days)", DB_PATH, TTL_SECONDS / 86400)


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def get_cached(
    state: str,
    city: str | None,
    category_ids: Iterable[str] | None,
) -> pd.DataFrame | None:
    """Return a DataFrame of fresh cached leads for this exact search, or
    ``None`` on a miss.

    A "hit" requires that EVERY requested category has at least one fresh
    cached row for this (state, city). If any requested category is
    uncached or fully stale, we return None and let the caller do a full
    live scrape — partial cache would silently under-deliver leads.
    """
    st, ct = _norm(state), _norm(city)
    cats = _category_keys(category_ids)
    cutoff = time.time() - TTL_SECONDS

    frames: list[pd.DataFrame] = []
    with _connect() as conn:
        for cat in cats:
            rows = conn.execute(
                """
                SELECT payload FROM leads_cache
                 WHERE state = ? AND city = ? AND category = ? AND cached_at >= ?
                """,
                (st, ct, cat, cutoff),
            ).fetchall()
            if not rows:
                # This category is uncached/stale → treat whole search as a
                # miss so we never return an incomplete lead set.
                logger.info("Cache MISS: '%s' has no fresh rows for %s/%s.", cat, st, ct)
                return None
            frames.append(pd.DataFrame([json.loads(r["payload"]) for r in rows]))

    combined = pd.concat(frames, ignore_index=True)
    # De-dupe across categories on place_id (a business tagged in two of
    # the requested categories should appear once).
    if "place_id" in combined.columns:
        combined = combined.drop_duplicates(subset=["place_id"], keep="first")
    logger.info("Cache HIT: %d fresh leads for %s/%s across %d category(ies).",
                len(combined), st, ct, len(cats))
    return combined.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Write (write-through after a live scrape)
# --------------------------------------------------------------------------- #
def put_cached(
    df: pd.DataFrame,
    state: str,
    city: str | None,
    category_ids: Iterable[str] | None,
) -> int:
    """Upsert scraped leads into the cache. Returns rows written.

    Each lead is stored once per requested category key, so a later
    single-category search still finds it. Leads without a usable
    ``place_id`` fall back to a synthetic key from name+city so OSM
    results (which lack Google place_ids) are still cacheable.
    """
    if df is None or df.empty:
        return 0

    st, ct = _norm(state), _norm(city)
    cats = _category_keys(category_ids)
    now = time.time()

    records = df.to_dict("records")
    rows: list[tuple] = []
    for rec in records:
        payload = json.dumps(rec, default=str)
        name = str(rec.get("name", "") or "")
        pid = str(rec.get("place_id", "") or "").strip()
        if not pid:
            # Synthesise a stable id for keyless (OSM) rows.
            pid = f"osm::{_norm(name)}::{ct}"
        for cat in cats:
            rows.append((pid, cat, name, st, ct, payload, now))

    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO leads_cache
                (place_id, category, name, state, city, payload, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(place_id, category) DO UPDATE SET
                name=excluded.name, state=excluded.state, city=excluded.city,
                payload=excluded.payload, cached_at=excluded.cached_at
            """,
            rows,
        )
    logger.info("Cache WRITE: upserted %d row(s) for %s/%s.", len(rows), st, ct)
    return len(rows)


# --------------------------------------------------------------------------- #
# Maintenance / stats
# --------------------------------------------------------------------------- #
def purge_stale() -> int:
    """Delete rows older than the TTL. Returns rows removed. Optional
    housekeeping — get_cached already ignores stale rows.
    """
    cutoff = time.time() - TTL_SECONDS
    with _connect() as conn:
        cur = conn.execute("DELETE FROM leads_cache WHERE cached_at < ?", (cutoff,))
        return cur.rowcount


def cache_stats() -> dict:
    """Small summary for a dashboard badge / health check."""
    cutoff = time.time() - TTL_SECONDS
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads_cache").fetchone()[0]
        fresh = conn.execute(
            "SELECT COUNT(*) FROM leads_cache WHERE cached_at >= ?", (cutoff,)
        ).fetchone()[0]
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT place_id) FROM leads_cache"
        ).fetchone()[0]
    return {
        "rows_total": total,
        "rows_fresh": fresh,
        "distinct_businesses": distinct,
        "ttl_days": TTL_SECONDS / 86400,
    }
