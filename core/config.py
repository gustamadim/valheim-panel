"""Configuration loading for the Valheim panel.

Reads a small env file plus process env. No third-party dependencies: the host
has no reliable PyPI access behind its egress proxy, so everything here is stdlib.
"""
from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overwriting real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    # web
    port: int = 8100
    bind: str = "0.0.0.0"
    password_hash: str = ""
    session_secret: str = ""
    lan_only: bool = True
    session_ttl_s: int = 12 * 3600

    # upstreams
    supervisor_url: str = "http://127.0.0.1:8088"
    supervisor_user: str = "admin"
    supervisor_pass: str = ""
    supervisor_program: str = "valheim-server"

    rcon_host: str = "127.0.0.1"
    rcon_port: int = 25575
    rcon_password: str = ""

    agent_url: str = "http://127.0.0.1:2456"
    agent_token: str = ""

    # paths (inside the panel container)
    config_dir: Path = field(default_factory=lambda: Path("/config"))
    probe_file: Path = field(default_factory=lambda: Path("/config/cache/hostprobe.json"))

    # The game writes log timestamps in the container's local time. This must
    # match the Valheim container's TZ or every derived age is wrong.
    game_tz: str = "Asia/Jakarta"

    # Panel state (users, audit, kv). On the /config mount so it survives
    # container rebuilds.
    db_file: Path = field(default_factory=lambda: Path("/config/cache/panel.db"))

    @property
    def worlds_dir(self) -> Path:
        return self.config_dir / "worlds_local"

    @property
    def backups_dir(self) -> Path:
        return self.config_dir / "backups"

    @property
    def plugins_dir(self) -> Path:
        return self.config_dir / "bepinex" / "plugins"

    @property
    def bepinex_config_dir(self) -> Path:
        return self.config_dir / "bepinex"


def _bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _resolve_password_hash(e) -> str:
    """Return the PBKDF2 hash, preferring the base64 form.

    Docker Compose interpolates `$` inside env_file values, which silently
    truncates a `pbkdf2_sha256$<rounds>$<salt>$<hash>` string (it treats `$<word>`
    as a variable reference). Storing the hash base64-encoded avoids the whole
    class of bug: the encoded form contains no `$`, so nothing can be rewritten.
    """
    encoded = e.get("PASSWORD_HASH_B64", "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded).decode()
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(f"PASSWORD_HASH_B64 is not valid base64: {exc}") from exc
    return e.get("PASSWORD_HASH", "")


def load(env_file: str | None = None) -> Config:
    if env_file is None:
        env_file = os.environ.get("PANEL_ENV", "/app/panel.env")
    _load_env_file(Path(env_file))
    e = os.environ

    return Config(
        port=int(e.get("PORT", "8100")),
        bind=e.get("BIND", "0.0.0.0"),
        password_hash=_resolve_password_hash(e),
        session_secret=e.get("SESSION_SECRET", ""),
        lan_only=_bool(e.get("LAN_ONLY", "true"), True),
        session_ttl_s=int(e.get("SESSION_TTL_S", str(12 * 3600))),
        supervisor_url=e.get("SUPERVISOR_URL", "http://127.0.0.1:8088"),
        supervisor_user=e.get("SUPERVISOR_USER", "admin"),
        supervisor_pass=e.get("SUPERVISOR_PASS", ""),
        supervisor_program=e.get("SUPERVISOR_PROGRAM", "valheim-server"),
        rcon_host=e.get("RCON_HOST", "127.0.0.1"),
        rcon_port=int(e.get("RCON_PORT", "25575")),
        rcon_password=e.get("RCON_PASSWORD", ""),
        agent_url=e.get("AGENT_URL", "http://127.0.0.1:2456"),
        agent_token=e.get("AGENT_TOKEN", ""),
        config_dir=Path(e.get("CONFIG_DIR", "/config")),
        probe_file=Path(e.get("PROBE_FILE", "/config/cache/hostprobe.json")),
        game_tz=e.get("GAME_TZ", "Asia/Jakarta"),
        db_file=Path(e.get("DB_FILE", "/config/cache/panel.db")),
    )
