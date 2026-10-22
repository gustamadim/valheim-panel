#!/bin/bash
# Host probe for the Valheim panel.
#
# The panel runs unprivileged and never touches Docker. This script is the ONLY
# privileged read it depends on, and it is read-only: docker inspect, docker logs,
# and cgroup files. It never mutates anything.
#
# Writes: /DATA/AppData/valheim-server/config/cache/hostprobe.json
#
#   {
#     "generated_at": "2026-10-22T17:05:00+07:00",
#     "container": {"id":"b576...","state":"running","started_at":"...","restart_count":0},
#     "joincode":  {"code":"703279","seen_at":"2026-10-22T09:57:19Z"},
#     "memory":    {"current":1225805824,"limit":6442450944},
#     "cpu":       {"usage_usec":123456789,"sampled_at":1792660000}
#   }
#
# Install with deploy/hostprobe.timer (every 30 s). Run it once before starting
# the panel so the file exists.

set -uo pipefail

CONTAINER="${VALHEIM_CONTAINER:-valheim-server-valheim-server-1}"
OUT="${VALHEIM_PROBE_OUT:-/DATA/AppData/valheim-server/config/cache/hostprobe.json}"
mkdir -p "$(dirname "$OUT")"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# ---- container identity + state -------------------------------------------
cid="$(docker inspect -f '{{.Id}}' "$CONTAINER" 2>/dev/null)"
state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)"
started="$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)"
restarts="$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null)"
[ -z "$state" ] && state="unknown"
[ -z "$restarts" ] && restarts=0

# ---- list-clobber env vars -------------------------------------------------
# The image rewrites adminlist.txt / bannedlist.txt / permittedlist.txt from
# these on every start. The panel owns those files now, so the UI must know when
# a stale env var would silently revert an edit. Publish only presence + value
# for these three keys -- never the whole environment.
listenv=""
for key in ADMINLIST_IDS BANNEDLIST_IDS PERMITTEDLIST_IDS; do
    val="$(docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" "$CONTAINER" 2>/dev/null \
            | sed -n "s/^${key}=//p" | head -1)"
    [ -n "$val" ] && listenv="${listenv}${key}=${val},"
done
listenv="${listenv%,}"

# ---- join code -------------------------------------------------------------
# Only the real registration line, never the blank startup one:
#   Session "FGR" registered with join code 703279
code=""
seen=""
line="$(docker logs "$CONTAINER" 2>&1 | grep -oE 'registered with join code [0-9]+' | tail -1)"
if [ -n "$line" ]; then
    code="${line##* }"
    seen="$(docker logs "$CONTAINER" --timestamps 2>&1 \
            | grep -F "registered with join code ${code}" \
            | tail -1 | awk '{print $1}')"
fi

# ---- cgroup v2 memory + cpu ------------------------------------------------
mem_cur=0
mem_max=0
cpu_usec=0
if [ -n "$cid" ]; then
    scope="/sys/fs/cgroup/system.slice/docker-${cid}.scope"
    [ -r "$scope/memory.current" ] && mem_cur="$(cat "$scope/memory.current" 2>/dev/null || echo 0)"
    [ -r "$scope/memory.max" ]     && mem_max="$(cat "$scope/memory.max" 2>/dev/null || echo 0)"
    if [ -r "$scope/cpu.stat" ]; then
        cpu_usec="$(awk '/^usage_usec/{print $2}' "$scope/cpu.stat" 2>/dev/null)"
    fi
fi
[ -z "$mem_cur" ] && mem_cur=0
[ -z "$mem_max" ] && mem_max=0
[ "$mem_max" = "max" ] && mem_max=0
[ -z "$cpu_usec" ] && cpu_usec=0

# ---- emit ------------------------------------------------------------------
{
    printf '{"generated_at":"%s",' "$(date --iso-8601=seconds)"
    printf '"container":{"id":"%s","state":"%s","started_at":"%s","restart_count":%s},' \
        "$(json_escape "$cid")" "$(json_escape "$state")" "$(json_escape "$started")" "$restarts"
    if [ -n "$code" ]; then
        printf '"joincode":{"code":"%s","seen_at":"%s"},' "$(json_escape "$code")" "$(json_escape "$seen")"
    else
        printf '"joincode":{"code":null,"seen_at":null,"error":"no join code line found"},'
    fi
    printf '"memory":{"current":%s,"limit":%s},' "$mem_cur" "$mem_max"
    printf '"cpu":{"usage_usec":%s,"sampled_at":%s},' "$cpu_usec" "$(date +%s)"
    # Presence of these keys is what the panel's clobber warning keys off.
    printf '"env":{'
    first=1
    if [ -n "$listenv" ]; then
        oldifs="$IFS"; IFS=','
        for pair in $listenv; do
            IFS="$oldifs"
            k="${pair%%=*}"; v="${pair#*=}"
            [ $first -eq 1 ] || printf ','
            printf '"%s":"%s"' "$(json_escape "$k")" "$(json_escape "$v")"
            first=0
            IFS=','
        done
        IFS="$oldifs"
    fi
    printf '}'
    printf '}\n'
} > "$tmp"

mv "$tmp" "$OUT"
chmod 644 "$OUT"
