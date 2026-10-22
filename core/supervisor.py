"""Supervisor XML-RPC client.

The container's supervisor is a full control plane: process state, start/stop,
and the process log with byte offsets. Using it means the panel never needs the
Docker socket.
"""
from __future__ import annotations

import base64
import socket
import time
import urllib.request
import xmlrpc.client
from typing import Any


class SupervisorError(RuntimeError):
    pass


class _BasicAuthTransport(xmlrpc.client.Transport):
    def __init__(self, user: str, password: str) -> None:
        super().__init__()
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {token}"

    def send_headers(self, connection, headers):  # type: ignore[override]
        connection.putheader("Authorization", self._auth)
        super().send_headers(connection, headers)


class Supervisor:
    def __init__(self, url: str, user: str, password: str, timeout: float = 10.0) -> None:
        self._url = url.rstrip("/") + "/RPC2"
        self._user = user
        self._password = password
        self._timeout = timeout
        self._proxy: xmlrpc.client.ServerProxy | None = None

    def _proxy_for(self) -> xmlrpc.client.ServerProxy:
        if self._proxy is None:
            transport = _BasicAuthTransport(self._user, self._password)
            self._proxy = xmlrpc.client.ServerProxy(self._url, transport=transport, allow_none=True)
        return self._proxy

    def _call(self, method: str, *args: Any) -> Any:
        try:
            return getattr(self._proxy_for(), method)(*args)
        except (socket.error, OSError, xmlrpc.client.ProtocolError, xmlrpc.client.Fault) as exc:
            self._proxy = None  # force a fresh connection next time
            raise SupervisorError(f"{method}: {exc}") from exc

    # ---- reads -----------------------------------------------------------
    def process_info(self, name: str) -> dict:
        return self._call("supervisor.getProcessInfo", name)

    def all_processes(self) -> list[dict]:
        return self._call("supervisor.getAllProcessInfo")

    def read_log(self, name: str, offset: int, length: int = 65536) -> str:
        return self._call("supervisor.readProcessLog", name, offset, length)

    def state(self) -> dict:
        return self._call("supervisor.getState")

    # ---- mutations (used from P3 onward) ---------------------------------
    def stop(self, name: str) -> bool:
        return bool(self._call("supervisor.stopProcess", name))

    def start(self, name: str) -> bool:
        return bool(self._call("supervisor.startProcess", name))

    def signal(self, name: str, sig: str) -> bool:
        return bool(self._call("supervisor.signalProcess", name, sig))

    def run_oneshot(self, name: str, timeout: float = 180.0) -> tuple[bool, str]:
        """Run a one-shot program and wait for it to exit.

        `valheim-bootstrap` is configured without autorestart, so it sits in
        EXITED until something starts it. Starting it is how the image syncs
        /config/bepinex/{plugins,patchers} into the live game tree.
        """
        try:
            self._call("supervisor.startProcess", name)
        except SupervisorError as exc:
            # ALREADY_STARTED means it is running right now; not an error here.
            if "ALREADY_STARTED" not in str(exc).upper():
                return False, str(exc)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                info = self.process_info(name)
            except SupervisorError as exc:
                return False, str(exc)
            state = (info.get("statename") or "").upper()
            if state in ("EXITED", "FATAL"):
                exitstatus = info.get("exitstatus")
                if exitstatus not in (0, None):
                    return False, f"{name} exited with status {exitstatus}"
                return True, state
            time.sleep(1.5)
        return False, f"{name} did not finish within {int(timeout)}s"

    # ---- convenience -----------------------------------------------------
    def log_size(self, name: str) -> int:
        """Byte offset just past the end of the current process log."""
        info = self.process_info(name)
        # supervisor does not expose the size directly; read to EOF in chunks.
        offset = 0
        while True:
            chunk = self.read_log(name, offset, 262144)
            if not chunk:
                return offset
            offset += len(chunk.encode("utf-8", "replace"))
            if len(chunk) < 1024:
                return offset


def http_logtail(url: str, user: str, password: str, process: str,
                 offset: int = 0, length: int = 65536, timeout: float = 10.0) -> tuple[str, int]:
    """Read via supervisor's /logtail endpoint. Returns (text, bytes_consumed)."""
    import urllib.parse

    query = urllib.parse.urlencode({"offset": offset, "length": length})
    full = f"{url.rstrip('/')}/logtail/{urllib.parse.quote(process)}?{query}"
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(full, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    text = body.decode("utf-8", "replace")
    return text, len(body)
