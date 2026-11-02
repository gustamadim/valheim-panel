"""SQLite persistence: users, audit log, and a small key/value store.

Lives on the /config mount so it survives container rebuilds. Stdlib sqlite3 only.

Concurrency: the panel is a ThreadingHTTPServer, so every connection is created
per-call with a short timeout and WAL enabled. Writers are serialised by SQLite
itself; we keep transactions small rather than holding a global lock.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK (role IN ('admin','mod','user')),
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    created_by    TEXT,
    updated_at    REAL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    last_login_at REAL,
    last_login_ip TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    role    TEXT NOT NULL,
    ip      TEXT,
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT,
    ok      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS audit_actor ON audit(actor);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Game events relayed by the agent: joins, leaves, commands and durable deaths.
-- `dedupe` is a content hash so a sequence-number reset after a game restart
-- cannot duplicate history.
CREATE TABLE IF NOT EXISTS game_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe     TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    source_seq INTEGER,
    at         REAL NOT NULL,
    day        INTEGER,
    biome      TEXT,
    actor      TEXT,
    character  TEXT,
    position   TEXT,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS game_events_kind ON game_events(kind, at DESC);
CREATE INDEX IF NOT EXISTS game_events_at ON game_events(at DESC);
"""


def extract_event_fields(ev: dict) -> dict:
    """Pull the fields the panel stores out of an agent event payload.

    The agent's event ring nests everything meaningful inside `data`; the envelope
    carries only `seq`, `kind` and `at`. Read both so a flat payload would still
    work, but prefer the nested form, which is what the agent actually sends:

        {"seq": ..., "kind": "player.death", "at": "...",
         "data": {"character_id", "player_name", "day", "biome", "x", "z", ...}}

    `command` events carry the verb in `data.name`, which is not a person -- the
    caller is expected to drop the actor for that kind.
    """
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}

    def pick(*keys):
        for src in (data, ev):
            for k in keys:
                v = src.get(k)
                if v is not None and v != "":
                    return v
        return None

    actor = pick("player_name", "name", "player", "actor")
    character = pick("character_id", "character")
    day = pick("day")
    biome = pick("biome")
    x, y, z = pick("x"), pick("y"), pick("z")

    if x is not None and z is not None:
        position = f"{x},{y if y is not None else 0},{z}"
    else:
        position = None

    return {
        "actor": str(actor) if actor is not None else None,
        "character": str(character) if character is not None else None,
        "day": day if isinstance(day, int) else None,
        "biome": str(biome) if biome is not None else None,
        "position": position,
    }


def event_dedupe(kind: str, ev: dict) -> str:
    """Content hash for an event row, shared by the poller and the backfill.

    Deliberately excludes the sequence number so a replay after a restart maps
    to the same row, and includes the timestamp so two genuinely separate events
    with the same actor are both kept. Names come from the extracted fields, so
    a join and a leave by the same player in the same second still differ.
    """
    fields = extract_event_fields(ev)
    parts = [
        kind,
        str(ev.get("at") or ev.get("time") or ev.get("timestamp") or ""),
        fields["actor"] or "",
        fields["character"] or "",
        fields["biome"] or "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init()

    # ---- connection helpers ---------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta(key,value) VALUES('version',?)",
                             (str(SCHEMA_VERSION),))
            conn.commit()

    # ---- users ----------------------------------------------------------
    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,username,role,created_at,created_by,updated_at,"
                "disabled,last_login_at,last_login_ip FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_user(self, username: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def count_users(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, username: str, role: str, password_hash: str,
                    created_by: str) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(username,role,password_hash,created_at,created_by)"
                " VALUES(?,?,?,?,?)",
                (username, role, password_hash, time.time(), created_by))
            conn.commit()
            return int(cur.lastrowid)

    def update_user(self, username: str, *, role: str | None = None,
                    password_hash: str | None = None,
                    disabled: bool | None = None) -> bool:
        sets, params = [], []
        if role is not None:
            sets.append("role=?"); params.append(role)
        if password_hash is not None:
            sets.append("password_hash=?"); params.append(password_hash)
        if disabled is not None:
            sets.append("disabled=?"); params.append(1 if disabled else 0)
        if not sets:
            return False
        sets.append("updated_at=?"); params.append(time.time())
        params.append(username)
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE username=?", params)
            conn.commit()
            return cur.rowcount > 0

    def delete_user(self, username: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
            return cur.rowcount > 0

    def count_admins(self, exclude: str | None = None) -> int:
        with self._connect() as conn:
            if exclude:
                row = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0"
                    " AND username<>?", (exclude,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()
        return int(row[0])

    def note_login(self, username: str, ip: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE users SET last_login_at=?, last_login_ip=? WHERE username=?",
                         (time.time(), ip, username))
            conn.commit()

    # ---- audit ----------------------------------------------------------
    def audit(self, actor: str, role: str, ip: str, action: str,
              target: str | None = None, detail: Any = None, ok: bool = True) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO audit(ts,actor,role,ip,action,target,detail,ok)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), actor, role, ip, action, target, detail, 1 if ok else 0))
            conn.commit()

    def audit_tail(self, limit: int = 200, actor: str | None = None,
                   action: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit"
        where, params = [], []
        if actor:
            where.append("actor=?"); params.append(actor)
        if action:
            where.append("action LIKE ?"); params.append(f"{action}%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def audit_prune(self, keep_days: int = 90) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount

    # ---- kv -------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("INSERT INTO kv(key,value) VALUES(?,?)"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))
            conn.commit()

    # ---- game events ----------------------------------------------------
    def add_event(self, *, dedupe: str, kind: str, source_seq: int | None,
                  at: float, day: int | None = None, biome: str | None = None,
                  actor: str | None = None, character: str | None = None,
                  position: str | None = None, detail: str | None = None) -> bool:
        """Insert an event; returns False when it was already stored."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO game_events"
                "(dedupe,kind,source_seq,at,day,biome,actor,character,position,detail)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (dedupe, kind, source_seq, at, day, biome, actor, character,
                 position, detail))
            conn.commit()
            return cur.rowcount > 0

    def events(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM game_events"
        params: list = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def death_summary(self) -> list[dict]:
        """Deaths grouped by character, newest first.

        Groups on the display name rather than the raw character id: a leaderboard
        of numbers nobody can read is useless, and a player's name is what the
        question "who keeps dying" is actually about.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(actor, character) AS character,"
                " COUNT(*) AS deaths, MAX(at) AS last_at"
                " FROM game_events WHERE kind='player.death'"
                " AND COALESCE(actor, character) IS NOT NULL"
                " GROUP BY COALESCE(actor, character) ORDER BY deaths DESC").fetchall()
        return [dict(r) for r in rows]

    def reindex_events(self) -> dict:
        """Repair rows written before the poller learned that the agent nests
        its fields inside `data`.

        Reads the raw payload kept in `detail`, re-extracts actor/character/day/
        biome/position, and fills only the columns that are still NULL -- never
        overwriting a value that is already correct. It also recomputes the
        dedupe hash so replays after a game restart still match existing rows.

        Returns {"rows", "repaired", "hash_updated", "collisions_skipped"}.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, kind, detail FROM game_events").fetchall()

        repaired = 0
        hashes = 0
        collisions = 0
        for row in rows:
            rid, kind, detail = row["id"], row["kind"], row["detail"]
            try:
                ev = json.loads(detail) if detail else {}
            except (json.JSONDecodeError, TypeError):
                continue

            fields = extract_event_fields(ev)
            if kind == "command":
                fields["actor"] = None

            for col in ("actor", "character", "day", "biome", "position"):
                if fields[col] is None:
                    continue
                with self._write_lock, self._connect() as conn:
                    cur = conn.execute(
                        f"UPDATE game_events SET {col}=?"
                        f" WHERE id=? AND {col} IS NULL",
                        (fields[col], rid))
                    conn.commit()
                repaired += cur.rowcount

            new_hash = event_dedupe(kind, ev)
            if new_hash:
                try:
                    with self._write_lock, self._connect() as conn:
                        cur = conn.execute(
                            "UPDATE game_events SET dedupe=?"
                            " WHERE id=? AND dedupe<>?",
                            (new_hash, rid, new_hash))
                        conn.commit()
                    if cur.rowcount:
                        hashes += 1
                except sqlite3.IntegrityError:
                    # A recomputed hash colliding with another row is harmless:
                    # leave the original and move on.
                    collisions += 1

        return {"rows": len(rows), "repaired": repaired,
                "hash_updated": hashes, "collisions_skipped": collisions}

    def event_count(self, kind: str | None = None) -> int:
        with self._connect() as conn:
            if kind:
                return int(conn.execute(
                    "SELECT COUNT(*) FROM game_events WHERE kind=?", (kind,)).fetchone()[0])
            return int(conn.execute("SELECT COUNT(*) FROM game_events").fetchone()[0])
