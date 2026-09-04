import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from test_control import _lock, _runtime

from euboulia.control import TaskManager
from euboulia.server import ControlApplication, create_server


def _request(url: str, *, body: dict[str, object] | None = None, control: bool = False):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if control:
        headers["X-Euboulia-Control"] = "1"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(
        urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET"),
        timeout=5,
    )


def test_application_exposes_real_run_detail_and_no_values(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    lock, _ = _lock(tmp_path)
    manager = TaskManager(runtime)
    application = ControlApplication(manager)

    payload = application.submit(
        {"recipe": str(lock), "executor": "gpu", "node": "worker-8", "name": "dsv4"}
    )
    run = payload["run"]
    assert isinstance(run, dict)
    detail = application.run_detail(str(run["run_uid"]))

    assert detail["run"] == run
    assert detail["progress"] is None
    assert detail["artifacts"] == {"manifest": None, "items": []}
    assert "values" not in json.dumps(detail).lower()


def test_loopback_server_serves_console_and_protects_controls(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    lock, _ = _lock(tmp_path)
    manager = TaskManager(runtime)
    server = create_server(manager, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    body = {"recipe": str(lock), "executor": "gpu", "node": "worker-8"}
    try:
        with _request(url + "/") as response:
            html = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
            assert "EUBOULIA" in html
            assert "Submit target validation" in html
        try:
            _request(url + "/api/runs", body=body)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:  # pragma: no cover - fail-closed control contract
            raise AssertionError("mutation without the control header was accepted")
        with _request(url + "/api/runs", body=body, control=True) as response:
            payload = json.load(response)
            assert response.status == 201
            assert payload["run"]["status"] == "queued"
        with _request(url + "/api/runs") as response:
            payload = json.load(response)
            assert len(payload["runs"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_rejects_non_loopback_binding(tmp_path: Path) -> None:
    manager = TaskManager(_runtime(tmp_path))

    try:
        create_server(manager, host="0.0.0.0")
    except ValueError as exc:
        assert "localhost" in str(exc)
    else:  # pragma: no cover - security boundary
        raise AssertionError("non-loopback server binding was accepted")
