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

    def command(self, argv: list[str], deadline: float, cwd: Path) -> int:
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
        try:
            while selector.get_map() or process.poll() is None:
                self.check(deadline)
                for key, _ in selector.select(0.1):
                    data = os.read(key.fd, 4096)
                    stream = key.data
                    text = decoders[stream].decode(data, final=not data)
                    if text:
                        self.output(stream, text)
                    if not data:
                        selector.unregister(key.fileobj)
                # A script may leave child processes holding its pipes open.
                if process.poll() is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
            return process.wait()
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
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
        self.emit("phase", phase="running")
        argv = [python, "-u", str(self.run_dir / "solution.py"), *r.get("args", [])]
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
