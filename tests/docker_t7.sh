#!/usr/bin/env bash
# tests/docker_t7.sh — T7 (SPEC section 8/9). BOX DOCKER ONLY. Never run on mdkappsvr.
#
#   DOCKER=docker  T7_OUT=/path/to/out  tests/docker_t7.sh [watermark|legacy_unseen]
#
# Phase A1 (both modes, = T7): ingest vs a blackhole fake IMAP, restart unless-stopped,
#   IMAP socket timeout (60 s) > poll deadline (20 s), i.e. a hang the socket timeout misses.
#   PASS = Health.Status reached "unhealthy" AND RestartCount >= 1 within deadline+120 s.
# Phase A2 (informational): production ratio, socket timeout 5 s < deadline 20 s, STALL_CYCLES=2.
#   Every poll fails fast; recovery comes from the exit-71 path. Health is recorded as observed.
# Phase B (watermark only): 3 seeded messages ingested, watermark recorded,
#   `docker restart` of the ingest, watermark + ledger unchanged, "Found 0" after restart.
# Phase C: egress check from inside the ingest's network namespace (expect unreachable).
# Requires the image ffis-scrubber:hardening-dev to exist (built from this branch).
set -uo pipefail
MODE="${1:-watermark}"
DOCKER="${DOCKER:-docker}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${T7_OUT:-$HERE/_evidence}"
mkdir -p "$OUT"
LOG="$OUT/T7-docker-$MODE.txt"
PROJ="ffist7${MODE//_/}"
DEADLINE=20
WINDOW=$((DEADLINE + 120))
export T7_MODE="$MODE"
export T7_STATE_DIR="$(mktemp -d /tmp/ffis-t7-state-XXXX)"
chmod 0700 "$T7_STATE_DIR"
FILES=(-f "$HERE/t7/compose.base.yaml" -f "$REPO/deploy/mdkappsvr/compose.ingest.override.yaml" -f "$HERE/t7/compose.t7.yaml")
dc() { $DOCKER compose -p "$PROJ" "${FILES[@]}" --profile ingest "$@"; }
say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
: > "$LOG"
say "T7 mode=$MODE project=$PROJ deadline=${DEADLINE}s window=${WINDOW}s state=$T7_STATE_DIR"
say "image: $($DOCKER image inspect ffis-scrubber:hardening-dev --format '{{.Id}} created={{.Created}}')"
say "merged ingest service (docker compose config):"
dc config ingest 2>&1 | sed -n '/^  ingest:/,$p' | sed -E 's/((PASSWORD|SECRET|TOKEN|KEY)[A-Z_]*: ).*/\1<redacted>/' | tee -a "$LOG" >/dev/null
RESULT_A=FAIL; RESULT_B="N/A"; RESULT_C=FAIL

# ── Phase A ── run_a LABEL IMAP_TIMEOUT STALL_CYCLES
run_a() {
  local label=$1; export T7_IMAP_TIMEOUT=$2 T7_STALL_CYCLES=$3
  say "=== phase $label: blackhole, FFIS_IMAP_TIMEOUT_S=$2, FFIS_POLL_DEADLINE_S=$DEADLINE, FFIS_STALL_CYCLES=$3 ==="
  T7_FAKE_MODE=blackhole T7_FAKE_SEED=0 dc up -d 2>&1 | tee -a "$LOG" >/dev/null
  CID="$(dc ps -q ingest)"
  say "ingest container=$CID restart policy=$($DOCKER inspect "$CID" --format '{{.HostConfig.RestartPolicy.Name}}') network=$($DOCKER inspect "$CID" --format '{{.HostConfig.NetworkMode}}')"
  if [ "$label" = A1 ]; then
    sleep 2
    EG="$($DOCKER exec "$CID" python -c "
import socket
for h,p in (('1.1.1.1',443),('imap.gmail.com',993)):
    try:
        socket.create_connection((h,p),timeout=3); print(h,'CONNECTED')
    except Exception as e: print(h,'blocked:',type(e).__name__, e)
" 2>&1)"
    IFACES="$($DOCKER exec "$CID" python -c "import socket;print([i[1] for i in socket.if_nameindex()])" 2>&1)"
    say "phase C egress probe from ingest netns: $(echo $EG) | interfaces=$IFACES"
    if echo "$EG" | grep -q blocked && ! echo "$EG" | grep -q CONNECTED; then RESULT_C=PASS; fi
  fi
  local t0 now el st seen_unhealthy="" first_restart="" states=""
  t0=$(date +%s)
  while :; do
    now=$(date +%s); el=$((now - t0))
    st="$($DOCKER inspect "$CID" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}} {{.State.Status}} exit={{.State.ExitCode}}')"
    set -- $st
    say "t+${el}s health=$1 restarts=$2 state=$3 $4"
    case " $states " in *" $1 "*) ;; *) states="$states $1";; esac
    [ "$1" = "unhealthy" ] && [ -z "$seen_unhealthy" ] && seen_unhealthy=$el
    [ "$2" -ge 1 ] && [ -z "$first_restart" ] && first_restart=$el
    if [ -n "$seen_unhealthy" ] && [ -n "$first_restart" ]; then break; fi
    [ "$el" -ge "$WINDOW" ] && break
    sleep 2
  done
  say "phase $label: health states seen:${states}; first unhealthy t+${seen_unhealthy:-never}s; RestartCount>=1 t+${first_restart:-never}s (window ${WINDOW}s)"
  say "ingest logs ($label):"; $DOCKER logs --timestamps "$CID" 2>&1 | grep -E 'Polling|Found|STALL|ALERT|Watching|failed' | tee -a "$LOG" >/dev/null
  say "stall.json ($label): $(cat "$T7_STATE_DIR/stall.json" 2>/dev/null)"
  if [ -n "$seen_unhealthy" ] && [ -n "$first_restart" ]; then eval "RESULT_$label=PASS"; else eval "RESULT_$label=FAIL"; fi
  [ -n "$first_restart" ] && eval "RESTART_$label=t+${first_restart}s" || eval "RESTART_$label=never"
  dc down -t 2 2>&1 | tee -a "$LOG" >/dev/null
  rm -rf "$T7_STATE_DIR"/*
}
RESULT_A2=FAIL; RESTART_A2=never
run_a A1 60 4
RESULT_A=$RESULT_A1
run_a A2 5 2      # informational: production ratio (socket timeout < deadline) -> exit 71 path

# ── Phase B (watermark persistence across docker restart) ──
if [ "$MODE" = "watermark" ]; then
  RESULT_B=FAIL
  rm -rf "$T7_STATE_DIR"/*
  T7_FAKE_MODE=normal T7_FAKE_SEED=3 dc up -d 2>&1 | tee -a "$LOG"
  CID="$(dc ps -q ingest)"
  wm() { python3 - "$T7_STATE_DIR/ingest.db" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    print(c.execute("select folder,uidvalidity,last_uid from watermark").fetchall(),
          c.execute("select key,status from ledger order by uid").fetchall())
except Exception as e:
    print("ERR", e)
PY
  }
  for i in $(seq 1 30); do
    sleep 2
    W="$(wm)"; echo "$W" | grep -q "'INBOX', 1000, 3" && break
  done
  BEFORE="$(wm)"; say "before restart: $BEFORE"
  say "health before restart: $($DOCKER inspect "$CID" --format '{{.State.Health.Status}}')"
  $DOCKER restart -t 5 "$CID" >/dev/null; say "docker restart issued"
  for i in $(seq 1 30); do
    sleep 2
    n=$($DOCKER logs "$CID" 2>&1 | grep -c "Found 0 message(s) with attachments")
    [ "$n" -ge 1 ] && break
  done
  AFTER="$(wm)"; say "after restart:  $AFTER"
  say "started-at: $($DOCKER inspect "$CID" --format '{{.State.StartedAt}}') heartbeat: $(cat "$T7_STATE_DIR/heartbeat.json")"
  say "ingest logs:"; $DOCKER logs --timestamps "$CID" 2>&1 | grep -E 'Polling|Found|Watermark|Watching|resync' | tee -a "$LOG" >/dev/null
  for i in $(seq 1 10); do h=$($DOCKER inspect "$CID" --format '{{.State.Health.Status}}'); [ "$h" = healthy ] && break; sleep 2; done
  say "health after restart: $h"
  if [ "$BEFORE" = "$AFTER" ] && echo "$AFTER" | grep -q "'INBOX', 1000, 3" && [ "$n" -ge 1 ]; then RESULT_B=PASS; fi
  dc down -t 2 2>&1 | tee -a "$LOG" >/dev/null
fi
say "T7 RESULT mode=$MODE: A=A1(unhealthy+restart<=${WINDOW}s)=$RESULT_A [A2 info: unhealthy+restart=$RESULT_A2, restart=$RESTART_A2] B(watermark survives docker restart)=$RESULT_B C(no egress)=$RESULT_C"
rm -rf "$T7_STATE_DIR"
[ "$RESULT_A" = PASS ] && [ "$RESULT_C" = PASS ] && { [ "$RESULT_B" = PASS ] || [ "$RESULT_B" = "N/A" ]; }
