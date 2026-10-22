"""Crossplay compatibility classification.

There is no machine-readable crossplay flag in either store. Authors self-report
via categories (`Server-only`, `Server-side`, `Client-side`, `Client-only`) and
free text. This module therefore *classifies by evidence* and always keeps the
evidence string, so the UI can show why a verdict was reached and the operator
can override it.

Tiers, strongest first:

    server-only    Hexium `Server-only`, or Thunderstore `Server-side` with no
                   client tag. Runs entirely on the server; console clients
                   (Xbox/PS/Switch) are unaffected.
    declared-safe  Free text explicitly claims vanilla/console compatibility.
                   Still allowed, but labelled as an author claim, not proof.
    unknown        No server/client tags either way. Allowed with a warning.
    client-required  A client tag without a server tag. Every player must
                   install it; with crossplay on, console clients cannot.

Honesty requirement: a `server-only` tag is *not* a guarantee. A server mod that
injects a network message console clients cannot parse will still break them, and
no server-side signal reveals it. The UI must say so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TIER_ORDER = {"server-only": 0, "declared-safe": 1, "unknown": 2, "client-required": 3}

TIER_LABEL = {
    "server-only": "Server-only",
    "declared-safe": "Server-only (author claims console-safe)",
    "unknown": "Unclassified",
    "client-required": "Needs client install",
}

TIER_HINT = {
    "server-only": "Tagged server-side only. Players install nothing.",
    "declared-safe": "The author states vanilla/console clients work. That is a claim, not a guarantee.",
    "unknown": "No server/client tags. It may still need client installs — check the mod page.",
    "client-required": "Every player must install this. Console clients cannot, so crossplay players will fail to join.",
}

# Phrases that indicate the author explicitly considered non-modded clients.
_SAFE_PATTERNS = [
    r"vanilla clients?",
    r"console clients?",
    r"xbox|playstation|\bps[45]\b|switch",
    r"serverside[- ]only",
    r"server[- ]side only",
    r"no client (?:mod|install|requirement)",
    r"clients? (?:need|install) nothing",
    r"players? (?:need|install) nothing",
    r"compatible with (?:vanilla|unmodded)",
]

_CLIENT_TAGS = {"client-side", "client-only", "client side", "client only"}
_SERVER_TAGS = {"server-side", "server-only", "server side", "server only"}


@dataclass
class Verdict:
    tier: str
    evidence: list[str] = field(default_factory=list)
    crossplay_ok: bool = True

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "label": TIER_LABEL[self.tier],
            "hint": TIER_HINT[self.tier],
            "evidence": self.evidence,
            "crossplay_ok": self.crossplay_ok,
        }


def _tags(categories: list[str]) -> tuple[bool, bool]:
    low = {c.strip().lower() for c in categories}
    has_client = bool(low & _CLIENT_TAGS)
    has_server = bool(low & _SERVER_TAGS)
    return has_server, has_client


def classify(categories: list[str], description: str = "",
             readme: str = "") -> Verdict:
    has_server, has_client = _tags(categories)
    evidence: list[str] = []

    server_tags = [c for c in categories if c.strip().lower() in _SERVER_TAGS]
    client_tags = [c for c in categories if c.strip().lower() in _CLIENT_TAGS]
    if server_tags:
        evidence.append("categories: " + ", ".join(server_tags))
    if client_tags:
        evidence.append("categories: " + ", ".join(client_tags))

    text = f"{description}\n{readme}".lower()
    claims: list[str] = []
    for pat in _SAFE_PATTERNS:
        m = re.search(pat, text)
        if m:
            claims.append(m.group(0))
    if claims:
        evidence.append("text claims: " + ", ".join(sorted(set(claims))[:3]))

    # A client tag without a server tag is the only hard mismatch.
    if has_client and not has_server:
        return Verdict("client-required", evidence, crossplay_ok=False)

    if has_server:
        if claims:
            return Verdict("declared-safe", evidence, crossplay_ok=True)
        return Verdict("server-only", evidence, crossplay_ok=True)

    if claims:
        # Claims console safety but no server tag: still unproven, but better
        # than silence. Treat as declared-safe so it is not buried as unknown.
        return Verdict("declared-safe", evidence, crossplay_ok=True)

    return Verdict("unknown", evidence, crossplay_ok=True)


def policy_decision(verdict: Verdict, crossplay_enabled: bool) -> dict:
    """What the UI should do about this package right now.

    Owner decision: never pre-disable. Only `client-required` earns a hard
    warning, and even that is a warning — installation stays possible so an
    operator can knowingly break crossplay for a private session.
    """
    if not crossplay_enabled:
        return {"warn": verdict.tier == "client-required",
                "level": "info" if verdict.tier != "client-required" else "warn",
                "reason": "crossplay is off; client mods are fine as long as everyone installs them"}

    if verdict.tier == "client-required":
        return {"warn": True, "level": "danger",
                "reason": "crossplay is on and this needs a client install — console players cannot join"}
    if verdict.tier == "unknown":
        return {"warn": True, "level": "warn",
                "reason": "crossplay is on and this mod is unclassified; it may need client installs"}
    return {"warn": False, "level": "ok",
            "reason": "crossplay-compatible by tag/claim"}
