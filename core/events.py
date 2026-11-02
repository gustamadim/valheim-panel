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

import json
import threading
import time
import traceback

from .agent import Agent, AgentError
from .db import Database, event_dedupe, extract_event_fields


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

            # The agent nests the useful fields inside `data`; the envelope only
            # carries seq/kind/at. Command events have no actor, so a name there
            # would be the verb, not a person.
            fields = extract_event_fields(ev)
            if kind == "command":
                fields["actor"] = None

            inserted = self.db.add_event(
                dedupe=event_dedupe(kind, ev),
                kind=kind,
                source_seq=ev.get("seq") if isinstance(ev.get("seq"), int) else None,
                at=_epoch(ev, now),
                day=fields["day"],
                biome=fields["biome"],
                actor=fields["actor"],
                character=fields["character"],
                position=fields["position"],
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
