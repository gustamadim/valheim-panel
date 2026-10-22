"""Map data: cached layer/fog PNGs, objects, and players.

Why the panel does not style the map server-side
------------------------------------------------
The agent ships a 2048x2048 RGBA *data* image (biome code, height, forest,
mask), not a picture. Styling it means touching 4.2M pixels; in pure Python that
is seconds per render, and this host has no numpy/PIL by design. The browser does
the same work natively in a canvas in a few milliseconds, and can then pan and
zoom without asking the server again.

So the panel caches the raw PNGs on disk, serves them once with long-lived
caching, and lets `static/map.js` do the styling. The server's job here is
caching, authorisation, and aggregating objects/players — not pixels.

Fog is the one exception: the explored percentage is cheap to derive once and
useful in the API, so it is computed when the fog image changes and cached.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

from .agent import Agent, AgentError
from .mapimg import PngError, decode_fog


class MapError(RuntimeError):
    pass


class MapData:
    def __init__(self, agent: Agent, cache_dir: Path) -> None:
        self.agent = agent
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._layers_key: str | None = None
        self._fog_key: str | None = None
        self._fog_stats: dict = {}
        self._fog_grey: array | None = None
        self._objects: dict = {}
        self._objects_at = 0.0

    # ---- raw agent fetches ----------------------------------------------
    def _fetch(self, path: str, timeout: float = 120.0) -> bytes:
        url = f"{self.agent._base}{path}"  # noqa: SLF001 - same package
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.agent._token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise MapError(f"{path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MapError(f"{path}: {exc}") from exc

    def info(self) -> dict:
        try:
            return self.agent.map_info()
        except AgentError as exc:
            raise MapError(str(exc)) from exc

    # ---- layers ----------------------------------------------------------
    def layers_png(self) -> bytes:
        """Raw layers image, cached per (seed, size)."""
        info = self.info()
        if info.get("state") != "ready" or not info.get("layers"):
            raise MapError(
                f"map layers not ready (state={info.get('state')}, "
                f"progress={info.get('progress')})")
        seed = info.get("seed")
        size = info.get("size")
        key = f"{seed}-{size}"
        path = self.cache_dir / f"layers-{key}.png"

        with self._lock:
            if path.is_file() and self._layers_key == key:
                return path.read_bytes()

        data = self._fetch("/v1/map/layers")
        # A 202-style JSON body means sampling is still running.
        if data[:1] == b"{":
            raise MapError("map layers are still being sampled")

        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise MapError(f"could not cache layers: {exc}") from exc

        with self._lock:
            self._layers_key = key
            # Drop other worlds' layers: they are ~10 MB each.
            for old in self.cache_dir.glob("layers-*.png"):
                if old.name != path.name:
                    old.unlink(missing_ok=True)
        return data

    # ---- fog -------------------------------------------------------------
    def fog_png(self) -> bytes:
        info = self.info()
        seed = info.get("seed")
        path = self.cache_dir / f"fog-{seed}.png"

        with self._lock:
            if path.is_file() and self._fog_key == str(seed):
                return path.read_bytes()

        data = self._fetch("/v1/map/explored")
        if data[:1] == b"{":
            raise MapError("fog mask unavailable")

        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise MapError(f"could not cache fog: {exc}") from exc

        with self._lock:
            self._fog_key = str(seed)
            self._fog_stats = {}
            self._fog_grey = None
        return data

    def fog_stats(self) -> dict:
        """Explored share. Computed once per fog image, not per request."""
        with self._lock:
            if self._fog_stats:
                return dict(self._fog_stats)

        data = self.fog_png()
        digest = hashlib.sha256(data).hexdigest()[:16]
        stat_file = self.cache_dir / f"fogstats-{digest}.json"

        if stat_file.is_file():
            try:
                stats = json.loads(stat_file.read_text())
                with self._lock:
                    self._fog_stats = stats
                return dict(stats)
            except (OSError, json.JSONDecodeError):
                pass

        try:
            w, h, grey = decode_fog(data)
            # 255 = unexplored; anything lower is explored to some degree.
            explored = sum(1 for v in grey if v < 128)
            total = w * h
            stats = {
                "width": w, "height": h,
                "explored_pixels": explored,
                "total_pixels": total,
                "explored_percent": round(100.0 * explored / total, 2) if total else 0.0,
            }
            with self._lock:
                self._fog_grey = grey
        except (PngError, ValueError) as exc:
            stats = {"error": str(exc)}

        try:
            stat_file.write_text(json.dumps(stats))
            for old in self.cache_dir.glob("fogstats-*.json"):
                if old.name != stat_file.name:
                    old.unlink(missing_ok=True)
        except OSError:
            pass

        with self._lock:
            self._fog_stats = stats
        return dict(stats)

    # ---- cartography sampling -------------------------------------------
    def _fog_grid(self) -> tuple[int, int, array] | None:
        """The fog mask as a flat grey array, loaded once per image."""
        with self._lock:
            if self._fog_grey is not None:
                stats = self._fog_stats or {}
                return (stats.get("width"), stats.get("height"), self._fog_grey)
        try:
            self.fog_stats()
        except MapError:
            return None
        with self._lock:
            if self._fog_grey is None:
                return None
            stats = self._fog_stats or {}
            return (stats.get("width"), stats.get("height"), self._fog_grey)

    def explored_at(self, x: float, z: float) -> bool:
        """Is the terrain at world (x, z) revealed on the cartography tables?

        This is the honest test for "may this viewer see this object": it asks the
        same mask that darkens the map, so an icon is visible exactly when the
        ground under it is. Gating on the agent's per-object `explored` flag
        instead produced nonsense — locations never set it at all, so every boss
        altar and trader was hidden even inside explored terrain.
        """
        grid = self._fog_grid()
        if grid is None:
            return False
        gw, gh, grey = grid
        if not gw or not gh:
            return False

        R = 10500.0
        try:
            info = self.info()
            R = float(info.get("world_radius") or R)
        except MapError:
            pass

        u = (x + R) / (2 * R)
        v = (R - z) / (2 * R)
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            return False

        px = min(gw - 1, max(0, int(u * gw)))
        py = min(gh - 1, max(0, int(v * gh)))
        return grey[py * gw + px] < 128

    # ---- objects ---------------------------------------------------------
    def objects(self, max_age: float = 15.0) -> dict:
        now = time.time()
        with self._lock:
            if self._objects and (now - self._objects_at) < max_age:
                return dict(self._objects)
        try:
            data = self.agent.map_objects()
        except AgentError as exc:
            raise MapError(str(exc)) from exc
        with self._lock:
            self._objects = data
            self._objects_at = now
        return dict(data)

    # ---- players ---------------------------------------------------------
    def players(self) -> list[dict]:
        try:
            st = self.agent.status()
        except AgentError:
            return []
        out = []
        for p in st.get("players") or []:
            pos = p.get("position") or {}
            out.append({
                "name": p.get("name"),
                "x": pos.get("x"),
                "y": pos.get("y"),
                "z": pos.get("z"),
                "biome": p.get("biome"),
                "visible": p.get("visible", True),
            })
        return out

    # ---- meta ------------------------------------------------------------
    def meta(self) -> dict:
        info = self.info()
        out = {
            "seed": info.get("seed"),
            "size": info.get("size"),
            "world_radius": info.get("world_radius"),
            "playable_radius": info.get("playable_radius"),
            "sea_level": info.get("sea_level"),
            "layers_ready": info.get("state") == "ready" and bool(info.get("layers")),
            "state": info.get("state"),
            "progress": info.get("progress"),
        }
        try:
            out["fog"] = self.fog_stats()
        except MapError as exc:
            out["fog"] = {"error": str(exc)}
        return out
