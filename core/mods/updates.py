"""Update checking: the installed manifest against the live store index.

A mod is "updatable" when the merged store index knows a strictly higher
version than the one recorded at install time. Everything else is reported too
— current, ahead of the index, gone from the index, custom-URL imports — so
the operator sees the whole picture instead of a silent empty list.

Pinned mods are compared like everything else (the badge is useful information)
but excluded from the actionable list: pinning is the operator's explicit "do
not touch this".
"""
from __future__ import annotations

import time

from .install import ModManager
from .stores import StoreIndex, _version_key


def check_updates(manager: ModManager, index: StoreIndex) -> dict:
    """Compare every managed mod against the merged store index.

    Never raises on store hiccups: a store error is reported per-mod so one
    broken index cannot hide the rest of the list.
    """
    index.ensure_loaded()

    installed = {m.full_name.lower(): m for m in manager.installed()}
    rows: list[dict] = []
    counts = {"update": 0, "pinned": 0, "current": 0, "ahead": 0,
              "missing": 0, "custom": 0}

    for m in manager.installed():
        row = {
            "full_name": m.full_name,
            "installed_version": m.version,
            "store": m.store,
            "tier": m.tier,
            "pinned": m.pinned,
            "disabled": m.disabled,
            "custom_url": m.custom_url,
            "dependencies": m.dependencies,
        }

        if m.custom_url:
            row.update(status="custom",
                       note="imported from a URL — re-import to replace it")
            counts["custom"] += 1
            rows.append(row)
            continue

        pkg = index.get(m.full_name)
        if pkg is None:
            row.update(status="missing",
                       note="gone from both store indexes — renamed or removed?")
            counts["missing"] += 1
            rows.append(row)
            continue

        cand_version, _cand_url = candidate(pkg, m.version)
        row["latest_version"] = cand_version
        row["latest_raw"] = pkg.version
        row["prerelease_available"] = pkg.version != cand_version
        row["deprecated"] = pkg.deprecated
        row["website_url"] = pkg.website_url

        cmp = _cmp(_version_key(m.version), _version_key(cand_version))
        if cmp < 0:
            row["status"] = "update"
            row["actionable"] = not m.pinned
            counts["update" if not m.pinned else "pinned"] += 1
        elif cmp == 0:
            row.update(status="current")
            counts["current"] += 1
        else:
            row.update(status="ahead",
                       note="installed version is newer than the index — custom build?")
            counts["ahead"] += 1

        # A dependency that itself has a newer version is worth surfacing: the
        # next update of the parent may start requiring it. Same stable-channel
        # rule as the top-level offer.
        drift = []
        for dep in m.dependencies or []:
            dep_pkg = index.get(dep)
            dep_inst = installed.get(dep.lower())
            if dep_pkg is None or dep_inst is None:
                continue
            dep_candidate, _ = candidate(dep_pkg, dep_inst.version)
            if _cmp(_version_key(dep_inst.version), _version_key(dep_candidate)) < 0:
                drift.append({"full_name": dep,
                              "installed": dep_inst.version,
                              "latest": dep_candidate})
        if drift:
            row["dependency_updates"] = drift

        rows.append(row)

    updates = [r["full_name"] for r in rows if r.get("actionable")]
    return {
        "checked_at": time.time(),
        "store": index.status(),
        "mods": rows,
        "updates": updates,
        "counts": counts,
    }


def candidate(pkg, installed_version: str) -> tuple[str, str]:
    """(version, download_url) an update should offer.

    Stable channel by default: the newest non-prerelease version. If the
    installed version is itself a prerelease, the operator is already riding
    the beta channel, so the newest anything is offered. Falls back to the raw
    latest when a package ships no stable release at all.
    """
    if "-" in (installed_version or ""):
        return pkg.version, pkg.download_url
    return (pkg.stable_version or pkg.version,
            pkg.stable_download_url or pkg.download_url)


def _cmp(a: tuple, b: tuple) -> int:
    return (a > b) - (a < b)
