#!/usr/bin/env python3
"""Valheim control panel — HTTP server.

Phase 2: named users, roles (admin/mod/user), and an audit log.

Read-only game state; user management is the only mutation in this phase.
Server-mutating routes land in P3, already gated by the same capability map.

Run:  python3 panel.py
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import auth, config as config_mod, roles  # noqa: E402
from core.commands import CATALOG_BY_NAME, CommandError, CommandRunner, catalog_for, parse_players  # noqa: E402
from core.db import Database  # noqa: E402
from core.events import EventPoller  # noqa: E402
from core.gameplay import Gameplay, GameplayError, reload_mode_from_log  # noqa: E402
from core.logs import read_probe  # noqa: E402
from core.mapdata import MapData, MapError  # noqa: E402
from core.mods import ModError, ModManager, StoreError, StoreIndex, classify, policy_decision  # noqa: E402
from core.ops import Ops, OpsError  # noqa: E402
from core.state import StateProvider  # noqa: E402
from core.supervisor import Supervisor, SupervisorError  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

cfg = config_mod.load()
sessions = auth.SessionStore(cfg.session_secret or auth.new_secret(), cfg.session_ttl_s)
state = StateProvider(cfg)
sup_client = Supervisor(cfg.supervisor_url, cfg.supervisor_user, cfg.supervisor_pass)
db = Database(cfg.db_file)
ops = Ops(cfg, sup_client, state.rcon)
commands = CommandRunner(state.agent, state.rcon)
poller = EventPoller(state.agent, db, interval=2.0)
stores = StoreIndex(cfg.config_dir / "cache" / "store-index")
mods = ModManager(cfg.bepinex_config_dir)
gameplay = Gameplay(cfg.bepinex_config_dir)
mapdata = MapData(state.agent, cfg.config_dir / "cache" / "map")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Forbidden(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "ValheimPanel/0.2"
    protocol_version = "HTTP/1.1"

    # ---- plumbing ------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        log(f"{self.address_string()} {fmt % args}")

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:  # noqa: BLE001
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _session(self) -> auth.Session | None:
        return sessions.get(self._cookie(auth.SESSION_COOKIE))

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json", extra)

    def _html(self, status: int, html: str, extra: dict[str, str] | None = None) -> None:
        self._send(status, html.encode(), "text/html; charset=utf-8", extra)

    def _text(self, status: int, text: str, extra: dict[str, str] | None = None) -> None:
        self._send(status, text.encode(), "text/plain; charset=utf-8", extra)

    def _redirect(self, location: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > 1_000_000:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def _body_json(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc

    def _form(self) -> dict[str, str]:
        body = self._read_body().decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(body).items()}

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[-1].strip()
        return self.client_address[0]

    # ---- auth helpers --------------------------------------------------
    def _require(self, capability: str) -> auth.Session:
        session = self._session()
        if session is None:
            self._audit_denied(capability)
            raise Forbidden("auth required")
        if not roles.can(session.role, capability):
            self._audit_denied(capability)
            raise Forbidden(f"role '{session.role}' lacks '{capability}'")
        return session

    def _audit(self, session: auth.Session, action: str, target: str | None = None,
               detail=None, ok: bool = True) -> None:
        # A relay/proxy may act on behalf of a named person (e.g. a Discord user).
        # Recording only the service account would make the trail say "relay did
        # it", which is far less useful than "dimas asked relay to do it".
        via = (self.headers.get("X-Panel-Actor") or "").strip()
        if via:
            if isinstance(detail, dict):
                detail = {**detail, "via": via}
            elif detail is None:
                detail = {"via": via}
            else:
                detail = {"note": str(detail), "via": via}
        try:
            db.audit(session.user, session.role, self._client_ip(),
                     action, target, detail, ok)
        except Exception:  # noqa: BLE001 - auditing must never break a request
            log("audit write failed:\n" + traceback.format_exc())

    def _audit_denied(self, capability: str) -> None:
        """Record a rejected attempt. Denials are the point of an audit log."""
        session = self._session()
        actor = session.user if session else "anonymous"
        role = session.role if session else "-"
        detail = {"path": urlparse(self.path).path}
        via = (self.headers.get("X-Panel-Actor") or "").strip()
        if via:
            detail["via"] = via
        try:
            db.audit(actor, role, self._client_ip(), "access.denied",
                     capability, detail, ok=False)
        except Exception:  # noqa: BLE001
            log("audit write failed:\n" + traceback.format_exc())

    def _deny(self, message: str) -> None:
        return self._json(403, {"error": message})

    def _csrf_check(self, session: auth.Session, presented: str | None) -> bool:
        ok = auth.csrf_ok(session, presented)
        if not ok:
            self._audit(session, "csrf.rejected", urlparse(self.path).path, ok=False)
        return ok

    # ---- routing -------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if path == "/login":
                return self._login_page()
            if path.startswith("/static/"):
                return self._static(path)
            if path == "/api/v1/status.txt":
                return self._status_txt(query)

            session = self._session()
            if session is None:
                if path.startswith("/api/"):
                    return self._json(401, {"error": "auth required"})
                return self._redirect("/login")

            if path in ("/", "/index.html"):
                return self._dashboard(session)
            if path == "/api/v1/snapshot":
                return self._json(200, state.snapshot().as_dict())
            if path == "/api/v1/log":
                return self._log_slice(query)
            if path == "/api/v1/log/stream":
                return self._log_stream(session)
            if path == "/api/v1/session":
                return self._json(200, {
                    "user": session.user, "role": session.role,
                    "csrf": session.csrf,
                    "capabilities": roles.capabilities_for(session.role),
                })
            if path == "/api/v1/roles":
                return self._json(200, {"roles": roles.describe()})
            if path == "/api/v1/users":
                return self._list_users()
            if path == "/api/v1/audit":
                return self._list_audit(query)
            if path == "/api/v1/backups/download":
                return self._download_backup(session, query)
            if path.startswith("/api/v1/lists/"):
                return self._get_list(session, path.rsplit("/", 1)[-1])
            if path == "/api/v1/commands":
                return self._command_catalog(session)
            if path == "/api/v1/players":
                return self._player_list(session)
            if path == "/api/v1/chat":
                return self._chat(session, query)
            if path == "/api/v1/world":
                return self._world_state(session)
            if path == "/api/v1/events":
                return self._game_events(session, query)
            if path == "/api/v1/deaths":
                return self._deaths(session)
            if path == "/api/v1/mods":
                return self._mods_installed(session)
            if path == "/api/v1/mods/search":
                return self._mods_search(session, query)
            if path == "/api/v1/mods/package":
                return self._mods_package(session, query)
            if path == "/api/v1/mods/snapshots":
                return self._mods_snapshots(session)
            if path == "/api/v1/mods/config":
                return self._mods_config_read(session, query)
            if path == "/api/v1/gameplay":
                return self._gameplay_state(session)
            if path == "/api/v1/map":
                return self._map_meta(session)
            if path == "/api/v1/map/layers.png":
                return self._map_image(session, "layers")
            if path == "/api/v1/map/fog.png":
                return self._map_image(session, "fog")

            return self._json(404, {"error": "not found"})
        except Forbidden as exc:
            return self._json(403, {"error": str(exc)})
        except OpsError as exc:
            return self._json(409, {"error": str(exc)})
        except BrokenPipeError:
            return
        except Exception:  # noqa: BLE001
            log("unhandled error:\n" + traceback.format_exc())
            try:
                self._json(500, {"error": "internal error"})
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/v1/login":
                return self._do_login()
            if path == "/api/v1/logout":
                return self._do_logout()
            if path == "/api/v1/users":
                return self._create_user()
            if path == "/api/v1/users/update":
                return self._update_user()
            if path == "/api/v1/users/delete":
                return self._delete_user()
            if path == "/api/v1/password":
                return self._change_own_password()
            if path.startswith("/api/v1/server/"):
                return self._server_action(path.rsplit("/", 1)[-1])
            if path.startswith("/api/v1/lists/"):
                return self._set_list(path.rsplit("/", 1)[-1])
            if path.startswith("/api/v1/backups/"):
                return self._backup_action(path.rsplit("/", 1)[-1])
            if path.startswith("/api/v1/worlds/"):
                return self._world_action(path.rsplit("/", 1)[-1])
            if path == "/api/v1/commands/run":
                return self._run_command()
            if path == "/api/v1/commands/freeform":
                return self._run_freeform()
            if path.startswith("/api/v1/mods/"):
                return self._mods_action(path.rsplit("/", 1)[-1])
            if path == "/api/v1/gameplay":
                return self._gameplay_update()
            return self._json(404, {"error": "not found"})
        except Forbidden as exc:
            return self._json(403, {"error": str(exc)})
        except OpsError as exc:
            return self._json(409, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            log("unhandled POST error:\n" + traceback.format_exc())
            try:
                self._json(500, {"error": "internal error"})
            except Exception:  # noqa: BLE001
                pass

    # ---- auth handlers -------------------------------------------------
    def _login_page(self) -> None:
        page = STATIC_DIR / "login.html"
        if not page.is_file():
            return self._text(500, "login.html missing")
        return self._html(200, page.read_text(encoding="utf-8"))

    def _do_login(self) -> None:
        ip = self._client_ip()
        if sessions.is_rate_limited(ip):
            return self._json(429, {"error": "too many attempts, wait a few minutes"})

        form = self._form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""

        user = db.get_user(username) if username else None
        if user is None or user.get("disabled"):
            sessions.note_failure(ip)
            log(f"login failed for {username!r} from {ip}")
            # Same message and status for unknown user and bad password, so the
            # endpoint cannot be used to enumerate accounts.
            return self._json(401, {"error": "invalid credentials"})

        if not auth.verify_password(password, user["password_hash"]):
            sessions.note_failure(ip)
            log(f"login failed for {username!r} from {ip}")
            return self._json(401, {"error": "invalid credentials"})

        sessions.clear_failures(ip)
        session = sessions.create(user["username"], user["role"], user["id"])
        db.note_login(user["username"], ip)
        db.audit(user["username"], user["role"], ip, "auth.login", ok=True)
        log(f"login ok: {username} ({user['role']}) from {ip}")

        secure = "" if cfg.lan_only else "; Secure"
        cookie = (f"{auth.SESSION_COOKIE}={session.sid}; Path=/; HttpOnly; "
                  f"SameSite=Lax; Max-Age={cfg.session_ttl_s}{secure}")
        return self._json(200, {"ok": True, "role": session.role}, {"Set-Cookie": cookie})

    def _do_logout(self) -> None:
        session = self._session()
        if session is not None:
            self._audit(session, "auth.logout")
        sessions.destroy(self._cookie(auth.SESSION_COOKIE))
        cookie = f"{auth.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        return self._redirect("/login", {"Set-Cookie": cookie})

    # ---- pages ---------------------------------------------------------
    def _dashboard(self, session: auth.Session) -> None:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            return self._text(500, "index.html missing")
        html = page.read_text(encoding="utf-8")
        html = (html.replace("__CSRF__", session.csrf)
                    .replace("__USER__", session.user)
                    .replace("__ROLE__", session.role))
        return self._html(200, html)

    def _static(self, path: str) -> None:
        rel = path[len("/static/"):]
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._text(404, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    # ---- read handlers -------------------------------------------------
    def _log_slice(self, query: dict) -> None:
        try:
            offset = int(query.get("offset", ["0"])[0])
            length = min(int(query.get("length", ["65536"])[0]), 512_000)
        except ValueError:
            return self._json(400, {"error": "bad offset/length"})
        try:
            text = sup_client.read_log(cfg.supervisor_program, offset, length)
        except SupervisorError as exc:
            return self._json(502, {"error": f"supervisor log unavailable: {exc}"})
        consumed = len(text.encode("utf-8", "replace"))
        return self._json(200, {"offset": offset, "consumed": consumed,
                                "next_offset": offset + consumed, "text": text})

    def _log_stream(self, session: auth.Session) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        query = parse_qs(urlparse(self.path).query)
        try:
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            offset = 0
        if offset == 0:
            try:
                offset = max(0, state._log_offset - 16_384)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                offset = 0

        deadline = time.time() + 300
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while time.time() < deadline:
                try:
                    text = sup_client.read_log(cfg.supervisor_program, offset, 65536)
                except SupervisorError:
                    self.wfile.write(b"event: upstream_error\ndata: supervisor unavailable\n\n")
                    self.wfile.flush()
                    time.sleep(3)
                    continue
                if text:
                    offset += len(text.encode("utf-8", "replace"))
                    payload = json.dumps({"offset": offset, "text": text})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return

    # ---- users ---------------------------------------------------------
    def _list_users(self) -> None:
        session = self._require("users.manage")
        users = db.list_users()
        # Never leak password hashes.
        for u in users:
            u.pop("password_hash", None)
        return self._json(200, {"users": users,
                                "roles": roles.describe(),
                                "me": session.user})

    def _create_user(self) -> None:
        session = self._require("users.manage")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        username = (body.get("username") or "").strip()
        role = (body.get("role") or "").strip()
        password = body.get("password") or ""

        err = auth.validate_username(username)
        if err:
            return self._json(400, {"error": err})
        if not roles.valid_role(role):
            return self._json(400, {"error": "invalid role"})
        err = auth.validate_password(password)
        if err:
            return self._json(400, {"error": err})
        if db.get_user(username) is not None:
            return self._json(409, {"error": "username already exists"})

        db.create_user(username, role, auth.hash_password(password), session.user)
        self._audit(session, "user.create", username, {"role": role})
        log(f"{session.user} created user {username} ({role})")
        return self._json(200, {"ok": True})

    def _update_user(self) -> None:
        session = self._require("users.manage")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        username = (body.get("username") or "").strip()
        user = db.get_user(username)
        if user is None:
            return self._json(404, {"error": "no such user"})

        new_role = body.get("role")
        password = body.get("password") or ""
        disabled = body.get("disabled")

        changes: dict = {}
        if new_role is not None and new_role != user["role"]:
            if not roles.valid_role(new_role):
                return self._json(400, {"error": "invalid role"})
            # Refuse to remove the last enabled admin, including demoting self.
            if user["role"] == "admin" and new_role != "admin":
                if db.count_admins(exclude=username) == 0:
                    self._audit(session, "user.update", username,
                                {"denied": "cannot demote the last admin"}, ok=False)
                    return self._json(409, {"error": "cannot demote the last admin"})
            changes["role"] = new_role

        if password:
            err = auth.validate_password(password)
            if err:
                return self._json(400, {"error": err})
            changes["password_hash"] = auth.hash_password(password)

        if disabled is not None:
            dis = bool(disabled)
            if dis and user["role"] == "admin" and db.count_admins(exclude=username) == 0:
                self._audit(session, "user.update", username,
                            {"denied": "cannot disable the last admin"}, ok=False)
                return self._json(409, {"error": "cannot disable the last admin"})
            changes["disabled"] = dis

        if not changes:
            return self._json(200, {"ok": True, "changed": False})

        db.update_user(username, **changes)
        self._audit(session, "user.update", username,
                    {k: ("***" if k == "password_hash" else v) for k, v in changes.items()})

        # A role change applies to live sessions; a disable or password change
        # invalidates them.
        if "role" in changes:
            sessions.refresh_role(username, changes["role"])
        if "password_hash" in changes or changes.get("disabled"):
            sessions.destroy_user(username)

        log(f"{session.user} updated user {username}: {sorted(changes)}")
        return self._json(200, {"ok": True, "changed": True})

    def _delete_user(self) -> None:
        session = self._require("users.manage")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        username = (body.get("username") or "").strip()
        if username == session.user:
            self._audit(session, "user.delete", username,
                        {"denied": "cannot delete own account"}, ok=False)
            return self._json(409, {"error": "cannot delete your own account"})
        user = db.get_user(username)
        if user is None:
            return self._json(404, {"error": "no such user"})
        if user["role"] == "admin" and db.count_admins(exclude=username) == 0:
            self._audit(session, "user.delete", username,
                        {"denied": "cannot delete the last admin"}, ok=False)
            return self._json(409, {"error": "cannot delete the last admin"})

        db.delete_user(username)
        sessions.destroy_user(username)
        self._audit(session, "user.delete", username, {"role": user["role"]})
        log(f"{session.user} deleted user {username}")
        return self._json(200, {"ok": True})

    def _change_own_password(self) -> None:
        session = self._session()
        if session is None:
            raise Forbidden("auth required")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        user = db.get_user(session.user)
        if user is None:
            return self._json(404, {"error": "no such user"})
        if not auth.verify_password(body.get("current") or "", user["password_hash"]):
            self._audit(session, "auth.password_change", ok=False)
            return self._json(401, {"error": "current password is wrong"})

        new_pw = body.get("new") or ""
        err = auth.validate_password(new_pw)
        if err:
            return self._json(400, {"error": err})

        db.update_user(session.user, password_hash=auth.hash_password(new_pw))
        self._audit(session, "auth.password_change")
        return self._json(200, {"ok": True})

    def _list_audit(self, query: dict) -> None:
        session = self._require("audit.view")
        try:
            limit = min(int(query.get("limit", ["200"])[0]), 1000)
        except ValueError:
            limit = 200
        rows = db.audit_tail(limit=limit,
                             actor=(query.get("actor", [None])[0] or None),
                             action=(query.get("action", [None])[0] or None))
        return self._json(200, {"entries": rows, "can_manage": roles.can(session.role, "users.manage")})

    # ---- server control ------------------------------------------------
    def _server_action(self, action: str) -> None:
        capability = {
            "restart": "server.restart",
            "stop": "server.stop",
            "start": "server.start",
            "save": "server.restart",
        }.get(action)
        if capability is None:
            return self._json(404, {"error": "unknown action"})

        session = self._require(capability)
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        try:
            if action == "restart":
                # Always sync: bootstrap is idempotent and cheap, and a restart
                # that silently ignores a freshly installed plugin is the worst
                # possible outcome for a mod manager.
                result = ops.restart(sync=True)
            elif action == "stop":
                result = ops.stop(force=bool(body.get("force")))
            elif action == "start":
                result = ops.start()
            else:
                result = ops.save()
        except OpsError as exc:
            self._audit(session, f"server.{action}", detail={"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, f"server.{action}", detail=result.detail)
        log(f"{session.user} ran server.{action}")
        return self._json(200, {"ok": True, "message": result.message,
                                "detail": result.detail})

    # ---- lists ---------------------------------------------------------
    def _get_list(self, session: auth.Session, kind: str) -> None:
        self._require("view.dashboard")
        try:
            ids = ops.read_list(kind)
        except OpsError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, {
            "kind": kind,
            "ids": ids,
            "warning": ops.list_env_clobber_warning(),
            "can_edit": roles.can(session.role, "lists.edit"),
        })

    def _set_list(self, kind: str) -> None:
        session = self._require("lists.edit")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        ids = body.get("ids")
        if not isinstance(ids, list):
            return self._json(400, {"error": "ids must be a list"})
        try:
            before = ops.read_list(kind)
        except OpsError as exc:
            return self._json(400, {"error": str(exc)})

        try:
            result = ops.write_list(kind, [str(x) for x in ids])
        except OpsError as exc:
            self._audit(session, f"list.{kind}.write", kind, {"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, f"list.{kind}.write", kind,
                    {"before": before, "after": result.detail})
        log(f"{session.user} updated {kind}list ({len(ids)} entries)")
        return self._json(200, {"ok": True, "message": result.message,
                                "detail": result.detail,
                                "warning": ops.list_env_clobber_warning()})

    # ---- backups -------------------------------------------------------
    def _backup_action(self, action: str) -> None:
        capability = {
            "create": "backup.create",
            "delete": "backup.delete",
            "restore": "backup.restore",
        }.get(action)
        if capability is None:
            return self._json(404, {"error": "unknown action"})

        session = self._require(capability)
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        name = (body.get("name") or "").strip()
        if action != "create" and not name:
            return self._json(400, {"error": "name required"})

        try:
            if action == "create":
                result = ops.create_backup()
            elif action == "delete":
                result = ops.delete_backup(name)
            else:
                result = ops.restore_backup(name)
        except OpsError as exc:
            self._audit(session, f"backup.{action}", name or None,
                        {"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, f"backup.{action}", name or None, result.detail)
        log(f"{session.user} ran backup.{action} {name}".rstrip())
        return self._json(200, {"ok": True, "message": result.message,
                                "detail": result.detail})

    def _download_backup(self, session: auth.Session, query: dict) -> None:
        self._require("view.backups")
        name = (query.get("name", [""])[0] or "").strip()
        if not name:
            return self._json(400, {"error": "name required"})
        try:
            path = ops._backup_path(name)  # noqa: SLF001 - validated resolver
        except OpsError as exc:
            return self._json(400, {"error": str(exc)})
        if not path.is_file():
            return self._json(404, {"error": "no such backup"})

        self._audit(session, "backup.download", name)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with path.open("rb") as fh:
                while chunk := fh.read(262_144):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    # ---- worlds --------------------------------------------------------
    def _world_action(self, action: str) -> None:
        capability = {
            "delete": "world.delete",
            "regenerate": "world.regenerate",
        }.get(action)
        if capability is None:
            return self._json(404, {"error": "unknown action"})

        session = self._require(capability)
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        name = (body.get("name") or "").strip()
        if not name:
            return self._json(400, {"error": "name required"})

        snap = state.snapshot()
        active = snap.world.get("active")

        try:
            if action == "delete":
                result = ops.delete_world(name, active)
            else:
                result = ops.regenerate_world(name, active)
        except OpsError as exc:
            self._audit(session, f"world.{action}", name, {"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, f"world.{action}", name, result.detail)
        log(f"{session.user} ran world.{action} {name}")
        return self._json(200, {"ok": True, "message": result.message,
                                "detail": result.detail})

    # ---- commands ------------------------------------------------------
    def _command_catalog(self, session: auth.Session) -> None:
        self._require("view.dashboard")
        return self._json(200, {
            "commands": catalog_for(lambda cap: roles.can(session.role, cap)),
            "freeform": roles.can(session.role, "rcon.freeform"),
            "role": session.role,
        })

    def _run_command(self) -> None:
        session = self._session()
        if session is None:
            raise Forbidden("auth required")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        name = (body.get("name") or "").strip()
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return self._json(400, {"error": "args must be an object"})

        spec = CATALOG_BY_NAME.get(name)
        if spec is None:
            return self._json(404, {"error": f"unknown command {name!r}"})
        if not roles.can(session.role, spec.capability):
            self._audit_denied(spec.capability)
            return self._json(403, {"error": f"role '{session.role}' cannot run {name}"})

        try:
            result = commands.execute(name, args, freeform_allowed=False)
        except CommandError as exc:
            self._audit(session, f"command.{name}", detail={"args": args, "error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        # Commands are audited even on success: who ran what against the world
        # matters more here than in most panels.
        self._audit(session, f"command.{name}", detail={"args": args, "ok": result.ok})
        log(f"{session.user} ran command {name} via {result.transport}")
        return self._json(200, {"ok": result.ok, "output": result.output,
                                "transport": result.transport})

    def _run_freeform(self) -> None:
        session = self._require("rcon.freeform")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        raw = (body.get("command") or "").strip()
        try:
            result = commands.freeform(raw)
        except CommandError as exc:
            self._audit(session, "command.freeform", detail={"input": raw, "error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, "command.freeform", detail={"input": raw})
        log(f"{session.user} ran freeform RCON: {raw[:80]}")
        return self._json(200, {"ok": result.ok, "output": result.output,
                                "transport": result.transport})

    def _player_list(self, session: auth.Session) -> None:
        """Players from the agent (positions, authoritative) plus RCON detail."""
        self._require("view.players")
        players: list[dict] = []
        agent_ok = False
        try:
            st = state.agent.status()
            agent_ok = True
            players = st.get("players") or []
        except Exception:  # noqa: BLE001 - agent optional for this view
            players = []

        rcon_raw = ""
        try:
            rcon_raw = state.rcon.run("players")
        except Exception:  # noqa: BLE001
            pass

        return self._json(200, {
            "agent_available": agent_ok,
            "players": players,
            "rcon_players": parse_players(rcon_raw),
            "rcon_raw": rcon_raw,
            "can_message": roles.can(session.role, "player.message"),
            "can_kick": roles.can(session.role, "player.kick"),
        })

    def _chat(self, session: auth.Session, query: dict) -> None:
        self._require("view.logs")
        try:
            since = int(query.get("since", ["0"])[0])
        except ValueError:
            since = 0
        try:
            data = state.agent.chat(since=since, limit=200)
        except Exception as exc:  # noqa: BLE001
            return self._json(502, {"error": f"agent chat unavailable: {exc}"})
        return self._json(200, data)

    # ---- live world ----------------------------------------------------
    def _world_state(self, session: auth.Session) -> None:
        """World snapshot for the live view.

        Positions of players hidden on the map are stripped for non-admins, the
        same rule the upstream manager applies: an operator needs them, a viewer
        does not, and the agent exposes a `visible` flag precisely for this.
        """
        self._require("view.players")
        try:
            st = state.agent.status()
        except Exception as exc:  # noqa: BLE001
            return self._json(502, {"error": f"agent unavailable: {exc}"})

        is_admin = roles.can(session.role, "users.manage")
        players = []
        for p in st.get("players") or []:
            entry = dict(p)
            if not is_admin and entry.get("visible") is False:
                entry.pop("position", None)
                entry["hidden"] = True
            players.append(entry)

        world = st.get("world") or {}
        return self._json(200, {
            "ready": st.get("ready"),
            "world": world,
            "players": players,
            "global_keys": st.get("global_keys") or [],
            "modifiers": st.get("modifiers") or {},
            "poller": poller.status(),
            "event_count": db.event_count(),
            "death_count": db.event_count("player.death"),
            "can_see_positions": is_admin,
        })

    def _game_events(self, session: auth.Session, query: dict) -> None:
        self._require("view.logs")
        kind = (query.get("kind", [None])[0] or None)
        try:
            limit = min(int(query.get("limit", ["100"])[0]), 500)
        except ValueError:
            limit = 100
        rows = db.events(kind=kind, limit=limit)
        return self._json(200, {"events": rows, "poller": poller.status()})

    def _deaths(self, session: auth.Session) -> None:
        self._require("view.logs")
        return self._json(200, {
            "summary": db.death_summary(),
            "recent": db.events(kind="player.death", limit=50),
            "total": db.event_count("player.death"),
        })

    # ---- mods ----------------------------------------------------------
    def _crossplay_enabled(self) -> bool:
        """Crossplay is a compose env var the panel cannot read directly.

        The probe does not publish it, and guessing would make the whole
        classifier wrong in the direction that matters (silently allowing a
        client-only mod). Default to True: this server is crossplay and the
        warning is the safe side to err on.
        """
        probe = read_probe(cfg.probe_file)
        env = (probe.get("env") or {})
        val = str(env.get("CROSSPLAY", "true")).lower()
        return val in ("1", "true", "yes", "on")

    def _mods_installed(self, session: auth.Session) -> None:
        self._require("view.mods")
        crossplay = self._crossplay_enabled()
        installed = mods.installed()
        out = []
        for m in installed:
            entry = m.as_dict()
            verdict = classify([], "")  # tier stored at install time
            entry["policy"] = policy_decision(
                type("V", (), {"tier": m.tier})(), crossplay)
            out.append(entry)
        return self._json(200, {
            "installed": out,
            "unmanaged": mods.unmanaged_files(),
            "quarantined": mods.quarantined(),
            "crossplay": crossplay,
            "store": stores.status(),
            "can_install": roles.can(session.role, "mod.install"),
            "can_quarantine": roles.can(session.role, "mod.quarantine"),
        })

    def _mods_search(self, session: auth.Session, query: dict) -> None:
        self._require("view.mods")
        q = (query.get("q", [""])[0] or "").strip()
        store = (query.get("store", [None])[0] or None)
        server_only = (query.get("server_only", [""])[0] or "").lower() in ("1", "true")
        try:
            limit = min(int(query.get("limit", ["40"])[0]), 100)
        except ValueError:
            limit = 40

        # First call may need to build the index; do it in the background so the
        # request does not hang for a minute on a cold cache.
        st = stores.status()
        if st["packages"] == 0:
            stores.refresh(background=True)
            return self._json(200, {
                "results": [], "warming": True, "store": stores.status(),
                "message": "building the package index; this takes a few seconds",
            })

        try:
            results = stores.search(q, store=store, limit=limit,
                                    server_only=server_only)
        except StoreError as exc:
            return self._json(502, {"error": str(exc)})

        crossplay = self._crossplay_enabled()
        payload = []
        for pkg in results:
            verdict = classify(pkg.categories, pkg.description)
            payload.append({
                **pkg.as_dict(),
                "verdict": verdict.as_dict(),
                "policy": policy_decision(verdict, crossplay),
            })
        return self._json(200, {
            "results": payload, "count": len(payload),
            "store": stores.status(), "crossplay": crossplay,
            "categories": stores.all_categories()[:40],
        })

    def _mods_package(self, session: auth.Session, query: dict) -> None:
        self._require("view.mods")
        name = (query.get("name", [""])[0] or "").strip()
        if not name:
            return self._json(400, {"error": "name required"})
        stores.ensure_loaded()
        pkg = stores.get(name)
        if pkg is None:
            return self._json(404, {"error": "package not found in either store"})

        verdict = classify(pkg.categories, pkg.description)
        crossplay = self._crossplay_enabled()
        deps: list[dict] = []
        dep_error = None
        try:
            deps = [d.as_dict() for d in stores.resolve_dependencies(pkg.full_name)]
        except StoreError as exc:
            dep_error = str(exc)

        return self._json(200, {
            "package": pkg.as_dict(),
            "verdict": verdict.as_dict(),
            "policy": policy_decision(verdict, crossplay),
            "dependencies": deps,
            "dependency_error": dep_error,
            "crossplay": crossplay,
            "already_installed": any(
                m.full_name.lower() == pkg.full_name.lower() for m in mods.installed()),
        })

    def _mods_snapshots(self, session: auth.Session) -> None:
        self._require("view.mods")
        return self._json(200, {"snapshots": mods.snapshots()})

    def _mods_config_read(self, session: auth.Session, query: dict) -> None:
        self._require("view.mods")
        name = (query.get("name", [""])[0] or "").strip()
        if name:
            try:
                return self._json(200, {"name": name, "content": mods.read_config(name)})
            except ModError as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(200, {"files": mods.config_files()})

    def _mods_action(self, action: str) -> None:
        capability = {
            "install": "mod.install",
            "uninstall": "mod.uninstall",
            "enable": "mod.install",
            "disable": "mod.install",
            "pin": "mod.install",
            "quarantine": "mod.quarantine",
            "restore-snapshot": "mod.install",
            "config-write": "mod.install",
            "refresh": "mod.install",
            "import-url": "mod.install",
        }.get(action)
        if capability is None:
            return self._json(404, {"error": "unknown action"})

        session = self._require(capability)
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        try:
            if action == "install":
                return self._mods_install(session, body)
            if action == "import-url":
                return self._mods_import_url(session, body)
            if action == "uninstall":
                name = (body.get("name") or "").strip()
                result = mods.uninstall(name)
                self._audit(session, "mod.uninstall", name, result)
                return self._json(200, {"ok": True, **result})
            if action in ("enable", "disable"):
                name = (body.get("name") or "").strip()
                result = mods.set_enabled(name, action == "enable")
                self._audit(session, f"mod.{action}", name, result)
                return self._json(200, {"ok": True, **result})
            if action == "pin":
                name = (body.get("name") or "").strip()
                result = mods.set_pinned(name, bool(body.get("pinned")))
                self._audit(session, "mod.pin", name, result)
                return self._json(200, {"ok": True, **result})
            if action == "quarantine":
                name = (body.get("name") or "").strip()
                reason = (body.get("reason") or "reported broken").strip()
                result = mods.quarantine_mod(name, reason, body.get("excerpt") or "")
                self._audit(session, "mod.quarantine", name, {"reason": reason})
                return self._json(200, {"ok": True, **result})
            if action == "restore-snapshot":
                name = (body.get("name") or "").strip()
                mods.restore_snapshot(name)
                self._audit(session, "mod.restore_snapshot", name)
                return self._json(200, {"ok": True, "needs_restart": True})
            if action == "config-write":
                name = (body.get("name") or "").strip()
                content = body.get("content") or ""
                mods.write_config(name, content)
                self._audit(session, "mod.config_write", name,
                            {"bytes": len(content)})
                return self._json(200, {"ok": True})
            if action == "refresh":
                stores.refresh(background=True)
                self._audit(session, "mod.refresh_index")
                return self._json(200, {"ok": True, "warming": True})
        except ModError as exc:
            self._audit(session, f"mod.{action}", body.get("name"),
                        {"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})
        except StoreError as exc:
            self._audit(session, f"mod.{action}", body.get("name"),
                        {"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        return self._json(404, {"error": "unknown action"})

    def _mods_install(self, session: auth.Session, body: dict) -> None:
        full_name = (body.get("name") or "").strip()
        stores.ensure_loaded()
        pkg = stores.get(full_name)
        if pkg is None:
            return self._json(404, {"error": "package not found"})

        verdict = classify(pkg.categories, pkg.description)
        crossplay = self._crossplay_enabled()
        policy = policy_decision(verdict, crossplay)

        # Installing a client-only mod with crossplay on is allowed, but only on
        # an explicit acknowledgement: it will break console players.
        if policy["level"] == "danger" and not body.get("acknowledge"):
            return self._json(409, {
                "error": policy["reason"],
                "requires_acknowledgement": True,
                "verdict": verdict.as_dict(),
            })

        try:
            deps = stores.resolve_dependencies(pkg.full_name)
        except StoreError as exc:
            return self._json(409, {"error": str(exc)})

        installed_now: list[str] = []
        try:
            # One snapshot for the whole transaction: a per-package snapshot would
            # capture a half-installed set, making rollback restore a broken tree.
            shared = mods.snapshot("pre-install")

            # Dependencies first, so a plugin is never present without its libs.
            for dep in deps:
                dep_verdict = classify(dep.categories, dep.description)
                mods.install(
                    full_name=dep.full_name, version=dep.version, store=dep.store,
                    tier=dep_verdict.tier, url=dep.download_url,
                    evidence=dep_verdict.evidence, dependencies=[],
                    snapshot=False,
                )
                installed_now.append(dep.full_name)

            result = mods.install(
                full_name=pkg.full_name, version=pkg.version, store=pkg.store,
                tier=verdict.tier, url=pkg.download_url,
                evidence=verdict.evidence,
                dependencies=[d.full_name for d in deps],
                snapshot=False,
            )
            result["snapshot"] = shared.name
        except ModError as exc:
            self._audit(session, "mod.install", pkg.full_name,
                        {"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, "mod.install", pkg.full_name,
                    {"version": pkg.version, "deps": installed_now,
                     "tier": verdict.tier})
        log(f"{session.user} installed {pkg.full_name} {pkg.version}")
        return self._json(200, {
            "ok": True, "installed": [pkg.full_name] + installed_now,
            "files": result["files"], "snapshot": result["snapshot"],
            "needs_restart": True, "tier": verdict.tier,
        })

    def _mods_import_url(self, session: auth.Session, body: dict) -> None:
        url = (body.get("url") or "").strip()
        name = (body.get("name") or "").strip()
        if not url:
            return self._json(400, {"error": "url required"})
        if not name:
            # Derive a stable label from the URL so the manifest stays readable.
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            name = re.sub(r"[^A-Za-z0-9_.\-]", "_", tail) or "custom-mod"
            if not name.lower().endswith(".dll"):
                name = f"custom-{name}"
        label = f"custom/{name}"
        try:
            result = mods.install(
                full_name=label, version="custom", store="custom", tier="unknown",
                url=url, evidence=[f"imported from {url}"], custom_url=url,
            )
        except ModError as exc:
            self._audit(session, "mod.import_url", url, {"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, "mod.import_url", label, {"url": url})
        return self._json(200, {"ok": True, "name": label,
                                "files": result["files"],
                                "snapshot": result["snapshot"],
                                "needs_restart": True})

    # ---- gameplay helpers ----------------------------------------------
    def _raid_events(self) -> list[str]:
        """Event names from the agent catalog, used to validate the raid filter."""
        try:
            data = state.agent.catalog()
        except Exception:  # noqa: BLE001 - validation is best-effort
            return []
        return [e.get("name") for e in (data.get("events") or []) if e.get("name")]

    def _gameplay_state(self, session: auth.Session) -> None:
        self._require("view.mods")
        try:
            payload = gameplay.state(self._raid_events())
        except GameplayError as exc:
            return self._json(200, {"available": False, "error": str(exc)})
        payload["can_edit"] = roles.can(session.role, "mod.install")
        payload["reload_mode"] = self._reload_mode()
        return self._json(200, payload)

    def _reload_mode(self) -> str | None:
        """How the plugin last reloaded its config (watcher vs polled)."""
        try:
            text = sup_client.read_log(cfg.supervisor_program, 0, 400_000)
        except SupervisorError:
            return None
        return reload_mode_from_log(text.splitlines())

    def _gameplay_update(self) -> None:
        session = self._require("mod.install")
        body = self._body_json()
        if not self._csrf_check(session, body.get("csrf")):
            return self._json(403, {"error": "bad CSRF token"})

        payload = {
            "autofeed": body.get("autofeed") or {},
            "raids": body.get("raids") or {},
        }
        try:
            result = gameplay.update(payload, self._raid_events())
        except GameplayError as exc:
            self._audit(session, "gameplay.update", detail={"error": str(exc)}, ok=False)
            return self._json(409, {"error": str(exc)})

        self._audit(session, "gameplay.update", detail=result["changed"])
        log(f"{session.user} updated gameplay settings: {sorted(result['changed'])}")
        return self._json(200, {
            "ok": True,
            "changed": result["changed"],
            "state": result["state"],
            # The plugin hot-reloads; saying so avoids a pointless restart.
            "message": "applied live — no restart needed",
        })

    # ---- map -----------------------------------------------------------
    def _map_meta(self, session: auth.Session) -> None:
        self._require("view.players")
        try:
            meta = mapdata.meta()
        except MapError as exc:
            return self._json(502, {"error": str(exc), "layers_ready": False})

        players = mapdata.players()
        is_admin = roles.can(session.role, "users.manage")
        if not is_admin:
            # Same rule as /v1/world: a player hidden on the map keeps their
            # position private, and a viewer has no need for it.
            for p in players:
                if p.get("visible") is False:
                    p.pop("x", None); p.pop("y", None); p.pop("z", None)
                    p["hidden"] = True

        try:
            objs = mapdata.objects()
        except MapError:
            objs = {"objects": [], "pins": [], "locations": []}

        # Objects under unexplored fog are withheld from non-admins, matching the
        # upstream manager: knowing where a portal is before finding it is a
        # spoiler, not a convenience.
        def visible(o: dict) -> bool:
            return bool(o.get("explored", True)) or is_admin

        return self._json(200, {
            **meta,
            "players": players,
            "objects": [o for o in objs.get("objects") or [] if visible(o)],
            "pins": objs.get("pins") or [],
            "locations": [o for o in objs.get("locations") or [] if visible(o)],
            "can_see_all": is_admin,
        })

    def _map_image(self, session: auth.Session, which: str) -> None:
        self._require("view.players")
        try:
            data = mapdata.layers_png() if which == "layers" else mapdata.fog_png()
        except MapError as exc:
            return self._json(502, {"error": str(exc)})

        # The layers are ~10 MB and change only when the world does; the fog
        # changes as players explore. Cache the layers hard, the fog briefly.
        cache = "private, max-age=86400, immutable" if which == "layers" else \
                "private, max-age=10"
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                return

    # ---- bot status ----------------------------------------------------
    def _status_txt(self, query: dict) -> None:
        """Compact plain-text status for chat bots. GET-only, token-gated."""
        token = (query.get("token", [""])[0]
                 or (self.headers.get("Authorization", "")
                     .removeprefix("Bearer ").strip()))
        expected = os.environ.get("STATUS_TXT_TOKEN", "")
        if not expected:
            return self._text(503, "status endpoint disabled (no STATUS_TXT_TOKEN)")
        if not token or not _consteq(token, expected):
            return self._text(401, "unauthorized")

        snap = state.snapshot()
        lines: list[str] = []
        sup, ag = snap.supervisor, snap.agent
        world = ag.get("world") or {}
        lines.append(f"FGR · {world.get('name') or '?'} · {(sup.get('state') or 'unknown').lower()}")

        jc = snap.log.get("join_code")
        if jc:
            age = _human_age(snap.log.get("join_code_seen_at"))
            lines.append(f"Join code {jc}" + (f" (set {age})" if age else ""))
        else:
            lines.append("Join code unavailable")

        players = ag.get("players") or []
        if ag.get("reachable"):
            names = ", ".join(p.get("name", "?") for p in players) or "nobody"
            lines.append(f"Players {len(players)}: {names}")
            lines.append(f"Day {world.get('day')} · {world.get('weather') or '?'}"
                         + (" · night" if world.get("is_night") else ""))
        else:
            lines.append(f"Players ~{snap.log.get('online_count')} (agent down)")

        save_age = _human_age(snap.log.get("last_save_ts"))
        if save_age:
            lines.append(f"Last save {save_age} ago")

        mem = (snap.probe or {}).get("memory") or {}
        if mem.get("current"):
            lines.append(f"RAM {mem['current'] / 2**30:.2f} GiB")
        if snap.errors:
            lines.append("Warnings: " + "; ".join(snap.errors[:3]))
        return self._text(200, "\n".join(lines) + "\n")


def _consteq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def _human_age(iso: str | None) -> str:
    if not iso:
        return ""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d"


def bootstrap_admin() -> None:
    """Create the first admin from the environment on an empty database.

    The password is generated by the installer and written to a 0600 file; it is
    never printed here. Once any user exists this is a no-op, so a restart cannot
    resurrect a known password.
    """
    if db.count_users() > 0:
        return
    username = os.environ.get("BOOTSTRAP_ADMIN_USER", "admin").strip() or "admin"
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not password:
        log("FATAL: no users and BOOTSTRAP_ADMIN_PASSWORD is empty")
        sys.exit(1)
    db.create_user(username, "admin", auth.hash_password(password), created_by="bootstrap")
    db.audit(username, "admin", "127.0.0.1", "user.bootstrap", username,
             {"role": "admin", "note": "first admin from environment"})
    log(f"bootstrapped admin user {username!r}")


def main() -> None:
    if not cfg.lan_only and not cfg.session_secret:
        log("FATAL: LAN_ONLY=false requires SESSION_SECRET")
        sys.exit(1)

    bootstrap_admin()

    # Reconcile once at startup: the files on disk are the truth. A snapshot
    # restore, a manual delete, or a crash mid-install can all leave the manifest
    # claiming a plugin that is not there, and the UI would report it as loaded.
    try:
        dropped = mods.reconcile()
        if dropped:
            log(f"manifest reconciled: dropped {', '.join(dropped)}")
    except Exception:  # noqa: BLE001 - never block startup on bookkeeping
        log("manifest reconcile failed:\n" + traceback.format_exc())

    # Load the package index from disk so installs and package lookups resolve
    # immediately after a restart. A stale index is refreshed in the background.
    try:
        stores.ensure_loaded()
        log(f"store index: {stores.status()['packages']} packages")
    except Exception:  # noqa: BLE001
        log("store index load failed:\n" + traceback.format_exc())

    poller.start()
    log("event poller started (2s interval)")

    log(f"starting on {cfg.bind}:{cfg.port} "
        f"(supervisor {cfg.supervisor_url}, rcon {cfg.rcon_host}:{cfg.rcon_port}, "
        f"agent {cfg.agent_url}, db {cfg.db_file})")
    httpd = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
