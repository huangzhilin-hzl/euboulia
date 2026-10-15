import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_control import _failed_run, _lock, _runtime

from euboulia.control import TaskManager
from euboulia.server import ControlApplication, create_server


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    control: bool = False,
    method: str | None = None,
):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if control:
        headers["X-Euboulia-Control"] = "1"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(
        urllib.request.Request(
            url, data=data, headers=headers, method=method or ("POST" if data else "GET")
        ),
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
    assert detail["pod"] is None
    assert detail["artifacts"] == {"manifest": None, "items": []}
    assert [source["source"] for source in detail["log_sources"]] == [
        "controller",
        "kubernetes",
        "worker_stdout",
        "worker_stderr",
        "sglang_stdout",
        "sglang_stderr",
    ]
    assert "values" not in json.dumps(detail).lower()


def test_application_reads_run_logs_incrementally(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    lock, _ = _lock(tmp_path)
    manager = TaskManager(runtime)
    application = ControlApplication(manager)
    run = application.submit({"recipe": str(lock), "executor": "gpu", "node": "worker-8"})["run"]
    assert isinstance(run, dict)
    run_uid = str(run["run_uid"])
    log = tmp_path / "local-state" / "controller-logs" / f"{run_uid}.log"
    log.parent.mkdir(parents=True)
    log.write_text("Pod created\n", encoding="utf-8")

    first = application.run_log(run_uid, source="controller", after=0)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("Pod Ready\n")
    second = application.run_log(
        run_uid,
        source="controller",
        after=int(first["next_offset"]),
    )

    assert first["content"] == "Pod created\n"
    assert second["content"] == "Pod Ready\n"
    assert second["offset"] == first["next_offset"]


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
            assert "RUN ACTIVITY" in html
            assert '"console", "Console"' in html
            assert "status-breathe" in html
            assert "prefers-reduced-motion" in html
            assert "/logs?source=" in html
            assert 'aria-label", "Copy run ID"' in html
            assert "navigator.clipboard.writeText" in html
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
            run_uid = payload["run"]["run_uid"]
        log = tmp_path / "local-state" / "controller-logs" / f"{run_uid}.log"
        log.parent.mkdir(parents=True)
        log.write_text("controller online\n", encoding="utf-8")
        with _request(url + f"/api/runs/{run_uid}/logs?source=controller&after=0") as response:
            payload = json.load(response)
            assert payload["content"] == "controller online\n"
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


def test_run_detail_exposes_recorded_pod_identity(tmp_path: Path) -> None:
    manager, run, run_dir = _failed_run(tmp_path)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_uid": run.run_uid,
                "pod": "euboulia-owned-pod",
                "namespace": "private-inference",
                "pod_uid": "owned-pod-uid",
            }
        )
    )
    detail = ControlApplication(manager).run_detail(run.run_uid)
    assert detail["pod"] == {
        "name": "euboulia-owned-pod",
        "namespace": "private-inference",
        "uid": "owned-pod-uid",
    }


def test_delete_routes_protect_controls_and_remove_failed_experiment(tmp_path: Path) -> None:
    manager, run, _ = _failed_run(tmp_path)
    server = create_server(manager, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/runs/{run.run_uid}"
    try:
        for path in [url, url + "/pod"]:
            with pytest.raises(urllib.error.HTTPError) as rejected:
                _request(path, method="DELETE")
            assert rejected.value.code == 403
        with _request(url, method="DELETE", control=True) as response:
            assert json.load(response) == {"deleted": run.run_uid}
        with pytest.raises(urllib.error.HTTPError) as missing:
            _request(url)
        assert missing.value.code == 404
        with _request(f"http://127.0.0.1:{server.server_port}/api/runs") as response:
            assert json.load(response) == {"runs": []}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_delete_routes_report_conflicts_without_changing_running_run(tmp_path: Path) -> None:
    manager = TaskManager(_runtime(tmp_path))
    lock, _ = _lock(tmp_path)
    run = manager.submit(recipe=lock, executor="gpu", node="worker-8")
    manager.store.update(run.run_uid, status="running")
    server = create_server(manager, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/runs/{run.run_uid}"
    try:
        for path in [url, url + "/pod"]:
            with pytest.raises(urllib.error.HTTPError) as rejected:
                _request(path, method="DELETE", control=True)
            assert rejected.value.code == 409
            assert "error" in json.load(rejected.value)
        assert manager.store.get(run.run_uid).status == "running"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
