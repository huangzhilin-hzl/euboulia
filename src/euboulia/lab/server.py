"""Authenticated loopback Lab API and research-room assets."""

from __future__ import annotations

import json
import mimetypes
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from euboulia.lab.store import LabStore, text_field


def web_root() -> Path:
    bundled = Path(__file__).resolve().parents[1] / "lab_web"
    return (
        bundled
        if bundled.is_dir()
        else (Path(__file__).resolve().parents[3] / "docs/prototypes/inference-world")
    )


class LabServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store: LabStore, port: int = 8773) -> None:
        self.store = store
        self.sessions: dict[str, float] = {}
        self.assets = web_root()
        super().__init__(("127.0.0.1", port), LabHandler)


class LabHandler(BaseHTTPRequestHandler):
    server: LabServer

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Never log credentials, prompt content, or request paths.

    def respond(self, data: Any, status: int = 200, *, cookie: str | None = None) -> None:
        self.send_bytes(
            json.dumps(data, ensure_ascii=False).encode(), "application/json", status, cookie=cookie
        )

    def send_bytes(
        self, data: bytes, mime: str, status: int = 200, *, cookie: str | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'",
        )
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def token(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def admin(self) -> None:
        if secrets.compare_digest(self.token().encode(), self.server.store.admin_key.encode()):
            return
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        session = cookie.get("lab_session")
        if session and self.server.sessions.get(session.value, 0) > time.time():
            return
        raise PermissionError("Sign in with the Lab admin key")

    def boundary(self, *, write: bool = False) -> None:
        host = self.headers.get("Host", "")
        if host not in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }:
            raise PermissionError("Invalid Host")
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            raise PermissionError("Cross-origin request rejected")
        if write and self.headers.get("X-Lab-Control") != "1":
            raise PermissionError("Missing Lab control header")

    def body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Expected application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 262144 or self.headers.get("Transfer-Encoding"):
            raise ValueError("Invalid body length (maximum 256 KiB)")
        self.connection.settimeout(10)
        data = json.loads(self.rfile.read(size))
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        return data

    def do_GET(self) -> None:
        try:
            self.boundary()
            path = urlsplit(self.path).path
            if path == "/api/lab/health":
                self.respond({"service": "euboulia-lab", "version": 1})
            elif path == "/api/lab/state":
                self.admin()
                self.respond(self.server.store.snapshot())
            elif path.startswith("/api/lab/jobs/"):
                self.admin()
                self.respond(self.server.store.detail(path.removeprefix("/api/lab/jobs/")))
            else:
                name = path.removeprefix("/prototypes/inference-world/").lstrip("/")
                name = name or "index.html"
                if (
                    "/" in name
                    or name.startswith(".")
                    or Path(name).suffix not in {".html", ".css", ".js", ".mjs"}
                ):
                    self.respond({"error": "Not found"}, 404)
                    return
                asset = self.server.assets / name
                if not asset.is_file():
                    self.respond({"error": "Not found"}, 404)
                    return
                mime = (
                    "text/javascript"
                    if name.endswith((".js", ".mjs"))
                    else (mimetypes.guess_type(name)[0] or "text/plain")
                )
                self.send_bytes(asset.read_bytes(), mime)
        except PermissionError as exc:
            self.respond({"error": str(exc)}, 403)
        except (ValueError, OSError) as exc:
            self.respond({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        try:
            self.boundary(write=True)
            path = urlsplit(self.path).path
            data = self.body()
            store = self.server.store
            if path == "/api/lab/login":
                if not secrets.compare_digest(
                    text_field(data, "key", 200).encode(), store.admin_key.encode()
                ):
                    raise PermissionError("Invalid Lab admin key")
                self.server.sessions = {
                    key: expiry
                    for key, expiry in self.server.sessions.items()
                    if expiry > time.time()
                }
                session = secrets.token_urlsafe(32)
                self.server.sessions[session] = time.time() + 28800
                self.respond(
                    {"ok": True},
                    cookie=f"lab_session={session}; HttpOnly; "
                    "SameSite=Strict; Path=/; Max-Age=28800",
                )
                return
            if path == "/api/lab/pair":
                self.respond(store.pair(data))
                return
            if path in {"/api/lab/poll", "/api/lab/report", "/api/lab/disconnect"}:
                agent_id = store.authenticate(self.token())
                if path.endswith("/poll"):
                    self.respond(
                        store.poll(
                            agent_id,
                            text_field(data, "instance", 100),
                            claim=data.get("claim", True) is True,
                        )
                    )
                elif path.endswith("/report"):
                    self.respond(store.report(agent_id, data))
                else:
                    store.disconnect(agent_id, text_field(data, "instance", 100))
                    self.respond({"ok": True})
                return
            self.admin()
            if path == "/api/lab/agents":
                self.respond(store.create_agent(data), 201)
            elif path == "/api/lab/repair":
                self.respond(store.repair(text_field(data, "agent_id", 80)))
            elif path == "/api/lab/revoke":
                store.revoke(text_field(data, "agent_id", 80))
                self.respond({"ok": True})
            elif path == "/api/lab/jobs":
                self.respond(store.submit(data), 201)
            elif path == "/api/lab/cancel":
                store.cancel(text_field(data, "job_id", 80))
                self.respond({"ok": True})
            elif path == "/api/lab/logout":
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                if "lab_session" in cookie:
                    self.server.sessions.pop(cookie["lab_session"].value, None)
                self.respond(
                    {"ok": True},
                    cookie="lab_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
                )
            else:
                self.respond({"error": "Not found"}, 404)
        except PermissionError as exc:
            self.respond({"error": str(exc)}, 403)
        except (ValueError, OSError) as exc:
            self.respond({"error": str(exc)}, 400)
