"""Client for the ValheimUI.Agent loopback API.

The agent is a server-side BepInEx plugin (MIT, from jonasthim/valheim-server-ui)
that exposes the running world as JSON: players with positions, day, weather,
global keys, deaths, chat, and raw map layers. It binds 127.0.0.1 and requires a
bearer token.

This is the panel's primary *read* channel: it is structured, includes positions,
and needs no log scraping.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class AgentError(RuntimeError):
    pass


class Agent:
    def __init__(self, base_url: str, token: str, timeout: float = 8.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        data = None
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self._base}{path}", data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AgentError(f"{path}: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AgentError(f"{path}: {exc}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(f"{path}: invalid JSON ({exc})") from exc

    # ---- reads -----------------------------------------------------------
    def status(self) -> dict:
        return self._request("/v1/status")

    def events(self, since: int = 0) -> dict:
        return self._request(f"/v1/events?since={int(since)}")

    def chat(self, since: int = 0, limit: int = 100) -> dict:
        return self._request(f"/v1/chat?since={int(since)}&limit={int(limit)}")

    def catalog(self) -> dict:
        return self._request("/v1/catalog")

    def map_info(self) -> dict:
        return self._request("/v1/map/info")

    def map_layers(self) -> dict:
        return self._request("/v1/map/layers")

    def map_objects(self) -> dict:
        return self._request("/v1/map/objects")

    def map_explored(self) -> dict:
        return self._request("/v1/map/explored")

    def map_explored_info(self) -> dict:
        """Fog metadata: version, mask_version, explored share, updated_at."""
        return self._request("/v1/map/explored/info")

    # ---- commands (structured; preferred over RCON for these) ------------
    def command(self, name: str, **kwargs) -> dict:
        return self._request(f"/v1/commands/{urllib.parse.quote(name)}",
                             method="POST", body=kwargs or {})

    def healthy(self) -> bool:
        try:
            self.status()
            return True
        except AgentError:
            return False
