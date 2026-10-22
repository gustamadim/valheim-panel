"""Package stores: Thunderstore and Hexium, merged.

Both speak the same v1 schema, so one client serves both:

    GET <base>/api/v1/package/  ->  [{
        name, full_name, owner, package_url, date_created, date_updated,
        uuid4, rating_score, is_pinned, is_deprecated, has_nsfw_content,
        categories: [...],
        versions: [{ version_number, description, icon, dependencies: [...],
                     download_url, downloads, date_created, website_url,
                     file_size, is_active }]
    }]

Package identity is `Owner-Name` (`full_name`), which is stable across both
stores — that is what lets the merged view dedupe.

Indexes are large (Thunderstore ~13.7k packages), so they are cached on disk with
a TTL and refreshed in the background rather than on every request.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "valheim-panel/0.6 (+local)"

STORES = {
    "thunderstore": "https://thunderstore.io/c/valheim",
    "hexium": "https://valheim.hexium.gg",
}

INDEX_TTL_S = 6 * 3600


class StoreError(RuntimeError):
    pass


def _version_key(v: str) -> tuple:
    """Sort key for a semver-ish string, tolerant of prerelease suffixes."""
    main, _, pre = (v or "").partition("-")
    parts = []
    for chunk in main.split("."):
        m = re.match(r"(\d+)", chunk)
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    # A prerelease sorts *before* the same release version.
    return (*parts[:3], 0 if pre else 1, pre)


@dataclass
class Package:
    full_name: str
    name: str
    owner: str
    version: str
    description: str
    categories: list[str]
    dependencies: list[str]
    download_url: str
    website_url: str
    file_size: int | None
    downloads: int
    date_updated: str
    store: str
    deprecated: bool = False
    icon: str = ""

    @property
    def key(self) -> str:
        return self.full_name.lower()

    def as_dict(self) -> dict:
        return {
            "full_name": self.full_name, "name": self.name, "owner": self.owner,
            "version": self.version, "description": self.description,
            "categories": self.categories, "dependencies": self.dependencies,
            "download_url": self.download_url, "website_url": self.website_url,
            "file_size": self.file_size, "downloads": self.downloads,
            "date_updated": self.date_updated, "store": self.store,
            "deprecated": self.deprecated, "icon": self.icon,
        }


class StoreIndex:
    """Merged, cached view over both stores."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._packages: dict[str, Package] = {}
        self._by_store: dict[str, list[Package]] = {}
        self._loaded_at: float = 0.0
        self._errors: dict[str, str] = {}
        self._refreshing = False

    # ---- fetch -----------------------------------------------------------
    def _cache_file(self, store: str) -> Path:
        return self.cache_dir / f"index-{store}.json"

    def _fetch(self, store: str, base: str) -> list[dict]:
        url = f"{base}/api/v1/package/"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"{store}: {exc}") from exc
        if not isinstance(data, list):
            raise StoreError(f"{store}: unexpected payload")
        return data

    def _read_cache(self, store: str) -> tuple[list[dict] | None, float]:
        path = self._cache_file(store)
        if not path.is_file():
            return None, 0.0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, 0.0
        return payload.get("packages"), float(payload.get("fetched_at") or 0)

    def _write_cache(self, store: str, packages: list[dict]) -> None:
        path = self._cache_file(store)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"fetched_at": time.time(),
                                       "packages": packages}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)

    # ---- loading ---------------------------------------------------------
    def load_cache(self) -> bool:
        """Populate from the on-disk cache only. No network.

        Called at startup so a container restart does not leave the panel with an
        empty index — installs and package lookups resolve through `get()`, which
        would otherwise 404 for every package until a search warmed the index.
        """
        merged: dict[str, Package] = {}
        by_store: dict[str, list[Package]] = {}
        newest = 0.0
        loaded_any = False

        for store in STORES:
            cached, cached_at = self._read_cache(store)
            if not cached:
                continue
            loaded_any = True
            newest = max(newest, cached_at)
            entries = self._build_entries(store, cached)
            by_store[store] = entries
            for pkg in entries:
                self._merge(merged, pkg)

        if not loaded_any:
            return False

        with self._lock:
            self._packages = merged
            self._by_store = by_store
            self._loaded_at = newest
        return True

    def ensure_loaded(self, max_age: float = INDEX_TTL_S) -> None:
        """Make the index usable without ever blocking a request on the network.

        Disk cache load is synchronous (bounded, local); a stale or missing index
        is refreshed in the background. Callers then see either the cached data
        or an empty index with `refreshing: true`, never a hung request.
        """
        with self._lock:
            loaded = bool(self._packages)
            age = (time.time() - self._loaded_at) if self._loaded_at else None

        if loaded and age is not None and age < max_age:
            return

        if not loaded:
            self.load_cache()
            with self._lock:
                loaded = bool(self._packages)
                age = (time.time() - self._loaded_at) if self._loaded_at else None
            if loaded and age is not None and age < max_age:
                return

        self.refresh(background=True)

    def refresh(self, background: bool = False) -> None:
        if background:
            if self._refreshing:
                return
            self._refreshing = True

            def worker() -> None:
                try:
                    self._do_refresh()
                finally:
                    self._refreshing = False

            threading.Thread(target=worker, name="store-refresh", daemon=True).start()
            return
        self._do_refresh()

    def _build_entries(self, store: str, raw: list[dict]) -> list[Package]:
        entries: list[Package] = []
        for item in raw:
            versions = item.get("versions") or []
            if not versions:
                continue
            latest = max(versions, key=lambda v: _version_key(v.get("version_number", "")))
            entries.append(Package(
                full_name=item.get("full_name") or "",
                name=item.get("name") or "",
                owner=item.get("owner") or "",
                version=latest.get("version_number") or "",
                description=(latest.get("description") or "").strip(),
                categories=item.get("categories") or [],
                dependencies=latest.get("dependencies") or [],
                download_url=latest.get("download_url") or "",
                website_url=latest.get("website_url") or "",
                file_size=latest.get("file_size"),
                downloads=latest.get("downloads") or 0,
                date_updated=item.get("date_updated") or "",
                store=store,
                deprecated=bool(item.get("is_deprecated")),
                icon=latest.get("icon") or "",
            ))
        return entries

    @staticmethod
    def _merge(merged: dict[str, Package], pkg: Package) -> None:
        """Same package in both stores: keep the higher live version."""
        existing = merged.get(pkg.key)
        if existing is None:
            merged[pkg.key] = pkg
            return
        if existing.deprecated and not pkg.deprecated:
            merged[pkg.key] = pkg
        elif pkg.deprecated == existing.deprecated:
            if _version_key(pkg.version) > _version_key(existing.version):
                merged[pkg.key] = pkg

    def _do_refresh(self) -> None:
        merged: dict[str, Package] = {}
        by_store: dict[str, list[Package]] = {}

        for store, base in STORES.items():
            cached, cached_at = self._read_cache(store)
            raw = None
            # Serve the cache immediately if it is not ancient, then refresh.
            if cached and (time.time() - cached_at) < INDEX_TTL_S * 24:
                raw = cached
            if raw is None:
                try:
                    raw = self._fetch(store, base)
                    self._write_cache(store, raw)
                    self._errors.pop(store, None)
                except StoreError as exc:
                    self._errors[store] = str(exc)
                    if cached:
                        raw = cached
                    else:
                        continue

            entries = self._build_entries(store, raw)
            by_store[store] = entries
            for pkg in entries:
                self._merge(merged, pkg)

        with self._lock:
            self._packages = merged
            self._by_store = by_store
            self._loaded_at = time.time()

    # ---- queries ---------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "packages": len(self._packages),
                "per_store": {s: len(v) for s, v in self._by_store.items()},
                "loaded_at": self._loaded_at,
                "age_seconds": time.time() - self._loaded_at if self._loaded_at else None,
                "errors": dict(self._errors),
                "refreshing": self._refreshing,
            }

    def get(self, full_name: str) -> Package | None:
        with self._lock:
            return self._packages.get((full_name or "").lower())

    def search(self, query: str = "", *, categories: list[str] | None = None,
               store: str | None = None, limit: int = 60,
               server_only: bool = False) -> list[Package]:
        q = (query or "").strip().lower()
        cats = {c.lower() for c in (categories or [])}
        with self._lock:
            items = list(self._packages.values())

        out: list[Package] = []
        for pkg in items:
            if store and pkg.store != store:
                continue
            if server_only:
                low = {c.lower() for c in pkg.categories}
                if "server-only" not in low and "server-side" not in low:
                    continue
            if cats and not cats.issubset({c.lower() for c in pkg.categories}):
                continue
            if q:
                hay = f"{pkg.full_name} {pkg.description}".lower()
                if q not in hay:
                    continue
            out.append(pkg)

        # Rank: exact name match, then download count, then recency.
        def rank(p: Package):
            exact = 0 if p.name.lower() == q else 1
            return (exact, -p.downloads, p.date_updated)

        out.sort(key=rank)
        return out[:limit]

    def all_categories(self) -> list[str]:
        with self._lock:
            counts: dict[str, int] = {}
            for pkg in self._packages.values():
                for c in pkg.categories:
                    counts[c] = counts.get(c, 0) + 1
        return [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    def resolve_dependencies(self, full_name: str) -> list[Package]:
        """Transitive dependencies, BepInEx pack excluded (the image provides it).

        Returns dependencies in install order (deepest first), de-duplicated.
        A dependency that is not in either index is reported by raising, because
        installing a plugin with a missing library produces a silent no-op at
        load time that is painful to diagnose.
        """
        root = self.get(full_name)
        if root is None:
            raise StoreError(f"package not found: {full_name}")

        seen: dict[str, Package] = {}
        missing: list[str] = []

        def walk(pkg: Package, depth: int = 0) -> None:
            if depth > 8:
                return
            for dep in pkg.dependencies:
                # "Owner-Name-1.2.3" -> "Owner-Name"
                dep_full = _strip_version(dep)
                if not dep_full:
                    continue
                low = dep_full.lower()
                if low in seen:
                    continue
                # BepInEx itself is installed and managed by the game image.
                if "bepinexpack" in low:
                    continue
                found = self.get(dep_full)
                if found is None:
                    missing.append(dep_full)
                    continue
                seen[low] = found
                walk(found, depth + 1)

        walk(root)
        if missing:
            raise StoreError(
                "missing dependencies not in either store index: " + ", ".join(sorted(set(missing))))
        # Deepest-first: a dict preserves insertion order, and walking the root
        # last means its dependencies are already present.
        return list(seen.values())


def _strip_version(dep: str) -> str:
    """`Owner-Name-1.2.3` -> `Owner-Name`. Owner and name have no hyphens by
    convention, but be defensive: strip only a trailing semver."""
    if not dep:
        return ""
    m = re.match(r"^(.+?)-(\d+(?:\.\d+)*(?:-[A-Za-z0-9.]+)?)$", dep)
    return m.group(1) if m else dep
