from pathlib import Path

import pytest

from euboulia.optimization.contracts import (
    AnalysisReport,
    Finding,
    MemoryEntry,
    OutcomeStatus,
    StageContext,
)
from euboulia.optimization.planner import PatchCatalog, PatchCatalogError, RulePlanner


def _write_catalog(tmp_path: Path) -> Path:
    patch = tmp_path / "launch.diff"
    patch.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """schema_version: 1
entries:
  - id: batch-launches
    title: Batch scheduler launches
    rationale: Reduce repeated host launch overhead.
    triggers: [launch]
    patch: launch.diff
    predicted_metric: output_throughput
    risk: medium
""",
        encoding="utf-8",
    )
    return catalog


def _report() -> AnalysisReport:
    return AnalysisReport(
        analysis_id="analysis-1",
        profile_id="profile-1",
        summary="launch pressure",
        findings=(
            Finding(
                finding_id="finding-1",
                category="launch",
                summary="many short kernels",
                confidence=0.8,
                evidence_artifact_ids=("trace",),
            ),
        ),
    )


def _context(tmp_path: Path) -> StageContext:
    return StageContext(run_id="run", iteration_id="i-1", artifact_dir=tmp_path)


def test_rule_planner_selects_matching_reviewed_patch(tmp_path: Path) -> None:
    planner = RulePlanner(PatchCatalog.load(_write_catalog(tmp_path)))

    proposals = planner.propose(_report(), (), _context(tmp_path))

    assert len(proposals) == 1
    assert proposals[0].catalog_entry_id == "batch-launches"
    assert proposals[0].metadata["finding_category"] == "launch"


def test_rule_planner_deduplicates_patch_from_memory(tmp_path: Path) -> None:
    planner = RulePlanner(PatchCatalog.load(_write_catalog(tmp_path)))
    first = planner.propose(_report(), (), _context(tmp_path))[0]
    memory = MemoryEntry(
        memory_id="memory",
        outcome_id="outcome",
        run_id="old-run",
        iteration_id="old-iteration",
        framework="vllm",
        framework_revision="abc",
        hardware_fingerprint="gpu",
        model_revision="model",
        workload_digest="workload",
        benchmark_policy_digest="policy",
        proposal_id=first.proposal_id,
        outcome=OutcomeStatus.REJECTED,
        summary="did not help",
        patch_digest=str(first.metadata["patch_sha256"]),
    )

    assert planner.propose(_report(), (memory,), _context(tmp_path)) == ()


def test_patch_catalog_rejects_duplicate_content(tmp_path: Path) -> None:
    patch = tmp_path / "same.diff"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """schema_version: 1
entries:
  - {id: one, title: One, rationale: One, triggers: ['*'], patch: same.diff}
  - {id: two, title: Two, rationale: Two, triggers: ['*'], patch: same.diff}
""",
        encoding="utf-8",
    )

    with pytest.raises(PatchCatalogError, match="duplicate patch content"):
        PatchCatalog.load(catalog)
