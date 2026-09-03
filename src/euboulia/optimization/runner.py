"""Event-driven orchestration for the first iterative optimization runtime."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from euboulia.models import JSONValue
from euboulia.optimization.budget import BudgetLimits, BudgetTracker
from euboulia.optimization.config import (
    EvaluationTierConfig,
    OptimizationCommandConfig,
    OptimizationConfig,
    WorkloadPointConfig,
)
from euboulia.optimization.contracts import (
    AnalysisReport,
    ArtifactRef,
    Capability,
    ChangeProposal,
    Finding,
    IterationOutcome,
    IterationState,
    MemoryEntry,
    MemoryQuery,
    OutcomeStatus,
    ProfileRequest,
    ProfileResult,
    RunState,
    StageContext,
)
from euboulia.optimization.evaluator import (
    BenchmarkSpec,
    CommandSpec,
    EvaluationOutcome,
    EvaluationPlan,
    ObjectiveDirection,
    ObjectiveSpec,
    TieredEvaluationResult,
    TieredEvaluator,
    WorkloadSuiteEvaluationResult,
)
from euboulia.optimization.events import (
    EventLedger,
    EventType,
    OptimizationEvent,
)
from euboulia.optimization.memory import SQLiteMemoryStore
from euboulia.optimization.planner import PatchCatalog, RulePlanner
from euboulia.optimization.profiling import ImportedProfiler, RuleAnalyzer
from euboulia.optimization.provenance import (
    RuntimeProvenanceRecord,
    capture_runtime_provenance,
    hardware_identity,
    validate_runtime_provenance,
    write_runtime_provenance,
)
from euboulia.optimization.state import Lifecycle
from euboulia.optimization.target import (
    BuildCommandSpec,
    BuildSpec,
    ReadinessSpec,
    ServerArgumentPatch,
    ServiceHandle,
    SGLangTargetController,
    TargetChangeSet,
    TargetController,
    TargetSpec,
)
from euboulia.optimization.workspace import (
    GitWorktreeWorkspace,
    PatchLimits,
    PatchRejected,
    WorkspaceError,
)


class OptimizationRuntimeError(RuntimeError):
    """Raised when a requested active run lacks an executable declaration."""


@dataclass(frozen=True, slots=True)
class OptimizationPlan:
    campaign: str
    framework: str
    profile: ProfileResult
    analysis: AnalysisReport
    proposals: tuple[ChangeProposal, ...]
    required_capabilities: tuple[Capability, ...]
    warnings: tuple[str, ...]

    @property
    def recipe(self) -> str:
        """Return the public recipe name; ``campaign`` remains an internal alias."""

        return self.campaign

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "recipe": self.recipe,
            "framework": self.framework,
            "profile": _profile_dict(self.profile),
            "analysis": _analysis_dict(self.analysis),
            "proposals": [_proposal_dict(proposal) for proposal in self.proposals],
            "required_capabilities": [item.value for item in self.required_capabilities],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class OptimizationRunResult:
    run_id: str
    run_state: RunState
    iteration_state: IterationState | None
    champion_id: str
    profile: ProfileResult | None
    analysis: AnalysisReport | None
    proposals: tuple[ChangeProposal, ...]
    outcomes: tuple[IterationOutcome, ...]
    event_ledger: Path
    memory: Path
    stop_reason: str | None = None

    @property
    def waiting_for_approval(self) -> bool:
        return self.run_state is RunState.WAITING_FOR_APPROVAL

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "run_id": self.run_id,
            "run_state": self.run_state.value,
            "iteration_state": (
                self.iteration_state.value if self.iteration_state is not None else None
            ),
            "champion_id": self.champion_id,
            "profile": _profile_dict(self.profile) if self.profile is not None else None,
            "analysis": _analysis_dict(self.analysis) if self.analysis is not None else None,
            "proposals": [_proposal_dict(proposal) for proposal in self.proposals],
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "event_ledger": str(self.event_ledger),
            "memory": str(self.memory),
            "stop_reason": self.stop_reason,
            "waiting_for_approval": self.waiting_for_approval,
        }


@dataclass(frozen=True, slots=True)
class _CandidateExecutionResult:
    lifecycle: Lifecycle
    evaluation: TieredEvaluationResult | WorkloadSuiteEvaluationResult
    change_digest: str
    workspace_path: Path
    baseline_evaluation: TieredEvaluationResult | WorkloadSuiteEvaluationResult | None = None


class OptimizationRunner:
    """Join imported evidence, reviewed changes, isolated trials, and memory.

    A configuration without ``target`` retains the external-service compatibility
    path.  A managed SGLang target is built, started, checked, evaluated, and
    stopped by an owned ``TargetController`` under separate capabilities.
    """

    def __init__(self, target_controller: TargetController | None = None) -> None:
        self._target_controller = target_controller

    @staticmethod
    def required_capabilities(config: OptimizationConfig) -> tuple[Capability, ...]:
        """Return the deterministic side-effect boundary for ``config``."""

        return _required_capabilities(config)

    def plan(self, config: OptimizationConfig) -> OptimizationPlan:
        """Analyze declared artifacts and select catalog proposals without writes."""

        if config.target is not None:
            _target_spec(config)
        context = StageContext(
            run_id="preview",
            iteration_id="preview-001",
            artifact_dir=config.execution.artifacts_dir,
        )
        profile, analysis, proposals = self._deliberate(config, context, ())
        required_capabilities = self.required_capabilities(config)
        target_warning = (
            "managed SGLang trials use a fresh baseline service and candidate service"
            if config.target is not None
            else (
                "target is absent; server-argument changes are ineligible and evaluation "
                "commands must address an externally managed service"
            )
        )
        warnings = (
            "plan is read-only; no event, memory, worktree, command, or service was created",
            "profile evidence is diagnostic-only and cannot promote a candidate",
            "Cookbook conversion is external; only reviewed target and change "
            "declarations are used",
            target_warning,
        )
        return OptimizationPlan(
            campaign=config.name,
            framework=config.framework.value,
            profile=profile,
            analysis=analysis,
            proposals=proposals,
            required_capabilities=required_capabilities,
            warnings=warnings,
        )

    def run(
        self,
        config: OptimizationConfig,
        authorizations: frozenset[Capability] = frozenset(),
        *,
        run_id: str | None = None,
    ) -> OptimizationRunResult:
        """Run bounded iterations or pause before the first unauthorized side effect."""

        selected_run_id = _run_id(run_id)
        ledger = EventLedger(config.execution.event_ledger, fsync=True)
        if ledger.by_run(selected_run_id):
            raise OptimizationRuntimeError(
                f"run_id {selected_run_id!r} already exists; resume is not implemented, "
                "choose a new run id"
            )
        memory = SQLiteMemoryStore(config.execution.memory)
        lifecycle = Lifecycle()
        champion_id = config.baseline.candidate_id
        profile: ProfileResult | None = None
        analysis: AnalysisReport | None = None
        observed_proposals: list[ChangeProposal] = []
        outcomes: list[IterationOutcome] = []
        config_digest = _config_digest(config)
        workload_digest = _workload_digest(config)
        policy_digest = _policy_digest(config)
        hardware_fingerprint = _hardware_fingerprint(config)
        budget_config = config.optimization.budget
        budget = BudgetTracker(
            BudgetLimits(
                max_iterations=budget_config.max_iterations,
                max_failures=budget_config.max_consecutive_failures,
                max_wall_time_seconds=budget_config.max_wall_time_seconds,
            )
        )
        ledger.append(
            OptimizationEvent.create(
                EventType.RUN_PLANNED,
                selected_run_id,
                input_digest=config_digest,
                payload={"campaign": config.name, "framework": config.framework.value},
            )
        )
        ledger.append(
            OptimizationEvent.create(
                EventType.RUN_STARTED,
                selected_run_id,
                input_digest=config_digest,
                payload={"reference_baseline": config.baseline.candidate_id},
            )
        )
        lifecycle = lifecycle.move_run(RunState.ITERATING)

        try:
            no_improvement = 0
            while not budget.snapshot().exhausted:
                reservation = budget.reserve_iteration()
                iteration_number = reservation.iterations_started
                iteration_id = f"iteration-{iteration_number:03d}"
                lifecycle = lifecycle.begin_iteration()
                ledger.append(
                    OptimizationEvent.create(
                        EventType.BUDGET_RESERVED,
                        selected_run_id,
                        iteration_id=iteration_id,
                        payload={
                            "iteration": iteration_number,
                            "elapsed_seconds": reservation.elapsed_seconds,
                            "max_iterations": budget_config.max_iterations,
                        },
                    )
                )
                ledger.append(
                    OptimizationEvent.create(
                        EventType.ITERATION_STARTED,
                        selected_run_id,
                        iteration_id=iteration_id,
                        payload={"champion_before": champion_id},
                    )
                )
                context = StageContext(
                    run_id=selected_run_id,
                    iteration_id=iteration_id,
                    artifact_dir=config.execution.artifacts_dir / selected_run_id / iteration_id,
                    authorizations=authorizations,
                    input_digest=config_digest,
                )
                recalled = memory.recall(
                    MemoryQuery(
                        framework=config.framework.value,
                        framework_revision=config.baseline.source_revision,
                        hardware_fingerprint=hardware_fingerprint,
                        model_revision=config.models.target.revision,
                        workload_digest=workload_digest,
                        benchmark_policy_digest=policy_digest,
                        limit=100,
                    )
                )
                lifecycle = lifecycle.move_iteration(IterationState.PROFILING)
                ledger.append(
                    OptimizationEvent.create(
                        EventType.PROFILE_STARTED,
                        selected_run_id,
                        iteration_id=iteration_id,
                        payload={"provider": "imported", "candidate_id": champion_id},
                    )
                )
                profile, analysis, proposals = self._deliberate(
                    config, context, recalled, candidate_id=champion_id
                )
                ledger.append(
                    OptimizationEvent.create(
                        EventType.PROFILE_COMPLETED,
                        selected_run_id,
                        iteration_id=iteration_id,
                        entity_id=profile.profile_id,
                        payload=_json_payload(_profile_dict(profile)),
                        artifacts=profile.artifacts,
                    )
                )
                lifecycle = lifecycle.move_iteration(IterationState.ANALYZING)
                ledger.append(
                    OptimizationEvent.create(
                        EventType.ANALYSIS_COMPLETED,
                        selected_run_id,
                        iteration_id=iteration_id,
                        entity_id=analysis.analysis_id,
                        payload=_json_payload(_analysis_dict(analysis)),
                    )
                )
                lifecycle = lifecycle.move_iteration(IterationState.PLANNING)
                for proposal in proposals:
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.PROPOSAL_CREATED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            entity_id=proposal.proposal_id,
                            payload=_json_payload(_proposal_dict(proposal)),
                        )
                    )
                observed_proposals.extend(proposals)
                if not proposals:
                    lifecycle = lifecycle.move_iteration(IterationState.RECORDING_MEMORY)
                    lifecycle = lifecycle.move_iteration(IterationState.REJECTED)
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.ITERATION_COMPLETED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            payload={
                                "state": "rejected",
                                "reason": "planner produced no new proposal",
                            },
                        )
                    )
                    lifecycle = lifecycle.move_run(RunState.COMPLETED)
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.RUN_COMPLETED,
                            selected_run_id,
                            payload={"reason": "planner produced no new proposal"},
                        )
                    )
                    return self._result(
                        selected_run_id,
                        lifecycle,
                        champion_id,
                        profile,
                        analysis,
                        observed_proposals,
                        outcomes,
                        config,
                        "planner produced no new proposal",
                    )

                proposal = proposals[0]
                lifecycle = lifecycle.move_iteration(IterationState.WAITING_FOR_APPROVAL)
                required = set(self.required_capabilities(config))
                missing = sorted(required - authorizations, key=lambda item: item.value)
                if missing:
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.APPROVAL_REQUESTED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            entity_id=proposal.proposal_id,
                            payload={"missing_capabilities": [item.value for item in missing]},
                        )
                    )
                    lifecycle = lifecycle.move_run(RunState.WAITING_FOR_APPROVAL)
                    return self._result(
                        selected_run_id,
                        lifecycle,
                        champion_id,
                        profile,
                        analysis,
                        observed_proposals,
                        outcomes,
                        config,
                        "explicit authorization is required for every declared side effect",
                    )
                ledger.append(
                    OptimizationEvent.create(
                        EventType.APPROVAL_GRANTED,
                        selected_run_id,
                        iteration_id=iteration_id,
                        entity_id=proposal.proposal_id,
                        payload={
                            "capabilities": [
                                item.value for item in sorted(required, key=lambda item: item.value)
                            ]
                        },
                    )
                )

                try:
                    execution = self._execute_candidate(
                        config,
                        selected_run_id,
                        iteration_id,
                        champion_id,
                        proposal,
                        lifecycle,
                        ledger,
                    )
                    lifecycle = execution.lifecycle
                    evaluation = execution.evaluation
                    outcome_status = _outcome_status(evaluation)
                    champion_after = (
                        proposal.proposal_id
                        if outcome_status is OutcomeStatus.ACCEPTED
                        else champion_id
                    )
                    summary = evaluation.reason or f"evaluation {evaluation.outcome.value}"
                    outcome = IterationOutcome(
                        outcome_id=f"outcome-{uuid.uuid4().hex}",
                        run_id=selected_run_id,
                        iteration_id=iteration_id,
                        proposal_id=proposal.proposal_id,
                        status=outcome_status,
                        summary=summary,
                        champion_before=champion_id,
                        champion_after=champion_after,
                        relative_improvement=evaluation.relative_improvement,
                        patch_digest=execution.change_digest,
                        artifacts=(_artifact_ref(execution.workspace_path, "workspace"),),
                        metadata={
                            "evaluation": dict(_json_payload(evaluation.to_dict())),
                            **(
                                {
                                    "baseline_evaluation": dict(
                                        _json_payload(execution.baseline_evaluation.to_dict())
                                    )
                                }
                                if execution.baseline_evaluation is not None
                                else {}
                            ),
                        },
                    )
                    lifecycle = lifecycle.move_iteration(IterationState.RECORDING_MEMORY)
                    memory_entry = self._remember(
                        memory,
                        outcome,
                        config,
                        hardware_fingerprint,
                        workload_digest,
                        policy_digest,
                    )
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.MEMORY_RECORDED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            entity_id=memory_entry.memory_id,
                            payload={"status": outcome.status.value},
                        )
                    )
                    terminal = _terminal_iteration_state(outcome_status)
                    lifecycle = lifecycle.move_iteration(terminal)
                    outcomes.append(outcome)
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.VERDICT_RECORDED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            entity_id=outcome.outcome_id,
                            payload=_json_payload(outcome.to_dict()),
                        )
                    )
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.ITERATION_COMPLETED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            payload={"state": terminal.value},
                        )
                    )
                    if outcome.accepted:
                        champion_id = champion_after
                        ledger.append(
                            OptimizationEvent.create(
                                EventType.CHAMPION_UPDATED,
                                selected_run_id,
                                iteration_id=iteration_id,
                                entity_id=champion_id,
                                payload={
                                    "champion_before": outcome.champion_before,
                                    "champion_after": champion_id,
                                    "reason": "candidate passed unprofiled evaluation",
                                },
                            )
                        )
                        lifecycle = lifecycle.move_run(RunState.COMPLETED)
                        ledger.append(
                            OptimizationEvent.create(
                                EventType.RUN_COMPLETED,
                                selected_run_id,
                                payload={"reason": "candidate accepted", "champion": champion_id},
                            )
                        )
                        return self._result(
                            selected_run_id,
                            lifecycle,
                            champion_id,
                            profile,
                            analysis,
                            observed_proposals,
                            outcomes,
                            config,
                            "candidate accepted; a fresh champion profile is required",
                        )
                    no_improvement += 1
                    if outcome_status in {OutcomeStatus.FAILED, OutcomeStatus.INVALID}:
                        budget.record_failure()
                    if no_improvement >= budget_config.no_improvement_patience:
                        break
                except (PatchRejected, WorkspaceError) as exc:
                    # The execution helper owns intermediate immutable lifecycle values.
                    # If it fails before returning, close the caller's approval state via
                    # the recording boundary instead of attempting an illegal direct jump.
                    lifecycle = lifecycle.move_iteration(IterationState.RECORDING_MEMORY)
                    failed_outcome = self._failed_outcome(
                        selected_run_id,
                        iteration_id,
                        champion_id,
                        proposal,
                        OutcomeStatus.INVALID,
                        str(exc),
                    )
                    outcomes.append(failed_outcome)
                    invalid_memory = self._remember(
                        memory,
                        failed_outcome,
                        config,
                        hardware_fingerprint,
                        workload_digest,
                        policy_digest,
                    )
                    budget.record_failure()
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.MEMORY_RECORDED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            entity_id=invalid_memory.memory_id,
                            payload={"status": failed_outcome.status.value},
                        )
                    )
                    lifecycle = lifecycle.move_iteration(IterationState.INVALID)
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.PATCH_REJECTED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            entity_id=proposal.proposal_id,
                            payload={"error": str(exc)},
                        )
                    )
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.ITERATION_FAILED,
                            selected_run_id,
                            iteration_id=iteration_id,
                            payload={"state": "invalid", "error": str(exc)},
                        )
                    )

            reason = budget.snapshot().exhausted_reason or "no-improvement patience exhausted"
            lifecycle = lifecycle.move_run(RunState.STOPPED)
            ledger.append(
                OptimizationEvent.create(
                    EventType.RUN_STOPPED,
                    selected_run_id,
                    payload={"reason": reason},
                )
            )
            return self._result(
                selected_run_id,
                lifecycle,
                champion_id,
                profile,
                analysis,
                observed_proposals,
                outcomes,
                config,
                reason,
            )
        except Exception as exc:
            ledger.append(
                OptimizationEvent.create(
                    EventType.RUN_FAILED,
                    selected_run_id,
                    payload={"error_type": type(exc).__name__, "error": str(exc)},
                )
            )
            raise

    def _deliberate(
        self,
        config: OptimizationConfig,
        context: StageContext,
        recalled: tuple[MemoryEntry, ...],
        *,
        candidate_id: str | None = None,
    ) -> tuple[ProfileResult, AnalysisReport, tuple[ChangeProposal, ...]]:
        profiler = ImportedProfiler(config.optimization.profiles.artifacts)
        profile = profiler.capture(
            ProfileRequest(
                candidate_id=candidate_id or config.baseline.candidate_id,
                source_revision=config.baseline.source_revision,
                workload_digest=_workload_digest(config),
                max_bytes=config.optimization.budget.max_profile_bytes,
            ),
            context,
        )
        analysis = RuleAnalyzer(profiler).analyze(profile, recalled, context)
        planner_config = config.optimization.planner
        catalog = PatchCatalog.load(planner_config.patch_catalog)
        if config.target is None:
            catalog = PatchCatalog(
                entries=tuple(
                    entry
                    for entry in catalog.entries
                    if not entry.server_args_set and not entry.server_args_remove
                ),
                source=catalog.source,
            )
        planner = RulePlanner(
            catalog,
            max_proposals=planner_config.max_proposals_per_iteration,
            reject_duplicates=planner_config.reject_duplicate_diffs,
        )
        proposals = planner.propose(analysis, recalled, context)
        return profile, analysis, proposals

    def _execute_candidate(
        self,
        config: OptimizationConfig,
        run_id: str,
        iteration_id: str,
        champion_id: str,
        proposal: ChangeProposal,
        lifecycle: Lifecycle,
        ledger: EventLedger,
    ) -> _CandidateExecutionResult:
        if config.target is not None:
            return self._execute_managed_candidate(
                config,
                run_id,
                iteration_id,
                champion_id,
                proposal,
                lifecycle,
                ledger,
            )
        workspace_config = config.optimization.workspace
        if workspace_config is None:
            raise OptimizationRuntimeError(
                "optimization.workspace is required for active patch evaluation"
            )
        patch_path_value = proposal.metadata.get("patch_path")
        if not isinstance(patch_path_value, str):
            raise OptimizationRuntimeError("proposal does not contain a patch_path")
        patch_path = Path(patch_path_value)
        lifecycle = lifecycle.move_iteration(IterationState.PREPARING_WORKSPACE)
        workspace_root = workspace_config.root_dir / run_id / iteration_id
        workspace = GitWorktreeWorkspace.create(
            workspace_config.repository,
            workspace_root,
            revision=config.baseline.source_revision,
            timeout_seconds=workspace_config.timeout_seconds,
        )
        ledger.append(
            OptimizationEvent.create(
                EventType.WORKSPACE_PREPARED,
                run_id,
                iteration_id=iteration_id,
                entity_id=proposal.proposal_id,
                payload={
                    "worktree": str(workspace.path),
                    "base_commit": workspace.base_commit,
                    "automatic_cleanup": False,
                },
                artifacts=(
                    _artifact_ref(workspace.evidence_dir / "workspace-manifest.json", "manifest"),
                ),
            )
        )
        prepared = workspace.prepare_patch(
            patch_path.read_bytes(),
            limits=PatchLimits(
                max_bytes=workspace_config.max_patch_bytes,
                max_files=workspace_config.max_changed_files,
                max_changed_lines=workspace_config.max_changed_lines,
            ),
        )
        lifecycle = lifecycle.move_iteration(IterationState.APPLYING_PATCH)
        application = workspace.apply_patch(prepared, authorize=True)
        ledger.append(
            OptimizationEvent.create(
                EventType.PATCH_APPLIED,
                run_id,
                iteration_id=iteration_id,
                entity_id=proposal.proposal_id,
                payload={
                    "patch_digest": prepared.inspection.sha256,
                    "changed_files": list(prepared.inspection.files),
                    "changed_lines": prepared.inspection.changed_lines,
                },
                artifacts=(
                    _artifact_ref(prepared.patch_path, "patch"),
                    _artifact_ref(application.diff_evidence.stdout_path, "resulting-diff"),
                ),
            )
        )
        lifecycle = lifecycle.move_iteration(IterationState.EVALUATING)
        ledger.append(
            OptimizationEvent.create(
                EventType.EVALUATION_STARTED,
                run_id,
                iteration_id=iteration_id,
                entity_id=proposal.proposal_id,
                payload={"profiler_trial": False, "workspace": str(workspace.path)},
            )
        )
        evaluator = TieredEvaluator(config.execution.artifacts_dir / run_id / "evaluations")
        configured_baselines = dict(config.baseline.metric_values)
        if config.schema_version == 2:
            legacy_baseline = config.optimization.evaluation.baseline_value
            if legacy_baseline is not None:
                configured_baselines[config.workload_suite.points[0].point_id] = legacy_baseline
        evaluation = _execute_workload_suite(
            config,
            trial_id=iteration_id,
            workspace=workspace.path,
            baseline_values=configured_baselines,
            apply_promotion_gate=True,
            evaluator=evaluator,
            artifact_dir=config.execution.artifacts_dir / run_id / "evaluations" / iteration_id,
        )
        ledger.append(
            OptimizationEvent.create(
                EventType.EVALUATION_COMPLETED,
                run_id,
                iteration_id=iteration_id,
                entity_id=proposal.proposal_id,
                payload=_json_payload(evaluation.to_dict()),
                artifacts=(_artifact_ref(_evaluation_artifact(evaluation), "evaluation"),),
            )
        )
        return _CandidateExecutionResult(
            lifecycle=lifecycle,
            evaluation=evaluation,
            change_digest=prepared.inspection.sha256,
            workspace_path=workspace.path,
        )

    def _execute_managed_candidate(
        self,
        config: OptimizationConfig,
        run_id: str,
        iteration_id: str,
        champion_id: str,
        proposal: ChangeProposal,
        lifecycle: Lifecycle,
        ledger: EventLedger,
    ) -> _CandidateExecutionResult:
        workspace_config = config.optimization.workspace
        if workspace_config is None:
            raise OptimizationRuntimeError(
                "optimization.workspace is required for managed target evaluation"
            )
        controller = self._target_controller or SGLangTargetController()
        evidence_root = config.execution.artifacts_dir / run_id / iteration_id / "managed-target"
        provenance_record: RuntimeProvenanceRecord | None = None
        target = config.target
        if target is None:  # guarded by the managed execution entry point
            raise OptimizationRuntimeError("target configuration is required")
        if target.runtime is not None:
            provenance_record = capture_runtime_provenance(
                target.runtime,
                repository=workspace_config.repository,
            )
            provenance_path = write_runtime_provenance(
                provenance_record,
                evidence_root / "runtime-provenance.json",
            )
            ledger.append(
                OptimizationEvent.create(
                    EventType.RUNTIME_PROVENANCE_CAPTURED,
                    run_id,
                    iteration_id=iteration_id,
                    payload=_json_payload(provenance_record.to_dict()),
                    artifacts=(_artifact_ref(provenance_path, "runtime-provenance"),),
                )
            )
            validate_runtime_provenance(target.runtime, provenance_record)
        target_spec = _target_spec(config, provenance_record)

        ledger.append(
            OptimizationEvent.create(
                EventType.BASELINE_STARTED,
                run_id,
                iteration_id=iteration_id,
                entity_id=champion_id,
                payload={"source_revision": config.baseline.source_revision},
            )
        )
        lifecycle = lifecycle.move_iteration(IterationState.PREPARING_BASELINE)
        baseline_workspace = self._prepare_managed_workspace(
            config,
            run_id,
            iteration_id,
            role="baseline",
            entity_id=champion_id,
            ledger=ledger,
        )
        baseline_change = TargetChangeSet()
        self._record_target_materialized(
            ledger,
            run_id,
            iteration_id,
            champion_id,
            "baseline",
            target_spec,
            baseline_change,
            None,
        )
        lifecycle = lifecycle.move_iteration(IterationState.BUILDING_BASELINE)
        self._build_managed_target(
            controller,
            target_spec,
            baseline_workspace,
            evidence_root / "baseline" / "build",
            ledger,
            run_id,
            iteration_id,
            champion_id,
            "baseline",
        )
        lifecycle, baseline_evaluation = self._evaluate_managed_service(
            controller=controller,
            config=config,
            target_spec=target_spec,
            change_set=baseline_change,
            workspace=baseline_workspace,
            evidence_dir=evidence_root / "baseline" / "service",
            ledger=ledger,
            run_id=run_id,
            iteration_id=iteration_id,
            entity_id=champion_id,
            role="baseline",
            trial_id=f"{iteration_id}-baseline",
            lifecycle=lifecycle,
            start_state=IterationState.STARTING_BASELINE,
            ready_state=IterationState.WAITING_FOR_BASELINE,
            evaluation_state=IterationState.EVALUATING_BASELINE,
            stop_state=IterationState.STOPPING_BASELINE,
            baseline_values={},
            apply_promotion_gate=False,
        )
        if baseline_evaluation.outcome is not EvaluationOutcome.PASSED or len(
            _objective_values(config, baseline_evaluation)
        ) != len(config.workload_suite.points):
            reason = baseline_evaluation.reason or "managed baseline evaluation failed"
            ledger.append(
                OptimizationEvent.create(
                    EventType.BASELINE_INVALID,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=champion_id,
                    payload={"reason": reason},
                )
            )
            raise OptimizationRuntimeError(f"managed baseline is invalid: {reason}")
        measured_baselines = _objective_values(config, baseline_evaluation)
        ledger.append(
            OptimizationEvent.create(
                EventType.BASELINE_ESTABLISHED,
                run_id,
                iteration_id=iteration_id,
                entity_id=champion_id,
                payload=_json_payload(
                    {
                        "metric": config.optimization.evaluation.metric,
                        "values": measured_baselines,
                        "evaluation_trial_id": baseline_evaluation.trial_id,
                    }
                ),
                artifacts=(
                    _artifact_ref(
                        _evaluation_artifact(baseline_evaluation),
                        "baseline-evaluation",
                    ),
                ),
            )
        )

        lifecycle = lifecycle.move_iteration(IterationState.PREPARING_WORKSPACE)
        candidate_workspace = self._prepare_managed_workspace(
            config,
            run_id,
            iteration_id,
            role="candidate",
            entity_id=proposal.proposal_id,
            ledger=ledger,
            revision=baseline_workspace.base_commit,
        )
        lifecycle, candidate_change, change_digest = self._materialize_candidate_change(
            config,
            run_id,
            iteration_id,
            proposal,
            candidate_workspace,
            lifecycle,
            ledger,
        )
        self._record_target_materialized(
            ledger,
            run_id,
            iteration_id,
            proposal.proposal_id,
            "candidate",
            target_spec,
            candidate_change,
            change_digest,
        )
        lifecycle = lifecycle.move_iteration(IterationState.BUILDING)
        self._build_managed_target(
            controller,
            target_spec,
            candidate_workspace,
            evidence_root / "candidate" / "build",
            ledger,
            run_id,
            iteration_id,
            proposal.proposal_id,
            "candidate",
        )
        lifecycle, candidate_evaluation = self._evaluate_managed_service(
            controller=controller,
            config=config,
            target_spec=target_spec,
            change_set=candidate_change,
            workspace=candidate_workspace,
            evidence_dir=evidence_root / "candidate" / "service",
            ledger=ledger,
            run_id=run_id,
            iteration_id=iteration_id,
            entity_id=proposal.proposal_id,
            role="candidate",
            trial_id=f"{iteration_id}-candidate",
            lifecycle=lifecycle,
            start_state=IterationState.STARTING_SERVICE,
            ready_state=IterationState.WAITING_FOR_READY,
            evaluation_state=IterationState.EVALUATING,
            stop_state=IterationState.STOPPING_SERVICE,
            baseline_values=measured_baselines,
            apply_promotion_gate=True,
        )
        return _CandidateExecutionResult(
            lifecycle=lifecycle,
            evaluation=candidate_evaluation,
            change_digest=change_digest,
            workspace_path=candidate_workspace.path,
            baseline_evaluation=baseline_evaluation,
        )

    @staticmethod
    def _prepare_managed_workspace(
        config: OptimizationConfig,
        run_id: str,
        iteration_id: str,
        *,
        role: str,
        entity_id: str,
        ledger: EventLedger,
        revision: str | None = None,
    ) -> GitWorktreeWorkspace:
        workspace_config = config.optimization.workspace
        if workspace_config is None:  # guarded by the managed execution entry point
            raise OptimizationRuntimeError("optimization.workspace is required")
        workspace = GitWorktreeWorkspace.create(
            workspace_config.repository,
            workspace_config.root_dir / run_id / iteration_id / role,
            revision=revision or config.baseline.source_revision,
            timeout_seconds=workspace_config.timeout_seconds,
        )
        ledger.append(
            OptimizationEvent.create(
                EventType.WORKSPACE_PREPARED,
                run_id,
                iteration_id=iteration_id,
                entity_id=entity_id,
                payload={
                    "role": role,
                    "worktree": str(workspace.path),
                    "base_commit": workspace.base_commit,
                    "automatic_cleanup": False,
                },
                artifacts=(
                    _artifact_ref(workspace.evidence_dir / "workspace-manifest.json", "manifest"),
                ),
            )
        )
        return workspace

    @staticmethod
    def _materialize_candidate_change(
        config: OptimizationConfig,
        run_id: str,
        iteration_id: str,
        proposal: ChangeProposal,
        workspace: GitWorktreeWorkspace,
        lifecycle: Lifecycle,
        ledger: EventLedger,
    ) -> tuple[Lifecycle, TargetChangeSet, str]:
        raw_arguments = proposal.metadata.get("server_args")
        argument_set: dict[str, str | None] = {}
        argument_remove: tuple[str, ...] = ()
        if raw_arguments is not None:
            if not isinstance(raw_arguments, Mapping):
                raise OptimizationRuntimeError("proposal server_args must be a mapping")
            raw_set = raw_arguments.get("set", {})
            raw_remove = raw_arguments.get("remove", [])
            if not isinstance(raw_set, Mapping) or not isinstance(raw_remove, list):
                raise OptimizationRuntimeError("proposal server_args has an invalid shape")
            for raw_name, raw_value in raw_set.items():
                if not isinstance(raw_name, str):
                    raise OptimizationRuntimeError("proposal server argument names must be strings")
                if raw_value is None:
                    argument_set[raw_name] = None
                elif isinstance(raw_value, bool) or not isinstance(raw_value, str | int | float):
                    raise OptimizationRuntimeError(
                        f"proposal server argument {raw_name!r} has an invalid value"
                    )
                else:
                    argument_set[raw_name] = str(raw_value)
            if not all(isinstance(item, str) for item in raw_remove):
                raise OptimizationRuntimeError("proposal server_args.remove must contain strings")
            argument_remove = tuple(cast(list[str], raw_remove))
        argument_patch = ServerArgumentPatch(set=argument_set, remove=argument_remove)

        source_patch_path: Path | None = None
        patch_digest: str | None = None
        patch_path_value = proposal.metadata.get("patch_path")
        lifecycle = lifecycle.move_iteration(IterationState.APPLYING_PATCH)
        if patch_path_value is not None:
            if not isinstance(patch_path_value, str):
                raise OptimizationRuntimeError("proposal patch_path must be a string")
            workspace_config = config.optimization.workspace
            if workspace_config is None:  # guarded by managed execution
                raise OptimizationRuntimeError("optimization.workspace is required")
            prepared = workspace.prepare_patch(
                Path(patch_path_value).read_bytes(),
                limits=PatchLimits(
                    max_bytes=workspace_config.max_patch_bytes,
                    max_files=workspace_config.max_changed_files,
                    max_changed_lines=workspace_config.max_changed_lines,
                ),
            )
            application = workspace.apply_patch(prepared, authorize=True)
            source_patch_path = prepared.patch_path
            patch_digest = prepared.inspection.sha256
            ledger.append(
                OptimizationEvent.create(
                    EventType.PATCH_APPLIED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=proposal.proposal_id,
                    payload={
                        "role": "candidate",
                        "patch_digest": patch_digest,
                        "changed_files": list(prepared.inspection.files),
                        "changed_lines": prepared.inspection.changed_lines,
                    },
                    artifacts=(
                        _artifact_ref(prepared.patch_path, "patch"),
                        _artifact_ref(application.diff_evidence.stdout_path, "resulting-diff"),
                    ),
                )
            )
        if argument_set or argument_remove:
            ledger.append(
                OptimizationEvent.create(
                    EventType.SERVER_ARGUMENTS_APPLIED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=proposal.proposal_id,
                    payload={
                        "role": "candidate",
                        "set": dict(argument_set),
                        "remove": list(argument_remove),
                    },
                )
            )
        if source_patch_path is None and not argument_set and not argument_remove:
            raise OptimizationRuntimeError(
                "managed proposal must declare a source patch and/or server arguments"
            )
        digest_value = proposal.metadata.get("change_sha256")
        change_digest = (
            digest_value
            if isinstance(digest_value, str)
            else _stable_digest(
                {
                    "patch_sha256": patch_digest,
                    "server_args": {
                        "set": argument_set,
                        "remove": list(argument_remove),
                    },
                }
            )
        )
        return (
            lifecycle,
            TargetChangeSet(
                arg_patch=argument_patch,
                source_patch_path=source_patch_path,
            ),
            change_digest,
        )

    @staticmethod
    def _record_target_materialized(
        ledger: EventLedger,
        run_id: str,
        iteration_id: str,
        entity_id: str,
        role: str,
        spec: TargetSpec,
        change_set: TargetChangeSet,
        change_digest: str | None,
    ) -> None:
        ledger.append(
            OptimizationEvent.create(
                EventType.TARGET_MATERIALIZED,
                run_id,
                iteration_id=iteration_id,
                entity_id=entity_id,
                payload=_json_payload(
                    {
                        "role": role,
                        "provider": spec.provider,
                        "model": spec.model,
                        "endpoint": spec.endpoint,
                        "readiness_url": spec.readiness.url,
                        "fixed_gpu_ids": [str(item) for item in spec.gpus],
                        "server_arguments_set": sorted(change_set.arg_patch.set),
                        "server_arguments_removed": list(change_set.arg_patch.remove),
                        "has_source_patch": change_set.source_patch_path is not None,
                        "change_digest": change_digest,
                        "provenance": dict(spec.provenance),
                    }
                ),
            )
        )

    @staticmethod
    def _build_managed_target(
        controller: TargetController,
        spec: TargetSpec,
        workspace: GitWorktreeWorkspace,
        evidence_dir: Path,
        ledger: EventLedger,
        run_id: str,
        iteration_id: str,
        entity_id: str,
        role: str,
    ) -> None:
        build_spec = spec.build or BuildSpec()
        ledger.append(
            OptimizationEvent.create(
                EventType.BUILD_STARTED,
                run_id,
                iteration_id=iteration_id,
                entity_id=entity_id,
                payload={"role": role, "command_count": len(build_spec.commands)},
            )
        )
        try:
            results = controller.build(workspace.path, build_spec, evidence_dir)
        except BaseException as exc:
            ledger.append(
                OptimizationEvent.create(
                    EventType.BUILD_FAILED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=entity_id,
                    payload={"role": role, **_error_payload(exc)},
                    artifacts=_artifact_refs_in(evidence_dir),
                )
            )
            raise
        ledger.append(
            OptimizationEvent.create(
                EventType.BUILD_COMPLETED,
                run_id,
                iteration_id=iteration_id,
                entity_id=entity_id,
                payload={"role": role, "command_count": len(results)},
                artifacts=_artifact_refs_in(evidence_dir),
            )
        )

    @staticmethod
    def _evaluate_managed_service(
        *,
        controller: TargetController,
        config: OptimizationConfig,
        target_spec: TargetSpec,
        change_set: TargetChangeSet,
        workspace: GitWorktreeWorkspace,
        evidence_dir: Path,
        ledger: EventLedger,
        run_id: str,
        iteration_id: str,
        entity_id: str,
        role: str,
        trial_id: str,
        lifecycle: Lifecycle,
        start_state: IterationState,
        ready_state: IterationState,
        evaluation_state: IterationState,
        stop_state: IterationState,
        baseline_values: Mapping[str, float],
        apply_promotion_gate: bool,
    ) -> tuple[
        Lifecycle,
        TieredEvaluationResult | WorkloadSuiteEvaluationResult,
    ]:
        lifecycle = lifecycle.move_iteration(start_state)
        ledger.append(
            OptimizationEvent.create(
                EventType.SERVICE_STARTING,
                run_id,
                iteration_id=iteration_id,
                entity_id=entity_id,
                payload={"role": role, "trial_id": trial_id},
            )
        )
        try:
            handle = controller.start(
                workspace.path,
                target_spec,
                change_set,
                evidence_dir,
                run_id=run_id,
                trial_id=trial_id,
            )
        except BaseException as exc:
            ledger.append(
                OptimizationEvent.create(
                    EventType.SERVICE_FAILED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=entity_id,
                    payload={"role": role, "stage": "start", **_error_payload(exc)},
                    artifacts=_artifact_refs_in(evidence_dir),
                )
            )
            raise
        evaluation_result: TieredEvaluationResult | WorkloadSuiteEvaluationResult | None = None
        try:
            ledger.append(
                OptimizationEvent.create(
                    EventType.SERVICE_STARTED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=entity_id,
                    payload={
                        "role": role,
                        "trial_id": trial_id,
                        "handle_id": handle.handle_id,
                        "pid": handle.pid,
                    },
                )
            )
            lifecycle = lifecycle.move_iteration(ready_state)
            try:
                controller.wait_ready(handle)
            except BaseException as exc:
                ledger.append(
                    OptimizationEvent.create(
                        EventType.SERVICE_FAILED,
                        run_id,
                        iteration_id=iteration_id,
                        entity_id=entity_id,
                        payload={"role": role, "stage": "readiness", **_error_payload(exc)},
                    )
                )
                raise
            ledger.append(
                OptimizationEvent.create(
                    EventType.SERVICE_READY,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=entity_id,
                    payload={
                        "role": role,
                        "trial_id": trial_id,
                        "handle_id": handle.handle_id,
                        "endpoint": handle.endpoint,
                    },
                )
            )
            lifecycle = lifecycle.move_iteration(evaluation_state)
            runtime_environment = {
                "EUBOULIA_TARGET_ENDPOINT": handle.endpoint,
                "EUBOULIA_TARGET_ROLE": role,
                "EUBOULIA_TARGET_TRIAL_ID": trial_id,
            }
            ledger.append(
                OptimizationEvent.create(
                    EventType.EVALUATION_STARTED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=entity_id,
                    payload={
                        "role": role,
                        "trial_id": trial_id,
                        "workspace": str(workspace.path),
                        "profiler_trial": False,
                    },
                )
            )
            evaluator = TieredEvaluator(config.execution.artifacts_dir / run_id / "evaluations")
            evaluation_result = _execute_workload_suite(
                config,
                trial_id=trial_id,
                workspace=workspace.path,
                baseline_values=baseline_values,
                apply_promotion_gate=apply_promotion_gate,
                evaluator=evaluator,
                artifact_dir=config.execution.artifacts_dir / run_id / "evaluations" / trial_id,
                runtime_environment=runtime_environment,
            )
            ledger.append(
                OptimizationEvent.create(
                    EventType.EVALUATION_COMPLETED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=entity_id,
                    payload={"role": role, **_json_payload(evaluation_result.to_dict())},
                    artifacts=(
                        _artifact_ref(
                            _evaluation_artifact(evaluation_result),
                            f"{role}-evaluation",
                        ),
                    ),
                )
            )
        finally:
            stop_boundary_error: BaseException | None = None
            try:
                lifecycle = lifecycle.move_iteration(stop_state)
                ledger.append(
                    OptimizationEvent.create(
                        EventType.SERVICE_STOPPING,
                        run_id,
                        iteration_id=iteration_id,
                        entity_id=entity_id,
                        payload={
                            "role": role,
                            "trial_id": trial_id,
                            "handle_id": handle.handle_id,
                        },
                    )
                )
            except BaseException as exc:
                # Ledger/state failures must not bypass teardown of an owned child.
                stop_boundary_error = exc
            try:
                controller.stop(handle)
            except BaseException as exc:
                with suppress(BaseException):
                    ledger.append(
                        OptimizationEvent.create(
                            EventType.SERVICE_FAILED,
                            run_id,
                            iteration_id=iteration_id,
                            entity_id=entity_id,
                            payload={"role": role, "stage": "stop", **_error_payload(exc)},
                        )
                    )
                raise
            ledger.append(
                OptimizationEvent.create(
                    EventType.SERVICE_STOPPED,
                    run_id,
                    iteration_id=iteration_id,
                    entity_id=entity_id,
                    payload={
                        "role": role,
                        "trial_id": trial_id,
                        "handle_id": handle.handle_id,
                    },
                    artifacts=_service_artifacts(handle),
                )
            )
            if stop_boundary_error is not None:
                raise stop_boundary_error
        if evaluation_result is None:  # pragma: no cover - evaluation either returns or raises
            raise AssertionError("managed evaluation completed without a result")
        return lifecycle, evaluation_result

    @staticmethod
    def _remember(
        memory: SQLiteMemoryStore,
        outcome: IterationOutcome,
        config: OptimizationConfig,
        hardware_fingerprint: str,
        workload_digest: str,
        policy_digest: str,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            memory_id=f"memory-{hashlib.sha256(outcome.outcome_id.encode()).hexdigest()[:20]}",
            outcome_id=outcome.outcome_id,
            run_id=outcome.run_id,
            iteration_id=outcome.iteration_id,
            framework=config.framework.value,
            framework_revision=config.baseline.source_revision,
            hardware_fingerprint=hardware_fingerprint,
            model_revision=config.models.target.revision,
            workload_digest=workload_digest,
            benchmark_policy_digest=policy_digest,
            proposal_id=outcome.proposal_id,
            outcome=outcome.status,
            summary=outcome.summary,
            patch_digest=outcome.patch_digest,
            relative_improvement=outcome.relative_improvement,
            details={
                "champion_before": outcome.champion_before,
                "champion_after": outcome.champion_after,
                "evidence_outcome_id": outcome.outcome_id,
            },
        )
        return memory.record(entry)

    @staticmethod
    def _failed_outcome(
        run_id: str,
        iteration_id: str,
        champion_id: str,
        proposal: ChangeProposal,
        status: OutcomeStatus,
        error: str,
    ) -> IterationOutcome:
        patch_digest = proposal.metadata.get("change_sha256")
        if not isinstance(patch_digest, str):
            patch_digest = proposal.metadata.get("patch_sha256")
        return IterationOutcome(
            outcome_id=f"outcome-{uuid.uuid4().hex}",
            run_id=run_id,
            iteration_id=iteration_id,
            proposal_id=proposal.proposal_id,
            status=status,
            summary=error,
            champion_before=champion_id,
            champion_after=champion_id,
            patch_digest=patch_digest if isinstance(patch_digest, str) else None,
        )

    @staticmethod
    def _result(
        run_id: str,
        lifecycle: Lifecycle,
        champion_id: str,
        profile: ProfileResult | None,
        analysis: AnalysisReport | None,
        proposals: list[ChangeProposal],
        outcomes: list[IterationOutcome],
        config: OptimizationConfig,
        stop_reason: str | None,
    ) -> OptimizationRunResult:
        return OptimizationRunResult(
            run_id=run_id,
            run_state=lifecycle.run,
            iteration_state=lifecycle.iteration,
            champion_id=champion_id,
            profile=profile,
            analysis=analysis,
            proposals=tuple(proposals),
            outcomes=tuple(outcomes),
            event_ledger=config.execution.event_ledger,
            memory=config.execution.memory,
            stop_reason=stop_reason,
        )


def _target_spec(
    config: OptimizationConfig,
    runtime_record: RuntimeProvenanceRecord | None = None,
) -> TargetSpec:
    target = config.target
    if target is None:
        raise OptimizationRuntimeError("target configuration is required for managed execution")
    build: BuildSpec | None = None
    if target.build is not None:
        build = BuildSpec(
            commands=tuple(
                BuildCommandSpec(
                    name=command.name,
                    argv=command.argv,
                    timeout_seconds=command.timeout_seconds,
                    env=command.env,
                )
                for command in target.build.commands
            )
        )
    provenance: dict[str, JSONValue] = dict(target.provenance)
    if target.runtime is not None:
        provenance["runtime"] = (
            runtime_record.to_dict()
            if runtime_record is not None
            else {
                "expected": {
                    "container": (
                        None
                        if target.runtime.expected.container is None
                        else {
                            "image": target.runtime.expected.container.image,
                            "digest": target.runtime.expected.container.digest,
                        }
                    ),
                    "components": {
                        name: {
                            "version": component.version,
                            "revision": component.revision,
                            "digest": component.digest,
                            "path": None if component.path is None else str(component.path),
                            "dirty": component.dirty,
                            "metadata": dict(component.metadata),
                        }
                        for name, component in target.runtime.expected.components.items()
                    },
                }
            }
        )
    if target.serving is not None:
        speculative = target.serving.speculative
        draft = speculative.draft
        provenance["serving"] = {
            "backends": dict(target.serving.backends),
            "speculative": {
                "algorithm": speculative.algorithm,
                "draft": (
                    None
                    if draft is None
                    else {
                        "kind": draft.kind.value,
                        "model_ref": draft.model_ref,
                        "model_path": config.models.by_id(draft.model_ref).path,
                    }
                ),
                "num_steps": speculative.num_steps,
                "eagle_topk": speculative.eagle_topk,
                "num_draft_tokens": speculative.num_draft_tokens,
                "adaptive": speculative.adaptive,
                "draft_attention_backend": speculative.draft_attention_backend,
            },
        }
    return TargetSpec(
        provider=target.provider.value,
        model=config.models.target.path,
        launch_argv=target.launch.argv,
        launch_env=target.launch.env,
        endpoint=config.endpoint,
        readiness=ReadinessSpec(
            url=target.readiness.url,
            timeout_seconds=target.readiness.timeout_seconds,
            interval_seconds=target.readiness.interval_seconds,
        ),
        build=build,
        gpus=target.gpus,
        shutdown_timeout_seconds=target.shutdown_timeout_seconds,
        provenance=provenance,
    )


def _error_payload(exc: BaseException) -> dict[str, JSONValue]:
    return {"error_type": type(exc).__name__, "error": str(exc)}


def _artifact_refs_in(directory: Path) -> tuple[ArtifactRef, ...]:
    if not directory.is_dir():
        return ()
    artifacts: list[ArtifactRef] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_file() and not path.is_symlink():
            artifacts.append(_artifact_ref(path, f"target-{path.name}"))
    return tuple(artifacts)


def _service_artifacts(handle: ServiceHandle) -> tuple[ArtifactRef, ...]:
    artifacts: list[ArtifactRef] = []
    for label, path in (
        ("service-manifest", handle.manifest_path),
        ("service-stdout", handle.stdout_path),
        ("service-stderr", handle.stderr_path),
    ):
        if path.is_file() and not path.is_symlink():
            artifacts.append(_artifact_ref(path, label))
    return tuple(artifacts)


def _evaluation_plan(
    config: OptimizationConfig,
    iteration_id: str,
    workspace: Path,
    *,
    point: WorkloadPointConfig,
    metrics_path: Path,
    baseline_value: float | None,
    apply_promotion_gate: bool,
    include_checks: bool,
    runtime_environment: Mapping[str, str] | None = None,
) -> EvaluationPlan:
    evaluation = config.optimization.evaluation
    if evaluation.metrics_path is None:
        raise OptimizationRuntimeError(
            "optimization.evaluation.metrics_path is required for active evaluation"
        )
    if apply_promotion_gate and baseline_value is None:
        raise OptimizationRuntimeError(
            "a measured or configured baseline value is required for candidate evaluation"
        )
    evaluation_environment = {
        "EUBOULIA_BENCHMARK_MODE": config.benchmark.mode,
        "EUBOULIA_BENCHMARK_PARAMETERS": json.dumps(
            config.benchmark.parameters, sort_keys=True, separators=(",", ":")
        ),
        "EUBOULIA_CONCURRENCY": str(point.concurrency),
        "EUBOULIA_DATASET": config.workload_suite.dataset,
        "EUBOULIA_FRAMEWORK": config.framework.value,
        "EUBOULIA_INPUT_TOKENS": str(point.input_tokens),
        "EUBOULIA_METRICS_PATH": str(metrics_path),
        "EUBOULIA_MODEL": config.models.target.path,
        "EUBOULIA_MODEL_SERVED_NAME": config.models.target.served_name,
        "EUBOULIA_NUM_PROMPTS": str(point.num_prompts),
        "EUBOULIA_OUTPUT_TOKENS": str(point.output_tokens),
        "EUBOULIA_REQUEST_RATE": str(
            config.workload_suite.request_rate if point.request_rate is None else point.request_rate
        ),
        "EUBOULIA_TARGET_ENDPOINT": config.endpoint,
        "EUBOULIA_WORKLOAD_NAME": config.workload_suite.suite_id,
        "EUBOULIA_WORKLOAD_POINT_ID": point.point_id,
    }
    if runtime_environment is not None:
        evaluation_environment.update(runtime_environment)
    preflight: list[CommandSpec] = []
    correctness: list[CommandSpec] = []
    performance: list[CommandSpec] = []
    for tier in evaluation.tiers:
        commands = [
            _command_spec(command, tier, evaluation_environment) for command in tier.commands
        ]
        if tier.kind.value == "smoke":
            if include_checks:
                preflight.extend(commands)
        elif tier.kind.value == "correctness":
            if include_checks:
                correctness.extend(commands)
        else:
            performance.extend(commands)
    if include_checks and not correctness:
        raise OptimizationRuntimeError(
            "active evaluation requires at least one correctness command"
        )
    if len(performance) != 1:
        raise OptimizationRuntimeError("active evaluation requires exactly one performance command")
    promotion = evaluation.promotion
    if promotion is None:  # normalized configuration always supplies this
        raise OptimizationRuntimeError("optimization.evaluation.promotion is required")
    minimum = 0.0
    if apply_promotion_gate:
        if point.point_id in promotion.primary_points:
            minimum = max(0.0, promotion.min_relative_improvement - promotion.noise_tolerance)
        else:
            minimum = -(promotion.max_regression_per_point + promotion.noise_tolerance)
    objective = ObjectiveSpec(
        metric=evaluation.metric,
        direction=ObjectiveDirection(evaluation.direction.value),
        baseline=baseline_value if apply_promotion_gate else None,
        minimum_relative_improvement=minimum,
    )
    return EvaluationPlan(
        trial_id=iteration_id,
        workspace=workspace,
        preflight=tuple(preflight),
        correctness=tuple(correctness),
        benchmark=BenchmarkSpec(performance[0], metrics_path),
        objective=objective,
        profiler_trial=False,
    )


def _execute_workload_suite(
    config: OptimizationConfig,
    *,
    trial_id: str,
    workspace: Path,
    baseline_values: Mapping[str, float],
    apply_promotion_gate: bool,
    evaluator: TieredEvaluator,
    artifact_dir: Path,
    runtime_environment: Mapping[str, str] | None = None,
) -> TieredEvaluationResult | WorkloadSuiteEvaluationResult:
    evaluation = config.optimization.evaluation
    if evaluation.metrics_path is None:
        raise OptimizationRuntimeError(
            "optimization.evaluation.metrics_path is required for active evaluation"
        )
    point_results: dict[str, TieredEvaluationResult] = {}
    for index, point in enumerate(config.workload_suite.points):
        point_trial_id = (
            trial_id
            if config.schema_version == 2 and len(config.workload_suite.points) == 1
            else f"{trial_id}-{point.point_id}"
        )
        plan = _evaluation_plan(
            config,
            point_trial_id,
            workspace,
            point=point,
            metrics_path=_point_metrics_path(
                evaluation.metrics_path,
                point.point_id,
                single_point=len(config.workload_suite.points) == 1,
            ),
            baseline_value=baseline_values.get(point.point_id),
            apply_promotion_gate=apply_promotion_gate,
            include_checks=index == 0,
            runtime_environment=runtime_environment,
        )
        point_result = evaluator.execute(evaluator.authorize(plan, approved=True))
        point_results[point.point_id] = point_result
        promotion = evaluation.promotion
        if point_result.outcome is EvaluationOutcome.FAILED and (
            index == 0 or promotion is None or promotion.require_all_points_valid
        ):
            break
    if config.schema_version == 2 and len(point_results) == 1:
        return next(iter(point_results.values()))
    return _aggregate_suite_result(
        config,
        trial_id=trial_id,
        point_results=point_results,
        artifact_dir=artifact_dir,
        apply_promotion_gate=apply_promotion_gate,
    )


def _point_metrics_path(base: Path, point_id: str, *, single_point: bool) -> Path:
    if single_point:
        return base
    return base.parent / "workload-points" / point_id / base.name


def _evaluation_artifact(
    result: TieredEvaluationResult | WorkloadSuiteEvaluationResult,
) -> Path:
    filename = (
        "suite-evaluation.json"
        if isinstance(result, WorkloadSuiteEvaluationResult)
        else "evaluation.json"
    )
    return result.artifact_dir / filename


def _objective_values(
    config: OptimizationConfig,
    result: TieredEvaluationResult | WorkloadSuiteEvaluationResult,
) -> dict[str, float]:
    if isinstance(result, WorkloadSuiteEvaluationResult):
        return dict(result.objective_values)
    if result.objective_value is None:
        return {}
    return {config.workload_suite.points[0].point_id: result.objective_value}


def _command_spec(
    command: OptimizationCommandConfig,
    tier: EvaluationTierConfig,
    runtime_environment: Mapping[str, str] | None = None,
) -> CommandSpec:
    environment = dict(command.env)
    environment.setdefault("EUBOULIA_WARMUPS", str(tier.warmups))
    environment.setdefault("EUBOULIA_REPETITIONS", str(tier.repetitions))
    if runtime_environment is not None:
        environment.update(runtime_environment)
    return CommandSpec(
        name=command.name,
        argv=command.argv,
        timeout_seconds=command.timeout_seconds or tier.timeout_seconds,
        env_overrides=environment,
    )


def _aggregate_suite_result(
    config: OptimizationConfig,
    *,
    trial_id: str,
    point_results: Mapping[str, TieredEvaluationResult],
    artifact_dir: Path,
    apply_promotion_gate: bool,
) -> WorkloadSuiteEvaluationResult:
    promotion = config.optimization.evaluation.promotion
    if promotion is None:
        raise OptimizationRuntimeError("optimization.evaluation.promotion is required")
    objective_values = {
        point_id: result.objective_value
        for point_id, result in point_results.items()
        if result.objective_value is not None
    }
    relative_improvements = {
        point_id: result.relative_improvement
        for point_id, result in point_results.items()
        if result.relative_improvement is not None
    }
    required_ids = (
        tuple(point.point_id for point in config.workload_suite.points)
        if not apply_promotion_gate or promotion.require_all_points_valid
        else promotion.primary_points
    )
    missing_required = tuple(point_id for point_id in required_ids if point_id not in point_results)
    failed_required = tuple(
        point_id
        for point_id in required_ids
        if point_id in point_results and point_results[point_id].outcome is EvaluationOutcome.FAILED
    )
    rejected_points = tuple(
        point_id
        for point_id, result in point_results.items()
        if result.outcome is EvaluationOutcome.REJECTED
    )
    if missing_required or failed_required:
        outcome = EvaluationOutcome.FAILED
        reason = "required workload point evaluation failed"
    elif rejected_points:
        outcome = EvaluationOutcome.REJECTED
        reason = "promotion gate rejected workload point(s): " + ", ".join(rejected_points)
    else:
        outcome = EvaluationOutcome.PASSED
        reason = None
    primary_relatives = tuple(
        relative_improvements[point_id]
        for point_id in promotion.primary_points
        if point_id in relative_improvements
    )
    relative_improvement = min(primary_relatives) if primary_relatives else None
    gate_passed = outcome is EvaluationOutcome.PASSED
    result = WorkloadSuiteEvaluationResult(
        trial_id=trial_id,
        outcome=outcome,
        point_results=point_results,
        primary_points=promotion.primary_points,
        objective_values=objective_values,
        relative_improvements=relative_improvements,
        relative_improvement=relative_improvement,
        gate_passed=gate_passed,
        promotable=gate_passed,
        artifact_dir=artifact_dir,
        reason=reason,
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "suite-evaluation.json").write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _outcome_status(
    result: TieredEvaluationResult | WorkloadSuiteEvaluationResult,
) -> OutcomeStatus:
    if result.outcome is EvaluationOutcome.PASSED and result.promotable:
        return OutcomeStatus.ACCEPTED
    if result.outcome is EvaluationOutcome.REJECTED:
        return OutcomeStatus.REJECTED
    if result.outcome is EvaluationOutcome.PROFILE_ONLY:
        return OutcomeStatus.INVALID
    return OutcomeStatus.FAILED


def _terminal_iteration_state(status: OutcomeStatus) -> IterationState:
    return {
        OutcomeStatus.ACCEPTED: IterationState.ACCEPTED,
        OutcomeStatus.REJECTED: IterationState.REJECTED,
        OutcomeStatus.INVALID: IterationState.INVALID,
        OutcomeStatus.FAILED: IterationState.FAILED,
    }[status]


def _profile_dict(profile: ProfileResult) -> dict[str, JSONValue]:
    return {
        "profile_id": profile.profile_id,
        "provider": profile.provider,
        "candidate_id": profile.candidate_id,
        "artifacts": [artifact.to_dict() for artifact in profile.artifacts],
        "metrics": dict(profile.metrics),
        "complete": profile.complete,
        "metadata": dict(profile.metadata),
    }


def _finding_dict(finding: Finding) -> dict[str, JSONValue]:
    return {
        "finding_id": finding.finding_id,
        "category": finding.category,
        "summary": finding.summary,
        "confidence": finding.confidence,
        "evidence_artifact_ids": list(finding.evidence_artifact_ids),
        "metadata": dict(finding.metadata),
    }


def _analysis_dict(report: AnalysisReport) -> dict[str, JSONValue]:
    return {
        "analysis_id": report.analysis_id,
        "profile_id": report.profile_id,
        "summary": report.summary,
        "findings": [_finding_dict(finding) for finding in report.findings],
        "metadata": dict(report.metadata),
    }


def _proposal_dict(proposal: ChangeProposal) -> dict[str, JSONValue]:
    return {
        "proposal_id": proposal.proposal_id,
        "analysis_id": proposal.analysis_id,
        "title": proposal.title,
        "rationale": proposal.rationale,
        "change_kind": proposal.change_kind.value,
        "catalog_entry_id": proposal.catalog_entry_id,
        "predicted_metric": proposal.predicted_metric,
        "risk": proposal.risk,
        "metadata": dict(proposal.metadata),
    }


def _json_payload(value: Mapping[str, object]) -> Mapping[str, JSONValue]:
    normalized: object = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    if not isinstance(normalized, dict):  # pragma: no cover - mapping input guarantees this
        raise TypeError("event payload must normalize to an object")
    return cast(dict[str, JSONValue], normalized)


def _artifact_ref(path: Path, label: str) -> ArtifactRef:
    if path.is_dir():
        manifest = path.parent / "evidence" / "workspace-manifest.json"
        path = manifest if manifest.is_file() else path
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactRef(
        artifact_id=f"{label}-{digest[:16]}",
        path=str(path),
        sha256=digest,
        size_bytes=len(payload),
        media_type="application/json" if path.suffix == ".json" else "application/octet-stream",
    )


def _run_id(value: str | None) -> str:
    selected = value or f"opt-{uuid.uuid4().hex[:16]}"
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", selected) is None:
        raise ValueError("run_id contains unsafe path characters")
    return selected


def _required_capabilities(config: OptimizationConfig) -> tuple[Capability, ...]:
    required = {
        Capability.WORKSPACE_WRITE,
        Capability.BENCHMARK_EXECUTION,
    }
    if config.target is not None:
        required.add(Capability.OWNED_SERVICE_LIFECYCLE)
        if config.target.build is not None and config.target.build.commands:
            required.add(Capability.BUILD_EXECUTION)
    return tuple(sorted(required, key=lambda item: item.value))


def _stable_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _config_digest(config: OptimizationConfig) -> str:
    return hashlib.sha256(config.source.read_bytes()).hexdigest()


def _workload_digest(config: OptimizationConfig) -> str:
    target = config.target
    target_payload: Mapping[str, object] | None = None
    if target is not None:
        target_payload = {
            "provider": target.provider.value,
            "launch_argv": list(target.launch.argv),
            "launch_env": dict(sorted(target.launch.env.items())),
            "gpus": list(target.gpus),
            "provenance": dict(_target_spec(config).provenance),
            "build": (
                None
                if target.build is None
                else [
                    {
                        "name": command.name,
                        "argv": list(command.argv),
                        "timeout_seconds": command.timeout_seconds,
                        "env": dict(sorted(command.env.items())),
                    }
                    for command in target.build.commands
                ]
            ),
        }
    return _stable_digest(
        {
            "models": {
                "target": {
                    "id": config.models.target.model_id,
                    "path": config.models.target.path,
                    "served_name": config.models.target.served_name,
                    "revision": config.models.target.revision,
                    "weights_manifest_sha256": config.models.target.weights_manifest_sha256,
                },
                "drafts": [
                    {
                        "id": draft.model_id,
                        "path": draft.path,
                        "served_name": draft.served_name,
                        "revision": draft.revision,
                        "weights_manifest_sha256": draft.weights_manifest_sha256,
                    }
                    for draft in config.models.drafts
                ],
            },
            "workload_suite": {
                "id": config.workload_suite.suite_id,
                "dataset": config.workload_suite.dataset,
                "request_rate": config.workload_suite.request_rate,
                "points": [
                    {
                        "id": point.point_id,
                        "input_tokens": point.input_tokens,
                        "output_tokens": point.output_tokens,
                        "concurrency": point.concurrency,
                        "num_prompts": point.num_prompts,
                        "request_rate": point.request_rate,
                    }
                    for point in config.workload_suite.points
                ],
            },
            "endpoint": config.endpoint,
            "baseline_target_parameters": dict(config.baseline.target_parameters),
            "managed_target": target_payload,
        }
    )


def _policy_digest(config: OptimizationConfig) -> str:
    evaluation = config.optimization.evaluation
    promotion = evaluation.promotion
    return _stable_digest(
        {
            "metric": evaluation.metric,
            "direction": evaluation.direction.value,
            "minimum": evaluation.min_relative_improvement,
            "regression": evaluation.max_regression,
            "noise": evaluation.noise_tolerance,
            "tiers": [
                {
                    "kind": tier.kind.value,
                    "warmups": tier.warmups,
                    "repetitions": tier.repetitions,
                    "timeout_seconds": tier.timeout_seconds,
                    "required_metrics": list(tier.required_metrics),
                    "commands": [
                        {
                            "name": command.name,
                            "argv": list(command.argv),
                            "timeout_seconds": command.timeout_seconds,
                            "env": dict(sorted(command.env.items())),
                        }
                        for command in tier.commands
                    ],
                }
                for tier in evaluation.tiers
            ],
            "metrics_path": str(evaluation.metrics_path),
            "baseline_value": (None if config.target is not None else evaluation.baseline_value),
            "baseline_values": dict(config.baseline.metric_values),
            "promotion": (
                None
                if promotion is None
                else {
                    "primary_points": list(promotion.primary_points),
                    "require_all_points_valid": promotion.require_all_points_valid,
                    "min_relative_improvement": promotion.min_relative_improvement,
                    "max_regression_per_point": promotion.max_regression_per_point,
                    "noise_tolerance": promotion.noise_tolerance,
                }
            ),
        }
    )


def _hardware_fingerprint(config: OptimizationConfig) -> str:
    gpu_ids = () if config.target is None else config.target.gpus
    return hardware_identity(None, gpu_ids)


__all__ = [
    "OptimizationPlan",
    "OptimizationRunResult",
    "OptimizationRunner",
    "OptimizationRuntimeError",
]
