# Valheim Control Panel

A self-hosted web panel for a Valheim dedicated server running in Docker. Built to
sit *beside* an existing [`community-valheim-tools/valheim-server`](https://github.com/community-valheim-tools/valheim-server-docker)
container — it does not replace it, does not move your world, and does not need the
Docker socket.

## Why this exists

Every Valheim web UI I found either installs and owns its own server, or requires
every player to install a client mod. This one attaches to a server that is already
running and never asks players to install anything.

It talks to the server over three channels that are already available:

| Channel | Used for | Why |
|---|---|---|
| Supervisor XML-RPC (`:8088`) | start / stop / restart, process state | already running, authenticated, needs no extra privilege |
| RCON (`:25575`) | admin commands, player lists | out-of-band over TCP, works with crossplay |
| ValheimUI Agent (`:2456`) | players with positions, world state, deaths, map layers | structured JSON, no log scraping |

**No Docker socket. No privileged container. No shell interpolation.**

## Features

- **Dashboard** — join code with its age, online players, RAM, in-game day and
  weather, last save, backup count
- **Live log** — server-sent events with filters for joins, saves, errors, mods
- **Users and roles** — `user` / `mod` / `admin`, 30 capabilities, full audit log
  including denied attempts
- **Server control** — restart, stop, start, save; restart syncs plugins
- **Player lists** — admin, banned, permitted, with SteamID64 validation
- **Backups** — create, download, delete, restore with a pre-restore snapshot
- **Worlds** — regenerate, delete inactive, download; the active world is protected
- **Command console** — a 25-command catalog plus an admin-only free-form RCON box
- **Live world** — positions, biome, progression keys, deaths, event timeline
- **Mod manager** — Thunderstore + Hexium in one merged view, dependency
  resolution, crossplay compatibility classification, quarantine, rollback
- **Gameplay helpers** — autofeed and raid filter, hot-reloaded without a restart
- **Live map** — pan/zoom canvas map with fog of war, portals, ships, boss altars
- **Discord bridge** — optional; lets a Discord bot query and control the server
  with roles verified against the guild

## Requirements

- A running [`community-valheim-tools/valheim-server`](https://github.com/community-valheim-tools/valheim-server-docker)
  container with `BEPINEX=true`. This project was formerly published as
  `lloesche/valheim-server`; the old name still resolves to the same repository,
  and older `ghcr.io/lloesche/valheim-server` tags still work.
- Three server-side BepInEx plugins: `ValheimRcon`, `ValheimUI.Agent`,
  `ValheimUI.Gameplay`
- Docker and Docker Compose

## Quick start

```bash
git clone <this-repo> valheim-panel
cd valheim-panel

cp .env.example panel.env
# Edit panel.env — every secret below must be filled in.

docker compose up -d --build
```

Then open `http://<host>:8100` and sign in with `BOOTSTRAP_ADMIN_USER` and
`BOOTSTRAP_ADMIN_PASSWORD`. The bootstrap account is created only when the
database has no users, so a restart cannot resurrect it.

### Generating secrets

```bash
# Session secret and status token
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# Admin password hash — MUST be base64-encoded
python3 -c "
import base64, sys
sys.path.insert(0, '.')
from core.auth import hash_password
print(base64.b64encode(hash_password('your-password').encode()).decode())
"
```

**Why base64?** Docker Compose interpolates `$` inside `env_file` values, treating
`$word` as a variable reference. A raw `pbkdf2_sha256$260000$salt$hash` string is
silently truncated to nonsense and every login fails with a confusing error. The
base64 form contains no `$`, so nothing can rewrite it. This cost me an hour; it
is documented here so it does not cost you one.

## Host networking

The panel runs with `network_mode: host` on purpose. The agent binds `127.0.0.1`,
and supervisor and RCON are host-loopback services. A bridge network would need the
agent's bind address rewritten plus three gateway hops.

## The join code

The game prints its join code once per start, and the image's log filter strips it,
so supervisor's log does not contain it. `deploy/hostprobe.sh` runs on a systemd
timer and writes it, with its timestamp, to `/config/cache/hostprobe.json`.

That script is the only privileged read the panel depends on. Install it with:

```bash
systemctl --user enable --now valheim-hostprobe.timer
```

## Mods and crossplay

Neither store has a machine-readable crossplay flag, so the panel classifies by
evidence and always shows what the evidence was:

| Tier | Evidence | Behaviour |
|---|---|---|
| `server-only` | `Server-only` / `Server-side` tag | install freely |
| `declared-safe` | text claims vanilla/console support | install, labelled as an author claim |
| `unknown` | no server or client tags | install with a warning |
| `client-required` | client tag, no server tag | requires explicit acknowledgement |

A `server-only` tag is **not** a guarantee: a server mod that injects a network
message console clients cannot parse will still break them, and no server-side
signal reveals it. Quarantine catches crashes, load failures and disconnect storms
— not silent client-side breakage.

## Security notes

- LAN-only by default. Roles are already enforced, so exposing it publicly is a
  config change rather than a rewrite.
- Passwords use PBKDF2-SHA256 at 260,000 rounds.
- Sessions are in-memory by design: a restart logs everyone out, which is correct
  for a service that can stop a game server.
- Every mutation is audited with actor, role, IP and before/after values.
- Path handling is jailed to `/config`; list IDs are validated as 17-digit
  SteamID64s; archives are checked for traversal and symlink members.

## License

MIT. See [LICENSE](LICENSE).

The optional agent, gameplay and map-layer features consume
[`jonasthim/valheim-server-ui`](https://github.com/jonasthim/valheim-server-ui)
plugins, which are MIT licensed, Copyright (c) 2026 Jonas Thim. They are installed
from their official releases and are not vendored here.
