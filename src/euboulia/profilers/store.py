"""Local, bounded profile evidence queries. Raw artifacts are never rewritten.

A disposable SQLite index retains event timing and correlation data. Summary-only
captures stay summary-only: missing events are never reconstructed from totals.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, cast

from euboulia.profilers.parsers import iter_trace_records

_INDEX_VERSION = 1
_MAX_EVENTS = 2_000_000
_MAX_RECORDS = 6_000_000
_MAX_TIME = 2**53 - 1
_BUILD_SLOTS = threading.BoundedSemaphore(2)
_MAX_JSON = 32 * 1024 * 1024
_GPU = {"gpu_kernel", "communication", "memory_transfer", "memory_set"}


def _read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > _MAX_JSON:
        raise ValueError("profile metadata exceeds 32 MiB")
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("profile metadata must be an object")
    return result


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("profile path escapes the run artifact directory")
    return resolved


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _kind(category: str, name: str) -> str:
    if category in {"kernel", "gpu_kernel"}:
        return (
            "communication"
            if any(s in name.lower() for s in ("nccl", "allreduce", "all_gather"))
            else "gpu_kernel"
        )
    if (
        category in {"memory_transfer", "memory_set"}
        or "memcpy" in category.lower()
        or "memset" in category.lower()
    ):
        return "memory_transfer"
    if "runtime" in category.lower() or "driver" in category.lower():
        return "cuda_api"
    if category in {"cpu_op", "operator", "cpu_operator"}:
        return "cpu_operator"
    if any(s in category.lower() for s in ("annotation", "nvtx", "python")):
        return "annotation"
    return "other"


def _ident(args: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if isinstance(args.get(key), (str, int)):
            return str(args[key])[:1000]
    return None


def _corr(args: dict[str, Any]) -> dict[str, str]:
    output = {}
    for domain, keys in {
        "launch": ("correlation", "Correlation ID", "correlation_id"),
        "external": ("External id", "External Id", "external_id"),
        "batch": ("batch_id", "Batch ID"),
        "request": ("request_id", "Request ID"),
    }.items():
        value = _ident(args, *keys)
        if value is not None:
            output[domain] = value
    return output


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class ProfileStore:
    """Read profiles belonging to a known run; derive indexes in a separate cache."""

    def __init__(self, run_dir: Path, cache_dir: Path) -> None:
        self.root = run_dir.resolve()
        self.cache = cache_dir
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def _captures(self) -> dict[str, Path]:
        candidates: list[Path] = []
        for pattern in (
            "artifacts/**/profile/manifest.json",
            "*-recovery/snapshot/**/profile/manifest.json",
            "profile-replay-*/profile/manifest.json",
        ):
            candidates.extend(self.root.glob(pattern))
        result = {}
        for path in sorted(set(candidates))[:200]:
            try:
                path = _inside(path, self.root)
                relative = path.relative_to(self.root).as_posix()
                key = hashlib.sha256(relative.encode()).hexdigest()[:20]
                result[key] = path
            except ValueError:
                continue
        return result

    def _capture(self, key: str) -> tuple[Path, dict[str, Any]]:
        path = self._captures().get(key)
        if path is None:
            raise KeyError("profile not found")
        return path, _read(path)

    def _raw(self, manifest: Path, data: dict[str, Any]) -> list[dict[str, Any]]:
        records = data.get("raw_traces", [])
        if not isinstance(records, list):
            raise ValueError("invalid raw trace manifest")
        result = []
        names: set[str] = set()
        for index, record in enumerate(records[:256]):
            if not isinstance(record, dict):
                continue
            name = Path(str(record.get("path", ""))).name
            if not name or name in names:
                raise ValueError("ambiguous raw trace name")
            names.add(name)
            local = _inside(manifest.parent / "raw" / name, self.root)
            exists = local.is_file()
            result.append(
                {
                    "id": str(index),
                    "name": name,
                    "rank": str(record.get("rank", index)),
                    "available": exists,
                    "path": local,
                    "size_bytes": local.stat().st_size if exists else record.get("size_bytes"),
                    "sha256": record.get("sha256"),
                    "state": "local"
                    if exists
                    else "evicted"
                    if record.get("retained") is False
                    else "missing_local",
                }
            )
        return result

    def list_captures(self) -> list[dict[str, Any]]:
        result = []
        for key, path in self._captures().items():
            try:
                data = _read(path)
                raw = self._raw(path, data)
                result.append(
                    {
                        "id": key,
                        "profile_id": data.get("profile_id"),
                        "candidate_id": data.get("candidate_id"),
                        "workload_point": data.get("workload_point", "Legacy capture"),
                        "purpose": data.get("purpose", "diagnostic"),
                        "location": path.parent.relative_to(self.root).as_posix(),
                        "raw_available": sum(r["available"] for r in raw),
                        "raw_expected": len(raw),
                        "status": data.get("status", "unknown"),
                    }
                )
            except (ValueError, OSError) as exc:
                result.append({"id": key, "status": "invalid", "error": str(exc)})
        return result

    def _signature(self, manifest: Path, raw: list[dict[str, Any]]) -> str:
        fingerprint = [str(_INDEX_VERSION), str(manifest), _digest(manifest)]
        for record in raw:
            if record["available"]:
                stat = record["path"].stat()
                fingerprint.append(f"{record['name']}:{stat.st_size}:{stat.st_mtime_ns}")
        return hashlib.sha256("\0".join(fingerprint).encode()).hexdigest()

    def _ensure(
        self, key: str, manifest: Path, raw: list[dict[str, Any]]
    ) -> tuple[Path | None, dict[str, Any]]:
        if not any(r["available"] for r in raw):
            return None, {"state": "summary_only"}
        signature = self._signature(manifest, raw)
        target = self.cache / f"{signature}.sqlite"
        with self._lock:
            if target.is_file():
                return target, {"state": "ready"}
            if signature not in self._jobs:
                state: dict[str, Any] = {"state": "indexing", "events": 0}
                self._jobs[signature] = state
                threading.Thread(
                    target=self._build_queued, args=(target, raw, state), daemon=True
                ).start()
            return None, dict(self._jobs[signature])

    def _build_queued(self, target: Path, raw: list[dict[str, Any]], state: dict[str, Any]) -> None:
        with _BUILD_SLOTS:
            self._build(target, raw, state)

    def _build(self, target: Path, raw: list[dict[str, Any]], state: dict[str, Any]) -> None:
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.building")
        db: sqlite3.Connection | None = None
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(temporary)
            db.executescript("""
                PRAGMA journal_mode=OFF;
                CREATE TABLE events(id INTEGER PRIMARY KEY, file TEXT, rank TEXT,
                    pid TEXT, tid TEXT, stream TEXT, phase TEXT, kind TEXT, name TEXT,
                    ts INTEGER, dur INTEGER, args TEXT, corr TEXT);
                CREATE TABLE links(event INTEGER, file TEXT, domain TEXT, value TEXT);
                CREATE TABLE flow(file TEXT, flow_id TEXT, ph TEXT, ts INTEGER, pid TEXT, tid TEXT);
                CREATE TABLE meta(value TEXT);
            """)
            count = 0
            skipped = 0
            records_seen = 0
            phases: Counter[str] = Counter()
            kinds: Counter[str] = Counter()
            stack_count = shape_count = correlation_count = flow_count = 0
            truncated = False
            for record in raw:
                if not record["available"]:
                    continue
                path = record["path"]
                if record["sha256"] and _digest(path) != record["sha256"]:
                    raise ValueError(f"raw trace checksum mismatch: {record['name']}")
                pending: dict[tuple[str, str], list[dict[str, Any]]] = {}
                pending_count = 0
                for event in iter_trace_records(path):
                    records_seen += 1
                    if count >= _MAX_EVENTS or records_seen > _MAX_RECORDS:
                        truncated = True
                        break
                    ph = event.get("ph")
                    ts = _number(event.get("ts"))
                    if ph in {"s", "t", "f"} and ts is not None and abs(ts) < 9e15:
                        if flow_count < _MAX_EVENTS:
                            db.execute(
                                "INSERT INTO flow VALUES(?,?,?,?,?,?)",
                                (
                                    record["id"],
                                    json.dumps(
                                        [
                                            event.get("cat"),
                                            event.get("scope"),
                                            event.get("id", event.get("id2")),
                                        ],
                                        sort_keys=True,
                                    ),
                                    ph,
                                    round(ts * 1000),
                                    str(event.get("pid", "")),
                                    str(event.get("tid", "")),
                                ),
                            )
                            flow_count += 1
                        continue
                    track = (str(event.get("pid", "")), str(event.get("tid", "")))
                    if ph == "B":
                        if pending_count >= 10_000:
                            truncated = True
                            break
                        pending.setdefault(track, []).append(event)
                        pending_count += 1
                        continue
                    if ph == "E":
                        stack = pending.get(track, [])
                        if not stack or ts is None:
                            skipped += 1
                            continue
                        begin = stack.pop()
                        pending_count -= 1
                        start = _number(begin.get("ts"))
                        if start is None:
                            skipped += 1
                            continue
                        event = {**begin, "ph": "X", "dur": ts - start}
                        ts = start
                        ph = "X"
                    duration = _number(event.get("dur"))
                    if ph != "X":
                        continue
                    if (
                        ts is None
                        or duration is None
                        or duration < 0
                        or abs(ts) + duration >= 9e15
                        or not isinstance(event.get("name"), str)
                    ):
                        skipped += 1
                        continue
                    raw_args = event.get("args")
                    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
                    args_text = json.dumps(args, ensure_ascii=False)
                    if len(args_text) > 32_000:
                        args = {"_truncated": True, "retained_keys": list(args)[:100]}
                        args_text = json.dumps(args)
                    corr = _corr(args)
                    name = cast(str, event["name"])[:4000]
                    kind = _kind(str(event.get("cat", "")), name)
                    phase = _ident(args, "phase", "Phase", "forward_mode")
                    stream = _ident(args, "stream", "Stream", "stream id")
                    count += 1
                    db.execute(
                        "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            count,
                            record["id"],
                            record["rank"],
                            *track,
                            stream,
                            phase,
                            kind,
                            name,
                            round(ts * 1000),
                            round(duration * 1000),
                            args_text,
                            json.dumps(corr),
                        ),
                    )
                    db.executemany(
                        "INSERT INTO links VALUES(?,?,?,?)",
                        [(count, record["id"], domain, value) for domain, value in corr.items()],
                    )
                    kinds[kind] += 1
                    if phase:
                        phases[phase] += 1
                    correlation_count += bool(corr)
                    stack_count += any("stack" in k.lower() for k in args)
                    shape_count += any("dim" in k.lower() or "shape" in k.lower() for k in args)
                    if count % 5000 == 0:
                        state["events"] = count
                skipped += sum(len(stack) for stack in pending.values())
                if truncated:
                    break
            if not count:
                raise ValueError("no usable duration events in retained traces")
            origin, end = db.execute("SELECT MIN(ts), MAX(ts+dur) FROM events").fetchone()
            if end - origin > _MAX_TIME:
                raise ValueError("trace time span exceeds supported range")
            db.executescript("""
                CREATE INDEX time_idx ON events(ts);
                CREATE INDEX rank_time_idx ON events(rank,ts);
                CREATE INDEX name_idx ON events(name,kind);
                CREATE INDEX link_idx ON links(file,domain,value);
                CREATE INDEX flow_idx ON flow(file,flow_id);
            """)
            meta = {
                "events": count,
                "origin_ns": str(origin),
                "duration_ns": end - origin,
                "kinds": dict(kinds),
                "phases": dict(phases),
                "skipped_events": skipped,
                "stack_events": stack_count,
                "shape_events": shape_count,
                "correlated_events": correlation_count,
                "flow_events": flow_count,
                "truncated": truncated,
                "checksum_verified": all(r["sha256"] for r in raw if r["available"]),
            }
            db.execute("INSERT INTO meta VALUES(?)", (json.dumps(meta),))
            db.commit()
            db.close()
            db = None
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(target)
            state.update(state="ready", events=count)
        except Exception as exc:
            state.update(state="error", error=str(exc))
        finally:
            if db is not None:
                db.close()
            temporary.unlink(missing_ok=True)

    def _open(self, key: str) -> tuple[sqlite3.Connection | None, dict[str, Any], dict[str, Any]]:
        manifest, data = self._capture(key)
        db_path, state = self._ensure(key, manifest, self._raw(manifest, data))
        if db_path is None:
            return None, data, state
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db, data, state

    def detail(self, key: str) -> dict[str, Any]:
        manifest, data = self._capture(key)
        raw = self._raw(manifest, data)
        db, _, state = self._open(key)
        summary_path = _inside(manifest.parent / "summary.json", self.root)
        summary = _read(summary_path) if summary_path.is_file() else {}
        result: dict[str, Any] = {
            "id": key,
            "manifest": data,
            "index": state,
            "files": [{k: v for k, v in r.items() if k != "path"} for r in raw],
            "summary_rows": summary.get("summary_row_count"),
            "raw_observations": summary.get("raw_observation_count"),
            "dropped_aggregate_keys": summary.get("dropped_aggregate_keys"),
            "hypotheses": [
                _read(_inside(p, self.root))
                for p in sorted(
                    (self.root / "profile-analysis" / key / "hypotheses").glob("*.json")
                )[:100]
            ],
            "warnings": [
                "Diagnostic evidence only. Performance verdicts require an unprofiled A/B run.",
                "Ranks are separate clock domains until alignment is verified; "
                "no cross-rank critical path is inferred.",
                "Activity duration shares are not request latency shares. "
                "CPU nesting and GPU overlap can double-count time.",
            ],
        }
        if len([r for r in raw if r["available"]]) < len(raw):
            result["warnings"].append("Some or all rank traces are unavailable locally.")
        if summary.get("dropped_aggregate_keys"):
            result["warnings"].append(
                "The compact summary dropped groups; summary shares use retained groups only."
            )
        if db is None:
            result["hotspots"] = self._summary_hotspots(summary)
            result["warnings"].append(
                "Original event timing is unavailable until a retained trace is indexed."
            )
        else:
            with db:
                result["quality"] = json.loads(db.execute("SELECT value FROM meta").fetchone()[0])
                origin = int(result["quality"]["origin_ns"])
                result["tracks"] = [
                    {**dict(r), "start": r["start"] - origin, "end": r["end"] - origin}
                    for r in db.execute(
                        "SELECT rank,kind,stream,pid,tid,COUNT(*) AS count,MIN(ts) AS start,"
                        "MAX(ts+dur) AS end FROM events GROUP BY rank,kind,stream,pid,tid "
                        "ORDER BY rank,kind"
                    )
                ]
                result["hotspots"] = self._hotspots(db)
            db.close()
        return result

    @staticmethod
    def _summary_hotspots(summary: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        observations = summary.get("observations", [])
        if not isinstance(observations, list):
            return rows
        for item in observations[:20_000]:
            if not isinstance(item, dict):
                continue
            duration = _number(item.get("duration_ns"))
            count = _number(item.get("count"))
            if duration is None or duration < 0 or count is None or count <= 0:
                continue
            rows.append(
                {
                    "name": str(item.get("name", "Unknown")),
                    "kind": _kind(
                        str(item.get("subject_kind", "other")), str(item.get("name", ""))
                    ),
                    "rank": item.get("rank"),
                    "phase": item.get("phase"),
                    "count": count,
                    "total_ns": duration,
                    "mean_ns": duration / count,
                    "min_ns": None,
                    "max_ns": None,
                }
            )
        return ProfileStore._shares(rows)

    @staticmethod
    def _shares(
        rows: list[dict[str, Any]], denominators: Counter[tuple[str, str]] | None = None
    ) -> list[dict[str, Any]]:
        totals: Counter[tuple[str, str]] = Counter()
        for row in rows:
            domain = "gpu_activity" if row["kind"] in _GPU else str(row["kind"])
            row["share_basis"] = domain + " / rank " + str(row["rank"])
            totals[(domain, str(row["rank"]))] += row["total_ns"]
        if denominators is not None:
            totals = denominators
        for row in rows:
            domain = "gpu_activity" if row["kind"] in _GPU else str(row["kind"])
            total = totals[(domain, str(row["rank"]))]
            row["share"] = row["total_ns"] / total if total else 0
        return sorted(rows, key=lambda r: r["total_ns"], reverse=True)[:5000]

    @staticmethod
    def _hotspots(db: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT name,kind,rank,phase,COUNT(*) AS count,SUM(dur) AS total_ns,"
                "AVG(dur) AS mean_ns,MIN(dur) AS min_ns,MAX(dur) AS max_ns "
                "FROM events GROUP BY name,kind,rank,phase ORDER BY total_ns DESC LIMIT 5000"
            )
        ]
        totals: Counter[tuple[str, str]] = Counter()
        for row in db.execute("SELECT kind,rank,SUM(dur) AS total FROM events GROUP BY kind,rank"):
            domain = "gpu_activity" if row["kind"] in _GPU else str(row["kind"])
            totals[(domain, str(row["rank"]))] += row["total"]
        return ProfileStore._shares(rows, totals)

    def timeline(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        rank: str = "",
        kind: str = "",
        name: str = "",
        limit: int = 2500,
    ) -> dict[str, Any]:
        if (
            not 0 <= start <= _MAX_TIME
            or limit < 1
            or limit > 5000
            or (end is not None and not start < end <= _MAX_TIME)
        ):
            raise ValueError("invalid timeline window or limit")
        db, _, state = self._open(key)
        if db is None:
            return {"index": state, "events": []}
        try:
            meta = json.loads(db.execute("SELECT value FROM meta").fetchone()[0])
            origin = int(meta["origin_ns"])
            end = meta["duration_ns"] if end is None else end
            where = "ts+dur>=? AND ts<=?"
            args: list[Any] = [origin + start, origin + end]
            for field, value in (("rank", rank), ("kind", kind), ("name", name)):
                if value:
                    where += f" AND {field}=?"
                    args.append(value)
            rows = db.execute(
                f"SELECT * FROM events WHERE {where} ORDER BY ts,id LIMIT ?", (*args, limit + 1)
            ).fetchall()
            events = [self._event(row, origin) for row in rows[:limit]]
            return {
                "index": state,
                "events": events,
                "limited": len(rows) > limit,
                "start_ns": start,
                "end_ns": end,
                "duration_ns": meta["duration_ns"],
            }
        finally:
            db.close()

    @staticmethod
    def _event(row: sqlite3.Row, origin: int) -> dict[str, Any]:
        result = dict(row)
        result["ts"] -= origin
        result["args"] = json.loads(result["args"])
        result["corr"] = json.loads(result["corr"])
        return result

    def event(self, key: str, event_id: int) -> dict[str, Any]:
        if not 1 <= event_id <= _MAX_EVENTS:
            raise ValueError("invalid event id")
        db, _, state = self._open(key)
        if db is None:
            return {"index": state}
        try:
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError("event not found")
            origin = int(
                json.loads(db.execute("SELECT value FROM meta").fetchone()[0])["origin_ns"]
            )
            matches = db.execute(
                "SELECT DISTINCT e.* FROM links a JOIN links b ON a.file=b.file "
                "AND a.domain=b.domain AND a.value=b.value JOIN events e ON e.id=b.event "
                "WHERE a.event=? AND e.id!=? ORDER BY e.ts LIMIT 100",
                (event_id, event_id),
            ).fetchall()
            parents = db.execute(
                "SELECT * FROM events WHERE file=? AND pid=? AND tid=? AND ts<=? "
                "AND ts+dur>=? AND dur>? AND id!=? ORDER BY dur LIMIT 10",
                (
                    row["file"],
                    row["pid"],
                    row["tid"],
                    row["ts"],
                    row["ts"] + row["dur"],
                    row["dur"],
                    event_id,
                ),
            ).fetchall()
            flows = db.execute(
                "SELECT DISTINCT b.* FROM flow a JOIN flow b ON a.file=b.file "
                "AND a.flow_id=b.flow_id WHERE a.file=? AND a.pid=? AND a.tid=? "
                "AND a.ts>=? AND a.ts<=? ORDER BY b.ts LIMIT 100",
                (row["file"], row["pid"], row["tid"], row["ts"], row["ts"] + row["dur"]),
            ).fetchall()
            return {
                "event": self._event(row, origin),
                "flows": [{**dict(r), "ts": r["ts"] - origin} for r in flows],
                "correlated": [self._event(r, origin) for r in matches],
                "enclosing": [self._event(r, origin) for r in parents],
            }
        finally:
            db.close()

    def raw_path(self, key: str, file_id: str) -> Path:
        manifest, data = self._capture(key)
        for record in self._raw(manifest, data):
            if record["id"] == file_id and record["available"]:
                return Path(record["path"])
        raise KeyError("raw trace unavailable locally")

    def save_hypothesis(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        _, manifest = self._capture(key)
        title = payload.get("title", "")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 300:
            raise ValueError("hypothesis needs a title of 1-300 characters")
        for field in ("evidence", "validation", "rejection"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise ValueError(f"{field} is required")
            if len(payload[field]) > 10_000:
                raise ValueError(f"{field} exceeds 10,000 characters")
        destination = _inside(self.root / "profile-analysis" / key / "hypotheses", self.root)
        destination.mkdir(parents=True, exist_ok=True)
        content = {
            "schema_version": 1,
            "profile_id": manifest.get("profile_id"),
            "capture_id": key,
            "source_revision": manifest.get("source_revision"),
            "title": title.strip(),
            **{f: payload[f] for f in ("evidence", "validation", "rejection")},
            "state": "draft",
            "gate_eligible": False,
        }
        identity = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20]
        path = destination / f"{identity}.json"
        # Same evidence and text reuse one immutable draft.
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(content, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
        return {"id": identity, "path": str(path), "state": "draft"}
