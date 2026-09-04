"""Loopback-only HTTP control plane and web console for Euboulia."""

from __future__ import annotations

import json
import os
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from euboulia import __version__
from euboulia.control import (
    ControlError,
    TaskManager,
    controller_log_path,
    read_memory_entries,
)
from euboulia.models import JSONValue
from euboulia.progress import RUN_PHASES, read_run_progress
from euboulia.run_identity import normalize_run_uid

_CONTROL_HEADER = "X-Euboulia-Control"
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_LOG_CHUNK_BYTES = 64 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_LOG_SOURCE_LABELS = {
    "controller": "Controller",
    "kubernetes": "Kubernetes",
    "worker_stdout": "Worker stdout",
    "worker_stderr": "Worker stderr",
    "sglang_stdout": "SGLang stdout",
    "sglang_stderr": "SGLang stderr",
}


@dataclass(frozen=True, slots=True)
class ServerAddress:
    host: str
    port: int

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


class ControlApplication:
    """HTTP-independent application layer used by the handler and tests."""

    def __init__(self, manager: TaskManager) -> None:
        self.manager = manager

    def status(self) -> dict[str, JSONValue]:
        runtime = self.manager.runtime
        return {
            "version": __version__,
            "storage_root": str(runtime.storage.root),
            "executors": [
                {
                    "name": executor.name,
                    "namespace": executor.namespace,
                    "container": executor.container,
                }
                for executor in runtime.executors.values()
            ],
            "max_parallel": self.manager.max_parallel,
            "phases": list(RUN_PHASES),
        }

    def runs(self, *, limit: int = 200) -> dict[str, JSONValue]:
        runs = self.manager.store.list(limit=limit)
        return {"runs": [run.to_dict() for run in runs]}

    def run_detail(self, run_uid: str) -> dict[str, JSONValue]:
        selected_uid = normalize_run_uid(run_uid)
        run = self.manager.store.get(selected_uid)
        if run is None:
            raise KeyError(selected_uid)
        root = self.manager.runtime.storage.root
        run_dir = self.manager.runtime.storage.runs_dir / selected_uid
        manifest = _read_json(run_dir / "artifact-manifest.json")
        progress = _read_progress(run_dir / "worker-progress.json")
        validation = _read_json(run_dir / "artifacts" / "target-validation" / "validation.json")
        summary = _read_json(run_dir / "summary.json")
        return {
            "run": run.to_dict(),
            "events": list(self.manager.store.events(selected_uid)),
            "progress": None if progress is None else dict(progress),
            "summary": None if summary is None else dict(summary),
            "validation": None if validation is None else dict(validation),
            "artifacts": _artifact_view(manifest, run_dir),
            "log_sources": _log_source_view(root, run_dir, selected_uid),
        }

    def run_log(self, run_uid: str, *, source: str, after: int) -> dict[str, JSONValue]:
        selected_uid = normalize_run_uid(run_uid)
        if self.manager.store.get(selected_uid) is None:
            raise KeyError(selected_uid)
        if source not in _LOG_SOURCE_LABELS:
            raise ValueError(f"unknown run log source: {source}")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("log cursor must be a non-negative integer")
        root = self.manager.runtime.storage.root
        run_dir = self.manager.runtime.storage.runs_dir / selected_uid
        path = _log_source_path(root, run_dir, selected_uid, source)
        return _read_log_chunk(path, source=source, after=after)

    def memory(self, *, limit: int = 200) -> dict[str, JSONValue]:
        entries = read_memory_entries(self.manager.runtime.storage.memory_path, limit=limit)
        return {"entries": [dict(entry) for entry in entries]}

    def submit(self, payload: Mapping[str, object]) -> dict[str, JSONValue]:
        _reject_unknown(payload, {"recipe", "executor", "node", "name"})
        recipe = _required_string(payload, "recipe")
        executor = _required_string(payload, "executor")
        node = _required_string(payload, "node")
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            raise TypeError("name must be a string or null")
        run = self.manager.submit(recipe=recipe, executor=executor, node=node, name=name)
        return {"run": run.to_dict()}

    def cancel(self, run_uid: str) -> dict[str, JSONValue]:
        run = self.manager.cancel(run_uid)
        return {"run": run.to_dict()}


class EubouliaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], application: ControlApplication) -> None:
        self.application = application
        super().__init__(address, EubouliaRequestHandler)


class EubouliaRequestHandler(BaseHTTPRequestHandler):
    server: EubouliaHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        path = request.path
        try:
            if path == "/":
                self._send_bytes(HTTPStatus.OK, _index_html(), "text/html; charset=utf-8")
                return
            if path == "/api/status":
                self._send_json(HTTPStatus.OK, self.server.application.status())
                return
            if path == "/api/runs":
                self._send_json(HTTPStatus.OK, self.server.application.runs())
                return
            if path == "/api/memory":
                self._send_json(HTTPStatus.OK, self.server.application.memory())
                return
            log_run_uid = _run_log_route(path)
            if log_run_uid is not None:
                query = parse_qs(request.query, keep_blank_values=True)
                self._send_json(
                    HTTPStatus.OK,
                    self.server.application.run_log(
                        log_run_uid,
                        source=_single_query_value(query, "source", default="controller"),
                        after=_non_negative_query_int(query, "after", default=0),
                    ),
                )
                return
            run_uid = _run_path(path)
            if run_uid is not None:
                self._send_json(
                    HTTPStatus.OK,
                    self.server.application.run_detail(run_uid),
                )
                return
            self._send_error(HTTPStatus.NOT_FOUND, "route not found")
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "run not found")
        except (ControlError, OSError, TypeError, ValueError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_POST(self) -> None:
        if self.headers.get(_CONTROL_HEADER) != "1":
            self._send_error(HTTPStatus.FORBIDDEN, "missing local control header")
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/runs":
                result = self.server.application.submit(self._read_json_body())
                self._send_json(HTTPStatus.CREATED, result)
                return
            run_uid = _cancel_path(path)
            if run_uid is not None:
                self._read_empty_body()
                self._send_json(HTTPStatus.ACCEPTED, self.server.application.cancel(run_uid))
                return
            self._send_error(HTTPStatus.NOT_FOUND, "route not found")
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "run not found")
        except (ControlError, OSError, TypeError, ValueError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def _read_json_body(self) -> Mapping[str, object]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = _content_length(self.headers.get("Content-Length"))
        try:
            loaded: object = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(loaded, dict):
            raise TypeError("request body must be a JSON object")
        return cast(Mapping[str, object], loaded)

    def _read_empty_body(self) -> None:
        length = _content_length(self.headers.get("Content-Length"), allow_zero=True)
        if length:
            self.rfile.read(length)

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, JSONValue]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message[:512]})

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_server(
    manager: TaskManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> EubouliaHTTPServer:
    selected_host = host.strip().lower()
    if selected_host not in _LOOPBACK_HOSTS:
        raise ValueError("the control server may bind only to localhost")
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    return EubouliaHTTPServer((selected_host, port), ControlApplication(manager))


def serve(
    *,
    runtime_config: str | os.PathLike[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    max_parallel: int = 2,
    open_browser: bool = False,
) -> ServerAddress:
    manager = TaskManager(runtime_config, max_parallel=max_parallel)
    server = create_server(manager, host=host, port=port)
    actual_host, actual_port = cast(tuple[str, int], server.server_address[:2])
    address = ServerAddress(host=actual_host, port=actual_port)
    manager.start()
    print(f"Euboulia console: {address.url}")
    print(f"Storage: {manager.runtime.storage.root}")
    if open_browser:
        webbrowser.open(address.url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.stop()
    return address


def _index_html() -> bytes:
    return files("euboulia").joinpath("web", "index.html").read_bytes()


def _run_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[:2] == ["api", "runs"]:
        return unquote(parts[2])
    return None


def _cancel_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cancel":
        return unquote(parts[2])
    return None


def _run_log_route(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "logs":
        return unquote(parts[2])
    return None


def _single_query_value(
    query: Mapping[str, list[str]],
    name: str,
    *,
    default: str,
) -> str:
    values = query.get(name)
    if values is None:
        return default
    if len(values) != 1 or not values[0]:
        raise ValueError(f"query parameter {name} must have one non-empty value")
    return values[0]


def _non_negative_query_int(
    query: Mapping[str, list[str]],
    name: str,
    *,
    default: int,
) -> int:
    raw = _single_query_value(query, name, default=str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"query parameter {name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"query parameter {name} must be non-negative")
    return value


def _content_length(raw: str | None, *, allow_zero: bool = False) -> int:
    if raw is None:
        return 0 if allow_zero else _raise_length()
    try:
        length = int(raw)
    except ValueError as exc:
        raise ValueError("Content-Length must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if length < minimum or length > _MAX_REQUEST_BYTES:
        raise ValueError(f"request body must be {minimum}..{_MAX_REQUEST_BYTES} bytes")
    return length


def _raise_length() -> int:
    raise ValueError("Content-Length is required")


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _reject_unknown(payload: Mapping[str, object], allowed: set[str]) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unknown request field: {sorted(unknown)[0]}")


def _read_json(path: Path) -> Mapping[str, JSONValue] | None:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return cast(Mapping[str, JSONValue], loaded) if isinstance(loaded, dict) else None


def _read_progress(path: Path) -> Mapping[str, JSONValue] | None:
    try:
        return read_run_progress(path)
    except (OSError, ValueError):
        return None


def _artifact_view(
    manifest: Mapping[str, JSONValue] | None,
    run_dir: Path,
) -> dict[str, JSONValue]:
    if manifest is None:
        return {"manifest": None, "items": []}
    raw_items = manifest.get("artifacts")
    items: list[JSONValue] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            relative = copied.get("path")
            if copied.get("synced") is True and isinstance(relative, str):
                copied["local_path"] = str(run_dir / "artifacts" / relative)
            items.append(cast(JSONValue, copied))
    return {"manifest": dict(manifest), "items": items}


def _log_source_view(root: Path, run_dir: Path, run_uid: str) -> list[JSONValue]:
    sources: list[JSONValue] = []
    for source, label in _LOG_SOURCE_LABELS.items():
        path = _log_source_path(root, run_dir, run_uid, source)
        sources.append(
            {
                "source": source,
                "label": label,
                "available": path is not None,
                "size_bytes": None if path is None else path.stat().st_size,
            }
        )
    return sources


def _log_source_path(root: Path, run_dir: Path, run_uid: str, source: str) -> Path | None:
    if source == "controller":
        return _regular_file(controller_log_path(root, run_uid))
    if source == "kubernetes":
        return _regular_file(run_dir / "live" / "kubernetes.log")
    if source in {"worker_stdout", "worker_stderr"}:
        suffix = "stdout" if source.endswith("stdout") else "stderr"
        return _latest_regular_file(run_dir / "control", f"pod-worker-*.{suffix}.log")
    if source in {"sglang_stdout", "sglang_stderr"}:
        suffix = "stdout" if source.endswith("stdout") else "stderr"
        live = _regular_file(run_dir / "live" / f"sglang.{suffix}.log")
        return live or _latest_regular_file(
            run_dir / "artifacts" / "target-validation" / "service",
            f"service-*.{suffix}.log",
        )
    return None


def _regular_file(path: Path) -> Path | None:
    return path if path.is_file() and not path.is_symlink() else None


def _latest_regular_file(directory: Path, pattern: str) -> Path | None:
    if not directory.is_dir() or directory.is_symlink():
        return None
    matches = sorted(
        (path for path in directory.glob(pattern) if path.is_file() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    return matches[-1] if matches else None


def _read_log_chunk(path: Path | None, *, source: str, after: int) -> dict[str, JSONValue]:
    label = _LOG_SOURCE_LABELS[source]
    if path is None:
        return {
            "source": source,
            "label": label,
            "available": False,
            "offset": 0,
            "next_offset": 0,
            "reset": after != 0,
            "content": "",
            "eof": True,
        }
    size = path.stat().st_size
    reset = after > size or (after == 0 and size > _MAX_LOG_CHUNK_BYTES)
    offset = max(0, size - _MAX_LOG_CHUNK_BYTES) if reset else after
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(_MAX_LOG_CHUNK_BYTES)
    next_offset = offset + len(data)
    return {
        "source": source,
        "label": label,
        "available": True,
        "offset": offset,
        "next_offset": next_offset,
        "reset": reset,
        "content": data.decode("utf-8", errors="replace"),
        "eof": next_offset >= size,
    }


__all__ = [
    "ControlApplication",
    "EubouliaHTTPServer",
    "ServerAddress",
    "create_server",
    "serve",
]
