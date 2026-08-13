"""
api_server.py — Local control-plane API for the LeadGen Command Center.

Turns the dashboard from a "manually import a CSV someone ran from the
CLI" tool into a real product: the operator types a search query, picks a
state/city and one or more business categories, hits "Run Campaign", and
watches the pipeline execute live.

Endpoints
---------
GET  /api/health                    liveness probe
GET  /api/categories                friendly category catalog for the picker
GET  /api/states                    US state list
POST /api/campaigns                 start a new pipeline run -> {job_id}
GET  /api/campaigns/<job_id>        job status + progress log
GET  /api/campaigns/<job_id>/leads  resulting leads as JSON (once available)
POST /api/contact                   inbound lead from the agency-site contact form
POST /api/track                     hot-lead engagement beacon from demo-site (see below)

Hot-lead strategy
------------------
The single highest-leverage, lowest-effort addition to a "Trojan Horse"
landing page like this one isn't more design — it's knowing the moment a
prospect opens it. Response speed is one of the best-documented levers in
B2B sales; a call placed while someone is still on (or just left) their
personalised page converts dramatically better than the same call a day
later. `/api/track` receives a beacon from demo-site's script.js the
instant a lead opens their page (and again if they scroll toward
booking), logs it to `data/lead_engagement.csv` for a full history, and
— debounced to one email per lead per 30 minutes — fires an instant
"hot lead" email alert so you can call while it still matters. No new
paid tooling: it reuses the same Gmail SMTP credentials email_sender.py
already uses.

Deployment note: demo-site is a static site (deployed to Vercel) and
api_server.py currently binds to 127.0.0.1 for local use. For the beacon
to actually reach this server, deploy api_server.py somewhere publicly
reachable (a small Railway/Render/VPS instance is enough) and point
demo-site's `PULSFI_TRACK_ENDPOINT` (top of index.html) at it. Until then
the beacon fails silently and the rest of the page is unaffected.

Scaling note
------------
This uses an in-memory dict + a background thread per job, which is the
right amount of infrastructure for a single-operator local tool. The job
runner is intentionally isolated behind `_start_job()` so it can be
swapped for a real queue (Celery/RQ + Redis) and the in-memory `JOBS`
dict swapped for Postgres/SQLite without touching any route handler —
each route only talks to `_start_job`, `_get_job`, and `_list_job_leads`.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
import os

from config import CATEGORY_CATALOG, DATA_DIR, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL, SENDER_NAME, US_STATES
from db import init_db, save_job, update_job, add_log, get_job

# Initialize databases on startup
init_db()
try:
    from leads_cache import init_cache
    init_cache()
except Exception:  # noqa: BLE001 — cache is an optimization, never fatal
    logger.warning("Leads cache init skipped.", exc_info=True)

API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # dashboard/demo-site are served from different origins during local dev

# ---------------------------------------------------------------------------
# Authentication Middleware
# ---------------------------------------------------------------------------
PUBLIC_PATHS = ["/api/health", "/api/categories", "/api/states", "/api/track", "/api/contact"]

# Per-IP limiters for the two OPEN endpoints (track/contact). These stay
# unauthenticated because the demo site calls them from a prospect's
# browser, so a rate limit is the only thing standing between them and
# inbox/disk flooding. Generous enough for real visitor traffic, tight
# enough to stop abuse. Health/categories/states are read-only + cheap,
# so they're public but not rate-limited here.
from ratelimit_http import RateLimiter as _HTTPRateLimiter
_PUBLIC_LIMITER = _HTTPRateLimiter(
    max_requests=int(os.getenv("PUBLIC_RATE_MAX", "20")),
    window_seconds=int(os.getenv("PUBLIC_RATE_WINDOW", "60")),
)


def _client_ip() -> str:
    """Best-effort client IP, honouring the proxy header Render sets."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


@app.before_request
def check_auth():
    # Rate-limit the OPEN endpoints before doing anything else.
    if request.path in ("/api/track", "/api/contact"):
        if not _PUBLIC_LIMITER.allow(_client_ip()):
            resp = jsonify({"error": "Too many requests. Please slow down."})
            resp.status_code = 429
            resp.headers["Retry-After"] = str(_PUBLIC_LIMITER.retry_after())
            return resp

    # Public endpoints (no API key required)
    if request.path in PUBLIC_PATHS:
        return

    # If no secret key is set in environment, allow all (local mode)
    if not API_SECRET_KEY:
        return

    auth_key = request.headers.get("X-API-Key")
    if auth_key != API_SECRET_KEY:
        abort(401, description="Invalid or missing API Key")

# ---------------------------------------------------------------------------
# Hot-lead alert debounce.
# ---------------------------------------------------------------------------
_LAST_ALERTED: dict[tuple[str, str], float] = {}
_ALERT_LOCK = threading.Lock()
ALERT_DEBOUNCE_SECONDS = 30 * 60  # one alert per lead per 30 minutes


def _run_job(job_id: str, params: dict[str, Any]) -> None:
    from pipeline import run_pipeline

    update_job(job_id, status="running")
    try:
        summary = run_pipeline(
            state=params["state"],
            category_ids=params.get("category_ids") or None,
            city=params.get("city") or None,
            query_text=params.get("query") or None,
            dry_run=params.get("dry_run", True),
            generate_emails=params.get("generate_emails", False),
            force_refresh=params.get("force_refresh", False),
            progress_cb=lambda phase, msg: add_log(job_id, phase, msg),
        )
        update_job(job_id, status="done", summary=summary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s failed", job_id)
        update_job(job_id, status="error", error=str(exc))


def _start_job(params: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex[:12]
    save_job(job_id, params)
    thread = threading.Thread(target=_run_job, args=(job_id, params), daemon=True)
    thread.start()
    return job_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.post("/api/contact")
def contact() -> Any:
    """Receive an inbound lead from the agency-site contact form and
    append it to ``data/inbound_contacts.csv``. Kept intentionally simple
    (no DB) to match the rest of this project's local-first footprint —
    swap for a real datastore/CRM webhook when this goes to production.
    """
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    if not name or not email:
        return jsonify({"error": "'name' and 'email' are required"}), 400

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "inbound_contacts.csv"
    is_new = not out_path.exists()

    with open(out_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "name", "business", "email", "website", "message"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            name,
            (body.get("business") or "").strip(),
            email,
            (body.get("website") or "").strip(),
            (body.get("message") or "").strip(),
        ])

    logger.info("New inbound contact: %s <%s>", name, email)
    return jsonify({"status": "received"}), 201


# ---------------------------------------------------------------------------
# Hot-lead engagement tracking
# ---------------------------------------------------------------------------
ALLOWED_TRACK_EVENTS = {"page_view", "booking_intent", "booking_confirmed"}
# An actual booking is the single highest-value signal this system can
# produce — it must never be suppressed by the page_view/intent debounce.
NEVER_DEBOUNCED_EVENTS = {"booking_confirmed"}


def _send_hot_lead_alert(event: str, biz: str, city: str, cat: str, phone: str) -> None:
    """Fire a short, plain-text 'hot lead' email. Never raises — a
    notification failure must never break the tracking endpoint or take
    down the calling request.
    """
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAIL):
        logger.debug("Hot-lead alert skipped — SMTP/NOTIFY_EMAIL not configured.")
        return
    try:
        # Lazy import: keeps this module importable without email deps
        # present, matching the rest of the file's lazy-import pattern.
        from email.mime.text import MIMEText

        from email_sender import create_smtp_connection

        if event == "page_view":
            action = "just opened their audit page"
        elif event == "booking_intent":
            action = "is looking at booking a call"
        else:
            action = "just BOOKED a call — check your calendar"
        subject = f"🔥 Hot lead: {biz or 'A lead'} {action}"
        lines = [
            f"{biz or 'A lead'} in {city or 'an unknown city'} {action} — right now is the best time to call.",
            "",
            f"Business:  {biz or '—'}",
            f"City:      {city or '—'}",
            f"Category:  {cat or '—'}",
            f"Phone:     {phone or '—'}",
            f"Event:     {event}",
        ]
        msg = MIMEText("\n".join(lines))
        msg["From"] = f"{SENDER_NAME} <{GMAIL_ADDRESS}>"
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = subject

        conn = create_smtp_connection(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        try:
            conn.sendmail(GMAIL_ADDRESS, [NOTIFY_EMAIL], msg.as_string())
        finally:
            conn.quit()
        logger.info("Hot-lead alert sent for %s (%s).", biz, event)
    except Exception:  # noqa: BLE001 — a failed alert must never break tracking
        logger.exception("Failed to send hot-lead alert for %s", biz)


@app.post("/api/track")
def track() -> Any:
    """Receive an engagement beacon from demo-site's script.js.

    Body: ``{event, biz, city, cat, phone}`` where ``event`` is
    ``"page_view"`` (fired once the personalised page finishes loading)
    or ``"booking_intent"`` (fired when the visitor scrolls to/clicks the
    booking area). Every event is logged to
    ``data/lead_engagement.csv`` regardless of debounce; the *email*
    alert is debounced per lead (see ``ALERT_DEBOUNCE_SECONDS``) so a
    single visit doesn't generate several emails.
    """
    body = request.get_json(force=True, silent=True) or {}
    event = (body.get("event") or "").strip()
    if event not in ALLOWED_TRACK_EVENTS:
        return jsonify({"error": f"'event' must be one of {sorted(ALLOWED_TRACK_EVENTS)}"}), 400

    biz = (body.get("biz") or "").strip()
    city = (body.get("city") or "").strip()
    cat = (body.get("cat") or "").strip()
    phone = (body.get("phone") or "").strip()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "lead_engagement.csv"
    is_new = not out_path.exists()
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "event", "business", "city", "category", "phone"])
        writer.writerow([datetime.now(timezone.utc).isoformat(), event, biz, city, cat, phone])

    key = (biz.lower(), city.lower())
    now = time.time()
    should_alert = False
    with _ALERT_LOCK:
        if event in NEVER_DEBOUNCED_EVENTS:
            _LAST_ALERTED[key] = now
            should_alert = True
        else:
            last = _LAST_ALERTED.get(key, 0.0)
            if now - last >= ALERT_DEBOUNCE_SECONDS:
                _LAST_ALERTED[key] = now
                should_alert = True

    if should_alert:
        # Fire-and-forget in a background thread so a slow SMTP call
        # never delays the beacon response the browser is waiting on.
        threading.Thread(
            target=_send_hot_lead_alert, args=(event, biz, city, cat, phone), daemon=True
        ).start()

    return jsonify({"status": "tracked", "alerted": should_alert}), 201


@app.get("/api/health")
def health() -> Any:
    return jsonify({"status": "ok"})


@app.get("/api/cache/stats")
def cache_stats_endpoint() -> Any:
    """Small summary of the leads cache (for a dashboard badge)."""
    try:
        from leads_cache import cache_stats
        return jsonify(cache_stats())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 200


@app.post("/api/parse_query")
def parse_query_endpoint() -> Any:
    """Copilot-style natural-language parsing for the campaign form.

    Body: {"query": "scrape real estate businesses in Miami"}
    Returns: {state, city, category_ids, confidence, source, needs}

    Instant keyword layer first (free); Gemini only for complex phrasing.
    All outputs validated against the real state/category vocabularies.
    """
    body = request.get_json(force=True, silent=True) or {}
    q = (body.get("query") or "").strip()
    if len(q) > 300:
        return jsonify({"error": "Query too long (300 chars max)"}), 400
    from query_parser import parse_campaign_query
    try:
        return jsonify(parse_campaign_query(q)), 200
    except Exception as exc:  # noqa: BLE001
        logger.exception("parse_query failed")
        return jsonify({"error": f"Parse failed: {exc}"}), 500


@app.get("/api/categories")
def categories() -> Any:
    return jsonify([
        {"id": c["id"], "label": c["label"], "icon": c["icon"]} for c in CATEGORY_CATALOG
    ])


@app.get("/api/states")
def states() -> Any:
    return jsonify(sorted(US_STATES.keys()))


@app.get("/api/cities")
def cities_endpoint() -> Any:
    """Cities for a given state, to populate the city dropdown/datalist.

    GET /api/cities?state=Florida -> ["Fort Lauderdale", "Miami", ...]

    Empty list (not an error) if the state has no data — the UI treats an
    empty city as a valid state-wide search either way, so this never
    blocks a campaign; it only means fewer suggestions are offered.
    """
    state = (request.args.get("state") or "").strip()
    if not state or state not in US_STATES:
        return jsonify({"error": f"Unknown state '{state}'"}), 400
    from us_cities import CITIES_BY_STATE
    return jsonify(CITIES_BY_STATE.get(state, [])), 200


@app.post("/api/campaigns")
def create_campaign() -> Any:
    body = request.get_json(force=True, silent=True) or {}

    state = body.get("state")
    if not state or state not in US_STATES:
        return jsonify({"error": f"Invalid or missing 'state'. Choose from: {sorted(US_STATES)}"}), 400

    # Validate category ids up front so a bad selection fails fast with a
    # clear 400, rather than surfacing later as a background job error.
    category_ids = body.get("category_ids") or []
    if category_ids:
        from config import resolve_category_ids
        try:
            resolve_category_ids(category_ids)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    params = {
        "state": state,
        "city": (body.get("city") or "").strip() or None,
        "query": (body.get("query") or "").strip() or None,
        "category_ids": category_ids,
        "dry_run": bool(body.get("dry_run", True)),
        # Triage-first: dashboard runs scrape+enrich only, so the leads
        # table loads in ~1 min. Emails are generated on demand per lead
        # via /generate_email. Set generate_emails=true to opt into the
        # old bulk-generate behaviour (CLI/power use).
        "generate_emails": bool(body.get("generate_emails", False)),
        # When True, bypass the leads cache and force a fresh scrape+audit
        # (use to refresh a stale area on demand).
        "force_refresh": bool(body.get("force_refresh", False)),
    }
    job_id = _start_job(params)
    return jsonify({"job_id": job_id}), 202


@app.get("/api/campaigns/<job_id>")
def get_campaign(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    return jsonify(job)


@app.get("/api/campaigns/<job_id>/leads")
def get_campaign_leads(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    if job["status"] != "done" or not job.get("summary"):
        return jsonify({"error": "Job not finished yet", "status": job["status"]}), 409

    # Prefer the richest CSV available (with emails > enriched > raw scrape)
    csv_path = (
        job["summary"].get("email_csv")
        or job["summary"].get("enriched_csv")
        or job["summary"].get("scrape_csv")
    )
    if not csv_path or not Path(csv_path).exists():
        return jsonify({"error": "No output CSV found for this job"}), 404

    df = pd.read_csv(csv_path).fillna("")
    return jsonify(df.to_dict(orient="records"))


@app.post("/api/campaigns/<job_id>/generate_email")
def generate_campaign_email(job_id: str) -> Any:
    """On-demand: generate ONE personalised email for one lead.

    The dashboard calls this when the operator opens a lead's review
    panel, so Gemini is only hit for leads actually being worked — a
    handful — instead of all ~60 at campaign time. This is what keeps the
    free tier from 429-ing and keeps email generation off the critical
    path that loads the leads table.

    Body: a single lead object (name, city, strategy, primary_flaw,
    report_json, demo_url, …). Returns ``{subject, body}``.
    """
    # NOTE: we intentionally do NOT require the job row to exist here.
    # On Render's free tier the SQLite job store is on ephemeral disk, so
    # an instance restart between running a campaign and clicking Review
    # would wipe it — and generation only needs the lead payload the
    # dashboard sends, not the job. Requiring the job caused a 404 and a
    # blank email body in exactly that (common) situation.
    lead = request.get_json(force=True, silent=True) or {}
    if not (lead.get("name") or "").strip():
        return jsonify({"error": "Lead 'name' is required"}), 400

    from config import GEMINI_API_KEY, validate_config
    try:
        validate_config(require_gemini=True)
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 400

    try:
        from email_generator import create_client, generate_single, EmailGenerationError
        client = create_client(GEMINI_API_KEY)
        result = generate_single(client, lead)
        return jsonify(result), 200
    except EmailGenerationError as exc:
        # Clear, actionable failure instead of a silent hang.
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        logger.exception("On-demand email generation failed for job %s", job_id)
        return jsonify({"error": f"Generation failed: {exc}"}), 500


@app.post("/api/campaigns/<job_id>/send_emails")
def send_campaign_emails(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404

    # In triage-first mode there is no pre-generated email CSV — the
    # operator generated + edited each email in the dashboard. So the
    # dashboard sends the full approved lead objects (with their final,
    # edited bodies) and we build the send file from exactly those. This
    # also means what gets sent is precisely what the operator saw and
    # approved, not a re-read of some stale CSV.
    body = request.get_json(force=True, silent=True) or {}
    approved_leads = body.get("leads") or []
    rows: list[dict[str, Any]] = []
    for lead in approved_leads:
        email = (lead.get("email") or "").strip()
        email_body = (lead.get("email_body") or lead.get("emailBody") or "").strip()
        if not email or not email_body:
            continue  # skip leads missing an address or a generated body
        rows.append({
            "email": email,
            "name": (lead.get("name") or "").strip(),
            "city": (lead.get("city") or "").strip(),
            "email_body": email_body,
            "email_subject": (lead.get("email_subject") or lead.get("emailSubject") or "").strip(),
            "demo_url": (lead.get("demo_url") or lead.get("demoUrl") or "").strip(),
        })

    if not rows:
        return jsonify({"error": "No approved leads with both an email address and a generated body were provided"}), 400

    try:
        send_csv_path = str(DATA_DIR / f"{job_id}_approved_send.csv")
        pd.DataFrame(rows).to_csv(send_csv_path, index=False)
        logger.info("Job %s: prepared %d approved lead(s) for sending.", job_id, len(rows))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s: failed to prepare approved leads", job_id)
        return jsonify({"error": f"Failed to prepare approved leads: {exc}"}), 500

    # Ensure SMTP config is present before attempting to send
    from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD, SENDER_NAME, validate_config
    try:
        validate_config(require_smtp=True, require_demo_url=True)
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 400

    # Run email sending in a background thread to avoid blocking the API response
    def _send_job_emails(job_id: str, csv_path: str, gmail_addr: str, app_password: str, sender_name: str):
        from email_sender import batch_send
        try:
            sent_count = batch_send(
                input_csv=Path(csv_path),
                gmail_addr=gmail_addr,
                app_password=app_password,
                sender_name=sender_name,
            )
            update_job(job_id, emails_sent=sent_count, send_status="completed")
            logger.info("Job %s: Successfully sent %d emails.", job_id, sent_count)
        except Exception as exc:
            update_job(job_id, send_status="failed", send_error=str(exc))
            logger.exception("Job %s: Email sending failed.", job_id)

    update_job(job_id, send_status="sending", emails_sent=0)
    threading.Thread(
        target=_send_job_emails,
        args=(job_id, send_csv_path, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, SENDER_NAME),
        daemon=True
    ).start()

    return jsonify({"status": f"Sending {len(rows)} approved email(s) in background"}), 202


if __name__ == "__main__":
    import os
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    from config import GOOGLE_MAPS_API_KEY, GEMINI_API_KEY, GMAIL_ADDRESS
    port = int(os.environ.get("PORT", 5055))
    logger.info("=" * 60)
    logger.info("LeadGen API server starting on http://0.0.0.0:%s", port)
    logger.info("  GEMINI_API_KEY:        %s", "set" if GEMINI_API_KEY else "MISSING")
    logger.info("  GOOGLE_MAPS_API_KEY:   %s", "set" if GOOGLE_MAPS_API_KEY else "MISSING — will use slow OSM fallback")
    logger.info("  GMAIL_ADDRESS:         %s", GMAIL_ADDRESS or "MISSING")
    logger.info("=" * 60)
    # threaded=True so dashboard polling (GET /api/campaigns/<id>) is never
    # blocked by a slower request like on-demand /generate_email that waits
    # on Gemini. Each request is handled on its own thread.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
