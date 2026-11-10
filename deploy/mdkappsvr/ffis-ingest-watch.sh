#!/bin/sh
# ffis-ingest-watch.sh — phase-2 host check (SPEC section 5.2). NOT INSTALLED; needs Matt's GO.
# Reads docker inspect Health.Status / RestartCount and raises an alert through the
# container's own SMTP config when the ingest is unhealthy for 3 consecutive checks
# or RestartCount rose by >= 3 within an hour. Never prints secrets.
set -eu
C="${FFIS_INGEST_CONTAINER:-ffis-scrubber-ingest-1}"
S="${FFIS_WATCH_STATE:-/var/lib/ffis-ingest-watch}"
mkdir -p "$S"
now=$(date +%s)
if ! info=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}} {{.State.Running}}' "$C" 2>/dev/null); then
  echo "CRITICAL ffis-ingest-watch: container $C not found" >&2
  exit 1
fi
set -- $info
health=$1; restarts=$2; running=$3
if [ "$running" != "true" ]; then
  echo "CRITICAL ffis-ingest-watch: container $C is not running (health=$health restarts=$restarts)" >&2
  exit 1
fi
# consecutive unhealthy counter
cnt=0; [ -f "$S/unhealthy" ] && cnt=$(cat "$S/unhealthy")
if [ "$health" = "unhealthy" ]; then cnt=$((cnt+1)); else cnt=0; fi
echo "$cnt" > "$S/unhealthy"
# restart history (epoch count), keep last hour
echo "$now $restarts" >> "$S/restarts"
awk -v cut=$((now-3600)) '$1>=cut' "$S/restarts" > "$S/restarts.tmp" && mv "$S/restarts.tmp" "$S/restarts"
first=$(head -n1 "$S/restarts" | awk '{print $2}')
rose=$((restarts-first))
echo "ffis-ingest-watch: health=$health consecutive_unhealthy=$cnt restart_count=$restarts rose_1h=$rose"
if [ "$cnt" -ge 3 ] || [ "$rose" -ge 3 ]; then
  docker exec "$C" python -m ffis_ingest_health --alert || true
  echo "CRITICAL ffis-ingest-watch: alert raised (health=$health unhealthy_x=$cnt restarts_1h=$rose)" >&2
  exit 1
fi
