"""Role model.

Three roles, ordered. The panel is LAN-only today, but every mutating route is
already gated so exposing it later is a config change, not a rewrite.

    user  : observe everything read-only
    mod   : + talk to players, kick, back up now, quarantine a mod
    admin : everything, including process control and user management
"""
from __future__ import annotations

ROLE_RANK = {"user": 1, "mod": 2, "admin": 3}
ROLE_LABEL = {"user": "Viewer", "mod": "Moderator", "admin": "Admin"}

# Capability -> minimum role. Keeping this as data (not scattered ifs) makes the
# permission surface auditable in one place.
CAPABILITIES: dict[str, str] = {
    # reads -- everyone signed in
    "view.dashboard": "user",
    "view.logs": "user",
    "view.players": "user",
    "view.worlds": "user",
    "view.backups": "user",
    "view.mods": "user",

    # moderation
    "player.broadcast": "mod",
    "player.message": "mod",
    "player.kick": "mod",
    "backup.create": "mod",
    "mod.quarantine": "mod",

    # administration
    "server.restart": "admin",
    "server.stop": "admin",
    "server.start": "admin",
    "lists.edit": "admin",
    "backup.restore": "admin",
    "backup.delete": "admin",
    "world.switch": "admin",
    "world.regenerate": "admin",
    "world.upload": "admin",
    "world.delete": "admin",
    "rcon.freeform": "admin",
    "player.ban": "admin",
    "player.give": "admin",
    "player.teleport": "admin",
    "mod.install": "admin",
    "mod.uninstall": "admin",
    "settings.edit": "admin",
    "users.manage": "admin",
    "audit.view": "mod",
}


def rank(role: str) -> int:
    return ROLE_RANK.get(role, 0)


def can(role: str, capability: str) -> bool:
    needed = CAPABILITIES.get(capability)
    if needed is None:
        # Unknown capability: deny. Failing closed is the only safe default for a
        # service that can stop a game server.
        return False
    return rank(role) >= rank(needed)


def capabilities_for(role: str) -> list[str]:
    return sorted(c for c, need in CAPABILITIES.items() if can(role, c))


def valid_role(role: str) -> bool:
    return role in ROLE_RANK


def describe() -> list[dict]:
    """Role matrix for the UI."""
    out = []
    for role in ("user", "mod", "admin"):
        out.append({
            "role": role,
            "label": ROLE_LABEL[role],
            "rank": ROLE_RANK[role],
            "capabilities": capabilities_for(role),
        })
    return out
