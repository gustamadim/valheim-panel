"""Background poller for the agent's event ring.

The agent keeps a 500-event ring of joins, leaves, commands and durable deaths,
and returns `next` as the last sequence it handed out. Deaths are journaled on
disk by the plugin and survive a server restart; the ring does not.

Sequence numbers are **not** stable across a game restart, so `since` alone is
unsafe as an identity: after a restart the counter can rewind and replay events
we already stored. Each event therefore also gets a content hash, and the insert
is `INSERT OR IGNORE` on that hash. Replayed events are dropped silently instead
of duplicating the history.

The poller runs as a daemon thread and is deliberately failure-tolerant: a
stopped game server or an unreachable agent must not kill the panel.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback

from .agent import Agent, AgentError
from .db import Database


def _dedupe(kind: str, ev: dict) -> str:
    """Content hash for an event.

    Deliberately excludes the sequence number so a replay after a restart maps to
    the same row, and includes the timestamp so two genuinely separate events
    with the same actor are both kept.
    """
    parts = [
        kind,
        str(ev.get("at") or ev.get("time") or ev.get("timestamp") or ""),
        str(ev.get("player") or ev.get("name") or ev.get("character") or ""),
        str(ev.get("character") or ""),
        str(ev.get("biome") or ""),
        str(ev.get("type") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _position(ev: dict) -> str | None:
    pos = ev.get("position") or ev.get("pos")
    if isinstance(pos, dict):
        try:
            return f"{pos.get('x')},{pos.get('y')},{pos.get('z')}"
        except Exception:  # noqa: BLE001
            return None
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        return f"{pos[0]},{pos[1]},{pos[2]}"
    return None


def _epoch(ev: dict, fallback: float) -> float:
    """Event time as epoch seconds. Accepts seconds, millis, or ISO strings."""
    raw = ev.get("at") or ev.get("time") or ev.get("timestamp")
    if raw is None:
        return fallback
    if isinstance(raw, (int, float)):
        value = float(raw)
        # Heuristic: anything past year 2286 in seconds is really milliseconds.
        return value / 1000.0 if value > 1e10 else value
    if isinstance(raw, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return fallback
    return fallback


class EventPoller:
    def __init__(self, agent: Agent, db: Database, interval: float = 2.0) -> None:
        self.agent = agent
        self.db = db
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._since = 0
        self._last_error: str | None = None
        self._last_ok: float | None = None
        self._stored = 0
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="event-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - a poller must never die
                self._last_error = traceback.format_exc(limit=2)
            self._stop.wait(self.interval)

    # ---- one pass --------------------------------------------------------
    def poll_once(self) -> int:
        """Fetch and store new events. Returns how many rows were inserted."""
        try:
            data = self.agent.events(self._since)
        except AgentError as exc:
            self._last_error = str(exc)
            return 0

        events = data.get("events") or []
        nxt = data.get("next")
        stored = 0
        now = time.time()

        for ev in events:
            kind = (ev.get("type") or ev.get("kind") or "").strip()
            if not kind:
                continue
            # The ring uses dotted names; normalise the ones we care about.
            kind = {
                "join": "player.join", "leave": "player.leave",
                "death": "player.death", "command": "command",
            }.get(kind, kind)

            actor = ev.get("player") or ev.get("name") or ev.get("actor")
            character = ev.get("character") or ev.get("character_id")
            inserted = self.db.add_event(
                dedupe=_dedupe(kind, ev),
                kind=kind,
                source_seq=ev.get("seq") if isinstance(ev.get("seq"), int) else None,
                at=_epoch(ev, now),
                day=ev.get("day") if isinstance(ev.get("day"), int) else None,
                biome=ev.get("biome"),
                actor=str(actor) if actor else None,
                character=str(character) if character else None,
                position=_position(ev),
                detail=json.dumps(ev, default=str)[:2000],
            )
            if inserted:
                stored += 1

        with self._lock:
            self._stored += stored
            self._last_ok = time.time()
            self._last_error = None
            # Only advance the cursor once the batch is committed, so a crash
            # between fetch and insert replays rather than loses events.
            if isinstance(nxt, int) and nxt >= self._since:
                self._since = nxt

        return stored

    # ---- introspection ---------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "since": self._since,
                "last_ok": self._last_ok,
                "last_error": self._last_error,
                "stored_total": self._stored,
            }
