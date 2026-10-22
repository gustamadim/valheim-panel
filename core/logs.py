"""Parsing of the Valheim server log.

Two sources, deliberately separate:

* Supervisor `/logtail` (or readProcessLog) gives the game's stdout after the
  image's log filter. It carries saves, join/leave counters, PlayFab token
  refreshes, plugin load lines -- but NOT the join code, which the filter strips.
* The host probe writes the join code and container state to hostprobe.json,
  because only `docker logs` still contains it.

Nothing here guesses. When a fact is unavailable it returns None and the UI says
so, rather than inventing a value.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# "Player joined server "FGR" that has join code 703279, now 2 player(s)"
_RE_PLAYER_JOIN = re.compile(
    r'Player joined server "(?P<server>[^"]*)" that has join code (?P<code>\d+), now (?P<count>\d+) player')
_RE_PLAYER_LEAVE = re.compile(
    r'Player connection lost server "(?P<server>[^"]*)" that has join code (?P<code>\d+), now (?P<count>\d+) player')
_RE_SESSION_NEW = re.compile(
    r'New session server "(?P<server>[^"]*)" that has join code (?P<code>[0-9]*), now (?P<count>\d+) player')
_RE_SESSION_REG = re.compile(
    r'Session "(?P<server>[^"]*)" registered with join code (?P<code>\d+)')
_RE_ZDOID = re.compile(r'Got character ZDOID from (?P<name>.+?) :')
_RE_HISTORY = re.compile(
    r'Player history entry with index \d+:\s+(?P<name>.+?) \((?P<platform>Steam_\d+|PlayFab_[0-9A-Fa-f]+), (?P<hash>[0-9A-Fa-f]+)\)')
_RE_SAVE = re.compile(r'World save \((\d)/5\)')
_RE_SAVE_DONE = re.compile(r'World save \(5/5\) done\. Total time \[(\d+)ms\]')
_RE_LISTENING = re.compile(r'Server is now listening on UDP query port (\d+)')
_RE_LOBBY = re.compile(r'Lobby (?P<id>[0-9a-f-]+)\.r-\d+ for world')
_RE_MOD_LOAD = re.compile(r'Loading \[(?P<name>[^\]]+)\] \((?P<guid>[^)]+)\)')
_RE_MOD_FAIL = re.compile(r'\[Error\s*:\s*(?P<mod>[^\]]+)\]\s*(?P<msg>.+)')
_RE_PLUGIN_COUNT = re.compile(r'(\d+) plugins to load')
_RE_CHAINLOAD = re.compile(r'Chainloader startup complete \((\d+) loaded, (\d+) skipped, (\d+) failed\)')
_RE_CHAINLOAD_START = re.compile(r'Chainloader started')
_RE_CONNECTIONS = re.compile(r'Connections (\d+) ZDOS:(\d+)')

# Lines worth surfacing as structured events in the UI.
EVENT_KINDS = ("join", "leave", "save", "error", "restart", "mod", "listening")


@dataclass
class LogEvent:
    kind: str
    text: str
    count: int | None = None
    name: str | None = None
    ts: str | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text,
                "count": self.count, "name": self.name, "ts": self.ts}


@dataclass
class LogState:
    """Accumulated facts derived from a log stream."""
    server_name: str | None = None
    join_code: str | None = None
    online_count: int = 0
    players_online: list[str] = field(default_factory=list)
    roster: dict[str, str] = field(default_factory=dict)   # platform id -> name
    last_save_ts: str | None = None
    last_save_ms: int | None = None
    listening_port: int | None = None
    mods_loaded: list[str] = field(default_factory=list)
    mods_failed: list[dict] = field(default_factory=list)
    plugin_count: int | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "server_name": self.server_name,
            "join_code": self.join_code,
            "online_count": self.online_count,
            "players_online": self.players_online,
            "roster": self.roster,
            "last_save_ts": self.last_save_ts,
            "last_save_ms": self.last_save_ms,
            "listening_port": self.listening_port,
            "mods_loaded": self.mods_loaded,
            "mods_failed": self.mods_failed,
            "plugin_count": self.plugin_count,
            "errors": self.errors[-20:],
        }


def parse_line(line: str, state: LogState, tz: str = "Asia/Jakarta") -> LogEvent | None:
    """Fold one log line into `state`; return an event if it is noteworthy."""
    if not line:
        return None

    m = _RE_PLAYER_JOIN.search(line)
    if m:
        state.online_count = int(m.group("count"))
        state.server_name = m.group("server")
        return LogEvent("join", line, count=state.online_count)

    m = _RE_PLAYER_LEAVE.search(line)
    if m:
        state.online_count = int(m.group("count"))
        state.server_name = m.group("server")
        return LogEvent("leave", line, count=state.online_count)

    m = _RE_SESSION_REG.search(line)
    if m:
        state.join_code = m.group("code")
        state.server_name = m.group("server")
        return LogEvent("restart", line)

    m = _RE_SESSION_NEW.search(line)
    if m:
        state.online_count = int(m.group("count"))
        state.server_name = m.group("server")
        return LogEvent("restart", line, count=state.online_count)

    m = _RE_HISTORY.search(line)
    if m:
        state.roster[m.group("platform")] = m.group("name")
        return None

    m = _RE_ZDOID.search(line)
    if m:
        name = m.group("name").strip()
        if name not in state.players_online:
            state.players_online.append(name)
        return None

    if _RE_SAVE_DONE.search(line):
        state.last_save_ms = int(_RE_SAVE_DONE.search(line).group(1))
        state.last_save_ts = _extract_ts(line, tz)
        return LogEvent("save", line)

    m = _RE_LISTENING.search(line)
    if m:
        state.listening_port = int(m.group(1))
        return LogEvent("listening", line)

    m = _RE_CHAINLOAD.search(line)
    if m:
        loaded, skipped, failed = (int(x) for x in m.groups())
        state.plugin_count = loaded
        if failed:
            state.errors.append(f"{failed} plugin(s) failed to load")
        return LogEvent("mod", line)

    # A fresh chainload means a fresh plugin set: drop whatever the previous
    # start reported, otherwise stale load failures accumulate forever.
    if _RE_CHAINLOAD_START.search(line):
        state.mods_loaded.clear()
        state.mods_failed.clear()
        state.plugin_count = None
        return None

    m = _RE_PLUGIN_COUNT.search(line)
    if m and state.plugin_count is None:
        state.plugin_count = int(m.group(1))
        return None

    m = _RE_MOD_LOAD.search(line)
    if m:
        name = m.group("name")
        if name not in state.mods_loaded:
            state.mods_loaded.append(name)
        return LogEvent("mod", line, name=name)

    m = _RE_MOD_FAIL.search(line)
    if m:
        state.mods_failed.append({"mod": m.group("mod").strip(), "msg": m.group("msg").strip()})
        return LogEvent("error", line, name=m.group("mod").strip())

    if "Exception" in line or "Error" in line or "FATAL" in line:
        state.errors.append(line)
        return LogEvent("error", line)

    return None


def _extract_ts(line: str, tz: str = "Asia/Jakarta") -> str | None:
    """Pull the game's own MM/DD/YYYY HH:MM:SS timestamp out of a log line.

    The game writes these in the container's local time (TZ=Asia/Jakarta), not
    UTC. Tagging them as UTC puts every save ~7 hours in the future and makes
    the age render as negative, so the timezone must be applied here.
    """
    m = re.search(r'(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})', line)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return None
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    return dt.replace(tzinfo=zone).astimezone(timezone.utc).isoformat()


def scan(lines: Iterable[str], state: LogState | None = None,
         tz: str = "Asia/Jakarta") -> tuple[LogState, list[LogEvent]]:
    st = state or LogState()
    events: list[LogEvent] = []
    for line in lines:
        ev = parse_line(line.rstrip("\n"), st, tz)
        if ev is not None:
            events.append(ev)
    return st, events


def read_probe(path: Path) -> dict:
    """Read the host probe snapshot. Never raises; returns {} when absent."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def probe_age_seconds(probe: dict) -> float | None:
    """How stale the probe is, in seconds."""
    stamp = probe.get("generated_at")
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None
