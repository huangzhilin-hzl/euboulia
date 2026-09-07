"""Self-service Lab setup commands."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import signal
from pathlib import Path
from types import FrameType
from typing import Any

from euboulia.lab.connector import Connector, connect
from euboulia.lab.server import LabServer
from euboulia.lab.store import LabStore


def add_lab_parser(subparsers: Any) -> None:
    lab = subparsers.add_parser("lab", help="connect local and remote agents to a research lab")
    commands = lab.add_subparsers(dest="lab_command", required=True)
    serve = commands.add_parser("serve", help="serve an authenticated Lab on loopback")
    serve.add_argument("--state-dir", type=Path, default=Path(".euboulia/lab"))
    serve.add_argument("--port", type=int, default=8773)
    serve.set_defaults(handler=serve_lab)
    token = commands.add_parser("token", help="print the private browser admin key")
    token.add_argument("--state-dir", type=Path, default=Path(".euboulia/lab"))
    token.set_defaults(handler=show_token)
    pair = commands.add_parser("connect", help="pair an agent; prompts for its one-time code")
    pair.add_argument("--url", default="http://127.0.0.1:8773")
    pair.add_argument("--workspace", type=Path, required=True)
    pair.add_argument(
        "--config",
        type=Path,
        required=True,
        help="private config path; use a separate directory for each agent",
    )
    pair.add_argument("--runtime", choices=["codex", "command"], default="codex")
    pair.add_argument(
        "--command-file", type=Path, help="local JSON argv array for a custom adapter"
    )
    pair.add_argument("--timeout", type=int, default=600, help="maximum seconds per invocation")
    pair.set_defaults(handler=pair_agent)
    agent = commands.add_parser("agent", help="run an already paired connector")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    run = agent_commands.add_parser(
        "run", help="poll the durable inbox and run the configured adapter"
    )
    run.add_argument("--config", type=Path, required=True)
    run.add_argument(
        "--once", action="store_true", help="poll once, execute at most one job, then exit"
    )
    run.set_defaults(handler=run_agent)


def serve_lab(args: argparse.Namespace) -> int:
    if not 0 <= args.port <= 65535:
        raise ValueError("Invalid port")
    store = LabStore(args.state_dir.expanduser().resolve())
    server = LabServer(store, port=args.port)
    print(f"Lab: http://127.0.0.1:{server.server_port}/prototypes/inference-world/", flush=True)
    print(f"Browser admin key: {store.key_path} (use euboulia lab token to display)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def show_token(args: argparse.Namespace) -> int:
    path = args.state_dir.expanduser().resolve() / "admin.key"
    if not path.exists():
        raise ValueError("Start euboulia lab serve first")
    print(path.read_text().strip())
    return 0


def pair_agent(args: argparse.Namespace) -> int:
    if not 1 <= args.timeout <= 86400:
        raise ValueError("Timeout must be between 1 and 86400 seconds")
    command = json.loads(args.command_file.read_text()) if args.command_file else None
    if command is not None and (
        not isinstance(command, list) or not all(isinstance(arg, str) and arg for arg in command)
    ):
        raise ValueError("Command file must contain a JSON argv array")
    if args.runtime == "codex" and command is not None:
        raise ValueError("--command-file is only supported with --runtime command")
    config = args.config.expanduser().resolve()
    if config.exists():
        raise ValueError("Configuration exists; use lab agent run to reconnect")
    code = getpass.getpass("One-time pairing code (from Lab → Connect agent): ").strip()
    identity = connect(config, args.url, code, args.workspace, args.runtime, command, args.timeout)
    print(f"Paired {identity['name']}. Private configuration: {config}")
    print("Run euboulia lab agent run --config <this-config-path> to come online.")
    return 0


def run_agent(args: argparse.Namespace) -> int:
    connector = Connector(args.config.expanduser().resolve())

    def stop(signum: int, frame: FrameType | None) -> None:
        connector.stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        print("Agent connector running; press Ctrl+C to stop.", flush=True)
        connector.run(once=args.once)
    finally:
        with contextlib.suppress(OSError, ValueError):
            connector.client.call("disconnect", {"instance": connector.instance})
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
