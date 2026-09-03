import hashlib
import json
from pathlib import Path

import pytest

from euboulia.optimization.contracts import (
    AnalysisReport,
    ChangeKind,
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


def test_rule_planner_proposes_reviewed_server_arguments_without_patch(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """schema_version: 1
entries:
  - id: tune-scheduler
    title: Tune scheduler
    rationale: Match the scheduler to the measured workload.
    triggers: [launch]
    server_args:
      set:
        --enable-prefix-caching: null
        --max-running-requests: 128
        --schedule-policy: lpm
      remove:
        - --disable-cuda-graph
""",
        encoding="utf-8",
    )

    catalog = PatchCatalog.load(catalog_path)
    entry = catalog.entries[0]
    proposal = RulePlanner(catalog).propose(_report(), (), _context(tmp_path))[0]

    assert entry.patch_path is None
    assert entry.patch_sha256 is None
    assert entry.server_args_set == {
        "--enable-prefix-caching": None,
        "--max-running-requests": 128,
        "--schedule-policy": "lpm",
    }
    assert entry.server_args_remove == ("--disable-cuda-graph",)
    assert proposal.change_kind is ChangeKind.SERVER_PARAMETER
    assert proposal.metadata["change_sha256"] == entry.change_sha256
    assert proposal.metadata["server_args"] == {
        "set": {
            "--enable-prefix-caching": None,
            "--max-running-requests": 128,
            "--schedule-policy": "lpm",
        },
        "remove": ["--disable-cuda-graph"],
    }
    assert "patch_path" not in proposal.metadata
    assert "patch_sha256" not in proposal.metadata


def test_rule_planner_proposes_composite_patch_and_server_arguments(tmp_path: Path) -> None:
    patch = tmp_path / "scheduler.diff"
    patch.write_text("diff --git a/scheduler.py b/scheduler.py\n", encoding="utf-8")
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """schema_version: 1
entries:
  - id: scheduler-composite
    title: Patch and tune scheduler
    rationale: Apply a reviewed implementation and its matching launch policy.
    triggers: [launch]
    patch: scheduler.diff
    server_args:
      set:
        --schedule-policy: lpm
      remove: [--disable-cuda-graph]
""",
        encoding="utf-8",
    )

    catalog = PatchCatalog.load(catalog_path)
    entry = catalog.entries[0]
    proposal = RulePlanner(catalog).propose(_report(), (), _context(tmp_path))[0]

    assert proposal.change_kind is ChangeKind.COMPOSITE
    assert proposal.metadata["patch_path"] == str(patch.resolve())
    assert proposal.metadata["patch_sha256"] == entry.patch_sha256
    assert proposal.metadata["server_args"] == {
        "set": {"--schedule-policy": "lpm"},
        "remove": ["--disable-cuda-graph"],
    }
    assert proposal.metadata["change_sha256"] == entry.change_sha256
    assert entry.change_sha256 != entry.patch_sha256


def test_server_argument_digest_is_canonical_and_deduplicates_memory(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.yaml"
    first_path.write_text(
        """schema_version: 1
entries:
  - id: first
    title: First
    rationale: First ordering.
    triggers: [launch]
    server_args:
      set:
        --schedule-policy: lpm
        --max-running-requests: 128
      remove: [--disable-cuda-graph]
""",
        encoding="utf-8",
    )
    second_path = tmp_path / "second.yaml"
    second_path.write_text(
        """schema_version: 1
entries:
  - id: second
    title: Second
    rationale: Equivalent mapping with a different source ordering.
    triggers: [launch]
    server_args:
      remove: [--disable-cuda-graph]
      set:
        --max-running-requests: 128
        --schedule-policy: lpm
""",
        encoding="utf-8",
    )

    first = PatchCatalog.load(first_path).entries[0]
    second_catalog = PatchCatalog.load(second_path)
    second = second_catalog.entries[0]
    canonical_payload = json.dumps(
        {
            "patch_sha256": None,
            "server_args": {
                "remove": ["--disable-cuda-graph"],
                "set": {
                    "--max-running-requests": 128,
                    "--schedule-policy": "lpm",
                },
            },
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    expected_digest = hashlib.sha256(canonical_payload).hexdigest()
    memory = MemoryEntry(
        memory_id="memory",
        outcome_id="outcome",
        run_id="old-run",
        iteration_id="old-iteration",
        framework="sglang",
        framework_revision="abc",
        hardware_fingerprint="gpu",
        model_revision="model",
        workload_digest="workload",
        benchmark_policy_digest="policy",
        proposal_id="a-different-proposal-id",
        outcome=OutcomeStatus.REJECTED,
        summary="equivalent server arguments were already evaluated",
        patch_digest=expected_digest,
    )

    assert first.change_sha256 == expected_digest
    assert second.change_sha256 == expected_digest
    assert RulePlanner(second_catalog).propose(_report(), (memory,), _context(tmp_path)) == ()


@pytest.mark.parametrize("value", ["true", "false"])
def test_patch_catalog_rejects_boolean_server_argument_value(
    tmp_path: Path,
    value: str,
) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        f"""schema_version: 1
entries:
  - id: invalid-boolean
    title: Invalid boolean
    rationale: Switches must use null.
    triggers: ['*']
    server_args:
      set:
        --enable-prefix-caching: {value}
""",
        encoding="utf-8",
    )

    with pytest.raises(PatchCatalogError, match="must use null for a switch, not a boolean"):
        PatchCatalog.load(catalog)


def test_patch_catalog_rejects_server_argument_in_both_set_and_remove(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """schema_version: 1
entries:
  - id: conflicting-argument
    title: Conflicting argument
    rationale: An argument needs one unambiguous operation.
    triggers: ['*']
    server_args:
      set:
        --schedule-policy: lpm
      remove: [--schedule-policy]
""",
        encoding="utf-8",
    )

    with pytest.raises(PatchCatalogError, match="cannot set and remove the same option"):
        PatchCatalog.load(catalog)


@pytest.mark.parametrize(
    "invalid_flag",
    ["schedule-policy", "--schedule_policy", "--Schedule-policy", "---schedule-policy"],
)
def test_patch_catalog_rejects_noncanonical_server_argument_flag(
    tmp_path: Path,
    invalid_flag: str,
) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        f"""schema_version: 1
entries:
  - id: invalid-flag
    title: Invalid flag
    rationale: Flags must have one canonical spelling.
    triggers: ['*']
    server_args:
      set:
        '{invalid_flag}': lpm
""",
        encoding="utf-8",
    )

    with pytest.raises(PatchCatalogError, match="canonical --kebab-case server option"):
        PatchCatalog.load(catalog)
