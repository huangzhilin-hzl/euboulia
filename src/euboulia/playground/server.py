"""Loopback HTTP application for the GPU playground."""

from __future__ import annotations

import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from euboulia.playground.config import PlaygroundConfig, integer, load_config, mapping, string
from euboulia.playground.kubernetes import Kubernetes, PlaygroundError
from euboulia.playground.manager import Manager


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, manager: Manager, port: int = 8766) -> None:
        self.manager = manager
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", integer(port, "port", 0, 65535)), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server: Server

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def _guard(self, *, mutation: bool = False) -> None:
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in hosts:
            raise PermissionError("invalid Host")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in hosts}:
            raise PermissionError("cross-origin requests are not allowed")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("cross-site requests are not allowed")
        if mutation and not secrets.compare_digest(
            self.headers.get("X-Playground-Token", ""), self.server.token
        ):
            raise PermissionError("missing playground token")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value: object) -> None:
        self._send(code, json.dumps(value, ensure_ascii=True).encode(), "application/json")

    def _handle(self, mutation: bool) -> None:
        try:
            self._guard(mutation=mutation)
            request = urlsplit(self.path)
            parts = request.path.strip("/").split("/")
            manager = self.server.manager
            config = manager.config
            if not mutation:
                assets = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }
                if request.path in assets:
                    name, kind = assets[request.path]
                    self._send(
                        200, files("euboulia.playground").joinpath("web", name).read_bytes(), kind
                    )
                    return
                if request.path == "/api/config":
                    self._json(200, public_config(config) | {"token": self.server.token})
                    return
                if request.path == "/api/state":
                    self._json(200, manager.snapshot())
                    return
                if len(parts) == 4 and parts[:2] == ["api", "clusters"] and parts[3] == "nodes":
                    self._json(200, {"nodes": Kubernetes(config.clusters[parts[2]]).nodes()})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                    if parts[3] == "events":
                        query = parse_qs(request.query)
                        after = integer(int(query.get("after", ["0"])[0]), "cursor", 0, 2**63 - 1)
                        self._json(200, manager.events(parts[2], after))
                        return
                    if parts[3] == "code":
                        self._json(200, {"code": manager.code(parts[2])})
                        return
            else:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Transfer-Encoding is not supported")
                length = integer(
                    int(self.headers.get("Content-Length", "0")), "body length", 2, 300 * 1024
                )
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Content-Type must be application/json")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete request body")
                data = mapping(json.loads(body), "request")
                if request.path == "/api/sessions":
                    self._json(
                        202,
                        manager.connect(
                            string(data.get("cluster"), "cluster"), string(data.get("node"), "node")
                        ),
                    )
                    return
                if request.path == "/api/runs":
                    self._json(202, manager.submit(data))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                    if parts[3] == "release":
                        self._json(200, manager.release(parts[2]))
                        return
                    if parts[3] == "refresh":
                        self._json(200, manager.refresh(parts[2]))
                        return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cancel":
                    self._json(202, manager.cancel(parts[2]))
                    return
            self._json(404, {"error": "route not found"})
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except KeyError:
            self._json(404, {"error": "cluster, profile, session or run not found"})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except PlaygroundError as exc:
            self._json(409, {"error": str(exc)})
        except (OSError, TimeoutError) as exc:
            self._json(500, {"error": str(exc)})

    def do_GET(self) -> None:
        self._handle(False)

    def do_POST(self) -> None:
        self._handle(True)

    def log_message(self, format: str, *args: Any) -> None:
        return


def public_config(config: PlaygroundConfig) -> dict[str, Any]:
    templates = files("euboulia.playground").joinpath("starters")
    profiles = []
    for p in config.profiles.values():
        path = templates.joinpath(f"{p.name}.txt")
        code = path.read_text() if path.is_file() else "print('Hello from the GPU playground')\n"
        profiles.append({"name": p.name, "label": p.label, "code": code})
    return {
        "clusters": [{"name": c.name, "namespace": c.namespace} for c in config.clusters.values()],
        "profiles": profiles,
        "run_timeout_seconds": config.run_timeout,
    }


def serve(*, config_path: Path | None = None, port: int = 8766, open_browser: bool = False) -> None:
    manager = Manager(load_config(config_path))
    try:
        server = Server(manager, port)
    except BaseException:
        manager.close()
        raise
    print(f"Molou GPU Playground: {server.url}", flush=True)
    print(f"Config: {manager.config.source}\nStorage: {manager.config.storage}", flush=True)
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
