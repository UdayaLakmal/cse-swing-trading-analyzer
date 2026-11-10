#!/usr/bin/env bash
# Daily run on the host: wait for Docker and the network, fetch and analyse
# in the job container, make sure the web report is up, then show a desktop
# notification whose "Open report" button opens http://localhost:8080.
#
# Run by the cse-daily systemd user timer (scripts/install-timer.sh), or by
# hand. Options:
#   --force      rerun even if today already succeeded
#   --no-notify  print the summary instead of notifying
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="http://localhost:8080"
FORCE=()
NOTIFY=1
for a in "$@"; do
  case "$a" in
    --force) FORCE=(--force) ;;
    --no-notify) NOTIFY=0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

cd "$REPO" || exit 1
export UID
GID="$(id -g)"
export GID
log() { echo "$(date '+%F %T') $*"; }

notify() {  # urgency title body
  if [ "$NOTIFY" = 0 ] || ! command -v notify-send >/dev/null; then
    printf '%s\n%s\n' "$2" "$3"
    return
  fi
  # notify-send --wait blocks until the notification is clicked or closed, so
  # run it in its own transient unit: this service can finish meanwhile.
  local cmd
  cmd=$(cat <<EOF
a=\$(timeout 12h notify-send --app-name="CSE Analyzer" --icon=office-chart-line --urgency="\$0" \
  --action=default="Open report" --action=open="Open report" --wait -- "\$1" "\$2")
case "\$a" in default|open) xdg-open "$URL" ;; esac
EOF
)
  if command -v systemd-run >/dev/null && systemd-run --user --quiet --collect -- sh -c "$cmd" "$1" "$2" "$3" 2>/dev/null; then
    return
  fi
  sh -c "$cmd" "$1" "$2" "$3" &
  disown
}

fail() {
  log "FAILED: $1"
  notify critical "CSE daily run failed" "$1
Details: journalctl --user -u cse-daily"
  exit 1
}

wait_for() {  # description seconds command...
  local what="$1" limit="$2"
  shift 2
  local waited=0
  until "$@" >/dev/null 2>&1; do
    [ "$waited" -ge "$limit" ] && return 1
    [ "$waited" = 0 ] && log "waiting for $what"
    sleep 15
    waited=$((waited + 15))
  done
}

# After a resume or boot the network and Docker can take a moment.
wait_for "Docker" 300 docker info || fail "Docker is not running."
wait_for "cse.lk" 600 curl -sS -o /dev/null -m 15 https://www.cse.lk/ || fail "Could not reach www.cse.lk for 10 minutes."

docker compose up -d web >/dev/null 2>&1 || log "warning: could not start the web container"

log "running daily collection"
out=$(docker compose --profile job run --rm -T job daily "${FORCE[@]}" 2>&1)
rc=$?
echo "$out"
[ $rc -eq 0 ] || fail "Collection exited with code $rc. $(echo "$out" | grep -m1 -iE 'error|failed' || true)"
if echo "$out" | grep -q "already ran for"; then
  log "already ran today, no notification"
  exit 0
fi

summary=$(docker compose --profile job run --rm -T job summary 2>/dev/null | tail -n 1)
field() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$summary" "$1" 2>/dev/null; }
title=$(field title) || fail "Could not read the summary: $summary"
body=$(field body)
urgency=$(field urgency)
if echo "$out" | grep -q '"status": "partial"'; then
  body="$body
Some stocks failed to download; see the run log."
fi
log "$title"
notify "${urgency:-normal}" "$title" "$body"
