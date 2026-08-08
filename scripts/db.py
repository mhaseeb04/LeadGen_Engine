"""
db.py — SQLite-backed job store for the LeadGen control-plane.

api_server.py imports ``init_db, save_job, update_job, add_log, get_job``
from here. This replaces the old in-memory ``JOBS`` dict so that:

  1. Job status survives an API-server restart (Render restarts dynos;
     an in-memory dict loses every running/finished job silently).
  2. Multiple threads (job runner thread + request handlers) can read and
     write safely — SQLite in WAL mode with one short-lived connection
     per call handles this without any global connection sharing.
  3. The returned job dict matches EXACTLY what dashboard/js/campaign.js
     polls for: ``status``, ``phase``, ``message``, ``summary``,
     ``error`` — plus a full ``logs`` history for debugging.

Deliberately simple: no ORM, no migrations framework. The schema is tiny
and created idempotently on startup. Swap for Postgres later by keeping
these five function signatures identical.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH: Path = DATA_DIR / "leadgen.db"

# Columns that live as real columns on the jobs table. Anything else
# passed to update_job() (e.g. emails_sent, send_status, send_error)
# is merged into the JSON ``extra`` column so callers never need a
# schema change to attach new metadata to a job.
_JOB_COLUMNS = {"status", "phase", "message", "summary", "error"}


def _connect() -> sqlite3.Connection:
    """Open a short-lived connection. One connection per call is the
    simplest thread-safe pattern for SQLite; WAL mode allows concurrent
    readers while a writer is active, and a 10s busy timeout absorbs the
    brief write contention between the job thread and request handlers.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id     TEXT PRIMARY KEY,
                params     TEXT NOT NULL DEFAULT '{}',
                status     TEXT NOT NULL DEFAULT 'queued',
                phase      TEXT NOT NULL DEFAULT 'queued',
                message    TEXT NOT NULL DEFAULT '',
                summary    TEXT,
                error      TEXT,
                extra      TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id  TEXT NOT NULL,
                ts      REAL NOT NULL,
                phase   TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id)"
        )
    logger.info("Job store ready at %s", DB_PATH)


def save_job(job_id: str, params: dict[str, Any]) -> None:
    """Insert a new job in 'queued' state."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs
                (job_id, params, status, phase, message, created_at, updated_at)
            VALUES (?, ?, 'queued', 'queued', 'Campaign queued…', ?, ?)
            """,
            (job_id, json.dumps(params), now, now),
        )


def update_job(job_id: str, **fields: Any) -> None:
    """Update known columns; merge unknown keys into the JSON ``extra``.

    Examples::

        update_job(job_id, status="running")
        update_job(job_id, status="done", summary=summary_dict)
        update_job(job_id, emails_sent=12, send_status="completed")
    """
    if not fields:
        return

    column_updates: dict[str, Any] = {}
    extra_updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _JOB_COLUMNS:
            # summary is a dict → store as JSON text
            column_updates[key] = (
                json.dumps(value) if key == "summary" and value is not None else value
            )
        else:
            extra_updates[key] = value

    with _connect() as conn:
        if extra_updates:
            row = conn.execute(
                "SELECT extra FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            current_extra: dict[str, Any] = {}
            if row and row["extra"]:
                try:
                    current_extra = json.loads(row["extra"])
                except (ValueError, TypeError):
                    current_extra = {}
            current_extra.update(extra_updates)
            column_updates["extra"] = json.dumps(current_extra)

        set_clause = ", ".join(f"{col} = ?" for col in column_updates)
        values = list(column_updates.values()) + [time.time(), job_id]
        conn.execute(
            f"UPDATE jobs SET {set_clause}, updated_at = ? WHERE job_id = ?",
            values,
        )


def add_log(job_id: str, phase: str, message: str) -> None:
    """Append a progress line AND surface it as the job's current
    phase/message — this is what the dashboard's progress bar polls.
    """
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO job_logs (job_id, ts, phase, message) VALUES (?, ?, ?, ?)",
            (job_id, now, phase, message),
        )
        conn.execute(
            "UPDATE jobs SET phase = ?, message = ?, updated_at = ? WHERE job_id = ?",
            (phase, message, now, job_id),
        )


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return the full job dict the API serialises to the dashboard,
    or ``None`` if the job doesn't exist.

    Shape (consumed by dashboard/js/campaign.js — do not rename keys):
    ``{job_id, status, phase, message, summary, error, params,
    logs: [{ts, phase, message}, …], …extra}``
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None

        logs = [
            {"ts": lr["ts"], "phase": lr["phase"], "message": lr["message"]}
            for lr in conn.execute(
                "SELECT ts, phase, message FROM job_logs WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        ]

    def _json_or_default(text: str | None, default: Any) -> Any:
        if not text:
            return default
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return default

    job: dict[str, Any] = {
        "job_id": row["job_id"],
        "status": row["status"],
        "phase": row["phase"],
        "message": row["message"],
        "summary": _json_or_default(row["summary"], None),
        "error": row["error"],
        "params": _json_or_default(row["params"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "logs": logs,
    }
    # Flatten extra metadata (emails_sent, send_status, …) onto the job
    # so the dashboard can read job.send_status directly.
    job.update(_json_or_default(row["extra"], {}))
    return job
