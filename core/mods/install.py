"""Mod installation, quarantine, and rollback.

Everything here writes under `/config/bepinex`. The rules that matter:

* **Archives are untrusted.** Every zip member is checked: no absolute paths, no
  `..`, no symlinks. A plugin zip is arbitrary third-party data.
* **BepInEx layout is normalised.** Thunderstore packs nest as
  `BepInExPack_X/BepInEx/plugins/*.dll`; Hexium packs vary. Members are flattened
  to `plugins/` (and `patchers/` when present) rather than reproducing whatever
  directory shape the author used.
* **Every change is reversible.** The managed plugin set is tracked in a manifest
  with its source package, version, and tier, and a zip of the previous state is
  taken before an install or uninstall.
* **Quarantine is a move, not a delete.** A plugin suspected of breaking the
  server is moved to `quarantine/` with a recorded reason and log excerpt, so
  "re-enable anyway" is always possible.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "valheim-panel/0.6 (+local)"
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 5000


class ModError(RuntimeError):
    pass


@dataclass
class Installed:
    full_name: str
    version: str
    store: str
    tier: str
    files: list[str]
    installed_at: float
    dependencies: list[str] = field(default_factory=list)
    pinned: bool = False
    disabled: bool = False
    custom_url: str | None = None
    evidence: list[str] = field(default_factory=list)
    quarantine: dict | None = None

    # Fields accepted from the manifest. Constructing with **raw and no filtering
    # meant any key added later (e.g. `quarantine`) raised TypeError, and the
    # caller's bare `except TypeError: continue` made the mod vanish silently from
    # every listing. That is exactly the kind of failure a mod manager must not have.
    FIELDS = (
        "full_name", "version", "store", "tier", "files", "installed_at",
        "dependencies", "pinned", "disabled", "custom_url", "evidence",
        "quarantine",
    )

    @classmethod
    def from_manifest(cls, raw: dict) -> "Installed":
        known = {k: v for k, v in raw.items() if k in cls.FIELDS}
        return cls(**known)

    def as_dict(self) -> dict:
        out = {
            "full_name": self.full_name, "version": self.version, "store": self.store,
            "tier": self.tier, "files": self.files, "installed_at": self.installed_at,
            "dependencies": self.dependencies, "pinned": self.pinned,
            "disabled": self.disabled, "custom_url": self.custom_url,
            "evidence": self.evidence,
        }
        if self.quarantine:
            out["quarantine"] = self.quarantine
        return out


class ModManager:
    def __init__(self, bepinex_root: Path) -> None:
        self.root = bepinex_root
        self.plugins = bepinex_root / "plugins"
        self.patchers = bepinex_root / "patchers"
        self.config_dir = bepinex_root
        self.quarantine = bepinex_root / "quarantine"
        self.manifest_path = bepinex_root / "panel-mods.json"
        self.archive_dir = bepinex_root / ".archives"
        for d in (self.plugins, self.patchers, self.quarantine, self.archive_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ==================================================================
    # manifest
    # ==================================================================
    def manifest(self) -> dict:
        if not self.manifest_path.is_file():
            return {"mods": {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"mods": {}}
        data.setdefault("mods", {})
        return data

    def _save_manifest(self, data: dict) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.manifest_path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise ModError(f"could not write manifest: {exc}") from exc

    def installed(self) -> list[Installed]:
        out = []
        for key, raw in self.manifest()["mods"].items():
            try:
                out.append(Installed.from_manifest(raw))
            except TypeError as exc:
                # A malformed entry must be visible, not silently dropped.
                raise ModError(f"manifest entry {key!r} is malformed: {exc}") from exc
        out.sort(key=lambda m: m.full_name.lower())
        return out

    def reconcile(self) -> list[str]:
        """Drop manifest entries whose DLLs are no longer on disk.

        Called after a snapshot restore: the restored files define reality, so a
        manifest entry pointing at a deleted plugin is stale bookkeeping.
        """
        data = self.manifest()
        dropped: list[str] = []
        for key, raw in list(data["mods"].items()):
            files = raw.get("files") or []
            present = any(
                (base / f).is_file()
                for f in files
                for base in (self.plugins, self.patchers, self.quarantine)
            )
            if not present:
                data["mods"].pop(key)
                dropped.append(key)
        if dropped:
            self._save_manifest(data)
        return dropped

    def unmanaged_files(self) -> list[str]:
        """DLLs present on disk that the manifest does not claim."""
        known = {f for m in self.installed() for f in m.files}
        on_disk = {p.name for p in self.plugins.glob("*.dll")}
        return sorted(on_disk - known)

    # ==================================================================
    # archive handling
    # ==================================================================
    def _safe_members(self, zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        if len(zf.infolist()) > MAX_MEMBERS:
            raise ModError(f"archive has too many members ({len(zf.infolist())})")
        out = []
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise ModError(f"unsafe path in archive: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ModError(f"symlink in archive refused: {name}")
            if info.file_size > MAX_ARCHIVE_BYTES:
                raise ModError(f"member too large: {name}")
            out.append(info)
        if not out:
            raise ModError("archive is empty")
        return out

    @staticmethod
    def _classify_member(name: str) -> str | None:
        """Map an archive member to 'plugins', 'patchers', 'config', or None."""
        low = name.lower()
        if not low.endswith((".dll", ".cfg", ".json", ".txt", ".md", ".yaml", ".yml")):
            return None
        if "/patchers/" in low or low.startswith("patchers/"):
            return "patchers"
        if "/plugins/" in low or low.startswith("plugins/"):
            return "plugins"
        if "/config/" in low or low.startswith("config/"):
            return "config"
        if low.endswith(".dll"):
            # A bare DLL at the archive root is a plugin.
            return "plugins"
        return None

    def _download(self, url: str, dest: Path) -> None:
        if not re.match(r"^https?://", url or ""):
            raise ModError("download URL must be http(s)")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = 0
                with dest.open("wb") as fh:
                    while chunk := resp.read(262_144):
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise ModError("archive exceeds the size limit")
                        fh.write(chunk)
        except (urllib.error.URLError, OSError) as exc:
            dest.unlink(missing_ok=True)
            raise ModError(f"download failed: {exc}") from exc

    # ==================================================================
    # snapshots
    # ==================================================================
    def snapshot(self, label: str) -> Path:
        """Zip the current managed plugin set so a change can be undone."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.archive_dir / f"{label}-{stamp}.zip"
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for base in (self.plugins, self.patchers):
                    for f in base.rglob("*"):
                        if f.is_file():
                            zf.write(f, f"{base.name}/{f.relative_to(base)}")
                if self.manifest_path.is_file():
                    zf.write(self.manifest_path, "panel-mods.json")
        except OSError as exc:
            raise ModError(f"snapshot failed: {exc}") from exc
        return dest

    def restore_snapshot(self, name: str) -> None:
        path = (self.archive_dir / name).resolve()
        if not str(path).startswith(str(self.archive_dir.resolve())) or not path.is_file():
            raise ModError("no such snapshot")
        try:
            with zipfile.ZipFile(path) as zf:
                self._safe_members(zf)
                for base in (self.plugins, self.patchers):
                    for f in base.glob("*"):
                        if f.is_file():
                            f.unlink()
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    target_rel = info.filename
                    if target_rel == "panel-mods.json":
                        # Write to the real manifest path. Extracting with
                        # self.root.parent landed it at /config/panel-mods.json
                        # (outside the mod tree) and left the live manifest stale.
                        with zf.open(info) as src, self.manifest_path.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        continue
                    top, _, rest = target_rel.partition("/")
                    if top not in ("plugins", "patchers"):
                        continue
                    target = self.root / top / rest
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ModError(f"restore failed: {exc}") from exc

        # The restored files are now the source of truth; drop manifest entries
        # whose DLLs the snapshot did not contain.
        self.reconcile()

    def snapshots(self) -> list[dict]:
        out = []
        for f in sorted(self.archive_dir.glob("*.zip"), reverse=True)[:40]:
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    # ==================================================================
    # install / uninstall
    # ==================================================================
    def install(self, *, full_name: str, version: str, store: str, tier: str,
                url: str, evidence: list[str] | None = None,
                dependencies: list[str] | None = None,
                custom_url: str | None = None,
                snapshot: bool = True) -> dict:
        """Install one package.

        `snapshot=False` is for multi-package transactions (a package plus its
        dependencies): the caller takes a single snapshot before the first
        install. Taking one per package would mean the "pre-install" snapshot of
        the second package already contains the first package's files, so a
        rollback would restore a half-installed set.
        """
        snap_name = self.snapshot("pre-install").name if snapshot else None
        tmp_zip = self.archive_dir / f".incoming-{time.strftime('%Y%m%d-%H%M%S-%f')}.zip"
        try:
            self._download(url, tmp_zip)
            written = self._extract_into(tmp_zip)
        finally:
            tmp_zip.unlink(missing_ok=True)

        if not written:
            raise ModError("archive contained no plugin DLLs")

        data = self.manifest()
        data["mods"][full_name] = Installed(
            full_name=full_name, version=version, store=store, tier=tier,
            files=written, installed_at=time.time(),
            dependencies=dependencies or [], pinned=False, disabled=False,
            custom_url=custom_url, evidence=evidence or [],
        ).as_dict()
        self._save_manifest(data)

        return {"files": written, "snapshot": snap_name,
                "needs_restart": True}

    def _extract_into(self, archive: Path) -> list[str]:
        written: list[str] = []
        try:
            with zipfile.ZipFile(archive) as zf:
                members = self._safe_members(zf)
                for info in members:
                    kind = self._classify_member(info.filename)
                    if kind is None:
                        continue
                    target_dir = self.plugins if kind == "plugins" else (
                        self.patchers if kind == "patchers" else self.config_dir)
                    # Flatten: keep only the basename so nested pack layouts do
                    # not recreate the author's directory tree.
                    target = target_dir / Path(info.filename).name
                    if target.suffix.lower() not in (".dll", ".cfg", ".json"):
                        continue
                    with zf.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    written.append(target.name)
        except zipfile.BadZipFile as exc:
            raise ModError(f"not a valid zip: {exc}") from exc
        except OSError as exc:
            raise ModError(f"extract failed: {exc}") from exc
        return written

    def uninstall(self, full_name: str) -> dict:
        data = self.manifest()
        entry = data["mods"].pop(full_name, None)
        if entry is None:
            raise ModError("not managed by the panel")
        snapshot = self.snapshot("pre-uninstall")
        removed = []
        for fname in entry.get("files") or []:
            for base in (self.plugins, self.patchers):
                target = base / fname
                if target.is_file():
                    target.unlink()
                    removed.append(fname)
        self._save_manifest(data)
        return {"removed": removed, "snapshot": snapshot.name, "needs_restart": True}

    def set_enabled(self, full_name: str, enabled: bool) -> dict:
        """Enable/disable by moving the DLLs to and from quarantine."""
        data = self.manifest()
        entry = data["mods"].get(full_name)
        if entry is None:
            raise ModError("not managed by the panel")
        files = entry.get("files") or []
        moved = []
        if enabled:
            for fname in files:
                src = self.quarantine / fname
                if src.is_file():
                    shutil.move(str(src), str(self.plugins / fname))
                    moved.append(fname)
            # Deliberate re-enable clears the quarantine verdict, otherwise the
            # mod stays listed as quarantined forever.
            entry.pop("quarantine", None)
        else:
            for fname in files:
                src = self.plugins / fname
                if src.is_file():
                    shutil.move(str(src), str(self.quarantine / fname))
                    moved.append(fname)
        entry["disabled"] = not enabled
        self._save_manifest(data)
        return {"moved": moved, "needs_restart": True}

    def set_pinned(self, full_name: str, pinned: bool) -> dict:
        data = self.manifest()
        entry = data["mods"].get(full_name)
        if entry is None:
            raise ModError("not managed by the panel")
        entry["pinned"] = pinned
        self._save_manifest(data)
        return {"pinned": pinned}

    # ==================================================================
    # quarantine
    # ==================================================================
    def quarantine_mod(self, full_name: str, reason: str,
                       excerpt: str = "") -> dict:
        data = self.manifest()
        entry = data["mods"].get(full_name)
        if entry is None:
            raise ModError("not managed by the panel")
        moved = []
        for fname in entry.get("files") or []:
            src = self.plugins / fname
            if src.is_file():
                shutil.move(str(src), str(self.quarantine / fname))
                moved.append(fname)
        entry["disabled"] = True
        entry["quarantine"] = {
            "reason": reason,
            "excerpt": (excerpt or "")[:4000],
            "at": time.time(),
        }
        self._save_manifest(data)
        return {"moved": moved, "needs_restart": True}

    def quarantined(self) -> list[dict]:
        """Mods carrying a quarantine record, whether or not they are disabled.

        Keyed on the record rather than `disabled`: an operator may re-enable a
        quarantined mod on purpose, and the reason must stay visible when they do.
        """
        data = self.manifest()
        out = []
        for m in self.installed():
            record = data["mods"].get(m.full_name, {}).get("quarantine")
            if record:
                out.append({**m.as_dict(), "quarantine": record})
        return out

    # ==================================================================
    # config editing
    # ==================================================================
    def config_files(self) -> list[dict]:
        out = []
        for f in sorted(self.config_dir.glob("*.cfg")):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def read_config(self, name: str) -> str:
        path = self._config_path(name)
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ModError(f"could not read {name}: {exc}") from exc

    def write_config(self, name: str, content: str) -> None:
        if len(content) > 512_000:
            raise ModError("config too large")
        path = self._config_path(name)
        try:
            backup = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
            if path.is_file():
                shutil.copy2(path, backup)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ModError(f"could not write {name}: {exc}") from exc

    def _config_path(self, name: str) -> Path:
        if not re.match(r"^[A-Za-z0-9_.\-]+\.cfg$", name or ""):
            raise ModError("invalid config name")
        path = (self.config_dir / name).resolve()
        if not str(path).startswith(str(self.config_dir.resolve())):
            raise ModError("path escapes the config directory")
        return path
