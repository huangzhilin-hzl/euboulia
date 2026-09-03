"""Bounded active SGLang profiling and conservative rule analysis."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from euboulia.execution import ExecutionResult
from euboulia.optimization.config import SGLangProfilingConfig
from euboulia.optimization.contracts import (
    AnalysisReport,
    ArtifactRef,
    Capability,
    Finding,
    MemoryEntry,
    ProfileRequest,
    ProfileResult,
    StageContext,
)
from euboulia.profilers import (
    Observation,
    ProfileSource,
    SubjectKind,
    analyze_observations,
    iter_torch_chrome_trace,
)


class ProfileCaptureError(RuntimeError):
    """Raised when an owned active profile is incomplete or exceeds policy."""


@dataclass(slots=True)
class _Aggregate:
    subject_kind: SubjectKind
    name: str
    artifact: str
    device: str | None
    rank: str | None
    phase: str | None
    total_duration_ns: int = 0
    count: int = 0


class SGLangProfiler:
    """Trigger SGLang's bounded Torch profiler and retain a compact summary."""

    def __init__(self, config: SGLangProfilingConfig) -> None:
        self.config = config
        self._observations: dict[str, tuple[Observation, ...]] = {}

    def capture(
        self,
        request: ProfileRequest,
        context: StageContext,
        *,
        endpoint: str,
        run_workload: Callable[[], ExecutionResult],
    ) -> ProfileResult:
        required = {Capability.PROFILE_EXECUTION, Capability.BENCHMARK_EXECUTION}
        missing = sorted(required - context.authorizations, key=lambda item: item.value)
        if missing:
            raise ProfileCaptureError(
                "active profile requires capabilities: "
                + ", ".join(item.value for item in missing)
            )

        profile_root = context.artifact_dir / "profile"
        raw_dir = profile_root / "raw"
        if profile_root.exists() or profile_root.is_symlink():
            raise ProfileCaptureError(f"profile artifact path already exists: {profile_root}")
        raw_dir.mkdir(parents=True)
        free_bytes = shutil.disk_usage(profile_root).free
        if free_bytes < self.config.min_free_disk_bytes:
            raise ProfileCaptureError(
                "insufficient free disk for profile capture "
                f"({free_bytes} < {self.config.min_free_disk_bytes})"
            )

        payload = {
            "output_dir": str(raw_dir),
            "activities": list(self.config.activities),
            "start_step": self.config.start_step,
            "num_steps": self.config.num_steps,
            "merge_profiles": self.config.merge_profiles,
            "with_stack": self.config.with_stack,
            "record_shapes": self.config.record_shapes,
            "profile_by_stage": False,
            "profile_prefix": f"{context.run_id}-{context.iteration_id}",
        }
        started = False
        workload: ExecutionResult | None = None
        try:
            self._post_json(endpoint, "/start_profile", payload)
            started = True
            workload = run_workload()
            if not workload.succeeded:
                raise ProfileCaptureError(
                    "profile workload failed; inspect "
                    f"{workload.stdout_path} and {workload.stderr_path}"
                )
        finally:
            if started:
                self._post_json(endpoint, "/stop_profile", {})

        trace_paths = self._wait_for_traces(raw_dir)
        trace_ranks = self._trace_ranks(trace_paths)
        sizes = {path: path.stat().st_size for path in trace_paths}
        total_bytes = sum(sizes.values())
        byte_limit = min(request.max_bytes, self.config.max_raw_bytes)
        if total_bytes > byte_limit:
            raise ProfileCaptureError(
                f"profile traces exceed raw byte budget ({total_bytes} > {byte_limit})"
            )
        raw_records = []
        for path, size in sizes.items():
            raw_records.append(
                {
                    "path": str(path),
                    "rank": trace_ranks[path],
                    "size_bytes": size,
                    "sha256": _sha256_file(path),
                    "retained": self.config.keep_raw,
                }
            )

        observations, raw_observation_count, dropped_keys, matched_files = self._summarize(
            trace_paths,
            trace_ranks,
        )
        summary_path = profile_root / "summary.json"
        _write_json_durable(
            summary_path,
            {
                "schema_version": 1,
                "source": ProfileSource.TORCH_CHROME_TRACE.value,
                "raw_observation_count": raw_observation_count,
                "summary_row_count": len(observations),
                "dropped_aggregate_keys": dropped_keys,
                "observations": [item.to_dict() for item in observations],
            },
        )
        summary_digest = _sha256_file(summary_path)
        profile_seed = "\0".join(
            (
                request.candidate_id,
                request.source_revision,
                request.workload_digest,
                summary_digest,
                *(str(item["sha256"]) for item in raw_records),
            )
        )
        profile_id = f"profile-{hashlib.sha256(profile_seed.encode()).hexdigest()[:20]}"

        manifest_path = profile_root / "manifest.json"
        _write_json_durable(
            manifest_path,
            {
                "schema_version": 1,
                "status": "complete",
                "profile_id": profile_id,
                "provider": self.config.provider.value,
                "candidate_id": request.candidate_id,
                "source_revision": request.source_revision,
                "workload_digest": request.workload_digest,
                "capture": payload,
                "retention": {
                    "keep_raw": self.config.keep_raw,
                    "summary_path": str(summary_path),
                },
                "raw_total_bytes": total_bytes,
                "raw_traces": raw_records,
                "expected_rank_traces": self.config.expected_rank_traces,
                "required_kernel_pattern": self.config.required_kernel_pattern,
                "required_kernel_matched_files": sorted(matched_files),
                "workload_execution": None if workload is None else workload.to_dict(),
            },
        )

        # The manifest and compact summary must be durable before large traces are
        # evicted. A capture that fails before this point intentionally keeps raw
        # evidence for diagnosis.
        if not self.config.keep_raw:
            for path in trace_paths:
                path.unlink()
            _fsync_directory(raw_dir)

        references = [
            _artifact_ref(summary_path, "profile-summary", "application/json"),
            _artifact_ref(manifest_path, "profile-manifest", "application/json"),
        ]
        if self.config.keep_raw:
            references.extend(
                _artifact_ref(path, "profile-raw", _raw_media_type(path))
                for path in trace_paths
            )
        self._observations[profile_id] = observations
        return ProfileResult(
            profile_id=profile_id,
            provider=self.config.provider.value,
            candidate_id=request.candidate_id,
            artifacts=tuple(references),
            metrics={
                "raw_observation_count": raw_observation_count,
                "summary_row_count": len(observations),
                "raw_trace_count": len(trace_paths),
                "raw_bytes": total_bytes,
                "dropped_aggregate_keys": dropped_keys,
            },
            complete=True,
            metadata={
                "source_revision": request.source_revision,
                "workload_digest": request.workload_digest,
                "workload_point": self.config.workload_point,
                "raw_retained": self.config.keep_raw,
                "measurement_lane": "profile_diagnostic",
                "gate_eligible": False,
            },
        )

    def observations(self, profile_id: str) -> tuple[Observation, ...]:
        try:
            return self._observations[profile_id]
        except KeyError as exc:
            raise KeyError(f"unknown active profile: {profile_id}") from exc

    def _wait_for_traces(self, raw_dir: Path) -> tuple[Path, ...]:
        deadline = time.monotonic() + self.config.settle_timeout_seconds
        previous: tuple[tuple[str, int], ...] | None = None
        stable_polls = 0
        paths: tuple[Path, ...] = ()
        while time.monotonic() < deadline:
            paths = tuple(
                sorted(
                    (
                        path
                        for path in raw_dir.rglob("*.trace.json*")
                        if path.is_file() and not path.is_symlink()
                    ),
                    key=lambda item: str(item),
                )
            )
            state = tuple((str(path), path.stat().st_size) for path in paths)
            expected = self.config.expected_rank_traces
            expected_arrived = expected is None or len(paths) >= expected
            if state and state == previous and expected_arrived:
                stable_polls += 1
                if stable_polls >= 2:
                    break
            else:
                stable_polls = 0
            previous = state
            time.sleep(0.25)
        if not paths:
            raise ProfileCaptureError(f"SGLang produced no trace under {raw_dir}")
        expected = self.config.expected_rank_traces
        if expected is not None and len(paths) != expected:
            raise ProfileCaptureError(
                f"expected {expected} per-rank traces, found {len(paths)}"
            )
        return paths

    def _summarize(
        self,
        paths: tuple[Path, ...],
        trace_ranks: dict[Path, str],
    ) -> tuple[tuple[Observation, ...], int, int, set[str]]:
        aggregates: dict[tuple[object, ...], _Aggregate] = {}
        raw_count = 0
        dropped_keys = 0
        matched_files: set[str] = set()
        required = (
            None
            if self.config.required_kernel_pattern is None
            else re.compile(self.config.required_kernel_pattern)
        )
        aggregate_limit = max(10_000, self.config.max_summary_rows * 10)
        for path in paths:
            fallback_rank = trace_ranks[path]
            for observation in iter_torch_chrome_trace(path):
                raw_count += 1
                rank = observation.rank or fallback_rank
                if required is not None and required.search(observation.name):
                    matched_files.add(str(path))
                key = (
                    observation.subject_kind,
                    observation.name,
                    observation.device,
                    rank,
                    observation.phase,
                )
                aggregate = aggregates.get(key)
                if aggregate is None:
                    if len(aggregates) >= aggregate_limit:
                        dropped_keys += 1
                        continue
                    aggregate = _Aggregate(
                        subject_kind=observation.subject_kind,
                        name=observation.name,
                        artifact=str(path),
                        device=observation.device,
                        rank=rank,
                        phase=observation.phase,
                    )
                    aggregates[key] = aggregate
                aggregate.count += observation.count or 1
                aggregate.total_duration_ns += observation.duration_ns or 0

        if raw_count == 0:
            raise ProfileCaptureError(
                "SGLang traces contained no complete-duration profile events"
            )
        if required is not None and len(matched_files) != len(paths):
            missing = sorted(str(path) for path in paths if str(path) not in matched_files)
            raise ProfileCaptureError(
                "required kernel pattern was absent from trace(s): " + ", ".join(missing)
            )
        selected = sorted(
            aggregates.values(),
            key=lambda item: (item.total_duration_ns, item.count, item.name),
            reverse=True,
        )[: self.config.max_summary_rows]
        observations = tuple(
            Observation(
                observation_id=f"summary:{index}",
                source=ProfileSource.TORCH_CHROME_TRACE,
                subject_kind=item.subject_kind,
                name=item.name,
                artifact=item.artifact,
                duration_ns=item.total_duration_ns,
                count=item.count,
                device=item.device,
                rank=item.rank,
                phase=item.phase,
                dimensions={"aggregated": True},
            )
            for index, item in enumerate(selected)
        )
        return observations, raw_count, dropped_keys, matched_files

    def _trace_ranks(self, paths: tuple[Path, ...]) -> dict[Path, str]:
        if self.config.merge_profiles:
            return {path: "merged" for path in paths}
        parsed = {path: _rank_from_trace_path(path) for path in paths}
        expected = self.config.expected_rank_traces
        if expected is not None:
            missing = sorted(path.name for path, rank in parsed.items() if rank is None)
            if missing:
                raise ProfileCaptureError(
                    "cannot identify rank from trace filename(s): " + ", ".join(missing)
                )
            observed = [int(rank) for rank in parsed.values() if rank is not None]
            if len(set(observed)) != len(observed):
                raise ProfileCaptureError(f"duplicate rank trace(s): {sorted(observed)}")
            required = set(range(expected))
            if set(observed) != required:
                raise ProfileCaptureError(
                    f"trace rank set is {sorted(observed)}, expected {sorted(required)}"
                )
        return {
            path: rank if rank is not None else str(index)
            for index, (path, rank) in enumerate(parsed.items())
        }

    def _post_json(self, endpoint: str, path: str, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        request = Request(
            endpoint.rstrip("/") + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ProfileCaptureError(f"SGLang profile control {path} failed: {exc}") from exc


class RuleAnalyzer:
    """Convert conservative profiler rules into planner-facing findings."""

    def __init__(self, profiler: SGLangProfiler, *, top_n: int = 30) -> None:
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n <= 0:
            raise ValueError("top_n must be a positive integer")
        self.profiler = profiler
        self.top_n = top_n

    def analyze(
        self,
        profile: ProfileResult,
        recalled: tuple[MemoryEntry, ...],
        context: StageContext,
    ) -> AnalysisReport:
        analysis = analyze_observations(
            self.profiler.observations(profile.profile_id),
            profile_id=profile.profile_id,
            top_n=self.top_n,
        )
        findings: list[Finding] = []
        artifact_ids = tuple(artifact.artifact_id for artifact in profile.artifacts)
        for index, finding in enumerate(analysis.findings):
            finding_seed = f"{profile.profile_id}\0{finding.rule_id}\0{index}"
            finding_id = f"finding-{hashlib.sha256(finding_seed.encode()).hexdigest()[:16]}"
            summary = "; ".join(finding.evidence) or finding.kind.value
            findings.append(
                Finding(
                    finding_id=finding_id,
                    category=finding.kind.value,
                    summary=summary,
                    confidence=finding.confidence,
                    evidence_artifact_ids=artifact_ids,
                    metadata={
                        "rule_id": finding.rule_id,
                        "caveats": list(finding.caveats),
                        "required_sources": [source.value for source in finding.required_sources],
                    },
                )
            )
        categories = ", ".join(finding.category for finding in findings[:5])
        return AnalysisReport(
            analysis_id=f"analysis-{profile.profile_id.removeprefix('profile-')}",
            profile_id=profile.profile_id,
            summary=(
                f"diagnostic findings: {categories}"
                if categories
                else "no diagnostic finding was produced"
            ),
            findings=tuple(findings),
            metadata={
                "run_id": context.run_id,
                "iteration_id": context.iteration_id,
                "observation_count": analysis.observation_count,
                "raw_observation_count": profile.metrics.get("raw_observation_count", 0),
                "hotspot_count": len(analysis.hotspots),
                "warnings": list(analysis.warnings),
                "recalled_memory_count": len(recalled),
                "measurement_lane": "profile_diagnostic",
                "gate_eligible": False,
            },
        )


def _artifact_ref(path: Path, label: str, media_type: str) -> ArtifactRef:
    digest = _sha256_file(path)
    return ArtifactRef(
        artifact_id=f"{label}-{digest[:16]}",
        path=str(path),
        sha256=digest,
        size_bytes=path.stat().st_size,
        media_type=media_type,
        metadata={"diagnostic_only": True},
    )


def _raw_media_type(path: Path) -> str:
    return "application/gzip" if path.name.endswith(".gz") else "application/json"


_TRACE_RANK = re.compile(r"(?:^|[^A-Za-z0-9])(?:TP|rank)[_-](\d+)(?:[^0-9]|$)", re.I)


def _rank_from_trace_path(path: Path) -> str | None:
    match = _TRACE_RANK.search(path.name)
    return None if match is None else str(int(match.group(1)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_durable(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


__all__ = ["ProfileCaptureError", "RuleAnalyzer", "SGLangProfiler"]
