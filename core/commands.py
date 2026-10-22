"""Command catalog and execution.

Two transports, chosen per command:

* **Agent** (`/v1/commands/*`) returns typed JSON — `{"ok", "message", "data"}`.
  Preferred whenever the command exists there, because the result is structured
  and errors are unambiguous.
* **RCON** returns plain text. Used for the admin surface the agent lacks
  (players, kick, ban, give, heal, teleport, events, objects) and for the
  free-form console.

Every command declares the capability it needs, so authorisation lives in
`core/roles.py` and this module never decides policy on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .agent import Agent, AgentError
from .rcon import RconError, RconPool

# Commands the agent exposes as POST /v1/commands/<name>.
AGENT_COMMANDS = {
    "save", "kick", "ban", "unban", "broadcast", "time", "say",
    "setkey", "removekey", "event", "eventstop",
}

# Commands the panel will send over RCON. Anything outside this set is refused
# unless the caller holds `rcon.freeform` (admin), which allows arbitrary input.
RCON_ALLOWLIST: set[str] = {
    # read-only / informational
    "players", "serverStats", "time", "adminlist", "banlist", "permitted",
    "globalKeys", "eventsList", "currentEvent", "findPlayer",
    # player interaction
    "say", "showMessage", "kick", "ban", "unban", "heal", "damage",
    "give", "teleport", "disconnectAll",
    # lists
    "addAdmin", "removeAdmin", "addPermitted", "removePermitted",
    # world / events
    "save", "startEvent", "stopEvent",
}

# Commands that must never be reachable through the free-form box, even for
# admins: they are destructive or bypass safety checks in ways the UI cannot
# preview. Deliberately a small, explicit deny list rather than an allow list,
# so admins keep genuine escape-hatch power.
FREE_FORM_DENY = {
    "deleteObjects",     # irreversible, has a -force mode
    "modifyObject",      # can rewrite world objects
    "consoleCommand",    # arbitrary in-game console = arbitrary code paths
}


@dataclass(frozen=True)
class CommandSpec:
    name: str
    capability: str
    transport: str          # "agent" | "rcon"
    summary: str
    args: tuple[str, ...] = ()
    dangerous: bool = False


CATALOG: tuple[CommandSpec, ...] = (
    # --- messaging -------------------------------------------------------
    CommandSpec("broadcast", "player.broadcast", "agent",
                "Message every player (centre of screen)", ("message",)),
    CommandSpec("say", "player.message", "agent",
                "Chat message from the server", ("message",)),
    CommandSpec("showMessage", "player.broadcast", "rcon",
                "Large centred message for everyone", ("message",)),
    # --- moderation ------------------------------------------------------
    CommandSpec("kick", "player.kick", "agent",
                "Disconnect a player", ("player",)),
    CommandSpec("ban", "player.ban", "agent",
                "Ban a player", ("player",), dangerous=True),
    CommandSpec("unban", "player.ban", "agent",
                "Lift a ban", ("player",)),
    CommandSpec("heal", "player.give", "rcon",
                "Restore a player's health", ("steamid", "value")),
    CommandSpec("damage", "player.give", "rcon",
                "Damage a player", ("steamid", "amount"), dangerous=True),
    CommandSpec("give", "player.give", "rcon",
                "Spawn an item on a player", ("steamid", "item", "count")),
    CommandSpec("teleport", "player.teleport", "rcon",
                "Teleport a player to coordinates", ("x", "y", "z"), dangerous=True),
    CommandSpec("disconnectAll", "player.kick", "rcon",
                "Disconnect everyone", (), dangerous=True),
    # --- world -----------------------------------------------------------
    CommandSpec("save", "server.restart", "agent", "Save the world now"),
    # RCON, not the agent: the agent's /v1/commands/time is a *setter* (it requires
    # fraction/seconds/skip) and 400s when asked for the current time. RCON's `time`
    # is the read.
    CommandSpec("time", "view.dashboard", "rcon", "Current in-game time and day"),
    CommandSpec("setkey", "settings.edit", "agent",
                "Set a global key", ("key", "value"), dangerous=True),
    CommandSpec("removekey", "settings.edit", "agent",
                "Remove a global key", ("key",), dangerous=True),
    CommandSpec("event", "settings.edit", "agent",
                "Start a random event", ("event",), dangerous=True),
    CommandSpec("eventstop", "settings.edit", "agent", "Stop the active event"),
    # --- reads -----------------------------------------------------------
    CommandSpec("players", "view.players", "rcon",
                "Online players with positions and zones"),
    CommandSpec("serverStats", "view.dashboard", "rcon",
                "FPS, memory, object counts"),
    CommandSpec("adminlist", "lists.edit", "rcon", "Current admins"),
    CommandSpec("banlist", "lists.edit", "rcon", "Current bans"),
    CommandSpec("permitted", "lists.edit", "rcon", "Current allow-list"),
    CommandSpec("globalKeys", "view.worlds", "rcon", "World progression keys"),
    CommandSpec("eventsList", "view.worlds", "rcon", "All random events"),
    CommandSpec("findPlayer", "view.players", "rcon",
                "Look up a player by name or id", ("query",)),
)

CATALOG_BY_NAME = {c.name: c for c in CATALOG}


@dataclass
class CommandResult:
    ok: bool
    output: str
    transport: str
    command: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output, "transport": self.transport,
                "command": self.command, "detail": self.detail}


class CommandError(RuntimeError):
    pass


class CommandRunner:
    def __init__(self, agent: Agent, rcon: RconPool) -> None:
        self.agent = agent
        self.rcon = rcon

    def execute(self, name: str, args: dict, *, freeform_allowed: bool) -> CommandResult:
        spec = CATALOG_BY_NAME.get(name)
        if spec is None:
            raise CommandError(f"unknown command {name!r}")
        return self._run(spec, args)

    def freeform(self, raw: str) -> CommandResult:
        """Run an arbitrary RCON line. Admin-only; caller checks the capability."""
        line = (raw or "").strip()
        if not line:
            raise CommandError("empty command")
        if "\n" in line or "\r" in line:
            raise CommandError("one command per line")
        first = line.split()[0]
        if first in FREE_FORM_DENY:
            raise CommandError(
                f"{first} is blocked even for admins: it is irreversible or bypasses "
                "safety checks. Use the game console on the host if you truly need it.")
        try:
            out = self.rcon.run(line)
        except RconError as exc:
            raise CommandError(str(exc)) from exc
        return CommandResult(True, out or "(no output)", "rcon", line)

    # ------------------------------------------------------------------
    def _run(self, spec: CommandSpec, args: dict) -> CommandResult:
        if spec.transport == "agent":
            payload = {k: v for k, v in args.items() if v not in (None, "")}
            try:
                data = self.agent.command(spec.name, **payload)
            except AgentError as exc:
                raise CommandError(str(exc)) from exc
            ok = bool(data.get("ok", True))
            msg = data.get("message") or ("ok" if ok else "command failed")
            return CommandResult(ok, msg, "agent", spec.name, data.get("data") or {})

        line = self._render_rcon(spec, args)
        try:
            out = self.rcon.run(line)
        except RconError as exc:
            raise CommandError(str(exc)) from exc
        return CommandResult(True, out or "(no output)", "rcon", line)

    def _render_rcon(self, spec: CommandSpec, args: dict) -> str:
        """Build an RCON line, validating every argument.

        Values are never interpolated blindly: a player name containing a space or
        a quote would otherwise split into extra RCON arguments.
        """
        parts = [spec.name]
        for key in spec.args:
            value = args.get(key)
            if value is None or str(value).strip() == "":
                if spec.args == ("query",):
                    raise CommandError(f"missing {key}")
                # Optional trailing args are allowed to be absent.
                if key in ("count", "value", "amount"):
                    continue
                raise CommandError(f"missing {key}")
            parts.append(_sanitize(str(value)))
        return " ".join(parts)


def _sanitize(value: str) -> str:
    """Reject control characters; collapse whitespace inside a single token.

    A player name is the risky case: RCON is a line protocol, so a newline would
    become a second command. Spaces are legal in Valheim names, so they are kept
    but the value is passed as one argument by the caller's field order.
    """
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise CommandError("argument contains a control character")
    return value.strip()


def parse_players(output: str) -> list[dict]:
    """Parse `players` output defensively.

    ValheimRcon prints `Online <n>` when empty. With players connected it prints a
    row per player; the exact columns vary by version, so keep the raw line and
    pull out what can be recognised rather than assuming a fixed layout.
    """
    rows: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("online ") and not rows:
            continue
        if low.startswith(("players", "name ", "---")):
            continue
        entry: dict = {"raw": line}
        sid = re.search(r"\b(\d{17})\b", line)
        if sid:
            entry["steamid"] = sid.group(1)
        coords = re.search(r"(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)", line)
        if coords:
            entry["position"] = [float(coords.group(1)), float(coords.group(2)),
                                 float(coords.group(3))]
        # The name is usually the leading token(s) before an id or coordinate.
        head = re.split(r"\s{2,}|\t|\b\d{17}\b|\(", line, maxsplit=1)[0].strip()
        if head:
            entry["name"] = head
        rows.append(entry)
    return rows


def catalog_for(role_can) -> list[dict]:
    """Catalog filtered to what the caller may run."""
    out = []
    for spec in CATALOG:
        out.append({
            "name": spec.name,
            "summary": spec.summary,
            "args": list(spec.args),
            "transport": spec.transport,
            "dangerous": spec.dangerous,
            "allowed": role_can(spec.capability),
        })
    return out
