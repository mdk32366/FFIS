# ffis-scrubber: mdkappsvr CUTOVER

> Repo copy of the runbook (from Trinity's CUTOVER-DRAFT.md, 2026-11-10, ffis-ingest-hardening@0.0).
> Installing it to `/opt/apps/ffis-scrubber/CUTOVER.md` is a server write and needs Matt's GO.

## What runs where
- **mdkappsvr** (`/opt/apps/ffis-scrubber/compose.yaml`, image `ffis-scrubber:fly-v32`, digest sha256:f5261804…):
  - `app`: Streamlit UI on 127.0.0.1:8501, healthcheck `/_stcore/health`.
  - `ingest` (profile `ingest`): `python ffis_email_pipeline.py --watch --interval 120`. It is the **only** ingest **LIVE since 2026-11-09**.
- **Fly app `ffis-scrubber`:**
  - The `ingest` process group is scaled to **0**.
  - The `app` group is still running (min_machines_running=1), with the same mailbox credentials.
  - To be retired at Fly teardown.
- **Source:** github.com/mdk32366/FFIS `main`. Since FFIS#1 (5a7d2411) `.github/workflows/fly-deploy.yml` is `workflow_dispatch` only, so a push to main no longer deploys to Fly; a manual dispatch still would.

## Invariants
1. **Never run two ingests.** Both would poll the same Gmail INBOX and double-reply. Before any `docker compose --profile ingest up`, confirm the Fly ingest count is 0.
2. Do not use the UI "fetch from email" tab in prod. Its RFC822 fetch marks mail read (flat_file_scrubber.py:483).
3. Secrets stay in `.env` (mode 0600). Never print them.

## Health and monitoring
- **History:**
  - At cutover the ingest healthcheck was set `disable: true`, with no reason recorded. The inferred reason: the inherited image HEALTHCHECK probes :8501, which the ingest doesn't serve.
  - Result: from **11/9 21:45:41Z to 11/10 15:05Z (about 17 h)** the ingest hung silently on an IMAP socket with no timeout.
  - Restarted with Matt's GO at 07:05 PT 11/10.
- **After the ffis-ingest-hardening@0.0 deploy:**
  - Heartbeat at `/opt/apps/ffis-scrubber/state/heartbeat.json`.
  - Health is `python ffis_ingest_health.py`.
  - The watchdog exits 70 (poll deadline) or 71 (no successful poll for 4 cycles), and `restart: unless-stopped` recovers it.
  - Alerts go to `FFIS_ALERT_TO` (TBD by Matt).
- **Check:**
  - `docker inspect ffis-scrubber-ingest-1 --format '{{.State.Health.Status}} {{.RestartCount}}'`
  - `docker logs --since 10m ffis-scrubber-ingest-1 | grep -E 'Polling|Found|STALL'`

## State
- `/opt/apps/ffis-scrubber/state/ingest.db` holds the watermark (UIDVALIDITY plus last UID) and the Message-ID ledger. It is inside the LUKS volume.
- **Do not delete it.** Deleting it forces a resync from UNSEEN, or from SINCE minus 2 days.
- Backup: TBD (appdb-backup extension, Matt GO).

## Restart / stuck procedure (each needs Matt's GO in prod)
1. `docker logs --timestamps --since 30m ffis-scrubber-ingest-1`: note the last Polling/Found pair.
2. `docker restart ffis-scrubber-ingest-1`. This touches only the ingest; leave `app` alone.
3. Verify a "Found N" line within 2 min.
4. If the hang lasted more than a few cycles, run the LOSS-VERDICT check: an EXAMINE-only header metadata read, with Matt's GO.

## Rollback
- **Code:** set `FFIS_INGEST_MODE=legacy_unseen` in compose `environment`, then `docker compose --profile ingest up -d ingest`.
- **Image:** pin back to `ffis-scrubber:fly-v32` and restore the compose.yaml backup made at deploy time.
- **Never** roll back by scaling the Fly ingest up while the server ingest is running.

## Fly teardown items (ffis)
- Confirm the ingest group is 0 and stop the `app` group.
- Export the Fly secret **names** (values are already in the server .env).
- No Fly volume or DB exists for ffis (ffis has no DB). UNVERIFIED: run `fly volumes list -a ffis-scrubber` (read-only) from MDKDevLaptop with Matt's GO.
- Disable or retarget the `fly-deploy.yml` workflow (already dispatch-only since FFIS#1).

## Hardening reference (ffis-ingest-hardening@0.0)
Applied via `deploy/mdkappsvr/compose.ingest.override.yaml` (repo only until Matt GOs the server deploy).

| Env knob | Default | Meaning |
|---|---|---|
| `FFIS_INGEST_MODE` | `watermark` | `watermark` = UID watermark + Message-ID ledger; `legacy_unseen` = old UNSEEN/\Seen path plus timeouts/watchdog/health (rollback) |
| `FFIS_STATE_DIR` | `/state` | heartbeat.json, stall.json, ingest.db. Not writable -> in-memory fallback with a WARNING |
| `FFIS_IMAP_TIMEOUT_S` | 30 | socket timeout for every IMAP connect/recv/send |
| `FFIS_POLL_DEADLINE_S` | 300 | wall-clock limit for one poll; watchdog exits 70 past it |
| `FFIS_SMTP_TIMEOUT_S` | 30 | reply SMTP socket timeout |
| `FFIS_STALL_CYCLES` | 4 | exit 71 when no successful IMAP leg for cycles x (interval + deadline) |
| `FFIS_MAX_ATTEMPTS` | 3 | processing failures before a message is `poison` (alert, then advance) |
| `FFIS_IMAP_STARTTLS` | true | only used when `IMAP_USE_SSL=false` |
| `FFIS_WATCH_INTERVAL_S` | 120 | health-check view of the poll interval (keep equal to `--interval`) |
| `FFIS_HEALTH_MAX_AGE_S` | 2 x interval + deadline (540) | health: max age of `poll_finished_at` |
| `FFIS_ALERT_TO` | unset | alert recipient (Matt decision). Unset = CRITICAL log + stall.json only |

- **Exit codes:** 70 = poll exceeded deadline; 71 = no successful IMAP leg for the stall window. Docker `restart: unless-stopped` restarts on either. Docker does **not** restart a merely *unhealthy* container.
- **Health:** `python ffis_ingest_health.py` -> exit 0/1 plus one JSON line (ages only). Unhealthy if the heartbeat is missing/stale, a poll is in flight past the deadline, or `consecutive_failures` >= 3 (resets on a good poll). Compose `start_period` is 420 s (>= deadline 300 + interval 120); a hang from container start therefore recovers by exit 70 before health can turn unhealthy.
- **Auth alert:** an IMAP LOGIN rejection sends one alert on the first failure, then at most one per hour (`last_auth_alert_at` in stall.json).
- **Replies:** idempotent per (Message-ID, attachment index) via the `replies` table in ingest.db; a retried half-done message re-sends nothing already sent.
- **\Seen:** in `watermark` mode `\Seen` is set only after the ledger commit and is no longer the source of truth; mail read by a human or the UI is still ingested. Poison messages are left unread.
- **Reply crash window:** at most one duplicate reply is possible if the process dies between `sendmail` and the reply-ledger write (accepted; exactly-once over SMTP is not achievable). A failed reply is logged and the message is still `done` with `reply_sent=0`.
- **First start / UIDVALIDITY change:** resync via `UID SEARCH UNSEEN` (first ever) or `SINCE <last done - 2 days>`; the ledger dedups by Message-ID; the watermark is rebased to UIDNEXT-1 only after the whole resync batch commits.
