"""Event-driven orchestration for the first iterative optimization runtime."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from euboulia.models import JSONValue
from euboulia.optimization.budget import BudgetLimits, BudgetTracker
from euboulia.optimization.config import (
    EvaluationTierConfig,
    OptimizationCommandConfig,
    OptimizationConfig,
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
)
from euboulia.optimization.events import (
    EventLedger,
    EventType,
    OptimizationEvent,
)
from euboulia.optimization.memory import SQLiteMemoryStore
from euboulia.optimization.planner import PatchCatalog, RulePlanner
from euboulia.optimization.profiling import ImportedProfiler, RuleAnalyzer
from euboulia.optimization.state import Lifecycle
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

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "campaign": self.campaign,
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


class OptimizationRunner:
    """Join imported evidence, rules, isolated patches, evaluation, and memory.

    The first runtime intentionally does not start or stop a serving process. An
    evaluation command must be finite and must own any setup it requires. A
    future ``TargetController`` can add owned-service lifecycle as a separate
    capability without widening this runner's existing authorization flags.
    """

    def plan(self, config: OptimizationConfig) -> OptimizationPlan:
        """Analyze declared artifacts and select catalog proposals without writes."""

        context = StageContext(
            run_id="preview",
            iteration_id="preview-001",
            artifact_dir=config.execution.artifacts_dir,
        )
        profile, analysis, proposals = self._deliberate(config, context, ())
        warnings = (
            "plan is read-only; no event, memory, worktree, command, or service was created",
            "profile evidence is diagnostic-only and cannot promote a candidate",
            "active patch evaluation requires both workspace_write and benchmark_execution",
            "the initial runner does not manage a persistent SGLang or vLLM service",
        )
        return OptimizationPlan(
            campaign=config.name,
            framework=config.framework.value,
            profile=profile,
            analysis=analysis,
            proposals=proposals,
            required_capabilities=(
                Capability.WORKSPACE_WRITE,
                Capability.BENCHMARK_EXECUTION,
            ),
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
        hardware_fingerprint = _hardware_fingerprint()
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
                        model_revision=config.workload.model,
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
                required = {Capability.WORKSPACE_WRITE, Capability.BENCHMARK_EXECUTION}
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
                        "explicit patch and evaluation authorization required",
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
                    lifecycle, evaluation, patch_digest, workspace_path = self._execute_candidate(
                        config,
                        selected_run_id,
                        iteration_id,
                        champion_id,
                        proposal,
                        lifecycle,
                        ledger,
                    )
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
                        patch_digest=patch_digest,
                        artifacts=(_artifact_ref(workspace_path, "workspace"),),
                        metadata={"evaluation": dict(_json_payload(evaluation.to_dict()))},
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
                    lifecycle = lifecycle.move_iteration(IterationState.INVALID)
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
        planner = RulePlanner(
            PatchCatalog.load(planner_config.patch_catalog),
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
    ) -> tuple[Lifecycle, TieredEvaluationResult, str, Path]:
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
        evaluation_plan = _evaluation_plan(
            config,
            iteration_id,
            workspace.path,
            champion_id,
        )
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
        evaluation = evaluator.execute(evaluator.authorize(evaluation_plan, approved=True))
        ledger.append(
            OptimizationEvent.create(
                EventType.EVALUATION_COMPLETED,
                run_id,
                iteration_id=iteration_id,
                entity_id=proposal.proposal_id,
                payload=_json_payload(evaluation.to_dict()),
                artifacts=(
                    _artifact_ref(evaluation.artifact_dir / "evaluation.json", "evaluation"),
                ),
            )
        )
        return lifecycle, evaluation, prepared.inspection.sha256, workspace.path

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
            model_revision=config.workload.model,
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


def _evaluation_plan(
    config: OptimizationConfig,
    iteration_id: str,
    workspace: Path,
    champion_id: str,
) -> EvaluationPlan:
    del champion_id  # baseline value is pinned in the current configuration
    evaluation = config.optimization.evaluation
    if evaluation.metrics_path is None:
        raise OptimizationRuntimeError(
            "optimization.evaluation.metrics_path is required for active evaluation"
        )
    if evaluation.baseline_value is None:
        raise OptimizationRuntimeError(
            "optimization.evaluation.baseline_value is required for active evaluation"
        )
    preflight: list[CommandSpec] = []
    correctness: list[CommandSpec] = []
    performance: list[CommandSpec] = []
    for tier in evaluation.tiers:
        commands = [_command_spec(command, tier) for command in tier.commands]
        if tier.kind.value == "smoke":
            preflight.extend(commands)
        elif tier.kind.value == "correctness":
            correctness.extend(commands)
        else:
            performance.extend(commands)
    if not correctness:
        raise OptimizationRuntimeError(
            "active evaluation requires at least one correctness command"
        )
    if len(performance) != 1:
        raise OptimizationRuntimeError("active evaluation requires exactly one performance command")
    minimum = evaluation.min_relative_improvement
    if minimum == 0.0:
        minimum = -(evaluation.max_regression + evaluation.noise_tolerance)
    objective = ObjectiveSpec(
        metric=evaluation.metric,
        direction=ObjectiveDirection(evaluation.direction.value),
        baseline=evaluation.baseline_value,
        minimum_relative_improvement=minimum,
    )
    return EvaluationPlan(
        trial_id=iteration_id,
        workspace=workspace,
        preflight=tuple(preflight),
        correctness=tuple(correctness),
        benchmark=BenchmarkSpec(performance[0], evaluation.metrics_path),
        objective=objective,
        profiler_trial=False,
    )


def _command_spec(command: OptimizationCommandConfig, tier: EvaluationTierConfig) -> CommandSpec:
    environment = dict(command.env)
    environment.setdefault("EUBOULIA_WARMUPS", str(tier.warmups))
    environment.setdefault("EUBOULIA_REPETITIONS", str(tier.repetitions))
    return CommandSpec(
        name=command.name,
        argv=command.argv,
        timeout_seconds=command.timeout_seconds or tier.timeout_seconds,
        env_overrides=environment,
    )


def _outcome_status(result: TieredEvaluationResult) -> OutcomeStatus:
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


def _stable_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _config_digest(config: OptimizationConfig) -> str:
    return hashlib.sha256(config.source.read_bytes()).hexdigest()


def _workload_digest(config: OptimizationConfig) -> str:
    workload = config.workload
    return _stable_digest(
        {
            "name": workload.name,
            "model": workload.model,
            "input_tokens": workload.input_tokens,
            "output_tokens": workload.output_tokens,
            "concurrency": workload.concurrency,
            "num_prompts": workload.num_prompts,
            "dataset": workload.dataset,
        }
    )


def _policy_digest(config: OptimizationConfig) -> str:
    evaluation = config.optimization.evaluation
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
                    "commands": [list(command.argv) for command in tier.commands],
                }
                for tier in evaluation.tiers
            ],
        }
    )


def _hardware_fingerprint() -> str:
    value = f"{platform.node()}\0{platform.machine()}\0{platform.platform()}"
    return f"host-{hashlib.sha256(value.encode()).hexdigest()[:20]}"


__all__ = [
    "OptimizationPlan",
    "OptimizationRunResult",
    "OptimizationRunner",
    "OptimizationRuntimeError",
]
