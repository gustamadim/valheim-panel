"""Aggregates the three upstreams into one snapshot for the UI.

Read-only by design: nothing here mutates the server. Every field is either
observed or explicitly None, so the UI can distinguish "zero players" from
"could not ask".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .agent import Agent, AgentError
from .config import Config
from .logs import LogState, read_probe, scan
from .rcon import RconError, RconPool
from .supervisor import Supervisor, SupervisorError


@dataclass
class Snapshot:
    generated_at: float
    supervisor: dict = field(default_factory=dict)
    agent: dict = field(default_factory=dict)
    rcon: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)
    log: dict = field(default_factory=dict)
    world: dict = field(default_factory=dict)
    backups: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "supervisor": self.supervisor,
            "agent": self.agent,
            "rcon": self.rcon,
            "probe": self.probe,
            "log": self.log,
            "world": self.world,
            "backups": self.backups,
            "errors": self.errors,
        }


class StateProvider:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sup = Supervisor(cfg.supervisor_url, cfg.supervisor_user, cfg.supervisor_pass)
        self.agent = Agent(cfg.agent_url, cfg.agent_token)
        self.rcon = RconPool(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
        self._log_cache: LogState | None = None
        self._log_offset = 0
        self._log_cache_at = 0.0
        self._log_seeded = False

    # ---- log -----------------------------------------------------------
    def _refresh_log(self, max_bytes: int = 512_000) -> LogState:
        """Incrementally consume the supervisor log into a cached LogState.

        Uses XML-RPC readProcessLog, not the /logtail HTTP endpoint: /logtail is a
        blocking long-poll that waits for new data and times out whenever the
        server is quiet, which is most of the time.
        """
        if self._log_cache is None:
            self._log_cache = LogState()
            self._log_offset = 0
            self._log_seeded = False

        now = time.time()
        if self._log_seeded and now - self._log_cache_at < 2.0:
            return self._log_cache

        # First pass reads from the start so startup lines (plugin loads, the
        # session registration) are captured; afterwards we continue from the
        # last offset.
        budget = 4_000_000 if not self._log_seeded else max_bytes
        try:
            text = self.sup.read_log(self.cfg.supervisor_program, self._log_offset, budget)
        except SupervisorError:
            self._log_cache_at = now
            return self._log_cache

        if text:
            scan(text.splitlines(), self._log_cache, tz=self.cfg.game_tz)
            self._log_offset += len(text.encode("utf-8", "replace"))
        self._log_seeded = True
        self._log_cache_at = now
        return self._log_cache

    def reset_log(self) -> None:
        self._log_cache = LogState()
        self._log_offset = 0
        self._log_cache_at = 0.0
        self._log_seeded = False

    # ---- world / backups (filesystem) ----------------------------------
    def _worlds(self) -> dict:
        out: dict = {"active": None, "items": []}
        try:
            if not self.cfg.worlds_dir.is_dir():
                return out
            for entry in sorted(self.cfg.worlds_dir.iterdir()):
                if not entry.is_dir():
                    continue
                ok_files = sorted(entry.glob("_main.*.ok"))
                newest_gen = None
                if ok_files:
                    gens = []
                    for f in ok_files:
                        parts = f.name.split(".")
                        if len(parts) >= 3 and parts[1].isdigit():
                            gens.append(int(parts[1]))
                    newest_gen = max(gens) if gens else None
                size = sum(f.stat().st_size for f in entry.glob("*") if f.is_file())
                chunks = len(list(entry.glob("*.chunk")))
                item = {
                    "name": entry.name,
                    "generation": newest_gen,
                    "size": size,
                    "chunks": chunks,
                    "is_backup": "_backup" in entry.name,
                    "mtime": entry.stat().st_mtime,
                }
                out["items"].append(item)
        except OSError as exc:
            out["error"] = str(exc)
        return out

    def _backups(self, limit: int = 50) -> dict:
        out: dict = {"items": [], "total": 0, "total_size": 0}
        try:
            if not self.cfg.backups_dir.is_dir():
                return out
            files = [f for f in self.cfg.backups_dir.iterdir()
                     if f.is_file() and f.suffix == ".zip"]
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            total_size = sum(f.stat().st_size for f in files)
            out["total"] = len(files)
            out["total_size"] = total_size
            for f in files[:limit]:
                st = f.stat()
                out["items"].append({
                    "name": f.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
        except OSError as exc:
            out["error"] = str(exc)
        return out

    # ---- aggregate ------------------------------------------------------
    def snapshot(self) -> Snapshot:
        snap = Snapshot(generated_at=time.time())

        probe = read_probe(self.cfg.probe_file)
        snap.probe = probe

        # supervisor
        try:
            info = self.sup.process_info(self.cfg.supervisor_program)
            snap.supervisor = {
                "reachable": True,
                "state": info.get("statename"),
                "pid": info.get("pid"),
                "start": info.get("start"),
                "description": info.get("description"),
                "exitstatus": info.get("exitstatus"),
                "spawnerr": info.get("spawnerr"),
            }
        except SupervisorError as exc:
            snap.supervisor = {"reachable": False, "error": str(exc)}
            snap.errors.append(f"supervisor: {exc}")

        # agent (authoritative for players, day, weather)
        try:
            st = self.agent.status()
            snap.agent = {
                "reachable": True,
                "ready": st.get("ready"),
                "game_version": st.get("game_version"),
                "agent_version": st.get("agent_version"),
                "uptime_seconds": st.get("uptime_seconds"),
                "captured_at": st.get("captured_at"),
                "world": st.get("world") or {},
                "players": st.get("players") or [],
                "global_keys": st.get("global_keys") or [],
                "modifiers": st.get("modifiers") or {},
            }
        except AgentError as exc:
            snap.agent = {"reachable": False, "error": str(exc)}
            snap.errors.append(f"agent: {exc}")

        # rcon (server stats, admin surface health)
        ok, out = self.rcon.try_run("serverStats")
        if ok:
            snap.rcon = {"reachable": True, "server_stats": out}
        else:
            snap.rcon = {"reachable": False, "error": out}
            snap.errors.append(f"rcon: {out}")

        # log
        log_state = self._refresh_log()
        snap.log = log_state.as_dict()

        # join code: probe is the only place it survives
        jc = probe.get("joincode") or {}
        snap.log["join_code"] = jc.get("code") or log_state.join_code
        snap.log["join_code_seen_at"] = jc.get("seen_at")

        snap.world = self._worlds()
        active_name = (snap.agent.get("world") or {}).get("name")
        for item in snap.world["items"]:
            if item["name"] == active_name:
                snap.world["active"] = item["name"]
                break
        if snap.world["active"] is None and snap.world["items"]:
            live = [i for i in snap.world["items"] if not i["is_backup"]]
            if live:
                snap.world["active"] = live[0]["name"]

        snap.backups = self._backups()
        return snap
