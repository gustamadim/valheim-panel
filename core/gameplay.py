"""Gameplay helpers: autofeed and the raid filter.

Backed by the `ValheimUI.Gameplay` plugin (MIT, jonasthim/valheim-server-ui),
configured through `BepInEx/config/se.jonasthim.valheimui.gameplay.cfg`.

The plugin hot-reloads this file via a `FileSystemWatcher` with a 2 s polling
fallback, so **no restart is needed** for any change here. The log line says
which mechanism fired (`config reloaded (watcher)` vs `(polled)`); a run of
`polled` reloads means the watcher is not working and is worth surfacing.

Editing rules
-------------
* Only the keys this module knows about are touched. Everything else in the file
  (plugin comments, other sections) is preserved verbatim, because BepInEx
  regenerates a file without its comments if it is written back naively, and the
  comments are the only documentation the operator has.
* Values are validated against the plugin's own documented ranges before writing.
* A `.bak` is kept per save.
"""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

CFG_NAME = "se.jonasthim.valheimui.gameplay.cfg"


class GameplayError(RuntimeError):
    pass


# (section, key) -> (kind, min, max)
SPEC: dict[tuple[str, str], tuple[str, float | None, float | None]] = {
    ("Autofeed", "Enabled"): ("bool", None, None),
    ("Autofeed", "SignPrefix"): ("str", None, None),
    ("Autofeed", "SignRadius"): ("float", 0.5, 6.0),
    ("Autofeed", "Range"): ("float", 1.0, 100.0),
    ("Autofeed", "IntervalSeconds"): ("int", 5, 600),
    ("Autofeed", "Debug"): ("bool", None, None),
    ("Raids", "Enabled"): ("bool", None, None),
    ("Raids", "Disabled"): ("list", None, None),
}

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_]+)\s*=\s*(?P<value>.*)$")


@dataclass
class CfgFile:
    """A parsed BepInEx cfg that can be written back with comments intact."""
    lines: list[str] = field(default_factory=list)
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "CfgFile":
        lines = text.splitlines()
        values: dict[tuple[str, str], str] = {}
        section = ""
        for line in lines:
            m = _SECTION_RE.match(line)
            if m:
                section = m.group("name").strip()
                continue
            m = _KEY_RE.match(line)
            if m and section:
                values[(section, m.group("key"))] = m.group("value").strip()
        return cls(lines=lines, values=values)

    def get(self, section: str, key: str, default: str = "") -> str:
        return self.values.get((section, key), default)

    def set(self, section: str, key: str, value: str) -> None:
        """Replace a key's value in place, preserving comments and ordering."""
        section_re = re.compile(r"^\s*\[" + re.escape(section) + r"\]\s*$")
        key_re = re.compile(r"^(\s*" + re.escape(key) + r"\s*=\s*).*$")

        in_section = False
        for i, line in enumerate(self.lines):
            if section_re.match(line):
                in_section = True
                continue
            if in_section and _SECTION_RE.match(line):
                break  # left the section without finding the key
            if in_section:
                m = key_re.match(line)
                if m:
                    self.lines[i] = f"{m.group(1)}{value}"
                    self.values[(section, key)] = value
                    return

        # Key absent: append at the end of its section, or create the section.
        out: list[str] = []
        in_section = False
        inserted = False
        for line in self.lines:
            if section_re.match(line):
                in_section = True
                out.append(line)
                continue
            if in_section and _SECTION_RE.match(line):
                if not inserted:
                    out.append(f"{key} = {value}")
                    inserted = True
                in_section = False
            out.append(line)
        if in_section and not inserted:
            out.append(f"{key} = {value}")
            inserted = True
        if not inserted:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{section}]")
            out.append(f"{key} = {value}")
        self.lines = out
        self.values[(section, key)] = value

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


class Gameplay:
    def __init__(self, bepinex_root: Path) -> None:
        self.root = bepinex_root
        self.path = bepinex_root / CFG_NAME

    # ---- read ------------------------------------------------------------
    def _load(self) -> CfgFile:
        if not self.path.is_file():
            raise GameplayError(
                f"{CFG_NAME} not found — is the Valheim UI Gameplay plugin installed "
                "and has the server started once since?")
        try:
            return CfgFile.parse(self.path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            raise GameplayError(f"could not read the gameplay config: {exc}") from exc

    def state(self, raid_events: list[str] | None = None) -> dict:
        cfg = self._load()

        def num(key: str, cast, default):
            raw = cfg.get("Autofeed", key)
            try:
                return cast(raw) if raw != "" else default
            except (TypeError, ValueError):
                return default

        disabled_raw = cfg.get("Raids", "Disabled")
        disabled = [x.strip() for x in disabled_raw.split(",") if x.strip()]

        known = list(raid_events or [])
        return {
            "available": True,
            "config_name": CFG_NAME,
            "autofeed": {
                "enabled": _as_bool(cfg.get("Autofeed", "Enabled", "false")),
                "sign_prefix": cfg.get("Autofeed", "SignPrefix", "feed"),
                "sign_radius": num("SignRadius", float, 2.0),
                "range": num("Range", float, 20.0),
                "interval_seconds": num("IntervalSeconds", int, 30),
                "debug": _as_bool(cfg.get("Autofeed", "Debug", "false")),
            },
            "raids": {
                "enabled": _as_bool(cfg.get("Raids", "Enabled", "true")),
                "disabled": disabled,
                # Unknown names are kept (the plugin ignores them) but flagged, so a
                # typo does not silently fail to disable a raid the operator expects
                # to be off.
                "unknown_disabled": [d for d in disabled if known and d not in known],
            },
            "known_events": known,
            "hot_reload": True,
        }

    # ---- write -----------------------------------------------------------
    def update(self, payload: dict, raid_events: list[str] | None = None) -> dict:
        cfg = self._load()
        changed: dict[str, object] = {}

        autofeed = payload.get("autofeed") or {}
        for key, value in autofeed.items():
            spec_key = ("Autofeed", _canonical(key))
            if spec_key not in SPEC:
                raise GameplayError(f"unknown autofeed setting {key!r}")
            text = self._coerce(spec_key, value)
            cfg.set(*spec_key, text)
            changed[f"autofeed.{key}"] = text

        raids = payload.get("raids") or {}
        if "enabled" in raids:
            text = self._coerce(("Raids", "Enabled"), raids["enabled"])
            cfg.set("Raids", "Enabled", text)
            changed["raids.enabled"] = text
        if "disabled" in raids:
            names = raids["disabled"]
            if isinstance(names, str):
                names = [n.strip() for n in names.split(",") if n.strip()]
            if not isinstance(names, list):
                raise GameplayError("raids.disabled must be a list of event names")
            cleaned: list[str] = []
            for name in names:
                token = str(name).strip()
                if not re.match(r"^[a-z0-9_]+$", token):
                    raise GameplayError(f"invalid event name {token!r}")
                if token not in cleaned:
                    cleaned.append(token)
            if raid_events:
                unknown = [n for n in cleaned if n not in raid_events]
                if unknown:
                    raise GameplayError(
                        "unknown raid event(s): " + ", ".join(unknown) +
                        " — pick from the list rather than typing")
            text = ",".join(cleaned)
            cfg.set("Raids", "Disabled", text)
            changed["raids.disabled"] = text

        if not changed:
            return {"changed": {}, "state": self.state(raid_events)}

        # Keep one .bak per save so a bad edit is recoverable.
        try:
            backup = self.path.with_suffix(self.path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(self.path, backup)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(cfg.text(), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            raise GameplayError(f"could not write the gameplay config: {exc}") from exc

        return {"changed": changed, "state": self.state(raid_events)}

    @staticmethod
    def _coerce(spec_key: tuple[str, str], value) -> str:
        kind, lo, hi = SPEC[spec_key]
        section, key = spec_key
        label = f"{section}.{key}"

        if kind == "bool":
            if isinstance(value, bool):
                return _fmt_bool(value)
            if isinstance(value, str) and value.strip().lower() in (
                    "true", "false", "1", "0", "yes", "no", "on", "off"):
                return _fmt_bool(_as_bool(value))
            raise GameplayError(f"{label} must be true or false")

        if kind == "int":
            try:
                ivalue = int(float(value))
            except (TypeError, ValueError):
                raise GameplayError(f"{label} must be a whole number") from None
            if lo is not None and ivalue < lo or hi is not None and ivalue > hi:
                raise GameplayError(f"{label} must be between {lo:g} and {hi:g}")
            return str(ivalue)

        if kind == "float":
            try:
                fvalue = float(value)
            except (TypeError, ValueError):
                raise GameplayError(f"{label} must be a number") from None
            if lo is not None and fvalue < lo or hi is not None and fvalue > hi:
                raise GameplayError(f"{label} must be between {lo:g} and {hi:g}")
            # Trim trailing zeros so the file stays readable.
            return f"{fvalue:g}"

        # str / list
        text = str(value).strip()
        if "\n" in text or "\r" in text:
            raise GameplayError(f"{label} must be a single line")
        if len(text) > 200:
            raise GameplayError(f"{label} is too long")
        return text


def _canonical(key: str) -> str:
    """Accept snake_case from the API, map to the plugin's PascalCase keys."""
    mapping = {
        "enabled": "Enabled", "sign_prefix": "SignPrefix", "sign_radius": "SignRadius",
        "range": "Range", "interval_seconds": "IntervalSeconds", "debug": "Debug",
    }
    if key in mapping:
        return mapping[key]
    # Already canonical?
    if key in SPEC_KEYS:
        return key
    raise GameplayError(f"unknown autofeed setting {key!r}")


SPEC_KEYS = {k for (_s, k) in SPEC}


def reload_mode_from_log(lines: list[str]) -> str | None:
    """Report how the plugin last picked up a config change.

    `watcher` is the fast path; `polled` means the FileSystemWatcher missed the
    event and the 2 s fallback caught it. A run of `polled` is a known Mono/Linux
    issue worth showing rather than hiding.
    """
    for line in reversed(lines):
        low = line.lower()
        if "config reloaded (watcher)" in low:
            return "watcher"
        if "config reloaded (polled)" in low:
            return "polled"
    return None
