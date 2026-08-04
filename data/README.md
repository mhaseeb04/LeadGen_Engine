# data/

This folder is populated at runtime by `scripts/pipeline.py`,
`scripts/scraper.py`, `scripts/email_sender.py`, and `scripts/api_server.py`.
Every file here except `historical_leads.csv` is safe to delete at any
time — the next pipeline run recreates whatever it needs.

## historical_leads.csv — keep this one
This is `email_sender.py`'s send-history log (`sent_at, to_email,
business_name, city, subject, status`). It's how the pipeline avoids
emailing the same lead twice. Deleting it doesn't break anything, but it
does remove that protection — any address in here will be treated as
"never contacted" on the next run.

## Everything else
`*_leads.csv`, `*_enriched.csv`, `*_with_emails.csv`, `pipeline.log`,
`lead_engagement.csv`, `inbound_contacts.csv` — all generated fresh each
time you run a campaign (via `pipeline.py`, the CLI, or the dashboard's
"New Campaign" panel). Previous versions of this folder shipped with
several stale sample/test CSVs (old `?name=`-style demo URLs, an old
`webcraft.agency` placeholder domain, fake `555`-prefixed phone numbers,
and a leftover debug `pipeline.log`) — those have been removed. Run a
real campaign to populate this folder with current, correctly-formatted
data.
