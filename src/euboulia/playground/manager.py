"""Durable session/run records and bounded asynchronous remote execution."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import selectors
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from euboulia.playground.config import PlaygroundConfig, integer, string
from euboulia.playground.kubernetes import Kubernetes, PlaygroundError, manifest
from euboulia.playground.profiling import DEFAULT_ARGUMENTS, REPORT_NAMES, validate_options

ACTIVE = {
    "queued",
    "verifying_gpu",
    "preparing_environment",
    "running",
    "cancelling",
    "profiling",
    "exporting",
    "finalizing",
}


def parse_arguments(value: object) -> list[str]:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("arguments must be text without NUL characters")
    if len(value.encode()) > 8192:
        raise ValueError("arguments are limited to 8 KiB")
    # shlex does not implement shell line continuation. Remove it before splitting,
    # except inside single quotes or when the backslash is itself escaped.
    normalized = []
    quote = None
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and quote != "'":
            if value[i + 1 : i + 3] == "\r\n":
                i += 3
                continue
            if value[i + 1 : i + 2] == "\n":
                i += 2
                continue
            normalized.append(value[i : i + 2])
            i += 2
            continue
        if char == quote:
            quote = None
        elif quote is None and char in {"'", '"'}:
            quote = char
        normalized.append(char)
        i += 1
    try:
        argv = shlex.split("".join(normalized))
    except ValueError as exc:
        raise ValueError(f"invalid arguments: {exc}") from exc
    if len(argv) > 256:
        raise ValueError("at most 256 arguments are allowed")
    return argv


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class Manager:
    def __init__(self, config: PlaygroundConfig) -> None:
        self.config = config
        config.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._file_lock = (config.storage / "server.lock").open("w")
        try:
            fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file_lock.close()
            raise PlaygroundError("another playground server uses this storage directory") from exc
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.threads: list[threading.Thread] = []
        for path in sorted((config.storage / "sessions").glob("*.json")):
            record = json.loads(path.read_text())
            if record["status"] in {"ready", "disconnected"}:
                record.update(status="disconnected", error=None)
            elif record["status"] != "released":
                # Never silently resume a Pod or worker after controller interruption.
                record.update(
                    status="recovery_required",
                    error="Server restarted; release this Pod "
                    "before creating a new session. Local code and logs are retained.",
                )
            self.sessions[record["id"]] = record
        for path in sorted((config.storage / "runs").glob("*/run.json")):
            record = json.loads(path.read_text())
            if record["status"] in ACTIVE:
                session = self.sessions.get(record["session"])
                if session is not None:
                    session.update(
                        status="recovery_required",
                        error="An interrupted worker may "
                        "still be running; release the owned Pod before reconnecting.",
                    )
                record.update(status="interrupted", finished_at=time.time())
                for report in record.get("reports", {}).values():
                    if report["status"] == "streaming":
                        report["status"] = "partial"
                        report_path = path.parent / "reports" / report["name"]
                        if report["name"] in REPORT_NAMES and report_path.is_file():
                            report["bytes"] = report_path.stat().st_size
                write_json(path, record)
            # Older run records kept execution metadata in a separate file only.
            execution_path = path.parent / "execution.json"
            if "execution" not in record and execution_path.exists():
                record["execution"] = json.loads(execution_path.read_text())
            self.runs[record["id"]] = record
        for session in self.sessions.values():
            self._save_session(session)

    def _spawn(self, function: Any, *args: Any) -> None:
        thread = threading.Thread(target=function, args=args, daemon=True)
        with self.lock:
            self.threads = [t for t in self.threads if t.is_alive()]
            self.threads.append(thread)
            thread.start()

    def _save_session(self, session: dict[str, Any]) -> None:
        write_json(self.config.storage / "sessions" / f"{session['id']}.json", session)

    def _run_dir(self, run_id: str) -> Path:
        if run_id not in self.runs:
            raise KeyError(run_id)
        return self.config.storage / "runs" / run_id

    def _save_run(self, run: dict[str, Any]) -> None:
        write_json(self._run_dir(run["id"]) / "run.json", run)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            # Copy under the lock; HTTP serialization must not race state transitions.
            return cast(
                dict[str, Any],
                json.loads(
                    json.dumps(
                        {
                            "sessions": list(self.sessions.values()),
                            "runs": sorted(
                                self.runs.values(), key=lambda r: r["created_at"], reverse=True
                            )[:200],
                        }
                    )
                ),
            )

    def _transport(self, session: dict[str, Any]) -> Kubernetes:
        cluster = self.config.clusters.get(session["cluster"])
        if cluster is None or cluster.fingerprint != session["cluster_fingerprint"]:
            raise PlaygroundError(
                "session config changed; restore the original cluster config "
                "to release its owned Pod"
            )
        return Kubernetes(cluster)

    def connect(self, cluster_name: str, node: str) -> dict[str, Any]:
        cluster = self.config.clusters[cluster_name]
        allowed_nodes = cluster.nodes or tuple(n["name"] for n in Kubernetes(cluster).nodes())
        if node not in allowed_nodes:
            raise ValueError("select a node listed by the configured cluster")
        with self.lock:
            if self.stopping.is_set():
                raise PlaygroundError("server is stopping")
            for session in self.sessions.values():
                if (
                    session["cluster"] == cluster_name
                    and session["node"] == node
                    and session["status"] != "released"
                ):
                    self._transport(session)
                    if session["status"] == "disconnected":
                        if session.get("template_hash") != self._template_hash(cluster_name):
                            raise PlaygroundError("Pod template changed; release the old Pod first")
                        session.update(status="starting", error=None)
                        self._save_session(session)
                        self._spawn(self._prepare, session["id"], False)
                    return dict(session)
            sid = uuid.uuid4().hex
            session = {
                "id": sid,
                "cluster": cluster_name,
                "node": node,
                "namespace": cluster.namespace,
                "pod": f"molou-playground-{sid[:20]}",
                "uid": None,
                "status": "starting",
                "gpus": [],
                "error": None,
                "created_at": time.time(),
                "cluster_fingerprint": cluster.fingerprint,
                "template_hash": self._template_hash(cluster_name),
            }
            self.sessions[sid] = session
            self._save_session(session)
            self._spawn(self._prepare, sid)
            return dict(session)

    def _template_hash(self, cluster_name: str) -> str:
        cluster = self.config.clusters[cluster_name]
        data = json.dumps([cluster.template, cluster.python, cluster.workdir], sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()

    def _prepare(self, sid: str, create: bool = True) -> None:
        session = self.sessions[sid]
        try:
            kube = self._transport(session)
            if create:
                try:
                    kube.call(
                        ["create", "-f", "-", "-o", "json"],
                        data=json.dumps(manifest(kube.cluster, session)),
                    )
                except PlaygroundError:
                    # A timeout after a successful create must not lose ownership.
                    if not kube.inspect(session, missing_ok=True):
                        raise
            deadline = time.monotonic() + kube.cluster.startup_timeout
            while not self.stopping.is_set():
                pod = kube.inspect(session)
                with self.lock:
                    session["uid"] = pod["metadata"]["uid"]
                    status = pod.get("status", {})
                    session["node_ip"] = status.get("hostIP")
                    session["pod_phase"] = status.get("phase", "Pending")
                    session["pod_status"] = status.get("containerStatuses", [])
                    self._save_session(session)
                if status.get("phase") in {"Failed", "Succeeded"}:
                    raise PlaygroundError(
                        f"session Pod exited: {status.get('reason', status['phase'])}"
                    )
                states = status.get("containerStatuses", [])
                if any(s.get("name") == kube.cluster.container and s.get("ready") for s in states):
                    gpus = kube.inventory(session)
                    with self.lock:
                        session.update(status="ready", gpus=gpus)
                        self._save_session(session)
                    return
                if time.monotonic() >= deadline:
                    raise PlaygroundError(
                        "Pod startup timed out; inspect Pod status, image and runtime"
                    )
                self.stopping.wait(1)
            raise PlaygroundError(
                "server stopped while starting Pod; release it before reconnecting"
            )
        except Exception as exc:
            with self.lock:
                session.update(status="error", error=str(exc))
                self._save_session(session)

    def refresh(self, sid: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions[sid]
            if session["status"] != "ready":
                raise PlaygroundError("session is not ready")
        gpus = self._transport(session).inventory(session)
        with self.lock:
            session["gpus"] = gpus
            self._save_session(session)
            return dict(session)

    def release(self, sid: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions[sid]
            if session["status"] == "released":
                return dict(session)
            if session["status"] in {"starting", "releasing"}:
                raise PlaygroundError("wait for the current Pod operation to finish")
            if any(r["session"] == sid and r["status"] in ACTIVE for r in self.runs.values()):
                raise PlaygroundError("stop the active run before releasing the Pod")
            session.update(status="releasing", error=None)
            self._save_session(session)
        try:
            self._transport(session).delete(session)
        except Exception as exc:
            with self.lock:
                session.update(status="error", error=str(exc))
                self._save_session(session)
            raise
        with self.lock:
            session.update(status="released", gpus=[])
            self._save_session(session)
            return dict(session)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.keys() - {
            "session",
            "profile",
            "gpu_index",
            "code",
            "arguments",
            "timeout_seconds",
            "mode",
            "profiler_arguments",
            "export_sass",
        }:
            raise ValueError("unknown run fields")
        sid = string(payload.get("session"), "session")
        profile = self.config.profiles[string(payload.get("profile"), "profile")]
        gpu_index = integer(payload.get("gpu_index"), "GPU index", 0, 1024)
        code = string(payload.get("code"), "code")
        if len(code.encode()) > 256 * 1024:
            raise ValueError("script is limited to 256 KiB")
        arguments = payload.get("arguments", "")
        argv = parse_arguments(arguments)
        mode = string(payload.get("mode", "run"), "mode")
        profiler_arguments = payload.get("profiler_arguments", DEFAULT_ARGUMENTS.get(mode, ""))
        profiler_args = parse_arguments(profiler_arguments)
        validate_options(mode, profiler_args)
        export_sass = payload.get("export_sass", False)
        if not isinstance(export_sass, bool) or (export_sass and mode != "ncu"):
            raise ValueError("export_sass must be a boolean and is only available for ncu")
        # Accept the legacy checkbox field, but every NCU run now retains both reports.
        export_sass = mode == "ncu"
        timeout_limit = self.config.run_timeout if mode == "run" else self.config.profiling_timeout
        timeout = integer(
            payload.get("timeout_seconds", timeout_limit),
            "timeout",
            1,
            timeout_limit,
        )
        with self.lock:
            session = self.sessions[sid]
            if self.stopping.is_set() or session["status"] != "ready":
                raise PlaygroundError("session is not ready")
            if any(r["session"] == sid and r["status"] in ACTIVE for r in self.runs.values()):
                raise PlaygroundError("this session already has an active run")
            matches = [g for g in session["gpus"] if g["index"] == gpu_index]
            if len(matches) != 1:
                raise ValueError("GPU index is not in the session inventory")
            rid = uuid.uuid4().hex
            run = {
                "id": rid,
                "session": sid,
                "cluster": session["cluster"],
                "node": session["node"],
                "node_ip": session.get("node_ip"),
                "namespace": session["namespace"],
                "pod": session["pod"],
                "container": self.config.clusters[session["cluster"]].container,
                "gpu_index": gpu_index,
                "gpu_uuid": matches[0]["uuid"],
                "profile": profile.name,
                "profile_fingerprint": profile.fingerprint,
                "mode": mode,
                "profiler_arguments": profiler_arguments,
                "profiler_args": profiler_args,
                "export_sass": export_sass,
                "reports": {},
                "arguments": arguments,
                "args": argv,
                "status": "queued",
                "exit_code": None,
                "created_at": time.time(),
                "finished_at": None,
                "timeout_seconds": timeout,
                "error": None,
            }
            self.runs[rid] = run
            self._save_run(run)
            code_path = self._run_dir(rid) / "solution.py"
            code_path.write_text(code, encoding="utf-8")
            code_path.chmod(0o600)
            request = {
                "id": rid,
                "root": f"{self.config.clusters[session['cluster']].workdir}/{sid}",
                "gpu_index": gpu_index,
                "gpu_uuid": run["gpu_uuid"],
                "code": code,
                "args": argv,
                "profile": asdict(profile),
                "mode": mode,
                "profiler_args": profiler_args,
                "export_sass": export_sass,
                "export_timeout": self.config.export_timeout,
                "max_report_bytes": self.config.max_report_bytes,
                "run_timeout": timeout,
                "setup_timeout": self.config.setup_timeout,
                "max_output_bytes": self.config.max_output_bytes,
            }
            self._spawn(self._execute, rid, request)
            return dict(run)

    def _event(self, rid: str, event: dict[str, Any]) -> None:
        with self.lock:
            run = self.runs[rid]
            if event["kind"].startswith("report_"):
                self._report_event(rid, event)
                return
            if event["kind"] == "profiling":
                run["profiling"] = {k: v for k, v in event.items() if k != "kind"}
                write_json(self._run_dir(rid) / "profiling.json", run["profiling"])
                self._save_run(run)
                return
            if event["kind"] == "environment":
                write_json(self._run_dir(rid) / "environment.json", event)
                if event["status"] == "preparing":
                    inherited = "yes" if event["system_site_packages"] else "no"
                    data = (
                        f"\nProfile: {event['profile']}\n"
                        f"Venv (preparing): {event['path']}\n"
                        f"Python: {event['python_version']} · {event['python']}\n"
                        f"Base interpreter: {event['base_python']}\n"
                        f"Inherit image packages: {inherited}\n"
                    )
                else:
                    gpu_packages = {"torch", "nvidia-cutlass-dsl", "tilelang", "triton"}
                    versions = ", ".join(
                        f"{name}=={version}"
                        for name, version in sorted(event["packages"].items())
                        if name.lower().replace("_", "-") in gpu_packages
                    )
                    reuse = "reused" if event["reused"] else "created"
                    data = f"Venv ready: {event['key']} ({reuse})\n"
                    if versions:
                        data += f"GPU packages: {versions}\n"
                    data += "Full resolved package versions saved with this run.\n"
                event = {
                    "kind": "output",
                    "stream": "system",
                    "data": data,
                }
            if event["kind"] == "execution":
                if event.get("stage") == "export":
                    run.setdefault("export_commands", []).append(dict(event))
                else:
                    write_json(self._run_dir(rid) / "execution.json", event)
                    run["execution"] = dict(event)
                self._save_run(run)
                event = {
                    "kind": "output",
                    "stream": "system",
                    "data": (
                        f"Working directory: {event['cwd']}\n"
                        f"CUDA_VISIBLE_DEVICES={shlex.quote(event['env']['CUDA_VISIBLE_DEVICES'])}\n"
                        f"Command (inside Pod): {shlex.join(event['argv'])}\n\n"
                    ),
                }
            if event["kind"] == "phase" and run["status"] != "cancelling":
                run["status"] = event["phase"]
                self._save_run(run)
            if event["kind"] == "result":
                for report in run.get("reports", {}).values():
                    if report["status"] == "streaming":
                        report["status"] = "partial"
                run.update(
                    status=event["status"],
                    exit_code=event.get("exit_code"),
                    finished_at=time.time(),
                )
                self._save_run(run)
            event["time"] = time.time()
            path = self._run_dir(rid) / "events.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(json.dumps(event, ensure_ascii=True) + "\n")

    def _report_event(self, rid: str, event: dict[str, Any]) -> None:
        name = event.get("name")
        if name not in REPORT_NAMES:
            raise PlaygroundError("invalid text report name")
        run = self.runs[rid]
        reports = run.setdefault("reports", {})
        directory = self._run_dir(rid) / "reports"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / name
        if event["kind"] == "report_begin":
            if name in reports:
                raise PlaygroundError("duplicate text report")
            with path.open("xb") as handle:
                os.chmod(handle.name, 0o600)
            reports[name] = {"name": name, "status": "streaming", "bytes": 0}
        elif name not in reports or reports[name]["status"] != "streaming":
            raise PlaygroundError("text report is not streaming")
        elif event["kind"] == "report_chunk":
            data = event["data"].encode("utf-8")
            if reports[name]["bytes"] + len(data) > self.config.max_report_bytes:
                raise PlaygroundError("text report exceeds configured size limit")
            with path.open("ab") as handle:
                handle.write(data)
            reports[name]["bytes"] += len(data)
            return
        elif event["kind"] == "report_end":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if event["bytes"] != path.stat().st_size or event["sha256"] != digest:
                raise PlaygroundError("text report checksum mismatch")
            if event["status"] not in {"ready", "partial"}:
                raise PlaygroundError("invalid text report status")
            reports[name].update(
                {k: event[k] for k in ("status", "bytes", "sha256", "exit_code", "truncated")}
            )
        else:
            raise PlaygroundError("invalid text report event")
        self._save_run(run)

    def report_path(self, rid: str, name: str) -> Path:
        with self.lock:
            if name not in REPORT_NAMES or name not in self.runs[rid].get("reports", {}):
                raise KeyError("text report not found")
            return self._run_dir(rid) / "reports" / name

    def events(self, rid: str, after: int) -> dict[str, Any]:
        with self.lock:
            path = self._run_dir(rid) / "events.jsonl"
            events = []
            cursor = after
            if path.exists():
                if after > path.stat().st_size:
                    raise ValueError("cursor exceeds log size")
                with path.open("rb") as handle:
                    handle.seek(after)
                    while handle.tell() - after < 64 * 1024:
                        line = handle.readline()
                        if not line:
                            break
                        events.append(json.loads(line))
                    cursor = handle.tell()
            return {
                "events": events,
                "next": cursor,
                "run": dict(self.runs[rid]),
                "has_more": path.exists() and cursor < path.stat().st_size,
            }

    def code(self, rid: str) -> str:
        with self.lock:
            return (self._run_dir(rid) / "solution.py").read_text(encoding="utf-8")

    def _execute(self, rid: str, request: dict[str, Any]) -> None:
        run = self.runs[rid]
        session = self.sessions[run["session"]]
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        try:
            try:
                kube = self._transport(session)
                pod = kube.inspect(session)
                with self.lock:
                    run["node_ip"] = pod.get("status", {}).get("hostIP")
                    self._save_run(run)
            finally:
                # The target remains in failed transport logs as well as successful runs.
                self._event(
                    rid,
                    {
                        "kind": "output",
                        "stream": "system",
                        "data": (
                            f"Cluster: {run['cluster']}\n"
                            f"Node IP: {run.get('node_ip') or 'unavailable'}\n"
                            f"Node: {run['node']}\n"
                            f"Pod: {run['namespace']} / {run['pod']}\n"
                            f"Container: {run['container']}\n"
                        ),
                    },
                )
            with self.lock:
                if run["status"] == "cancelling":
                    self._event(rid, {"kind": "result", "status": "cancelled", "exit_code": None})
                    return
            worker = files("euboulia.playground").joinpath("worker.py").read_text()
            process = subprocess.Popen(
                kube.exec_args(session, [kube.cluster.python, "-u", "-c", worker]),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdin and process.stdout and process.stderr
            input_bytes = json.dumps(request).encode()
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "input")
            for pipe, stream in ((process.stdout, "protocol"), (process.stderr, "transport")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, stream)
            buffer = b""
            transport_bytes = 0
            deadline = (
                time.monotonic()
                + request["setup_timeout"]
                + request["run_timeout"]
                + 90
                + (2 * request["export_timeout"] if request.get("mode") != "run" else 0)
            )
            result_seen = False
            while selector.get_map() or process.poll() is None:
                if time.monotonic() >= deadline:
                    raise PlaygroundError("remote transport timed out; release Pod before retrying")
                for key, _ in selector.select(0.2):
                    if key.data == "input":
                        written = os.write(key.fd, input_bytes[:8192])
                        input_bytes = input_bytes[written:]
                        if not input_bytes:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "transport":
                        if transport_bytes < 65536:
                            self._event(
                                rid,
                                {
                                    "kind": "output",
                                    "stream": "system",
                                    "data": chunk.decode("utf-8", errors="replace"),
                                },
                            )
                            transport_bytes += len(chunk)
                    else:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            event = json.loads(line)
                            result_seen |= event.get("kind") == "result"
                            self._event(rid, event)
                        if len(buffer) > 1024 * 1024:
                            raise PlaygroundError("invalid oversized worker protocol frame")
            returncode = process.wait()
            if returncode or not result_seen:
                raise PlaygroundError(
                    f"remote execution disconnected (kubectl exit {returncode}); "
                    "release Pod to stop any remaining worker"
                )
        except Exception as exc:
            with self.lock:
                run["error"] = str(exc)
                session.update(status="recovery_required", error=str(exc))
                self._save_session(session)
            self._event(rid, {"kind": "output", "stream": "system", "data": f"\n{exc}\n"})
            self._event(rid, {"kind": "result", "status": "failed", "exit_code": None})
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for stream_pipe in (process.stdin, process.stdout, process.stderr):
                    if stream_pipe:
                        stream_pipe.close()

    def cancel(self, rid: str) -> dict[str, Any]:
        with self.lock:
            run = self.runs[rid]
            if run["status"] not in ACTIVE or run["status"] == "cancelling":
                return dict(run)
            run["status"] = "cancelling"
            self._save_run(run)
            self._spawn(self._cancel_remote, rid)
            return dict(run)

    def _cancel_remote(self, rid: str) -> None:
        run = self.runs[rid]
        session = self.sessions[run["session"]]
        try:
            kube = self._transport(session)
            kube.inspect(session)
            cancel = f"{kube.cluster.workdir}/{session['id']}/runs/{rid}/cancel"
            script = (
                "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                "p.parent.mkdir(parents=True, exist_ok=True); p.touch()"
            )
            kube.call(
                [
                    "exec",
                    session["pod"],
                    "-c",
                    kube.cluster.container,
                    "--",
                    kube.cluster.python,
                    "-c",
                    script,
                    cancel,
                ]
            )
        except Exception as exc:
            with self.lock:
                run["error"] = f"Cancellation not confirmed: {exc}"
                self._save_run(run)
            self._event(
                rid,
                {
                    "kind": "output",
                    "stream": "system",
                    "data": f"\nCancellation not confirmed: {exc}\n",
                },
            )

    def close(self) -> None:
        self.stopping.set()
        with self.lock:
            for rid, run in self.runs.items():
                if run["status"] in ACTIVE:
                    self.cancel(rid)
        deadline = time.monotonic() + 50
        for thread in list(self.threads):
            thread.join(timeout=max(0, deadline - time.monotonic()))
        with contextlib.suppress(OSError):
            fcntl.flock(self._file_lock, fcntl.LOCK_UN)
            self._file_lock.close()
