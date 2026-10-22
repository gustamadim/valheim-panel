#!/usr/bin/env python3
"""Discord relay for the Valheim panel.

Called by the Gladys Discord agent through a single allowlisted command:

    python3 <workspace>/valheim-panel/deploy/discord-relay.py <verb> [args...]

Design constraints
------------------
* **Fixed verb list.** Only the verbs below exist. This is the whole surface a
  Discord user can reach, so it is deliberately tiny.
* **Discord-side authorisation.** The caller passes the invoker's Discord role
  IDs; the script maps them to a panel role via `relay-roles.json`. A member with
  no mapped role gets a refusal, not a default.
* **Panel-side authorisation.** The script authenticates to the panel as a
  dedicated `relay` account, so every action is audited under that identity.

Reads go through the panel's own API, mutations through a real session with CSRF,
so the panel's checks stay in force. The script never bypasses the panel.

Trust note: Gladys is the only caller and she reports role IDs she observed. This
is not a cryptographic boundary — it is a convenience boundary in front of an
already authenticated panel. Acceptable because the panel is LAN-only and every
action lands in its audit log.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE = os.environ.get("VPANEL_URL", "http://127.0.0.1:8100")
HERE = Path(__file__).resolve().parent
ROLES_FILE = HERE / "relay-roles.json"
SECRET_FILE = Path.home() / ".valheim-panel-relay.env"
GUILD_ID = os.environ.get("VPANEL_GUILD", "648742938117144586")

VERBS = {
    "status":    {"role": "user",  "desc": "server status, join code, players"},
    "players":   {"role": "user",  "desc": "online players with positions"},
    "world":     {"role": "user",  "desc": "in-game day, weather, progression"},
    "broadcast": {"role": "mod",   "desc": "message all players"},
    "say":       {"role": "mod",   "desc": "chat message from the server"},
    "kick":      {"role": "mod",   "desc": "kick a player"},
    "restart":   {"role": "admin", "desc": "restart the server"},
    "help":      {"role": "user",  "desc": "list available commands"},
}

RANK = {"user": 1, "mod": 2, "admin": 3}


def fail(message: str, code: int = 1) -> None:
    print(message)
    sys.exit(code)


def load_secret() -> dict:
    if not SECRET_FILE.is_file():
        fail("relay is not configured (missing credential file)")
    out: dict[str, str] = {}
    for line in SECRET_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    if not any(out.get(f"RELAY_{r.upper()}_PASSWORD") for r in ("user", "mod", "admin")):
        fail("relay credential file has no RELAY_<ROLE>_PASSWORD entries")
    return out


def load_roles() -> dict:
    if not ROLES_FILE.is_file():
        return {}
    try:
        return json.loads(ROLES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def role_for(discord_roles: list[str]) -> str | None:
    """Highest panel role granted by any of the invoker's Discord roles."""
    mapping = load_roles()
    best: str | None = None
    for rid in discord_roles:
        mapped = mapping.get(str(rid).strip())
        if mapped and (best is None or RANK.get(mapped, 0) > RANK.get(best, 0)):
            best = mapped
    return best


def fetch_member_roles(user_id: str, token: str) -> list[str]:
    """Look up a guild member's role IDs from Discord itself.

    This is the security-critical part. An earlier version of this script
    accepted role IDs from the caller (Gladys), which meant a prompt injection
    could have her claim the Admin role and it would be believed. Role IDs are
    now read from Discord, so the claim "I am an admin" is verified against the
    guild rather than trusted.

    The remaining trust assumption is the *user id*: Gladys relays which user
    asked. That is a weaker boundary than a signature, so this relay stays a
    convenience layer in front of an already-authenticated panel, and every
    action is still audited. A fully verified path would need an OpenClaw
    tool-policy plugin using ctx.requester.roleIds, which the host derives.
    """
    if not token:
        raise RuntimeError("no Discord bot token configured for role lookup")
    url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{user_id}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bot {token}",
                      "User-Agent": "DiscordBot (valheim-panel, 1.0)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("roles") or []


class Panel:
    def __init__(self, username: str, password: str, actor: str = "") -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.username = username
        self.password = password
        self.actor = actor
        self.csrf: str | None = None

    def _request(self, path: str, method: str = "GET", body: dict | None = None,
                 timeout: float = 60.0):
        data = json.dumps(body).encode() if body is not None else None
        headers = {}
        if data:
            headers["Content-Type"] = "application/json"
        if self.actor:
            # Lets the panel's audit log record who asked, not just the service
            # account that performed it.
            headers["X-Panel-Actor"] = self.actor
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, OSError) as exc:
            return 0, {"error": f"panel unreachable: {exc}"}

    def login(self) -> bool:
        body = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}).encode()
        req = urllib.request.Request(
            BASE + "/api/v1/login", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener.open(req, timeout=20) as resp:
                ok = resp.status == 200
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return False
        if not ok:
            return False
        status, data = self._request("/api/v1/session")
        if status != 200:
            return False
        self.csrf = data.get("csrf")
        return bool(self.csrf)

    def get(self, path: str):
        return self._request(path)

    def post(self, path: str, body: dict):
        return self._request(path, "POST", {**body, "csrf": self.csrf})


def cmd_status(panel: Panel, _args: list[str]) -> None:
    status, data = panel.get("/api/v1/snapshot")
    if status != 200:
        fail(f"could not read status: {data.get('error')}")
    sup = data.get("supervisor") or {}
    agent = data.get("agent") or {}
    world = agent.get("world") or {}
    log = data.get("log") or {}
    players = agent.get("players") or []

    lines = [f"**{world.get('name') or '?'}** · {(sup.get('state') or '?').lower()}"]
    code = log.get("join_code")
    if code:
        seen = log.get("join_code_seen_at") or ""
        lines.append(f"Join code: **{code}**" + (f" (set {_age(seen)})" if seen else ""))
    if agent.get("reachable"):
        names = ", ".join(p.get("name", "?") for p in players) or "nobody"
        lines.append(f"Players {len(players)}: {names}")
        lines.append(f"Day {world.get('day')} · {world.get('weather') or '?'}")
    else:
        lines.append(f"Players ~{log.get('online_count')} (agent offline)")
    print("\n".join(lines))


def cmd_players(panel: Panel, _args: list[str]) -> None:
    status, data = panel.get("/api/v1/players")
    if status != 200:
        fail(f"could not read players: {data.get('error')}")
    players = data.get("players") or []
    if not players:
        print("Nobody online.")
        return
    out = []
    for p in players:
        pos = p.get("position") or {}
        coord = ""
        if pos:
            coord = f" ({round(pos.get('x', 0))}, {round(pos.get('z', 0))})"
        biome = f" · {p['biome']}" if p.get("biome") else ""
        out.append(f"• {p.get('name', '?')}{coord}{biome}")
    print("\n".join(out))


def cmd_world(panel: Panel, _args: list[str]) -> None:
    status, data = panel.get("/api/v1/world")
    if status != 200:
        fail(f"could not read the world: {data.get('error')}")
    world = data.get("world") or {}
    keys = [k for k in (data.get("global_keys") or []) if not _is_modifier(k)]
    lines = [
        f"Day {world.get('day')} · {world.get('weather') or '?'}"
        + (" · night" if world.get("is_night") else ""),
    ]
    if world.get("event"):
        lines.append(f"Active event: {world['event'].get('name')}")
    if keys:
        pretty = ", ".join(k.replace("defeated_", "").replace("_", " ") for k in keys)
        lines.append(f"Progression: {pretty}")
    print("\n".join(lines))


def cmd_broadcast(panel: Panel, args: list[str]) -> None:
    message = " ".join(args).strip()
    if not message:
        fail("usage: broadcast <message>")
    if len(message) > 200:
        fail("message is too long (max 200 characters)")
    status, data = panel.post("/api/v1/commands/run",
                              {"name": "broadcast", "args": {"message": message}})
    if status != 200 or not data.get("ok"):
        fail(f"broadcast failed: {data.get('error') or data.get('output')}")
    print("Broadcast sent.")


def cmd_say(panel: Panel, args: list[str]) -> None:
    message = " ".join(args).strip()
    if not message:
        fail("usage: say <message>")
    if len(message) > 200:
        fail("message is too long (max 200 characters)")
    status, data = panel.post("/api/v1/commands/run",
                              {"name": "say", "args": {"message": message}})
    if status != 200 or not data.get("ok"):
        fail(f"say failed: {data.get('error') or data.get('output')}")
    print("Sent to chat.")


def cmd_kick(panel: Panel, args: list[str]) -> None:
    if not args:
        fail("usage: kick <player name>")
    target = " ".join(args).strip()
    status, data = panel.post("/api/v1/commands/run",
                              {"name": "kick", "args": {"player": target}})
    if status != 200 or not data.get("ok"):
        fail(f"kick failed: {data.get('error') or data.get('output')}")
    print(f"Kicked {target}.")


def cmd_restart(panel: Panel, _args: list[str]) -> None:
    status, data = panel.get("/api/v1/snapshot")
    if status == 200:
        players = len((data.get("agent") or {}).get("players") or [])
        if players:
            fail(f"{players} player(s) are online — refusing to restart from chat. "
                 "Use the panel if you really mean it.")
    status, data = panel.post("/api/v1/server/restart", {})
    if status != 200:
        fail(f"restart failed: {data.get('error')}")
    print(data.get("message") or "Restarting.")


def cmd_help(_panel: Panel | None, _args: list[str]) -> None:
    for verb, meta in VERBS.items():
        if verb == "help":
            continue
        print(f"  {verb:10} {meta['desc']}  [{meta['role']}]")


def _age(iso: str) -> str:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _is_modifier(key: str) -> bool:
    return bool(key) and key[0].islower() and any(c.isupper() for c in key)


HANDLERS = {
    "status": cmd_status, "players": cmd_players, "world": cmd_world,
    "broadcast": cmd_broadcast, "say": cmd_say, "kick": cmd_kick,
    "restart": cmd_restart, "help": cmd_help,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        cmd_help(None, [])
        return 0

    verb = argv[1].strip().lower()
    args = argv[2:]

    if verb not in VERBS:
        print(f"unknown command {verb!r}")
        cmd_help(None, [])
        return 2

    # `help` is intentionally reachable without a role: listing what exists is
    # how someone discovers they need one, and it touches nothing.
    if verb == "help":
        cmd_help(None, [])
        return 0

    user_id = ""
    if "--user-id" in args:
        idx = args.index("--user-id")
        try:
            user_id = args[idx + 1].strip()
            del args[idx:idx + 2]
        except IndexError:
            print("--user-id needs a value")
            return 2

    # Legacy/diagnostic path: explicit role IDs. Refused unless the caller proves
    # it is the operator by running as the same OS user; otherwise anyone who can
    # invoke this script could claim the Admin role.
    claimed_roles: list[str] = []
    if "--roles" in args:
        idx = args.index("--roles")
        try:
            claimed_roles = [r.strip() for r in args[idx + 1].split(",") if r.strip()]
            del args[idx:idx + 2]
        except IndexError:
            print("--roles needs a comma-separated value")
            return 2

    actor = "unknown"
    if "--actor" in args:
        idx = args.index("--actor")
        try:
            actor = args[idx + 1].strip()
            del args[idx:idx + 2]
        except IndexError:
            print("--actor needs a value")
            return 2

    required = VERBS[verb]["role"]
    secret = load_secret()

    if user_id:
        try:
            roles = fetch_member_roles(user_id, secret.get("DISCORD_BOT_TOKEN", ""))
        except Exception as exc:  # noqa: BLE001
            print(f"could not verify your Discord roles: {exc}")
            return 4
        granted = role_for(roles)
    elif claimed_roles:
        # Only the operator's own shell may assert roles directly.
        if os.getuid() != os.stat(SECRET_FILE).st_uid:
            print("role assertions are not accepted from this caller; pass --user-id")
            return 3
        granted = role_for(claimed_roles)
    else:
        print("missing --user-id (who is asking?)")
        return 2

    if granted is None:
        print(f"{actor or 'you'} has no Valheim panel role assigned.")
        return 3
    if RANK[granted] < RANK[required]:
        print(f"{verb} needs **{required}**; {actor or 'you'} is **{granted}**.")
        return 3

    panel = Panel(f"relay-{granted}", secret.get(f"RELAY_{granted.upper()}_PASSWORD", ""),
                  actor=actor)
    if not panel.login():
        print("could not authenticate to the panel — relay account missing or "
              "the panel is down")
        return 4

    print(f"[{actor or 'unknown'} → {granted}] {verb}")
    try:
        HANDLERS[verb](panel, args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - relay must not traceback at users
        print(f"{verb} failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
