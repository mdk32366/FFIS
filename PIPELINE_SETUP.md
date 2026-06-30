# FFIS Automated Email Ingestion Pipeline — Setup

`ffis_email_pipeline.py` adds an email-in / email-out ingestion flow to FFIS.
Drop it in the repo root next to `config.py`, `snowflake_connector.py`, and
`flat_file_scrubber.py` (it imports from those). It has **no Streamlit
dependency**, so it runs head-less as a cron job, a Fly.io worker, or a CLI.

## What it does

For every unread email that has a CSV/TXT attachment, the pipeline:

1. Loads the attachment into a DataFrame.
2. **Classifies** the Salesforce object type (Account, Contact, Lead,
   Opportunity, User, relationships) via the Anthropic API
   (`claude-haiku-4-5-20251001`), with a deterministic header-heuristic
   fallback when `ANTHROPIC_API_KEY` is not set.
3. **Infers the shape** — rows × columns, dtypes, null counts, required-field
   coverage.
4. **Routes** each record into three layers:
   - **good** — every required field (per `config.get_required_fields()`) is
     present and non-blank, and all values pass type/format checks (email,
     date, numeric).
   - **duplicate** — repeats within the file, or matches an existing record in
     the dedup source.
   - **bad** — everything else (missing required column/value, bad value).
   Duplicates are removed first, so a row is never double-counted.
5. **Replies to the sender** with `*_good.csv`, `*_duplicate.csv`,
   `*_bad.csv` (non-empty layers only) plus a plain-text shape + counts summary.

## Configuration

Reuses the existing `config.py` cascade (`.env` → `secrets.json` → env vars).

### Required — IMAP (read) and SMTP (reply)
The pipeline logs in with `IMAP_USER` / `IMAP_PASSWORD`; if unset it falls back
to `SMTP_FROM_EMAIL` / `SMTP_APP_PASSWORD` (handy when one Gmail account both
receives and replies).

```
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USE_SSL=true
IMAP_FOLDER=INBOX
IMAP_USER=ingest@yourdomain.com
IMAP_PASSWORD=your_app_password

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM_EMAIL=ingest@yourdomain.com
SMTP_APP_PASSWORD=your_app_password
```

### Pipeline behavior
```
FFIS_PIPELINE_SUBJECT_FILTER=        # only process emails whose subject contains this
FFIS_PIPELINE_ONLY_UNSEEN=true       # only unread messages
FFIS_PIPELINE_MARK_SEEN=true         # mark processed messages as read
FFIS_PIPELINE_REPLY=true             # send the results email
FFIS_DEDUP_BACKEND=auto              # auto | csv | snowflake | none
FFIS_REFERENCE_DIR=                  # folder of existing-record CSVs (csv backend)
```

### Auto-export of the good layer (optional)
Off by default. When enabled, the good records are loaded onward (in addition
to being emailed back), and the reply summary reports the load result.
```
FFIS_EXPORT_GOOD=false               # master switch; true to enable
FFIS_EXPORT_BACKEND=auto             # auto | snowflake | api | none
FFIS_EXPORT_IF_EXISTS=append         # snowflake write mode: append | replace
FFIS_EXPORT_TABLE_PREFIX=            # e.g. stg_  -> stg_account, stg_contact
FFIS_EXPORT_TABLE_ACCOUNT=           # per-object table override (optional)
# REST/Salesforce API backend reuses the existing API_* config:
API_ENDPOINT_URL=
API_BATCH_SIZE=200
```
`auto` picks **Snowflake** if its credentials are configured, otherwise the
**REST API** backend if `API_ENDPOINT_URL` is set, otherwise it stays off.
Snowflake uses `snowflake_connector.export_to_snowflake`; the API backend
batches `POST`s the good rows as JSON (the same shape `ffis_agent.export_to_api`
uses), so a Salesforce ingestion endpoint can consume it directly. The
destination table defaults to the object name (e.g. `account`), overridable via
prefix or per-object env var. Force on/off for a single run with
`--export` / `--no-export`.

### Optional — `ANTHROPIC_API_KEY` for LLM classification (else heuristic is used).

## Duplicate checking

`FFIS_DEDUP_BACKEND=auto` picks Snowflake if its credentials are configured,
otherwise the CSV reference backend, otherwise within-file only.

**CSV backend (now):** point `FFIS_REFERENCE_DIR` at a folder of existing-record
exports named after the object, e.g. `Account_existing.csv`, `Contact.csv`.
Any file whose name contains the object name is loaded and matched on the
object's key columns.

**Snowflake backend (when integrated):** map each object to a table via env
vars like `SNOWFLAKE_TABLE_ACCOUNT=ANALYTICS.SF.ACCOUNT`,
`SNOWFLAKE_TABLE_CONTACT=...`. The `SnowflakeDedup` class is already wired to
`snowflake_connector.query_snowflake`; once your Snowflake instance is live,
set `FFIS_DEDUP_BACKEND=snowflake` (or leave `auto`) and it takes over with no
code change.

Match keys per object are defined in `_DEDUP_KEYS` (Account→Name,
Contact→Email, Lead→Email, Opportunity→Name+AccountId, User→Username, ...) and
resolve down to required fields, then the whole row, if those columns aren't
present.

## Running

```bash
# One-shot poll (good for cron / scheduled task)
python ffis_email_pipeline.py --once

# Continuous watcher
python ffis_email_pipeline.py --watch --interval 120

# Offline test — classify + route a local CSV, no email sent
python ffis_email_pipeline.py --file sample.csv

# Process but suppress the reply email (dry run)
python ffis_email_pipeline.py --once --no-send --verbose
```

### Deploying on Fly.io
Run it as a scheduled task or a second process. Example `fly.toml` process:
```
[processes]
  web = "streamlit run flat_file_scrubber.py ..."
  ingest = "python ffis_email_pipeline.py --watch --interval 120"
```
Set the same secrets you use for the app (`fly secrets set IMAP_PASSWORD=... SMTP_APP_PASSWORD=...`).

## Extending later

- **Per-field validation via Salesforce describe** — replace the rules in
  `_validate_value` / `validate_records` with live picklist/type metadata.
- **More export destinations** — add an `Exporter` subclass (implement
  `export(df, object_type) -> dict`) and register it in `make_exporter`; e.g. a
  Salesforce Bulk API exporter once record-insert auth is available.
- **Surface in the Streamlit UI** — the core functions (`classify_object_type`,
  `infer_shape`, `route_records`) are import-safe and can back a new tab.

## Tests

`test_pipeline.py` covers classification, validation reasons, routing
precedence, and the dedup factory. Run with `python test_pipeline.py`.
```
```
