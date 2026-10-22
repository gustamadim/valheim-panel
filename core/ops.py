"""Server-mutating operations.

Every function here changes state on disk or in the running game server. They are
called only from routes that already checked a capability, and each caller writes
an audit row. The module itself stays policy-free so the permission surface lives
in exactly one place (core/roles.py).

Design notes
------------
* Lists are written atomically (temp file + rename) and copied to a timestamped
  backup first, because a truncated adminlist locks the owner out of admin powers.
* Backup restore is refused while the server is running unless the caller stops it
  first; the world directory is snapshotted before being replaced.
* Zip extraction rejects absolute paths, `..`, and symlink members. A backup zip is
  still user-supplied data.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .rcon import RconError, RconPool
from .supervisor import Supervisor, SupervisorError

STEAMID_RE = re.compile(r"^\d{17}$")
WORLD_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.zip$")

LIST_FILES = {
    "admin": "adminlist.txt",
    "banned": "bannedlist.txt",
    "permitted": "permittedlist.txt",
}

LIST_HEADER = {
    "admin": "// List admin players ID  ONE per line",
    "banned": "// List banned players ID  ONE per line",
    "permitted": "// List permitted players ID ONE per line",
}


class OpsError(RuntimeError):
    """A refusal or upstream failure, safe to show to the user."""


@dataclass
class Result:
    ok: bool
    message: str
    detail: dict | None = None


def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class Ops:
    def __init__(self, cfg: Config, sup: Supervisor, rcon: RconPool) -> None:
        self.cfg = cfg
        self.sup = sup
        self.rcon = rcon

    # ==================================================================
    # server control
    # ==================================================================
    def server_state(self) -> str:
        try:
            return (self.sup.process_info(self.cfg.supervisor_program).get("statename") or "").upper()
        except SupervisorError as exc:
            raise OpsError(f"supervisor unreachable: {exc}") from exc

    def players_online(self) -> int:
        """Best-effort live player count, used for the idle checks."""
        try:
            out = self.rcon.run("players")
        except RconError:
            return 0
        m = re.search(r"Online\s+(\d+)", out)
        if m:
            return int(m.group(1))
        # Fall back to counting non-header rows.
        rows = [l for l in out.splitlines()
                if l.strip() and not l.strip().lower().startswith(("online", "players", "name"))]
        return len(rows)

    def _wait_state(self, want: str, timeout: float = 180.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.server_state() == want:
                    return True
            except OpsError:
                pass
            time.sleep(2.0)
        return False

    def restart(self, sync: bool = False) -> Result:
        """Restart by stop-then-start.

        Supervisor's XML-RPC API has no `restartProcess` (only the HTML form and
        `supervisorctl` expose a restart verb; `supervisor.restart` restarts
        supervisord itself, not a child). Doing stop-then-start explicitly also
        means the game's own SIGTERM handler runs, so the world is saved.

        `sync=True` additionally runs `valheim-bootstrap` between the two steps.
        That is required for plugin changes: the image only copies
        /config/bepinex/{plugins,patchers} into the live tree
        (/opt/valheim/bepinex/BepInEx/...) during bootstrap, and bootstrap is a
        one-shot program that otherwise sits EXITED for the container's lifetime.
        Without it a newly installed mod is on disk but never loaded.
        """
        try:
            if self.server_state() == "RUNNING":
                self.sup.stop(self.cfg.supervisor_program)
        except SupervisorError as exc:
            raise OpsError(f"restart failed while stopping: {exc}") from exc

        if not self._wait_state("STOPPED", timeout=150):
            raise OpsError("restart failed: server did not reach STOPPED; check the log")

        synced = False
        if sync:
            ok, detail = self.sup.run_oneshot("valheim-bootstrap", timeout=240)
            if not ok:
                raise OpsError(f"plugin sync failed: {detail}")
            synced = True

        try:
            self.sup.start(self.cfg.supervisor_program)
        except SupervisorError as exc:
            raise OpsError(f"restart failed while starting: {exc}") from exc

        msg = "restarting; the world is saved and the server returns in ~1-2 min"
        if synced:
            msg = "plugins synced; restarting with the new plugin set (~1-2 min)"
        return Result(True, msg, {"synced": synced})

    def stop(self, force: bool = False) -> Result:
        online = self.players_online()
        if online and not force:
            raise OpsError(
                f"{online} player(s) online — stopping would disconnect them. "
                "Confirm the force flag to proceed.")
        try:
            self.sup.stop(self.cfg.supervisor_program)
        except SupervisorError as exc:
            raise OpsError(f"stop failed: {exc}") from exc
        stopped = self._wait_state("STOPPED", timeout=150)
        return Result(True, "server stopped" if stopped else "stop requested")

    def start(self) -> Result:
        """Start the game, syncing plugins first.

        Bootstrap runs before start so a server that was stopped while mods were
        installed comes up with the current plugin set, not the stale one.
        """
        if self.server_state() == "RUNNING":
            raise OpsError("server is already running")
        ok, detail = self.sup.run_oneshot("valheim-bootstrap", timeout=240)
        if not ok:
            raise OpsError(f"plugin sync failed: {detail}")
        try:
            self.sup.start(self.cfg.supervisor_program)
        except SupervisorError as exc:
            raise OpsError(f"start failed: {exc}") from exc
        return Result(True, "plugins synced; starting (~1-2 min to accept joins)",
                      {"synced": True})

    def save(self) -> Result:
        try:
            out = self.rcon.run("save")
        except RconError as exc:
            raise OpsError(f"save failed: {exc}") from exc
        return Result(True, out or "save requested")

    # ==================================================================
    # player lists
    # ==================================================================
    def _list_path(self, kind: str) -> Path:
        if kind not in LIST_FILES:
            raise OpsError(f"unknown list {kind!r}")
        return self.cfg.config_dir / LIST_FILES[kind]

    def read_list(self, kind: str) -> list[str]:
        path = self._list_path(kind)
        if not path.is_file():
            return []
        ids: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            # Tolerate the Steam_<id> form some tools write.
            if line.lower().startswith("steam_"):
                line = line[6:]
            if STEAMID_RE.match(line) and line not in ids:
                ids.append(line)
        return ids

    def write_list(self, kind: str, ids: list[str]) -> Result:
        cleaned: list[str] = []
        for raw in ids:
            value = (raw or "").strip()
            if value.lower().startswith("steam_"):
                value = value[6:]
            if not value:
                continue
            if not STEAMID_RE.match(value):
                raise OpsError(f"not a SteamID64 (17 digits): {raw!r}")
            if value not in cleaned:
                cleaned.append(value)

        path = self._list_path(kind)
        # Keep the old file: a bad write to adminlist.txt can cost admin access.
        if path.is_file():
            backup = path.with_suffix(path.suffix + f".bak-{_now_tag()}")
            try:
                shutil.copy2(path, backup)
            except OSError as exc:
                raise OpsError(f"could not back up {path.name}: {exc}") from exc

        content = LIST_HEADER[kind] + "\n" + "\n".join(cleaned) + ("\n" if cleaned else "")
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise OpsError(f"could not write {path.name}: {exc}") from exc

        return Result(True, f"{kind}list.txt updated ({len(cleaned)} entries)",
                      {"count": len(cleaned)})

    def list_env_clobber_warning(self) -> str | None:
        """Warn when compose will overwrite adminlist.txt on next start.

        The image rewrites the list files from ADMINLIST_IDS/BANNEDLIST_IDS/
        PERMITTEDLIST_IDS whenever those env vars are set. The panel owns the
        files now, so a stray env var silently reverts every edit.
        """
        try:
            env = self._container_env()
        except OpsError:
            return None
        risky = [k for k in ("ADMINLIST_IDS", "BANNEDLIST_IDS", "PERMITTEDLIST_IDS")
                 if env.get(k)]
        if not risky:
            return None
        return ("compose still sets " + ", ".join(risky) +
                " — these overwrite the list files on every start, so panel edits "
                "will be reverted. Remove them from docker-compose.yml.")

    def _container_env(self) -> dict[str, str]:
        """Read the game container's env from the host probe if present.

        The panel has no Docker access; the probe publishes a whitelist of env
        keys it observed. Absent probe data means "cannot tell", not "no risk".
        """
        import json
        probe = self.cfg.probe_file
        if not probe.is_file():
            raise OpsError("no host probe data")
        try:
            data = json.loads(probe.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OpsError(f"probe unreadable: {exc}") from exc
        return data.get("env") or {}

    # ==================================================================
    # backups
    # ==================================================================
    def _backup_path(self, name: str) -> Path:
        if not BACKUP_NAME_RE.match(name):
            raise OpsError("invalid backup name")
        path = (self.cfg.backups_dir / name).resolve()
        if not str(path).startswith(str(self.cfg.backups_dir.resolve())):
            raise OpsError("path escapes the backups directory")
        return path

    def create_backup(self) -> Result:
        """Ask the image's own backup supervisor to take a snapshot now."""
        try:
            # The backup loop traps SIGHUP and runs a backup immediately.
            self.sup.signal("valheim-backup", "HUP")
        except SupervisorError as exc:
            raise OpsError(
                "could not signal the backup process; is valheim-backup running? "
                f"({exc})") from exc
        return Result(True, "backup requested — it appears in the list within a few seconds")

    def delete_backup(self, name: str) -> Result:
        path = self._backup_path(name)
        if not path.is_file():
            raise OpsError("no such backup")
        try:
            path.unlink()
        except OSError as exc:
            raise OpsError(f"could not delete: {exc}") from exc
        return Result(True, f"deleted {name}")

    def restore_backup(self, name: str) -> Result:
        path = self._backup_path(name)
        if not path.is_file():
            raise OpsError("no such backup")
        if self.server_state() != "STOPPED":
            raise OpsError("stop the server before restoring a backup")

        # Safety snapshot of the current world tree before anything is replaced.
        safety = self.cfg.backups_dir / f"pre-restore-{_now_tag()}.zip"
        try:
            self._zip_dir(self.cfg.worlds_dir, safety, prefix="config/worlds_local")
        except OSError as exc:
            raise OpsError(f"could not take a pre-restore snapshot: {exc}") from exc

        try:
            with zipfile.ZipFile(path) as zf:
                members = self._safe_members(zf)
                for member in members:
                    zf.extract(member, self.cfg.config_dir.parent)
        except zipfile.BadZipFile as exc:
            raise OpsError(f"corrupt zip: {exc}") from exc
        except OSError as exc:
            raise OpsError(f"extract failed: {exc}") from exc

        return Result(True, f"restored {name}; pre-restore snapshot saved as {safety.name}",
                      {"safety": safety.name})

    def _zip_dir(self, src: Path, dest: Path, prefix: str) -> None:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(src):
                for fname in files:
                    full = Path(root) / fname
                    rel = full.relative_to(src)
                    zf.write(full, f"{prefix}/{rel}")

    def _safe_members(self, zf: zipfile.ZipFile) -> list[str]:
        """Reject traversal and symlink members; only config/worlds_local/**."""
        out: list[str] = []
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise OpsError(f"unsafe path in zip: {name}")
            if not name.startswith("config/worlds_local/"):
                raise OpsError(f"unexpected path in zip: {name}")
            # Symlink entries carry the unix mode in the high bits.
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise OpsError(f"symlink in zip refused: {name}")
            out.append(name)
        if not out:
            raise OpsError("zip contains no world files")
        return out

    # ==================================================================
    # worlds
    # ==================================================================
    def world_dir(self, name: str) -> Path:
        if not WORLD_NAME_RE.match(name):
            raise OpsError("invalid world name")
        path = (self.cfg.worlds_dir / name).resolve()
        if not str(path).startswith(str(self.cfg.worlds_dir.resolve())):
            raise OpsError("path escapes the worlds directory")
        return path

    def delete_world(self, name: str, active: str | None) -> Result:
        """Delete an inactive world. The active world is never a target."""
        if active and name == active:
            raise OpsError("refusing to delete the active world")
        path = self.world_dir(name)
        if not path.is_dir():
            raise OpsError("no such world")
        if self.server_state() != "STOPPED":
            raise OpsError("stop the server before deleting a world")

        trash = self.cfg.backups_dir / f"deleted-world-{name}-{_now_tag()}.zip"
        try:
            self._zip_dir(path, trash, prefix=f"config/worlds_local/{name}")
            shutil.rmtree(path)
        except OSError as exc:
            raise OpsError(f"could not delete {name}: {exc}") from exc
        return Result(True, f"deleted {name} (archived as {trash.name})",
                      {"archive": trash.name})

    def regenerate_world(self, name: str, active: str | None) -> Result:
        """Reset the active world: archive it, then let Valheim regenerate it.

        Valheim has no server-side seed choice; a new random seed is generated on
        the next start. The archive keeps the old world recoverable.
        """
        if active and name != active:
            raise OpsError("only the active world can be regenerated")
        path = self.world_dir(name)
        if not path.is_dir():
            raise OpsError("no such world")
        if self.server_state() != "STOPPED":
            raise OpsError("stop the server before regenerating a world")

        archive = self.cfg.backups_dir / f"regenerated-{name}-{_now_tag()}.zip"
        try:
            self._zip_dir(path, archive, prefix=f"config/worlds_local/{name}")
            shutil.rmtree(path)
        except OSError as exc:
            raise OpsError(f"could not archive/remove {name}: {exc}") from exc
        return Result(
            True,
            f"{name} archived as {archive.name}. Start the server to generate a fresh "
            "world with a new random seed.",
            {"archive": archive.name})

    def switch_world_supported(self) -> bool:
        """World switching needs a compose env change, which the panel cannot do.

        The game reads -world from WORLD_NAME in docker-compose.yml, so changing
        the active world means editing that file and recreating the container —
        both privileged. The panel refuses rather than pretending.
        """
        return False
