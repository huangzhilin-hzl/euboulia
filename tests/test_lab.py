"""Connectivity contracts use real HTTP, SQLite, and bounded owned subprocesses."""

import argparse
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from euboulia.lab.cli import add_lab_parser, pair_agent
from euboulia.lab.connector import Connector, LabClient, connect, save_private
from euboulia.lab.server import LabServer
from euboulia.lab.store import LabStore


@pytest.fixture
def lab(tmp_path):
    store = LabStore(tmp_path / "server")
    server = LabServer(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield store, server, url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def identity(store, name="Prism"):
    created = store.create_agent(
        {"name": name, "role": "Analyze evidence", "capabilities": "Read traces"}
    )
    return store.pair(
        {
            "code": created["pairing_code"],
            "host": "test-host",
            "workspace": "/research",
            "runtime": "command",
        }
    )


def job(store, agent, **overrides):
    return store.submit(
        {
            "agent_id": agent["agent_id"],
            "request_id": secrets.token_hex(8),
            "kind": "task",
            "prompt": "Analyze the supplied test evidence",
            "context": {"room_id": "room-a", "goal_id": "goal-a"},
            **overrides,
        }
    )


def request(url, path, data=None, *, token="", headers=None):
    merged = {
        "Content-Type": "application/json",
        "X-Lab-Control": "1",
        "Authorization": "Bearer " + token,
        **(headers or {}),
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(
        urllib.request.Request(
            url + path, data=json.dumps(data).encode() if data is not None else None, headers=merged
        ),
        timeout=5,
    )


def test_pairing_is_single_use_expiring_and_private(tmp_path):
    store = LabStore(tmp_path)
    created = store.create_agent({"name": "Agent", "role": "Research", "capabilities": "Read"})
    payload = {
        "code": created["pairing_code"],
        "host": "host",
        "workspace": "/repo",
        "runtime": "codex",
    }
    agent = store.pair(payload)
    assert store.authenticate(agent["token"]) == created["agent_id"]
    with pytest.raises(PermissionError):
        store.pair(payload)
    replacement = store.repair(created["agent_id"])
    with pytest.raises(PermissionError):
        store.authenticate(agent["token"])
    with store.db() as db:
        db.execute("UPDATE agents SET pairing_until=0")
    with pytest.raises(PermissionError):
        store.pair({**payload, "code": replacement["pairing_code"]})
    assert created["pairing_code"].encode() not in store.path.read_bytes()
    assert agent["token"].encode() not in store.path.read_bytes()
    assert store.key_path.stat().st_mode & 0o777 == 0o600
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_minimal_identity_keeps_legacy_capabilities_compatible(lab):
    store, _, url = lab
    with request(
        url,
        "/api/lab/agents",
        {"name": "Scout", "role": "Compare repositories"},
        token=store.admin_key,
    ) as response:
        created = json.load(response)
    minimal = store.pair(
        {
            "code": created["pairing_code"],
            "host": "test",
            "workspace": "/agent",
            "runtime": "command",
        }
    )
    legacy = identity(store, "Legacy")
    assert minimal["role"] == "Compare repositories"
    assert minimal["capabilities"] == ""
    assert legacy["capabilities"] == "Read traces"
    assert len(LabStore(store.path.parent).snapshot()["agents"]) == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "", "role": "Research"},
        {"name": "Scout", "role": " "},
        {"name": "Scout", "role": "Research", "capabilities": []},
    ],
)
def test_minimal_identity_rejects_invalid_fields(tmp_path, fields):
    store = LabStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_agent(fields)
    assert store.snapshot()["agents"] == []


def test_authentication_roles_origins_and_static_path(lab):
    store, _, url = lab
    agent = identity(store)
    for path, body, headers, token in [
        ("/api/lab/state", None, {}, ""),
        ("/api/lab/state", None, {}, agent["token"]),
        ("/api/lab/agents", {"name": "intruder"}, {}, agent["token"]),
        ("/api/lab/state", None, {"Host": "attacker.example"}, store.admin_key),
        ("/api/lab/jobs", {}, {"Origin": "https://attacker.example"}, store.admin_key),
        ("/api/lab/jobs", {}, {"X-Lab-Control": "0"}, store.admin_key),
    ]:
        with pytest.raises(urllib.error.HTTPError) as error:
            request(url, path, body, token=token, headers=headers)
        assert error.value.code == 403
    with request(url, "/prototypes/inference-world/") as response:
        page = response.read()
        assert b"loop.mjs" in page and b"loop.css" in page
        assert b'src="room.mjs"' not in page
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    with request(url, "/prototypes/inference-world/team.html") as response:
        assert b"lab.css" in response.read()
    with request(url, "/prototypes/inference-world/research.html") as response:
        assert b"research.mjs" in response.read()
    for asset in (
        "research.mjs", "research-state.mjs", "research.css",
        "loop.mjs", "loop-state.mjs", "loop.css",
    ):
        with request(url, "/prototypes/inference-world/" + asset) as response:
            assert response.status == 200
            assert response.read()
    for path in ("/../admin.key", "/%2e%2e/admin.key", "/README.md"):
        with pytest.raises(urllib.error.HTTPError) as error:
            request(url, path)
        assert error.value.code == 404
    with request(url, "/api/lab/login", {"key": store.admin_key}) as response:
        cookie = response.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    with request(url, "/api/lab/state", headers={"Cookie": cookie}) as response:
        assert len(json.load(response)["agents"]) == 1
    request(url, "/api/lab/logout", {}, headers={"Cookie": cookie}).close()
    with pytest.raises(urllib.error.HTTPError):
        request(url, "/api/lab/state", headers={"Cookie": cookie})


def test_scoped_claim_and_report(lab):
    store, _, url = lab
    alice, bob = identity(store, "Alice"), identity(store, "Bob")
    first, second = job(store, alice), job(store, bob)
    client = LabClient(url, alice["token"])
    claim = client.call("poll", {"instance": "alice-process"})
    assert claim["job"]["id"] == first["id"]
    assert "lease" not in store.detail(first["id"])["job"]
    with pytest.raises(PermissionError):
        LabClient(url, bob["token"]).call(
            "report",
            {
                "job_id": first["id"],
                "lease": claim["lease"],
                "state": "completed",
                "text": "forged",
            },
        )
    assert store.detail(second["id"])["job"]["state"] == "queued"


def test_atomic_claim_and_instance_conflict(tmp_path):
    store = LabStore(tmp_path)
    agent = identity(store)
    first = job(store, agent)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(lambda _: store.poll(agent["agent_id"], "same-process"), range(2))
        )
    assert sum(item["job"] is not None for item in claims) == 1
    with pytest.raises(ValueError, match="Another connector"):
        store.poll(agent["agent_id"], "other-process")
    assert store.detail(first["id"])["job"]["state"] == "running"


def test_dispatch_idempotency_and_restart_persistence(tmp_path):
    store = LabStore(tmp_path)
    agent = identity(store)
    first = job(store, agent, request_id="same-request")
    assert job(store, agent, request_id="same-request")["id"] == first["id"]
    with pytest.raises(ValueError, match="different content"):
        job(store, agent, request_id="same-request", prompt="changed")
    reopened = LabStore(tmp_path)
    assert reopened.admin_key == store.admin_key
    assert reopened.poll(agent["agent_id"], "process")["job"]["id"] == first["id"]


def test_references_are_per_dispatch_and_preserve_legacy_retries(tmp_path):
    store = LabStore(tmp_path)
    agent = identity(store)
    original_context = {"room_id": "room-a", "goal_id": "goal-a"}
    legacy = job(store, agent, request_id="legacy", context=original_context)
    assert "resources" not in legacy["context"]
    retry = job(store, agent, request_id="legacy", context={**original_context, "resources": " "})
    assert retry["id"] == legacy["id"]
    references = "/repos/one\n/repos/two\nhttps://example.org/research"
    current = job(
        store, agent, request_id="references", context={**original_context, "resources": references}
    )
    assert current["context"]["resources"] == references
    with pytest.raises(ValueError, match="different content"):
        job(
            store,
            agent,
            request_id="references",
            context={**original_context, "resources": "/repos/other"},
        )
    later = job(store, agent, context={**original_context, "resources": "/repos/other"})
    assert later["context"]["resources"] == "/repos/other"
    assert LabStore(tmp_path).detail(current["id"])["job"]["context"]["resources"] == references


def test_lease_expiry_never_requeues_execution(tmp_path):
    store = LabStore(tmp_path)
    agent = identity(store)
    first = job(store, agent)
    claim = store.poll(agent["agent_id"], "process")
    with store.db() as db:
        db.execute("UPDATE jobs SET lease_until=0")
        db.execute("UPDATE agents SET heartbeat=0")
    assert store.poll(agent["agent_id"], "restarted")["job"] is None
    assert store.detail(first["id"])["job"]["state"] == "interrupted"
    assert store.report(
        agent["agent_id"],
        {"job_id": first["id"], "lease": claim["lease"], "state": "completed", "text": "late"},
    )["stop"]
    assert store.detail(first["id"])["job"]["state"] == "interrupted"


def test_event_deduplication_cancellation_and_revoke(tmp_path):
    store = LabStore(tmp_path)
    agent = identity(store)
    first = job(store, agent)
    claim = store.poll(agent["agent_id"], "process")
    report = {
        "job_id": first["id"],
        "lease": claim["lease"],
        "state": "running",
        "text": "Progress",
        "sequence": 1,
    }
    store.report(agent["agent_id"], report)
    store.report(agent["agent_id"], report)
    assert len(store.detail(first["id"])["events"]) == 1
    with pytest.raises(ValueError):
        store.report(agent["agent_id"], {**report, "text": "changed"})
    store.cancel(first["id"])
    assert store.report(agent["agent_id"], {**report, "sequence": 0})["stop"]
    store.report(agent["agent_id"], {**report, "sequence": 2, "state": "completed"})
    assert store.detail(first["id"])["job"]["state"] == "cancelled"
    second = job(store, agent)
    store.revoke(agent["agent_id"])
    assert store.detail(second["id"])["job"]["state"] == "cancelled"
    with pytest.raises(PermissionError):
        store.authenticate(agent["token"])


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"prompt": 3},
        {"kind": "execute-shell"},
        {"context": "bad"},
        {"context": {"room_id": []}},
        {"context": {"resources": []}},
        {"context": {"resources": "x" * 8001}},
    ],
)
def test_malformed_jobs_fail_closed(lab, data):
    store, _, url = lab
    agent = identity(store)
    payload = {
        "agent_id": agent["agent_id"],
        "request_id": "test",
        "kind": "task",
        "prompt": "hello",
    }
    if isinstance(data, dict):
        payload.update(data)
    else:
        payload = data
    # JSON null must be sent as a POST body, not translated to GET by our helper.
    if data is None:
        payload = {"prompt": None}
    with pytest.raises(urllib.error.HTTPError) as error:
        request(url, "/api/lab/jobs", payload, token=store.admin_key)
    assert error.value.code == 400


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.test",
        "https://example.com",
        "http://127.0.0.1/path",
        "http://key@localhost:8773",
        "http://localhost:8773#key",
    ],
)
def test_connector_rejects_non_loopback_or_credential_urls(url):
    with pytest.raises(ValueError):
        LabClient(url)


def configured_connector(lab, tmp_path, script, timeout=10):
    store, _, url = lab
    created = store.create_agent(
        {"name": "Test agent", "role": "Inspect evidence", "capabilities": "Read"}
    )
    adapter = tmp_path / "adapter.py"
    adapter.write_text(script)
    config = tmp_path / "connector" / "agent.json"
    connect(
        config,
        url,
        created["pairing_code"],
        tmp_path,
        "command",
        [sys.executable, str(adapter)],
        timeout,
    )
    return Connector(config)


def test_cli_default_workspace_reads_multiple_external_references(
    lab, tmp_path, monkeypatch, capsys
):
    store, _, url = lab
    created = store.create_agent({"name": "Researcher", "role": "Compare source repositories"})
    sources = [tmp_path / "repo-one" / "source.txt", tmp_path / "repo-two" / "source.txt"]
    for index, source in enumerate(sources):
        source.parent.mkdir()
        source.write_text(f"source {index}")
    adapter = tmp_path / "adapter.py"
    adapter.write_text("""
import json, os
from pathlib import Path
import sys
data = json.load(sys.stdin)
sources = data['job']['context']['resources'].splitlines()
result = {'cwd': os.getcwd(), 'sources': [Path(p).read_text() for p in sources]}
print(json.dumps({'type': 'completed', 'text': json.dumps(result)}), flush=True)
""")
    command_file = tmp_path / "argv.json"
    command_file.write_text(json.dumps([sys.executable, str(adapter)]))
    config = tmp_path / "researcher" / "agent.json"
    parser = argparse.ArgumentParser()
    add_lab_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "lab",
            "connect",
            "--url",
            url,
            "--config",
            str(config),
            "--runtime",
            "command",
            "--command-file",
            str(command_file),
        ]
    )
    assert args.workspace is None
    monkeypatch.setattr("euboulia.lab.cli.getpass.getpass", lambda _: created["pairing_code"])
    assert pair_agent(args) == 0
    connector = Connector(config)
    workspace = config.parent / "workspace"
    assert connector.settings["workspace"] == str(workspace)
    assert workspace.stat().st_mode & 0o777 == 0o700
    output = capsys.readouterr().out
    assert str(workspace) in output
    assert created["pairing_code"] not in output
    assert connector.settings["token"] not in output
    assert store.snapshot()["agents"][0]["workspace"] == str(workspace)
    current = job(store, connector.settings, context={"resources": "\n".join(map(str, sources))})
    connector.run(once=True)
    result = json.loads(store.detail(current["id"])["job"]["result"])
    assert result == {"cwd": str(workspace), "sources": ["source 0", "source 1"]}
    assert Connector(config).settings["workspace"] == str(workspace)


def test_default_workspaces_are_separate_and_existing_contents_survive(lab, tmp_path):
    store, _, url = lab
    workspaces = []
    for name in ("one", "two"):
        config = tmp_path / name / "agent.json"
        workspace = config.parent / "workspace"
        workspace.mkdir(parents=True)
        note = workspace / "note.txt"
        note.write_text(name)
        created = store.create_agent({"name": name, "role": "Research"})
        settings = connect(
            config, url, created["pairing_code"], None, "command", [sys.executable], 10
        )
        assert Path(settings["workspace"]) == workspace
        assert note.read_text() == name
        with pytest.raises(ValueError, match="already exists"):
            connect(config, url, created["pairing_code"], None, "command", [sys.executable], 10)
        assert note.read_text() == name
        workspaces.append(workspace)
    assert workspaces[0] != workspaces[1]


@pytest.mark.parametrize("invalid_kind", ["file", "symlink"])
def test_invalid_default_workspace_does_not_consume_pairing_code(lab, tmp_path, invalid_kind):
    store, _, url = lab
    config = tmp_path / "agent" / "agent.json"
    config.parent.mkdir()
    workspace = config.parent / "workspace"
    target = tmp_path / "existing-repository"
    target.mkdir(mode=0o755)
    if invalid_kind == "file":
        workspace.write_text("preserve")
    else:
        workspace.symlink_to(target, target_is_directory=True)
    created = store.create_agent({"name": "Researcher", "role": "Research"})
    with pytest.raises(ValueError, match="regular directory"):
        connect(config, url, created["pairing_code"], None, "command", [sys.executable], 10)
    assert not config.exists()
    assert target.stat().st_mode & 0o777 == 0o755
    assert store.snapshot()["agents"][0]["status"] == "unpaired"
    # Explicit paths remain supported and retain their permissions and contents.
    settings = connect(
        config, url, created["pairing_code"], target, "command", [sys.executable], 10
    )
    assert settings["workspace"] == str(target)
    assert target.stat().st_mode & 0o777 == 0o755


def test_connector_executes_real_process_with_context_and_returns_result(lab, tmp_path):
    store, _, _ = lab
    connector = configured_connector(
        lab,
        tmp_path,
        """
import json, sys
data = json.load(sys.stdin)
print(json.dumps({'type':'progress', 'text':'Received scoped input'}), flush=True)
print(json.dumps({'type':'completed', 'text':data['job']['context']['goal_id']}), flush=True)
""",
    )
    first = job(store, connector.settings)
    connector.run(once=True)
    detail = store.detail(first["id"])
    assert detail["job"]["state"] == "completed"
    assert detail["job"]["result"] == "goal-a"
    assert detail["events"][0]["text"] == "Received scoped input"
    assert connector.config.stat().st_mode & 0o777 == 0o600
    assert "token" not in json.dumps(store.snapshot())


def test_connector_timeout_without_reading_stdin(lab, tmp_path):
    store, _, _ = lab
    connector = configured_connector(lab, tmp_path, "import time\ntime.sleep(20)\n", timeout=1)
    first = job(store, connector.settings, prompt="测" * 16000)
    started = time.monotonic()
    connector.run(once=True)
    assert time.monotonic() - started < 5
    assert store.detail(first["id"])["job"]["state"] == "failed"
    assert "timeout" in store.detail(first["id"])["job"]["result"]


def test_connector_obeys_cancellation(lab, tmp_path):
    store, _, _ = lab
    connector = configured_connector(lab, tmp_path, "import time\ntime.sleep(20)\n")
    first = job(store, connector.settings)
    thread = threading.Thread(target=connector.run, kwargs={"once": True})
    thread.start()
    deadline = time.monotonic() + 4
    while store.detail(first["id"])["job"]["state"] == "queued" and time.monotonic() < deadline:
        time.sleep(0.02)
    store.cancel(first["id"])
    thread.join(7)
    assert not thread.is_alive()
    assert store.detail(first["id"])["job"]["state"] == "cancelled"


def test_restarted_connector_reports_interruption_without_reexecuting(lab, tmp_path):
    store, _, _ = lab
    connector = configured_connector(lab, tmp_path, "raise AssertionError('must not run')")
    first = job(store, connector.settings)
    claim = store.poll(connector.settings["agent_id"], "crashed-process")
    save_private(connector.state_path, {"sessions": {}, "active": claim})
    restarted = Connector(connector.config)
    restarted.flush_final()
    assert store.detail(first["id"])["job"]["state"] == "interrupted"
    assert "active" not in restarted.state


def test_codex_adapter_keeps_scoped_sessions_and_read_only_flags(lab, tmp_path):
    connector = configured_connector(lab, tmp_path, "")
    connector.settings["runtime"] = "codex"
    connector.settings["command"] = ["codex"]
    first = job(lab[0], connector.settings)
    argv, prompt = connector.invocation(first)
    assert 'sandbox_mode="read-only"' in argv
    assert 'approval_policy="never"' in argv
    assert "--ignore-user-config" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert connector.settings["token"].encode() not in prompt
    connector.state["sessions"][connector.context_key(first)] = "owned-session"
    argv, _ = connector.invocation(first)
    assert argv[-3:] == ["resume", "owned-session", "-"]
    second = {**first, "context": {"room_id": "room-a", "goal_id": "different-goal"}}
    assert "resume" not in connector.invocation(second)[0]


def test_codex_events_resume_and_private_reasoning_filter(lab, tmp_path):
    connector = configured_connector(
        lab,
        tmp_path,
        """
import json, sys
sys.stdin.read()
print(json.dumps({'type':'thread.started', 'thread_id':'owned-session'}))
print(json.dumps({'type':'item.completed',
                  'item':{'type':'reasoning', 'text':'PRIVATE_REASONING'}}))
print(json.dumps({'type':'item.completed',
                  'item':{'type':'agent_message', 'text':'Scoped response'}}))
print(json.dumps({'type':'turn.completed'}))
""",
    )
    connector.settings["runtime"] = "codex"
    first = job(lab[0], connector.settings)
    connector.run(once=True)
    assert lab[0].detail(first["id"])["job"]["result"] == "Scoped response"
    assert "PRIVATE_REASONING" not in json.dumps(lab[0].detail(first["id"]))
    assert connector.state["sessions"][connector.context_key(first)] == "owned-session"


def test_cancel_before_start_never_launches_adapter(lab, tmp_path):
    connector = configured_connector(lab, tmp_path, "raise AssertionError('must not launch')")
    first = job(lab[0], connector.settings)
    claim = lab[0].poll(connector.settings["agent_id"], connector.instance)
    lab[0].cancel(first["id"])
    connector.execute(claim)
    connector.flush_final()
    detail = lab[0].detail(first["id"])["job"]
    assert detail["state"] == "cancelled"
    assert "before starting" in detail["result"]


def test_final_delivery_retries_without_reexecuting(lab, tmp_path, monkeypatch):
    connector = configured_connector(
        lab,
        tmp_path,
        """
import json
print(json.dumps({'type':'completed','text':'Receipt survives network failure'}))
""",
    )
    first = job(lab[0], connector.settings)
    claim = lab[0].poll(connector.settings["agent_id"], connector.instance)
    connector.execute(claim)
    original = connector.client.call

    def offline(*args, **kwargs):
        raise urllib.error.URLError("test outage")

    monkeypatch.setattr(connector.client, "call", offline)
    with pytest.raises(urllib.error.URLError):
        connector.flush_final()
    assert json.loads(connector.state_path.read_text())["final"]["state"] == "completed"
    monkeypatch.setattr(connector.client, "call", original)
    connector.flush_final()
    assert lab[0].detail(first["id"])["job"]["result"] == "Receipt survives network failure"
    assert "active" not in connector.state


def test_disconnect_allows_immediate_reconnection(lab):
    store, _, url = lab
    agent = identity(store)
    client = LabClient(url, agent["token"])
    client.call("poll", {"instance": "first"})
    client.call("disconnect", {"instance": "first"})
    assert client.call("poll", {"instance": "second"})["job"] is None


def test_empty_admin_key_is_rejected(tmp_path):
    (tmp_path / "admin.key").write_text("")
    with pytest.raises(ValueError, match="admin key"):
        LabStore(tmp_path)
