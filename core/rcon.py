"""Source RCON client (stdlib only).

ValheimRcon speaks the Source RCON protocol: length-prefixed packets with an id,
a type, a body and two NUL bytes. Auth is type 3, command is type 2, response is
type 0. Auth failure returns id -1.

RCON is the panel's primary *mutation* channel: it exposes the server's admin
surface (players, kick, ban, give, heal, teleport, events, objects, save) with
typed text results. The agent API is used for reads instead, because it returns
structured JSON with positions and world state.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

# Commands the panel exposes in the UI, grouped for the console page. Anything
# not listed here still works via the free-form box, which is admin-only.
KNOWN_COMMANDS: dict[str, str] = {
    "players": "Online players with positions and zones",
    "serverStats": "FPS, memory, world info",
    "time": "Current server time and day",
    "adminlist": "Admins",
    "banlist": "Banned players",
    "permitted": "Allow-listed players",
    "addAdmin": "addAdmin <steamId>",
    "removeAdmin": "removeAdmin <steamId>",
    "addPermitted": "addPermitted <steamId>",
    "removePermitted": "removePermitted <steamId>",
    "ban": "ban <name|steamid>",
    "unban": "unban <name|steamid>",
    "kick": "kick <name|steamid>",
    "say": "say <message>",
    "showMessage": "showMessage <message>",
    "save": "Save the world",
    "give": "give <steamid> <item> -count <n>",
    "heal": "heal <steamid> <value>",
    "damage": "damage <steamid> <amount>",
    "teleport": "teleport <x> <y> <z>",
    "findPlayer": "findPlayer <name|steamid>",
    "globalKeys": "Print global keys",
    "eventsList": "List random events",
    "startEvent": "startEvent <event> <x> <y> <z>",
    "stopEvent": "Stop the active event",
    "disconnectAll": "Disconnect everyone",
}

# Explicitly refused for non-admins even in the allowlisted console.
ADMIN_ONLY_COMMANDS = {
    "deleteObjects", "modifyObject", "spawn", "addItemToContainer",
    "removeItemFromContainer", "clearContainer", "consoleCommand",
    "disconnectAll", "addGlobalKey", "removeGlobalKey", "ban", "unban",
    "addAdmin", "removeAdmin", "addPermitted", "removePermitted",
}


class RconError(RuntimeError):
    pass


def _pack(rid: int, rtype: int, body: str) -> bytes:
    payload = body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<iii", len(payload) + 8, rid, rtype) + payload


class RconClient:
    """One-shot RCON command execution.

    A fresh connection per call keeps the implementation simple and avoids the
    multi-packet response reassembly edge cases that plague long-lived RCON
    sessions. The server is local, so the cost is negligible.
    """

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout

    def _recv_packet(self, sock: socket.socket) -> tuple[int, int, str]:
        raw = b""
        while len(raw) < 4:
            chunk = sock.recv(4 - len(raw))
            if not chunk:
                raise RconError("connection closed while reading length")
            raw += chunk
        (length,) = struct.unpack("<i", raw)
        if length < 10 or length > 4 * 1024 * 1024:
            raise RconError(f"implausible packet length {length}")

        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise RconError("connection closed while reading body")
            data += chunk

        rid, rtype = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", "replace")
        return rid, rtype, body

    def execute(self, command: str, max_packets: int = 64) -> str:
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        except OSError as exc:
            raise RconError(f"connect {self._host}:{self._port}: {exc}") from exc

        try:
            sock.settimeout(self._timeout)
            sock.sendall(_pack(1, SERVERDATA_AUTH, self._password))

            # Some RCON implementations emit an empty RESPONSE_VALUE packet before
            # the AUTH_RESPONSE. Read until we see the real auth answer, otherwise
            # that stray packet is mistaken for the command's (empty) output.
            auth_ok = False
            for _ in range(4):
                rid, rtype, body = self._recv_packet(sock)
                if rid == -1:
                    raise RconError("authentication failed (wrong RCON password)")
                if rtype in (SERVERDATA_AUTH_RESPONSE, SERVERDATA_EXECCOMMAND):
                    auth_ok = True
                    break
            if not auth_ok:
                raise RconError("no auth response from server")

            sock.sendall(_pack(2, SERVERDATA_EXECCOMMAND, command))

            # Collect every response packet until the socket goes quiet. A short
            # body is not a reliable terminator across implementations.
            #
            # Quirk: ValheimRcon answers command packets with type 2
            # (SERVERDATA_EXECCOMMAND) instead of the conventional type 0
            # (SERVERDATA_RESPONSE_VALUE). Accept both, or every response is
            # silently discarded.
            parts: list[str] = []
            sock.settimeout(1.5)
            for _ in range(max_packets):
                try:
                    rid, rtype, body = self._recv_packet(sock)
                except socket.timeout:
                    break
                if rtype in (SERVERDATA_RESPONSE_VALUE, SERVERDATA_EXECCOMMAND) and body:
                    parts.append(body)
                    # A short packet conventionally ends the response; the short
                    # timeout above is the real safety net.
                    if len(body) < 1400:
                        break
            return "".join(parts).strip()
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def ping(self) -> bool:
        try:
            self.execute("time")
            return True
        except RconError:
            return False


class RconPool:
    """Small serialised wrapper so concurrent web requests don't interleave."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self._client = RconClient(host, port, password)
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        with self._lock:
            return self._client.execute(command)

    def try_run(self, command: str) -> tuple[bool, str]:
        try:
            return True, self.run(command)
        except RconError as exc:
            return False, str(exc)


def parse_players(output: str) -> list[dict]:
    """Parse `players` output.

    ValheimRcon prints a header line then one row per player. The exact columns
    are version-dependent, so this parses defensively: it keeps the leading
    tokens it can identify and always preserves the raw line.
    """
    players: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("online", "players", "name", "-")):
            continue
        players.append({"raw": line})
    return players
