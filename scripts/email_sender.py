"""
email_sender.py — Gmail SMTP email sender with safety limits.

Sends personalised HTML cold emails to scraped leads, with random delays,
daily send-limit tracking, duplicate prevention via historical_leads.csv,
and full logging.
"""

import argparse
import csv
import logging
import random
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import pandas as pd

from config import (
    DATA_DIR,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    SENDER_NAME,
    SMTP_HOST,
    SMTP_PORT,
    safe_str,
    validate_config,
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
DAILY_SEND_LIMIT: int = 450           # stay safely under Gmail's 500/day cap
MIN_DELAY: float = 60.0               # minimum seconds between sends
MAX_DELAY: float = 120.0              # maximum seconds between sends
HISTORY_FILE: Path = DATA_DIR / "historical_leads.csv"
HISTORY_COLUMNS: list[str] = [
    "sent_at", "to_email", "business_name", "city", "subject", "status",
]


# ---------------------------------------------------------------------------
# SMTP connection
# ---------------------------------------------------------------------------
def create_smtp_connection(
    email: str,
    app_password: str,
) -> smtplib.SMTP:
    """Create and return an authenticated SMTP connection to Gmail.

    Args:
        email: Gmail address.
        app_password: Gmail App Password (not the regular password).

    Returns:
        An authenticated :class:`smtplib.SMTP` instance.
    """
    logger.info("Connecting to %s:%d …", SMTP_HOST, SMTP_PORT)
    conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    conn.ehlo()
    conn.starttls()
    conn.ehlo()
    conn.login(email, app_password)
    logger.info("SMTP authenticated as %s.", email)
    return conn


# ---------------------------------------------------------------------------
# Single email sender
# ---------------------------------------------------------------------------
def send_email(
    connection: smtplib.SMTP,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    sender_name: str,
    demo_url: str = "",
) -> None:
    """Send a single HTML email.

    The email body is wrapped in basic HTML with:
    - The generated copy
    - A styled CTA button linking to the demo URL
    - An unsubscribe footer

    Args:
        connection: Authenticated SMTP connection.
        from_addr: Sender email address.
        to_addr: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body (will be wrapped in HTML).
        sender_name: Display name for the From header.
        demo_url: Link for the CTA button.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{sender_name} <{from_addr}>"
    msg["To"] = to_addr
    msg["Subject"] = subject

    # ---- Build HTML body ----
    html_body = body.replace("\n", "<br>")

    button_html = ""
    if demo_url:
        button_html = f"""
        <div style="margin:24px 0;">
            <a href="{demo_url}"
               style="background-color:#2563eb;color:#ffffff;padding:12px 28px;
                      text-decoration:none;border-radius:6px;font-weight:600;
                      font-size:15px;display:inline-block;">
                View Your Free Demo Site &rarr;
            </a>
        </div>
        """

    html = f"""\
    <html>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
                 Roboto,Helvetica,Arial,sans-serif;font-size:15px;
                 line-height:1.6;color:#1f2937;max-width:560px;">
        <div>{html_body}</div>
        {button_html}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:32px 0 16px;">
        <p style="font-size:11px;color:#9ca3af;">
            You're receiving this because we thought {sender_name} could help
            your business. If you'd rather not hear from us, simply reply with
            "unsubscribe" and we'll remove you immediately.
        </p>
    </body>
    </html>
    """

    # Attach plain-text fallback and HTML version
    msg.attach(MIMEText(body + "\n\n---\nReply 'unsubscribe' to opt out.", "plain"))
    msg.attach(MIMEText(html, "html"))

    connection.sendmail(from_addr, to_addr, msg.as_string())
    logger.info("Email sent → %s  (subject: %s)", to_addr, subject)


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------
def _load_history() -> set[str]:
    """Return a set of email addresses already contacted."""
    if not HISTORY_FILE.exists():
        return set()
    try:
        df = pd.read_csv(HISTORY_FILE)
        return set(df["to_email"].dropna().str.strip().str.lower())
    except Exception as exc:
        logger.warning("Could not read history file: %s", exc)
        return set()


def _append_history(record: dict[str, str]) -> None:
    """Append a single send record to historical_leads.csv."""
    file_exists = HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)


def _count_today_sends() -> int:
    """Count how many emails were sent today (UTC)."""
    if not HISTORY_FILE.exists():
        return 0
    try:
        df = pd.read_csv(HISTORY_FILE)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return int(df["sent_at"].fillna("").str.startswith(today).sum())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Batch sender
# ---------------------------------------------------------------------------
def batch_send(
    input_csv: Path,
    gmail_addr: str | None = None,
    app_password: str | None = None,
    sender_name: str | None = None,
    subject_template: str = "Quick question for {name}",
) -> int:
    """Send emails to all leads in a CSV, with safety limits and logging.

    Args:
        input_csv: CSV with columns ``email``, ``name``, ``city``,
            ``email_body``, ``demo_url``.
        gmail_addr: Gmail address (overrides .env).
        app_password: Gmail App Password (overrides .env).
        sender_name: Display name for From header.
        subject_template: Subject line template. ``{name}`` is replaced
            with the business name.

    Returns:
        Number of emails successfully sent in this run.
    """
    gmail_addr = gmail_addr or GMAIL_ADDRESS
    app_password = app_password or GMAIL_APP_PASSWORD
    sender_name = sender_name or SENDER_NAME
    validate_config(require_smtp=True, require_demo_url=True)

    df = pd.read_csv(input_csv)
    logger.info("Loaded %d leads from %s.", len(df), input_csv)

    # Filter to rows that have both an email address and a generated body
    mask = df["email"].fillna("").str.strip().ne("") & df["email_body"].fillna("").str.strip().ne("")
    eligible = df.loc[mask].copy()
    logger.info("%d leads have email addresses and generated bodies.", len(eligible))

    if eligible.empty:
        logger.warning("No eligible leads to email. Exiting.")
        return 0

    # Load history to skip duplicates
    contacted = _load_history()
    today_count = _count_today_sends()
    logger.info(
        "Already contacted %d unique addresses. %d sent today.",
        len(contacted),
        today_count,
    )

    conn: smtplib.SMTP | None = None
    sent = 0

    try:
        conn = create_smtp_connection(gmail_addr, app_password)

        for _, row in eligible.iterrows():
            to_addr = str(row["email"]).strip().lower()

            # Skip duplicates
            if to_addr in contacted:
                logger.info("Skipping %s — already contacted.", to_addr)
                continue

            # Daily limit check
            if today_count + sent >= DAILY_SEND_LIMIT:
                logger.warning(
                    "Daily send limit (%d) reached. Stopping.", DAILY_SEND_LIMIT
                )
                break

            biz_name = safe_str(row.get("name", "there"), default="there")
            city = safe_str(row.get("city", ""))
            body = str(row["email_body"])
            demo_url = safe_str(row.get("demo_url", ""))
            # Prefer a subject already stored on the row (set by
            # email_generator.py, and editable by the operator during
            # triage) so dashboard edits actually take effect. Only fall
            # back to the template for older CSVs that predate this
            # column, or rows where it's genuinely unset — note pd.notna()
            # is required here, not a plain truthiness check, because a
            # NaN cell is truthy in Python and would otherwise stringify
            # to the literal text "nan" and get sent as the subject.
            stored_subject = safe_str(row.get("email_subject", ""))
            subject = stored_subject or subject_template.replace("{name}", biz_name)

            try:
                send_email(
                    connection=conn,
                    from_addr=gmail_addr,
                    to_addr=to_addr,
                    subject=subject,
                    body=body,
                    sender_name=sender_name,
                    demo_url=demo_url,
                )
                sent += 1
                contacted.add(to_addr)

                _append_history(
                    {
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "to_email": to_addr,
                        "business_name": biz_name,
                        "city": city,
                        "subject": subject,
                        "status": "sent",
                    }
                )

            except smtplib.SMTPException as exc:
                logger.error("SMTP error sending to %s: %s", to_addr, exc)
                _append_history(
                    {
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "to_email": to_addr,
                        "business_name": biz_name,
                        "city": city,
                        "subject": subject,
                        "status": f"error: {exc}",
                    }
                )
                # Reconnect on SMTP failure
                try:
                    conn.quit()
                except Exception:
                    pass
                conn = create_smtp_connection(gmail_addr, app_password)

            # Random delay between sends
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            logger.info("Waiting %.0f s before next send …", delay)
            time.sleep(delay)

    finally:
        if conn:
            try:
                conn.quit()
                logger.info("SMTP connection closed.")
            except Exception:
                pass

    logger.info("Batch send complete — %d emails sent this run.", sent)
    return sent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send generated cold emails to leads via Gmail SMTP.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="CSV with leads and generated email bodies.",
    )
    parser.add_argument(
        "--gmail",
        default=None,
        help="Gmail address (overrides .env).",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Gmail App Password (overrides .env).",
    )
    parser.add_argument(
        "--sender-name",
        default=None,
        help="From display name (overrides .env).",
    )
    parser.add_argument(
        "--subject",
        default="Quick question for {name}",
        help="Subject line template. Use {name} as placeholder.",
    )
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    try:
        total = batch_send(
            input_csv=args.input,
            gmail_addr=args.gmail,
            app_password=args.password,
            sender_name=args.sender_name,
            subject_template=args.subject,
        )
        logger.info("Done. %d emails sent.", total)
    except Exception:
        logger.exception("Email sending failed.")
        sys.exit(1)
