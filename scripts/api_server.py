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
from flask import Flask, jsonify, request
from flask_cors import CORS

from config import CATEGORY_CATALOG, DATA_DIR, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL, SENDER_NAME, US_STATES

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # dashboard/demo-site are served from different origins during local dev

# ---------------------------------------------------------------------------
# In-memory job store. Swap for a DB-backed table when moving beyond a
# single-operator local deployment (see module docstring).
# ---------------------------------------------------------------------------
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Hot-lead alert debounce. A visitor scrolling/re-rendering the demo page
# can fire several "page_view" beacons in quick succession — this keeps
# Haseeb from getting spammed with duplicate emails for the same visit.
# Keyed by (business name, city); swap for Redis if running multiple
# api_server.py processes behind a load balancer.
# ---------------------------------------------------------------------------
_LAST_ALERTED: dict[tuple[str, str], float] = {}
_ALERT_LOCK = threading.Lock()
ALERT_DEBOUNCE_SECONDS = 30 * 60  # one alert per lead per 30 minutes


def _new_job(params: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "params": params,
            "status": "queued",   # queued -> running -> done | error
            "phase": "queued",
            "message": "Waiting to start…",
            "log": [],
            "summary": None,
            "error": None,
        }
    return job_id


def _update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def _append_log(job_id: str, phase: str, message: str) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["phase"] = phase
            JOBS[job_id]["message"] = message
            JOBS[job_id]["log"].append({"phase": phase, "message": message})


def _run_job(job_id: str, params: dict[str, Any]) -> None:
    # Imported lazily so `python api_server.py --help`-style introspection
    # doesn't require Gemini/SMTP env vars to be present.
    from pipeline import run_pipeline

    _update_job(job_id, status="running")
    try:
        summary = run_pipeline(
            state=params["state"],
            category_ids=params.get("category_ids") or None,
            city=params.get("city") or None,
            query_text=params.get("query") or None,
            dry_run=params.get("dry_run", False),
            progress_cb=lambda phase, msg: _append_log(job_id, phase, msg),
        )
        _update_job(job_id, status="done", summary=summary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s failed", job_id)
        _update_job(job_id, status="error", error=str(exc))


def _start_job(params: dict[str, Any]) -> str:
    job_id = _new_job(params)
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


@app.get("/api/categories")
def categories() -> Any:
    return jsonify([
        {"id": c["id"], "label": c["label"], "icon": c["icon"]} for c in CATEGORY_CATALOG
    ])


@app.get("/api/states")
def states() -> Any:
    return jsonify(sorted(US_STATES.keys()))


@app.post("/api/campaigns")
def create_campaign() -> Any:
    body = request.get_json(force=True, silent=True) or {}

    state = body.get("state")
    if not state or state not in US_STATES:
        return jsonify({"error": f"Invalid or missing 'state'. Choose from: {sorted(US_STATES)}"}), 400

    params = {
        "state": state,
        "city": (body.get("city") or "").strip() or None,
        "query": (body.get("query") or "").strip() or None,
        "category_ids": body.get("category_ids") or [],
        "dry_run": bool(body.get("dry_run", True)),
    }
    job_id = _start_job(params)
    return jsonify({"job_id": job_id}), 202


@app.get("/api/campaigns/<job_id>")
def get_campaign(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    return jsonify(job)


@app.get("/api/campaigns/<job_id>/leads")
def get_campaign_leads(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
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


@app.post("/api/campaigns/<job_id>/send_emails")
def send_campaign_emails(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    if job["status"] != "done" or not job.get("summary"):
        return jsonify({"error": "Job not finished yet", "status": job["status"]}), 409

    email_csv_path = job["summary"].get("email_csv")
    if not email_csv_path or not Path(email_csv_path).exists():
        return jsonify({"error": "No emails generated for this job"}), 404

    # Restrict sending to exactly the leads the operator approved in
    # Triage — matched by email address. Previously this field was sent
    # by the dashboard but never read here, so "Send Approved Emails"
    # actually sent to every eligible lead in the whole campaign,
    # regardless of what was approved.
    body = request.get_json(force=True, silent=True) or {}
    approved_leads = body.get("leads") or []
    approved_emails = {
        (lead.get("email") or "").strip().lower()
        for lead in approved_leads
        if (lead.get("email") or "").strip()
    }
    if not approved_emails:
        return jsonify({"error": "No approved leads with an email address were provided"}), 400

    send_csv_path = email_csv_path
    try:
        full_df = pd.read_csv(email_csv_path)
        filtered_df = full_df[full_df["email"].fillna("").str.strip().str.lower().isin(approved_emails)]
        if filtered_df.empty:
            return jsonify({"error": "None of the approved leads were found in this campaign's results"}), 400
        # Write the approved subset to its own file rather than sending
        # the full campaign CSV — batch_send operates on whatever CSV
        # path it's given, so this is what actually enforces the filter.
        send_csv_path = str(Path(email_csv_path).with_name(f"{job_id}_approved_send.csv"))
        filtered_df.to_csv(send_csv_path, index=False)
        logger.info("Job %s: filtered to %d approved lead(s) for sending.", job_id, len(filtered_df))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s: failed to filter approved leads", job_id)
        return jsonify({"error": f"Failed to filter approved leads: {exc}"}), 500

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
            _update_job(job_id, emails_sent=sent_count, send_status="completed")
            logger.info("Job %s: Successfully sent %d emails.", job_id, sent_count)
        except Exception as exc:
            _update_job(job_id, send_status="failed", send_error=str(exc))
            logger.exception("Job %s: Email sending failed.", job_id)

    _update_job(job_id, send_status="sending", emails_sent=0)
    threading.Thread(
        target=_send_job_emails,
        args=(job_id, send_csv_path, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, SENDER_NAME),
        daemon=True
    ).start()

    return jsonify({"status": f"Sending {len(approved_emails)} approved email(s) in background"}), 202


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    from config import GOOGLE_MAPS_API_KEY, GEMINI_API_KEY, GMAIL_ADDRESS
    logger.info("=" * 60)
    logger.info("LeadGen API server starting on http://127.0.0.1:5055")
    logger.info("  GEMINI_API_KEY:        %s", "set" if GEMINI_API_KEY else "MISSING")
    logger.info("  GOOGLE_MAPS_API_KEY:   %s", "set" if GOOGLE_MAPS_API_KEY else "MISSING — will use slow OSM fallback")
    logger.info("  GMAIL_ADDRESS:         %s", GMAIL_ADDRESS or "MISSING")
    logger.info("=" * 60)
    app.run(host="127.0.0.1", port=5055, debug=False)
