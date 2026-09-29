"""Recorded inference scopes and conservative CPU-launch-to-GPU attribution.

Names are interpreted only for explicit annotations, never from kernel spelling.
The index stores the exact scope and launch event IDs used for every assignment.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any

GPU_KINDS = {"gpu_kernel", "communication", "memory_transfer", "memory_set"}
FIELDS = ("phase", "module", "step")
PREFIX = "euboulia::"


def scope_fields(name: str, kind: str, args: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for field, keys in {
        "phase": ("phase", "Phase", "forward_mode"),
        "module": ("module", "module_name", "layer_name"),
        "step": ("step", "step_id", "engine_step"),
    }.items():
        for key in keys:
            if isinstance(args.get(key), (str, int)) and not isinstance(args[key], bool):
                fields[field] = str(args[key])[:1000]
                break
    if kind == "annotation":
        if name.startswith(PREFIX):
            try:
                value = json.loads(name[len(PREFIX) :])
            except ValueError:
                value = None
            if isinstance(value, dict):
                for field in FIELDS:
                    if isinstance(value.get(field), (str, int)) and not isinstance(
                        value[field], bool
                    ):
                        fields[field] = str(value[field])[:1000]
        elif name.lower() in {"prefill", "decode", "mixed", "extend"}:
            fields.setdefault("phase", name.lower())
        else:
            # SGLang explicitly records ForwardMode in its CPU step range.
            match = re.fullmatch(r"step\[([A-Z_]+) bs=\d+(?: toks=\d+)?\]", name)
            if match:
                fields.setdefault("phase", match[1].lower())
    return {
        field: value
        for field, value in fields.items()
        if value and value.lower() not in {"unknown", "none", "null"}
    }


def attribute_events(db: sqlite3.Connection) -> None:
    """Propagate containing CPU scopes, then unique launch IDs, within each file.

    GPU timestamps are never compared with CPU range boundaries for attribution.
    Nested scopes win; conflicting overlapping scopes stay unassigned.
    """
    db.row_factory = sqlite3.Row
    files = [row[0] for row in db.execute("SELECT DISTINCT file FROM events")]
    for file in files:
        active: list[tuple[sqlite3.Row, dict[str, str]]] = []
        track: tuple[str, str] | None = None
        launches: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        cursor = db.execute(
            "SELECT * FROM events WHERE file=? AND kind NOT IN "
            "('gpu_kernel','communication','memory_transfer','memory_set') "
            "ORDER BY pid,tid,ts,dur DESC,id",
            (file,),
        )
        for row in cursor:
            this_track = (row["pid"], row["tid"])
            if track != this_track:
                active = []
                track = this_track
            active = [(r, f) for r, f in active if r["ts"] + r["dur"] > row["ts"]]
            own = scope_fields(row["name"], row["kind"], json.loads(row["args"]))
            evidence: dict[str, Any] = {}
            for field in FIELDS:
                parents = [
                    (r, f)
                    for r, f in active
                    if field in f and r["ts"] + r["dur"] >= row["ts"] + row["dur"]
                ]
                if field in own:
                    evidence[field] = {
                        "value": own[field],
                        "method": "recorded",
                        "scope_ids": [row["id"]],
                    }
                elif parents:
                    parents.sort(key=lambda pair: pair[0]["dur"])
                    leaf, values = parents[0]
                    # Non-nested overlapping ranges do not establish a scope tree.
                    valid = all(
                        r["ts"] <= leaf["ts"] and r["ts"] + r["dur"] >= leaf["ts"] + leaf["dur"]
                        for r, _ in parents
                    )
                    if valid:
                        evidence[field] = {
                            "value": values[field],
                            "method": "cpu_scope",
                            "scope_ids": [r["id"] for r, _ in reversed(parents)],
                        }
            _write(db, row["id"], evidence)
            corr = json.loads(row["corr"])
            if row["kind"] == "cuda_api" and corr.get("launch"):
                launches[corr["launch"]].append((row["id"], evidence))
            if own and row["kind"] in {"annotation", "cpu_operator"}:
                active.append((row, own))
                if len(active) > 1024:
                    raise ValueError("semantic scope overlap exceeds the 1024-range limit")
        for row in db.execute(
            "SELECT * FROM events WHERE file=? AND kind IN "
            "('gpu_kernel','communication','memory_transfer','memory_set')",
            (file,),
        ):
            own = scope_fields(row["name"], row["kind"], json.loads(row["args"]))
            evidence = {
                f: {"value": v, "method": "recorded", "scope_ids": [row["id"]]}
                for f, v in own.items()
            }
            candidates = launches.get(json.loads(row["corr"]).get("launch", ""), [])
            if len(candidates) == 1:
                launch, context = candidates[0]
                for field, source in context.items():
                    if field not in evidence:
                        evidence[field] = {**source, "method": "cuda_launch", "launch_id": launch}
            _write(db, row["id"], evidence)
    db.execute("CREATE INDEX semantic_idx ON events(phase,module,step,rank)")


def _write(db: sqlite3.Connection, event_id: int, evidence: dict[str, Any]) -> None:
    db.execute(
        "UPDATE events SET phase=?,module=?,step=?,evidence=? WHERE id=?",
        (*(evidence.get(f, {}).get("value") for f in FIELDS), json.dumps(evidence), event_id),
    )


def union_ns(intervals: list[tuple[int, int]]) -> int:
    total = 0
    end: int | None = None
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return total


def phase_overview(db: sqlite3.Connection) -> dict[str, Any]:
    """Per-rank activity unions; never sum clock domains into elapsed time."""
    phases: dict[tuple[str, str], dict[str, Any]] = {}
    intervals: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    assigned = total = 0
    for row in db.execute(
        "SELECT phase,rank,ts,dur FROM events WHERE kind IN "
        "('gpu_kernel','communication','memory_transfer','memory_set')"
    ):
        total += 1
        assigned += row["phase"] is not None
        key = (row["phase"] or "", row["rank"])
        item = phases.setdefault(
            key, {"phase": row["phase"], "rank": row["rank"], "count": 0, "activity_ns": 0}
        )
        item["count"] += 1
        item["activity_ns"] += row["dur"]
        intervals[key].append((row["ts"], row["ts"] + row["dur"]))
    for key, item in phases.items():
        item["busy_ns"] = union_ns(intervals[key])
        item["projected_span_ns"] = max(b for _, b in intervals[key]) - min(
            a for a, _ in intervals[key]
        )
    return {
        "gpu_events": total,
        "assigned_gpu_events": assigned,
        "coverage": assigned / total if total else None,
        "ranks": list(phases.values()),
        "steps": [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT step FROM events WHERE step IS NOT NULL ORDER BY step LIMIT 1000"
            )
        ],
        "modules": [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT module FROM events WHERE module IS NOT NULL "
                "ORDER BY module LIMIT 1000"
            )
        ],
        "critical_path": "unverified",
    }
