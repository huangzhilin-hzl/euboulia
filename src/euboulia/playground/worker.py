"""Standalone stdlib-only worker sent to the Pod's Python via kubectl exec.

The controller sends one JSON request on stdin. Only this worker writes protocol
frames; user stdout/stderr are captured, bounded, and encoded as separate events.
"""

from __future__ import annotations

import codecs
import contextlib
import fcntl
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class Stopped(Exception):
    def __init__(self, status: str) -> None:
        self.status = status


class Worker:
    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.root = Path(request["root"])
        self.run_dir = self.root / "runs" / request["id"]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_file = self.run_dir / "cancel"
        self.output_bytes = 0
        self.truncated = False
        self.env = dict(os.environ)
        self.env.update(request["profile"].get("env", {}))
        self.env.update(
            CUDA_VISIBLE_DEVICES=request["gpu_uuid"], PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8"
        )
        self.env.pop("PYTHONPATH", None)
        self.env.pop("PYTHONHOME", None)

    def emit(self, kind: str, **fields: Any) -> None:
        print(json.dumps({"kind": kind, **fields}, ensure_ascii=True), flush=True)

    def check(self, deadline: float) -> None:
        if self.cancel_file.exists():
            raise Stopped("cancelled")
        if time.monotonic() >= deadline:
            raise Stopped("timed_out")

    def output(self, stream: str, data: str) -> None:
        remaining = self.request["max_output_bytes"] - self.output_bytes
        encoded = data.encode("utf-8")
        if remaining > 0:
            text = encoded[:remaining].decode("utf-8", errors="replace")
            self.output_bytes += len(encoded[:remaining])
            self.emit("output", stream=stream, data=text)
        if len(encoded) > remaining and not self.truncated:
            self.truncated = True
            self.emit(
                "output",
                stream="system",
                data="\n[Output limit reached; further output discarded]\n",
            )

    def command(
        self,
        argv: list[str],
        deadline: float,
        cwd: Path,
        *,
        report: str | None = None,
        graceful: bool = False,
    ) -> int:
        self.check(deadline)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        decoders = {}
        assert process.stdout is not None and process.stderr is not None
        for pipe, stream in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, stream)
            decoders[stream] = codecs.getincrementaldecoder("utf-8")("replace")
        saved = 0
        digest = hashlib.sha256()
        truncated = False
        report_file = None
        stopping: Stopped | None = None
        stop_deadline = 0.0
        returncode = None
        try:
            if report:
                report_file = (cwd / report).open("wb")
                self.emit("report_begin", name=report)
            while selector.get_map() or process.poll() is None:
                if stopping is None:
                    try:
                        self.check(deadline)
                    except Stopped as exc:
                        if not graceful:
                            raise
                        stopping = exc
                        stop_deadline = time.monotonic() + 15
                        self.emit("phase", phase="finalizing")
                        self.output(
                            "system", "Stopping profiler; allowing up to 15s to finalize.\n"
                        )
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGINT)
                elif time.monotonic() >= stop_deadline:
                    raise stopping
                for key, _ in selector.select(0.1):
                    data = os.read(key.fd, 4096)
                    stream = key.data
                    text = decoders[stream].decode(data, final=not data)
                    if text:
                        if report_file is not None and stream == "stdout":
                            encoded = text.encode("utf-8")
                            if truncated:
                                continue
                            remaining = (
                                self.request.get("max_report_bytes", 32 * 1024 * 1024) - saved
                            )
                            chunk = encoded[: max(0, remaining)].decode("utf-8", errors="ignore")
                            data_bytes = chunk.encode("utf-8")
                            report_file.write(data_bytes)
                            digest.update(data_bytes)
                            saved += len(data_bytes)
                            truncated |= len(encoded) > remaining
                            if chunk:
                                self.emit("report_chunk", name=report, data=chunk)
                        else:
                            self.output(stream, text)
                    if not data:
                        selector.unregister(key.fileobj)
                # A script may leave child processes holding its pipes open.
                if process.poll() is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
            returncode = process.wait()
            if stopping:
                raise stopping
            return returncode
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if report_file is not None:
                report_file.close()
                self.emit(
                    "report_end",
                    name=report,
                    status="ready" if returncode == 0 and not truncated else "partial",
                    bytes=saved,
                    sha256=digest.hexdigest(),
                    exit_code=returncode,
                    truncated=truncated,
                )
            selector.close()
            process.stdout.close()
            process.stderr.close()

    def execute(self) -> None:
        r = self.request
        (self.run_dir / "solution.py").write_text(r["code"], encoding="utf-8")
        setup_deadline = time.monotonic() + r["setup_timeout"]
        env_key = hashlib.sha256(
            json.dumps(
                ["parent-sites-v2", r["profile"], sys.version, sys.executable], sort_keys=True
            ).encode()
        ).hexdigest()[:20]
        env_root = self.root / "venvs"
        venv = env_root / env_key
        python = str(venv / "bin/python")
        environment = {
            "key": env_key,
            "path": str(venv),
            "python": python,
            "python_version": sys.version.split()[0],
            "base_python": sys.executable,
            "profile": r["profile"]["name"],
            "system_site_packages": r["profile"]["system_site_packages"],
        }
        # Retain the planned environment even if GPU verification or setup fails.
        self.emit("environment", status="preparing", **environment)
        self.check(setup_deadline)
        # Query the exact UUID again at execution time; never fall back to cuda:0
        # before CUDA_VISIBLE_DEVICES remaps the requested physical device.
        self.emit("phase", phase="verifying_gpu")
        gpu_check = (
            "import csv,io,subprocess,sys; "
            "s=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid',"
            "'--format=csv,noheader'],text=True); "
            "pairs={(a.strip(),b.strip()) for a,b in csv.reader(io.StringIO(s))}; "
            "assert (sys.argv[1],sys.argv[2]) in pairs, 'GPU index/UUID changed'; "
            "print('GPU '+sys.argv[1]+' / '+sys.argv[2]+' -> cuda:0',flush=True)"
        )
        if self.command(
            [sys.executable, "-c", gpu_check, str(r["gpu_index"]), r["gpu_uuid"]],
            setup_deadline,
            self.run_dir,
        ):
            raise RuntimeError("GPU identity verification failed")
        self.emit("phase", phase="preparing_environment")
        env_root.mkdir(exist_ok=True)
        with (env_root / f"{env_key}.lock").open("w") as lock:
            while True:
                self.check(setup_deadline)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.1)
            ready = venv / ".ready.json"
            reused = ready.exists()
            if not reused:
                if venv.exists():
                    shutil.rmtree(venv)
                args = [sys.executable, "-m", "venv"]
                if r["profile"]["system_site_packages"]:
                    args.append("--system-site-packages")
                if self.command([*args, str(venv)], setup_deadline, self.run_dir):
                    raise RuntimeError("venv creation failed; the image needs python3-venv and pip")
                if r["profile"]["system_site_packages"]:
                    # The image's python may itself be a venv (e.g. /opt/sglang).
                    # --system-site-packages alone inherits the system interpreter,
                    # not that parent venv. Replay its site directories and .pth
                    # files so editable DSLs and wheel bootstrap paths are retained.
                    parents = [
                        p
                        for p in sys.path
                        if Path(p).name in {"site-packages", "dist-packages"} and Path(p).is_dir()
                    ]
                    site_dir = subprocess.check_output(
                        [python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                        env=self.env,
                        text=True,
                        timeout=30,
                    ).strip()
                    (Path(site_dir) / "00-molou-image-sites.pth").write_text(
                        f"import site; [site.addsitedir(p) for p in {parents!r}]\n"
                    )
                packages = r["profile"]["packages"]
                if packages and self.command(
                    [python, "-m", "pip", "install", "--disable-pip-version-check", *packages],
                    setup_deadline,
                    self.run_dir,
                ):
                    raise RuntimeError(
                        "package installation failed; fix the local profile or image"
                    )
                # Capture resolved versions even when packages come from the base image.
                versions = (
                    "import importlib.metadata as m,json; "
                    "names={d.metadata['Name'] for d in m.distributions() "
                    "if d.metadata.get('Name')}; "
                    "print(json.dumps({name:m.version(name) for name in sorted(names)}))"
                )
                installed = subprocess.check_output(
                    [python, "-c", versions], env=self.env, text=True, timeout=30
                )
                ready.write_text(installed)
            self.emit(
                "environment",
                status="ready",
                reused=reused,
                packages=json.loads(ready.read_text()),
                **environment,
            )
        self.env.update(VIRTUAL_ENV=str(venv), PATH=f"{venv / 'bin'}:{self.env.get('PATH', '')}")
        argv = [python, "-u", str(self.run_dir / "solution.py"), *r.get("args", [])]
        if r.get("mode", "run") != "run":
            self.profile_command(argv)
            return
        self.emit("phase", phase="running")
        self.emit(
            "execution",
            argv=argv,
            cwd=str(self.run_dir),
            # Do not serialize arbitrary image/profile environment variables.
            env={"CUDA_VISIBLE_DEVICES": self.env["CUDA_VISIBLE_DEVICES"]},
        )
        code = self.command(
            argv,
            time.monotonic() + r["run_timeout"],
            self.run_dir,
        )
        self.emit("result", status="succeeded" if code == 0 else "failed", exit_code=code)

    def invocation(self, argv: list[str], stage: str) -> None:
        self.emit(
            "execution",
            argv=argv,
            cwd=str(self.run_dir),
            stage=stage,
            env={"CUDA_VISIBLE_DEVICES": self.env["CUDA_VISIBLE_DEVICES"]},
        )

    def profile_command(self, target: list[str]) -> None:
        """Wrap an arbitrary argv; neither GPU selection nor target arguments are rewritten."""
        r = self.request
        mode = r["mode"]
        executable = shutil.which(mode, path=self.env.get("PATH"))
        if executable is None:
            raise RuntimeError(
                f"{mode} is not installed or not on the Pod PATH; update the image/profile PATH"
            )
        version = subprocess.run(
            [executable, "--version"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if version.returncode:
            raise RuntimeError(f"{mode} --version failed: {version.stderr[:2000]}")
        report_base = str(self.run_dir / "report")
        report_path = Path(report_base + (".ncu-rep" if mode == "ncu" else ".nsys-rep"))
        metadata: dict[str, Any] = {
            "mode": mode,
            "executable": executable,
            "version": (version.stdout + version.stderr).strip()[:4096],
            "remote_reports": [],
            "collection_status": "running",
            "export_status": "pending",
        }
        self.emit("profiling", **metadata)
        self.output("system", metadata["version"] + "\nShared GPU: profiling affects timings.\n")
        args = r["profiler_args"]
        if mode == "ncu":
            argv = [
                executable,
                "--config-file",
                "off",
                *args,
                "--clock-control",
                "none",
                "--export",
                report_base,
                *target,
            ]
        else:
            argv = [executable, "profile", *args, "--output", report_base, *target]
        self.emit("phase", phase="profiling")
        self.invocation(argv, "capture")
        stopped = None
        code = None
        try:
            code = self.command(
                argv,
                time.monotonic() + r["run_timeout"],
                self.run_dir,
                graceful=True,
            )
        except Stopped as exc:
            stopped = exc
        if report_path.is_file() and report_path.stat().st_size:
            metadata["remote_reports"] = [str(report_path)]
            self.output(
                "system",
                f"Raw report (Pod only): {report_path}\nDeleting the Pod removes this report.\n",
            )
        metadata["collection_status"] = (
            stopped.status if stopped else "succeeded" if code == 0 else "failed"
        )
        self.emit("profiling", **metadata)
        if stopped:
            metadata["export_status"] = "skipped"
            self.emit("profiling", **metadata)
            raise stopped
        if not metadata["remote_reports"]:
            metadata["export_status"] = "missing_report"
            self.emit("profiling", **metadata)
            self.output(
                "system",
                "No non-empty profiler report was created; "
                "check filters and profiler errors above.\n",
            )
            self.emit("result", status="failed", exit_code=code)
            return
        if mode == "ncu":
            exports = [
                (
                    "details.txt",
                    [
                        executable,
                        "--import",
                        str(report_path),
                        "--page",
                        "details",
                        "--print-details",
                        "all",
                    ],
                ),
                (
                    "sass.txt",
                    [
                        executable,
                        "--import",
                        str(report_path),
                        "--page",
                        "source",
                        "--print-source",
                        "sass",
                    ],
                ),
            ]
        else:
            exports = [
                (
                    "stats.txt",
                    [
                        executable,
                        "stats",
                        "--quiet",
                        "--report",
                        "cuda_gpu_kern_sum,cuda_api_sum,cuda_gpu_mem_time_sum",
                        "--format",
                        "column",
                        str(report_path),
                    ],
                )
            ]
        metadata["export_status"] = "exporting"
        self.emit("profiling", **metadata)
        self.emit("phase", phase="exporting")
        ok = True
        try:
            for name, command in exports:
                self.invocation(command, "export")
                exit_code = self.command(
                    command,
                    time.monotonic() + r.get("export_timeout", 300),
                    self.run_dir,
                    report=name,
                )
                ok &= exit_code == 0
                self.output("system", f"Text report: {name} (export exit {exit_code})\n")
        except Stopped:
            metadata["export_status"] = "interrupted"
            self.emit("profiling", **metadata)
            raise
        metadata["export_status"] = "succeeded" if ok else "failed"
        self.emit("profiling", **metadata)
        self.emit("result", status="succeeded" if code == 0 and ok else "failed", exit_code=code)


def main() -> None:
    os.umask(0o077)
    request = json.load(sys.stdin)
    worker = Worker(request)
    try:
        worker.execute()
    except Stopped as exc:
        worker.emit("result", status=exc.status, exit_code=None)
    except BrokenPipeError:
        # command() has already killed only the process group it created.
        return
    except Exception as exc:
        worker.emit("output", stream="system", data=f"\n{type(exc).__name__}: {exc}\n")
        worker.emit("result", status="failed", exit_code=None)


if __name__ == "__main__":
    main()
