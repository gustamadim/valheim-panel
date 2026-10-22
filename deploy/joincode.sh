#!/bin/bash
# Read the current Valheim join code from the container log and publish it to a
# file the panel container can read.
#
# Why this exists: the join code is printed once per start to the game's stdout,
# and the image's log filter strips it, so supervisor's /logtail never contains
# it. The only place it survives is Docker's json-file log, which needs the
# docker CLI (docker group). This script is the *only* privileged read the panel
# relies on; the panel itself never touches docker.
#
# Writes: /DATA/AppData/valheim-server/config/cache/joincode.json
#    {"code":"703279","seen_at":"2026-10-22T16:57:19+07:00","source":"docker-logs"}
#    {"code":null,"seen_at":null,"source":"docker-logs","error":"..."} on failure
#
# Install as a systemd timer (see deploy/joincode.timer), every minute.

set -uo pipefail

CONTAINER="${VALHEIM_CONTAINER:-valheim-server-valheim-server-1}"
OUT="${VALHEIM_JOINCODE_OUT:-/DATA/AppData/valheim-server/config/cache/joincode.json}"
mkdir -p "$(dirname "$OUT")"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# Match only the real registration line, never the blank startup one:
#   Session "FGR" registered with join code 703279
line="$(docker logs "$CONTAINER" 2>&1 \
        | grep -oE 'registered with join code [0-9]+' \
        | tail -1)"

if [ -z "$line" ]; then
    printf '{"code":null,"seen_at":null,"source":"docker-logs","error":"no join code line found"}\n' > "$tmp"
else
    code="${line##* }"
    # Timestamp of that log line, so staleness is visible in the UI.
    ts="$(docker logs "$CONTAINER" --timestamps 2>&1 \
          | grep -F "registered with join code ${code}" \
          | tail -1 | awk '{print $1}')"
    [ -z "$ts" ] && ts="$(date --iso-8601=seconds)"
    printf '{"code":"%s","seen_at":"%s","source":"docker-logs"}\n' "$code" "$ts" > "$tmp"
fi

mv "$tmp" "$OUT"
chmod 644 "$OUT"
