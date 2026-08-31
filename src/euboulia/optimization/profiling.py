"""Adapters from offline profiler artifacts to optimization contracts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from euboulia.optimization.config import ProfileArtifactConfig
from euboulia.optimization.contracts import (
    AnalysisReport,
    ArtifactRef,
    Finding,
    MemoryEntry,
    ProfileRequest,
    ProfileResult,
    StageContext,
)
from euboulia.profilers import Observation, analyze_observations, parse_profile


class ImportedProfiler:
    """Read declared trace exports without controlling a serving process."""

    def __init__(self, artifacts: tuple[ProfileArtifactConfig, ...]) -> None:
        if not artifacts:
            raise ValueError("at least one imported profile artifact is required")
        self.artifacts = artifacts
        self._observations: dict[str, tuple[Observation, ...]] = {}

    def capture(self, request: ProfileRequest, context: StageContext) -> ProfileResult:
        del context  # import is read-only and does not consume a capability
        references: list[ArtifactRef] = []
        observations: list[Observation] = []
        total_size = 0
        digest = hashlib.sha256()
        for index, artifact in enumerate(self.artifacts):
            try:
                size = artifact.path.stat().st_size
            except OSError as exc:
                raise ValueError(f"cannot read imported profile {artifact.path}: {exc}") from exc
            if not artifact.path.is_file():
                raise ValueError(f"imported profile is not a regular file: {artifact.path}")
            total_size += size
            if total_size > request.max_bytes:
                raise ValueError(
                    f"imported profiles exceed byte budget ({total_size} > {request.max_bytes})"
                )
            artifact_digest = _sha256_file(artifact.path)
            digest.update(artifact.source.encode())
            digest.update(b"\0")
            digest.update(artifact_digest.encode())
            artifact_id = f"profile-artifact-{artifact_digest[:16]}"
            references.append(
                ArtifactRef(
                    artifact_id=artifact_id,
                    path=str(artifact.path),
                    sha256=artifact_digest,
                    size_bytes=size,
                    media_type=_media_type(artifact.path),
                    metadata={
                        "source": artifact.source,
                        "nsys_report": artifact.nsys_report,
                        "diagnostic_only": True,
                    },
                )
            )
            parsed = parse_profile(
                artifact.path,
                artifact.source,
                nsys_report=artifact.nsys_report,
                timestamp_unit=artifact.timestamp_unit,
            )
            if (
                artifact.path.stat().st_size != size
                or _sha256_file(artifact.path) != artifact_digest
            ):
                raise ValueError(
                    f"imported profile changed while it was being parsed: {artifact.path}"
                )
            # Prefix parser-local IDs so multiple exports cannot collide.
            for observation in parsed:
                normalized = observation.to_dict()
                normalized["observation_id"] = f"artifact-{index}:{observation.observation_id}"
                observations.append(Observation.from_dict(normalized))
        profile_id = f"profile-{digest.hexdigest()[:20]}"
        self._observations[profile_id] = tuple(observations)
        return ProfileResult(
            profile_id=profile_id,
            provider="imported",
            candidate_id=request.candidate_id,
            artifacts=tuple(references),
            metrics={"observation_count": len(observations), "artifact_bytes": total_size},
            complete=True,
            metadata={
                "source_revision": request.source_revision,
                "workload_digest": request.workload_digest,
                "measurement_lane": "profile_diagnostic",
                "gate_eligible": False,
            },
        )

    def observations(self, profile_id: str) -> tuple[Observation, ...]:
        try:
            return self._observations[profile_id]
        except KeyError as exc:
            raise KeyError(f"unknown imported profile: {profile_id}") from exc


class RuleAnalyzer:
    """Convert conservative profiler rules into planner-facing findings."""

    def __init__(self, profiler: ImportedProfiler, *, top_n: int = 30) -> None:
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
                "hotspot_count": len(analysis.hotspots),
                "warnings": list(analysis.warnings),
                "recalled_memory_count": len(recalled),
                "measurement_lane": "profile_diagnostic",
                "gate_eligible": False,
            },
        )


def _media_type(path: Path) -> str:
    if path.name.endswith(".json.gz"):
        return "application/gzip"
    if path.suffix.casefold() == ".json":
        return "application/json"
    if path.suffix.casefold() == ".csv":
        return "text/csv"
    return "application/octet-stream"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ImportedProfiler", "RuleAnalyzer"]
