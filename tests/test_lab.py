"""Connectivity contracts use real HTTP, SQLite, and bounded owned subprocesses."""

import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

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
        assert b"lab.css" in response.read()
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
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
