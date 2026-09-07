import gzip
import hashlib
import json
import time
from pathlib import Path

import pytest

from euboulia.profilers import store as module
from euboulia.profilers.store import ProfileStore


def make_profile(root: Path, *, retained: bool = True) -> tuple[ProfileStore, str]:
    folder = root / "artifacts" / "target-validation" / "profile"
    (folder / "raw").mkdir(parents=True)
    records = []
    for rank in range(2):
        events = [
            {"ph": "B", "name": "forward", "cat": "cpu_op", "ts": 100, "pid": 1, "tid": 1},
            {
                "ph": "X",
                "name": "cudaLaunchKernel",
                "cat": "cuda_runtime",
                "ts": 110,
                "dur": 5,
                "pid": 1,
                "tid": 1,
                "args": {"correlation": 42, "External id": 3},
            },
            {"ph": "s", "cat": "launch", "id": 7, "ts": 111, "pid": 1, "tid": 1},
            {
                "ph": "X",
                "name": "gemm",
                "cat": "kernel",
                "ts": 120,
                "dur": 40 + 10 * rank,
                "pid": 2,
                "tid": 7,
                "args": {
                    "correlation": 42,
                    "stream": 7,
                    "phase": "decode",
                    "Call stack": "model.py:12",
                    "Input Dims": [[8, 16]],
                },
            },
            {"ph": "f", "cat": "launch", "id": 7, "ts": 120, "pid": 2, "tid": 7},
            {
                "ph": "X",
                "name": "ncclAllReduce",
                "cat": "kernel",
                "ts": 130,
                "dur": 10,
                "pid": 2,
                "tid": 8,
            },
            {"ph": "E", "ts": 200, "pid": 1, "tid": 1},
            {"ph": "X", "name": "invalid", "ts": 10, "dur": -2},
        ]
        path = folder / "raw" / f"rank-{rank}.trace.json.gz"
        with gzip.open(path, "wt") as handle:
            json.dump({"traceEvents": events}, handle)
        records.append(
            {
                "path": f"/worker/profile/raw/{path.name}",
                "rank": str(rank),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
                "retained": retained,
            }
        )
        if not retained:
            path.unlink()
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "profile_id": "profile-fixture",
                "source_revision": "a" * 40,
                "raw_traces": records,
                "workload_point": "test-point",
                "status": "complete",
            }
        )
    )
    (folder / "summary.json").write_text(
        json.dumps(
            {
                "raw_observation_count": 8,
                "observations": [
                    {
                        "name": "gemm",
                        "subject_kind": "gpu_kernel",
                        "rank": "0",
                        "count": 2,
                        "duration_ns": 40_000,
                    }
                ],
            }
        )
    )
    store = ProfileStore(root, root.parent / "cache")
    return store, store.list_captures()[0]["id"]


def ready(store: ProfileStore, key: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = store.detail(key)
        if result["index"]["state"] != "indexing":
            return result
        time.sleep(0.01)
    raise AssertionError("index did not complete")


def test_index_timing_correlations_flows_and_shares(tmp_path: Path) -> None:
    store, key = make_profile(tmp_path / "run")
    detail = ready(store, key)
    assert detail["index"]["state"] == "ready"
    assert detail["quality"] == {
        "events": 8,
        "origin_ns": "100000",
        "duration_ns": 100000,
        "kinds": {"cuda_api": 2, "gpu_kernel": 2, "communication": 2, "cpu_operator": 2},
        "phases": {"decode": 2},
        "skipped_events": 2,
        "stack_events": 2,
        "shape_events": 2,
        "correlated_events": 4,
        "flow_events": 4,
        "truncated": False,
        "checksum_verified": True,
    }
    gemm = next(h for h in detail["hotspots"] if h["name"] == "gemm" and h["rank"] == "0")
    assert gemm["share"] == 0.8  # GPU activity only, separately for rank 0.
    assert gemm["share_basis"] == "gpu_activity / rank 0"
    timeline = store.timeline(key, start=19000, end=21000, rank="0", kind="gpu_kernel")
    event = timeline["events"][0]
    assert event["ts"] == 20000 and event["dur"] == 40000
    evidence = store.event(key, event["id"])
    assert [(e["name"], e["rank"]) for e in evidence["correlated"]] == [("cudaLaunchKernel", "0")]
    assert evidence["event"]["args"]["Call stack"] == "model.py:12"
    assert [f["ph"] for f in evidence["flows"]] == ["s", "f"]
    launch = store.event(key, evidence["correlated"][0]["id"])
    assert [e["name"] for e in launch["enclosing"]] == ["forward"]
    assert store.timeline(key, limit=1)["limited"] is True
    assert all(t["start"] >= 0 for t in detail["tracks"])
    assert store.timeline(key, name="' OR 1=1 --")["events"] == []
    assert ready(ProfileStore(store.root, store.cache), key)["index"]["state"] == "ready"


def test_summary_only_does_not_invent_events_or_extrema(tmp_path: Path) -> None:
    store, key = make_profile(tmp_path / "run", retained=False)
    detail = ready(store, key)
    assert detail["index"]["state"] == "summary_only"
    assert "quality" not in detail
    assert detail["hotspots"][0]["mean_ns"] == 20_000
    assert detail["hotspots"][0]["min_ns"] is None
    assert store.timeline(key)["events"] == []
    with pytest.raises(KeyError):
        store.raw_path(key, "0")


def test_checksum_mismatch_fails_closed_and_truncation_is_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    store, key = make_profile(tmp_path / "bad")
    with store.raw_path(key, "0").open("ab") as handle:
        handle.write(b"changed")
    assert "checksum mismatch" in ready(store, key)["index"]["error"]
    store, key = make_profile(tmp_path / "bounded")
    monkeypatch.setattr(module, "_MAX_EVENTS", 2)
    detail = ready(store, key)
    assert detail["quality"]["truncated"] is True
    assert detail["quality"]["events"] == 2


def test_capture_namespace_paths_and_numeric_bounds(tmp_path: Path) -> None:
    store, key = make_profile(tmp_path / "run")
    ready(store, key)
    with pytest.raises(KeyError):
        store.detail("../manifest.json")
    with pytest.raises(ValueError):
        store.timeline(key, start=2**100)
    with pytest.raises(ValueError):
        store.timeline(key, start=20, end=10)
    with pytest.raises(ValueError):
        store.event(key, 2**100)
    with pytest.raises(KeyError):
        store.event(key, 1000)
    path = store.raw_path(key, "0")
    path.unlink()
    path.symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="escapes"):
        store.raw_path(key, "0")


def test_hypotheses_are_durable_scoped_drafts(tmp_path: Path) -> None:
    store, key = make_profile(tmp_path / "run", retained=False)
    payload = {
        "title": "Investigate launch gaps",
        "evidence": "Recorded CUDA calls",
        "validation": "Unprofiled A/B plus a diagnostic trace",
        "rejection": "No improvement",
    }
    with pytest.raises(ValueError, match="rejection"):
        store.save_hypothesis(key, {**payload, "rejection": ""})
    first = store.save_hypothesis(key, payload)
    assert store.save_hypothesis(key, payload) == first
    reloaded = ProfileStore(store.root, tmp_path / "replacement-cache").detail(key)["hypotheses"]
    assert len(reloaded) == 1
    assert reloaded[0]["source_revision"] == "a" * 40
    assert reloaded[0]["gate_eligible"] is False
    assert reloaded[0]["state"] == "draft"
    assert Path(first["path"]).is_relative_to(store.root)


def semantic_profile(root: Path, events: list[dict]) -> tuple[ProfileStore, str]:
    folder = root / "artifacts" / "target-validation" / "profile"
    (folder / "raw").mkdir(parents=True)
    path = folder / "raw" / "rank-0.trace.json"
    path.write_text(json.dumps({"traceEvents": events}))
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "raw_traces": [
                    {
                        "path": str(path),
                        "rank": "0",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                ]
            }
        )
    )
    store = ProfileStore(root, root.parent / "semantic-cache")
    return store, store.list_captures()[0]["id"]


def duration(name, ts, dur, cat="user_annotation", tid=1, args=None):
    return {
        "ph": "X",
        "name": name,
        "ts": ts,
        "dur": dur,
        "cat": cat,
        "pid": 1 if cat != "kernel" else 2,
        "tid": tid,
        "args": args or {},
    }


def test_stage_attribution_follows_launch_after_cpu_range_and_retains_evidence(tmp_path):
    # GPU execution occurs AFTER both CPU annotations ended. Timestamp containment
    # would misattribute this event to the next phase.
    store, key = semantic_profile(
        tmp_path / "run",
        [
            duration('euboulia::{"phase":"decode","step":"7"}', 0, 10),
            duration('euboulia::{"module":"layers.12.moe"}', 1, 8),
            duration("cudaLaunchKernel", 3, 1, "cuda_runtime", args={"correlation": 42}),
            duration('euboulia::{"phase":"prefill","step":"8"}', 15, 40),
            duration("gemm", 20, 20, "kernel", args={"correlation": 42}),
            duration("gemm", 25, 20, "kernel", tid=2, args={"correlation": 42}),
            duration("decode_named_kernel", 50, 5, "kernel"),
        ],
    )
    result = ready(store, key)
    assert result["stages"]["coverage"] == 2 / 3
    phase = next(r for r in result["stages"]["ranks"] if r["phase"] == "decode")
    assert phase["activity_ns"] == 40_000
    assert phase["busy_ns"] == 25_000  # overlapping GPU intervals counted once
    event = store.timeline(key, phase="decode", module="layers.12", step="7")["events"][-1]
    assert event["phase"] == "decode" and event["module"] == "layers.12.moe"
    assert event["evidence"]["phase"]["method"] == "cuda_launch"
    detail = store.event(key, event["id"])
    assert {r["name"] for r in detail["attribution_sources"]} == {
        'euboulia::{"phase":"decode","step":"7"}',
        'euboulia::{"module":"layers.12.moe"}',
        "cudaLaunchKernel",
    }
    scoped = store.detail(key, phase="decode", module="layers.12", step="7")
    gpu = [r for r in scoped["hotspots"] if r["kind"] == "gpu_kernel"]
    assert len(gpu) == 1 and gpu[0]["share"] == 1
    unknown = store.timeline(key, phase="__unknown__", kind="gpu_kernel")["events"]
    assert [r["name"] for r in unknown] == ["decode_named_kernel"]
    assert store.detail(key, phase="' OR 1=1 --")["hotspots"] == []


def test_ambiguous_launches_and_crossing_scopes_do_not_prove_phase(tmp_path):
    store, key = semantic_profile(
        tmp_path / "run",
        [
            duration("decode", 0, 20),
            duration("prefill", 10, 20),
            duration("cudaLaunchKernel", 12, 1, "cuda_runtime", args={"correlation": 1}),
            duration("kernel", 40, 5, "kernel", args={"correlation": 1}),
            duration("step[DECODE bs=1]", 50, 10),
            duration("cudaLaunchKernel", 52, 1, "cuda_runtime", args={"correlation": 2}),
            duration("cudaLaunchKernel", 70, 1, "cuda_runtime", args={"correlation": 2}),
            duration("kernel", 80, 5, "kernel", args={"correlation": 2}),
        ],
    )
    assert ready(store, key)["stages"]["coverage"] == 0
    assert all(not e["phase"] for e in store.timeline(key, kind="gpu_kernel")["events"])
