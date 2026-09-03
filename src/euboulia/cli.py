"""Command-line interface for Euboulia."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from euboulia import __version__
from euboulia.adapters import AdapterError
from euboulia.doctor import required_checks_pass, run_doctor
from euboulia.ledger import ExperimentLedger, LedgerCorruptionError
from euboulia.optimization.config import (
    OptimizationConfigError,
    OptimizationRecipeResolution,
    dump_resolved_optimization_config,
    load_optimization_config,
    optimization_execution_lock_issues,
    require_optimization_execution_lock,
    resolve_optimization_config,
)
from euboulia.optimization.contracts import Capability, RunState
from euboulia.optimization.evaluator import EvaluationError
from euboulia.optimization.events import EventLedger, EventLedgerCorruptionError
from euboulia.optimization.memory import MemoryConflictError
from euboulia.optimization.planner import PatchCatalogError
from euboulia.optimization.runner import OptimizationRunner, OptimizationRuntimeError
from euboulia.optimization.target import TargetError
from euboulia.optimization.workspace import WorkspaceError
from euboulia.recipe import (
    ConfigError,
    RecipeRunResult,
    RecipeSafetyError,
    evaluate_recipe_results,
    load_recipe,
    plan_recipe,
    run_recipe,
)

Command = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="euboulia",
        description="Evidence-driven optimization experiments for SGLang and vLLM.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="inspect local tools read-only")
    doctor_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    doctor_parser.set_defaults(handler=_doctor)

    plan_parser = subparsers.add_parser("plan", help="render a recipe without executing it")
    _add_recipe_argument(plan_parser)
    plan_parser.add_argument("--run-id", default="preview", help="artifact path label")
    plan_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    plan_parser.set_defaults(handler=_plan)

    run_parser = subparsers.add_parser("run", help="plan or explicitly execute a recipe")
    _add_recipe_argument(run_parser)
    authorization = run_parser.add_mutually_exclusive_group()
    authorization.add_argument(
        "--execute",
        action="store_true",
        help="authorize benchmark-client execution",
    )
    authorization.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly retain the default non-executing behavior",
    )
    run_parser.add_argument("--run-id", help="optional deterministic run identifier")
    run_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    run_parser.set_defaults(handler=_run)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="apply recipe gates to existing benchmark results"
    )
    _add_recipe_argument(evaluate_parser)
    evaluate_parser.add_argument("--baseline", required=True, type=Path)
    evaluate_parser.add_argument("--candidate", required=True, type=Path)
    evaluate_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    evaluate_parser.set_defaults(handler=_evaluate)

    history_parser = subparsers.add_parser("history", help="inspect an experiment ledger")
    history_parser.add_argument("--ledger", required=True, type=Path)
    history_parser.add_argument(
        "--limit", type=_positive_integer, default=20, help="latest snapshots to show"
    )
    history_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    history_parser.set_defaults(handler=_history)

    optimize_parser = subparsers.add_parser(
        "optimize", help="run the schema-v2 iterative optimization pipeline"
    )
    optimize_commands = optimize_parser.add_subparsers(dest="optimize_command", required=True)

    optimize_plan_parser = optimize_commands.add_parser(
        "plan", help="import profiles and propose reviewed changes without writing"
    )
    _add_recipe_argument(optimize_plan_parser)
    _add_values_argument(optimize_plan_parser)
    optimize_plan_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    optimize_plan_parser.set_defaults(handler=_optimize_plan)

    optimize_run_parser = optimize_commands.add_parser(
        "run", help="run until completion or an explicit capability boundary"
    )
    _add_recipe_argument(optimize_run_parser)
    _add_values_argument(optimize_run_parser)
    optimize_run_parser.add_argument("--run-id", help="optional deterministic run identifier")
    optimize_run_parser.add_argument(
        "--apply-patches",
        action="store_true",
        help="authorize writes only inside a fresh detached worktree",
    )
    optimize_run_parser.add_argument(
        "--run-evaluations",
        action="store_true",
        help="authorize finite evaluator commands; does not authorize service control",
    )
    optimize_run_parser.add_argument(
        "--run-builds",
        action="store_true",
        help="authorize declared argv-based build commands inside detached worktrees",
    )
    optimize_run_parser.add_argument(
        "--manage-services",
        action="store_true",
        help="authorize start/readiness/stop for services owned by this run",
    )
    optimize_run_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    optimize_run_parser.set_defaults(handler=_optimize_run)

    optimize_events_parser = optimize_commands.add_parser(
        "events", help="inspect the append-only optimization event stream"
    )
    optimize_events_parser.add_argument("--events", required=True, type=Path)
    optimize_events_parser.add_argument("--run-id")
    optimize_events_parser.add_argument(
        "--limit", type=_positive_integer, default=50, help="latest events to show"
    )
    optimize_events_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    optimize_events_parser.set_defaults(handler=_optimize_events)

    target_parser = subparsers.add_parser(
        "target", help="validate one managed SGLang baseline without creating a candidate"
    )
    target_commands = target_parser.add_subparsers(dest="target_command", required=True)
    target_resolve_parser = target_commands.add_parser(
        "resolve", help="bind required inputs and write an executable lock recipe"
    )
    _add_recipe_argument(target_resolve_parser)
    _add_values_argument(target_resolve_parser)
    target_resolve_parser.add_argument("--output", required=True, type=Path)
    target_resolve_parser.add_argument("--json", action="store_true")
    target_resolve_parser.set_defaults(handler=_target_resolve)

    target_plan_parser = target_commands.add_parser(
        "plan", help="inspect the managed baseline validation boundary"
    )
    _add_recipe_argument(target_plan_parser)
    _add_values_argument(target_plan_parser)
    target_plan_parser.add_argument("--json", action="store_true")
    target_plan_parser.set_defaults(handler=_target_plan)

    target_run_parser = target_commands.add_parser(
        "run", help="execute one managed baseline validation"
    )
    _add_recipe_argument(target_run_parser)
    _add_values_argument(target_run_parser)
    target_run_parser.add_argument("--run-id")
    target_run_parser.add_argument("--prepare-workspace", action="store_true")
    target_run_parser.add_argument("--run-evaluations", action="store_true")
    target_run_parser.add_argument("--run-builds", action="store_true")
    target_run_parser.add_argument("--manage-services", action="store_true")
    target_run_parser.add_argument("--json", action="store_true")
    target_run_parser.set_defaults(handler=_target_run)
    return parser


def _add_recipe_argument(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--recipe",
        dest="recipe",
        type=Path,
        help="Euboulia recipe YAML or JSON",
    )
    source.add_argument(
        "--config",
        dest="recipe",
        type=Path,
        help=argparse.SUPPRESS,
    )


def _add_values_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--values",
        type=Path,
        help="YAML mapping that binds declared recipe inputs",
    )


def _doctor(args: argparse.Namespace) -> int:
    checks = run_doctor()
    if args.json:
        _print_json({"checks": [check.to_dict() for check in checks]})
    else:
        width = max(len(check.name) for check in checks)
        for check in checks:
            status = "ok" if check.available else "missing"
            required = " (required)" if check.required else ""
            print(f"{check.name:<{width}}  {status:<7}  {check.detail}{required}")
    return 0 if required_checks_pass(checks) else 2


def _plan(args: argparse.Namespace) -> int:
    config = load_recipe(args.recipe)
    plans = plan_recipe(config, run_id=args.run_id)
    if args.json:
        _print_json({"recipe": config.name, "plans": [plan.to_dict() for plan in plans]})
    else:
        _print_plan(config.name, plans)
    return 0


def _run(args: argparse.Namespace) -> int:
    config = load_recipe(args.recipe)
    result = run_recipe(config, execute=args.execute, run_id=args.run_id)
    if args.json:
        _print_json(result.to_dict())
    elif not result.executed:
        _print_plan(config.name, result.plans)
        print("\nDry run only. No benchmark client was executed; pass --execute after review.")
    else:
        _print_run_summary(config.execution.ledger, result)
    return 2 if result.stopped_reason is not None or result.failed else 0


def _evaluate(args: argparse.Namespace) -> int:
    config = load_recipe(args.recipe)
    evaluation = evaluate_recipe_results(config, args.baseline, args.candidate)
    if args.json:
        _print_json(evaluation.to_dict())
    else:
        verdict = evaluation.verdict
        print(f"verdict: {verdict.status.value}")
        for reason in verdict.reasons:
            print(f"reason:  {reason}")
        if verdict.metric is not None:
            print(f"metric:  {verdict.metric}")
        if verdict.baseline_value is not None:
            print(f"baseline: {verdict.baseline_value:g}")
        if verdict.candidate_value is not None:
            print(f"candidate: {verdict.candidate_value:g}")
        if verdict.relative_improvement is not None:
            print(f"relative improvement: {verdict.relative_improvement:+.2%}")
    return 0 if evaluation.verdict.accepted else 1


def _history(args: argparse.Namespace) -> int:
    ledger = ExperimentLedger(args.ledger, create_parents=False)
    experiments = ledger.read_all()
    selected = experiments[-args.limit :]
    if args.json:
        _print_json([experiment.to_dict() for experiment in selected])
    elif not selected:
        print(f"No experiments recorded in {args.ledger}.")
    else:
        for experiment in selected:
            verdict = experiment.verdict.status.value if experiment.verdict else "-"
            print(
                f"{experiment.created_at}  {experiment.experiment_id}  "
                f"{experiment.status.value}  verdict={verdict}"
            )
    return 0


def _optimize_plan(args: argparse.Namespace) -> int:
    resolution = resolve_optimization_config(
        args.recipe, args.values, allow_unresolved=True
    )
    if resolution.config is None:
        _print_unresolved_resolution(resolution, as_json=args.json)
        return 0
    config = resolution.config
    plan = OptimizationRunner().plan(config)
    if args.json:
        _print_json(plan.to_dict())
    else:
        print(f"Optimization recipe: {plan.recipe} ({plan.framework})")
        observation_count = plan.profile.metrics.get("observation_count", 0)
        print(f"Profile: {plan.profile.profile_id}; observations={observation_count}")
        print(f"Analysis: {plan.analysis.summary}")
        if not plan.proposals:
            print("Proposals: none")
        for index, proposal in enumerate(plan.proposals, start=1):
            print(f"\n[{index}] {proposal.proposal_id}: {proposal.title}")
            print(f"risk:      {proposal.risk}")
            print(f"rationale: {proposal.rationale}")
            patch_path = proposal.metadata.get("patch_path")
            if patch_path is not None:
                print(f"patch:     {patch_path}")
        print("\nRead-only plan. No event, memory, worktree, command, or service was created.")
    return 0


def _optimize_run(args: argparse.Namespace) -> int:
    config = load_optimization_config(args.recipe, args.values)
    authorizations: set[Capability] = set()
    if args.apply_patches:
        authorizations.add(Capability.WORKSPACE_WRITE)
    if args.run_evaluations:
        authorizations.add(Capability.BENCHMARK_EXECUTION)
    if args.run_builds:
        authorizations.add(Capability.BUILD_EXECUTION)
    if args.manage_services:
        authorizations.add(Capability.OWNED_SERVICE_LIFECYCLE)
    result = OptimizationRunner().run(
        config,
        frozenset(authorizations),
        run_id=args.run_id,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        print(f"Optimization run: {result.run_id}")
        print(f"Run UID: {result.run_uid}")
        print(f"State: {result.run_state.value}")
        print(f"Champion: {result.champion_id}")
        print(f"Outcomes: {len(result.outcomes)}")
        print(f"Events: {result.event_ledger}")
        print(f"Memory: {result.memory}")
        if result.stop_reason:
            print(f"Reason: {result.stop_reason}")
        if result.waiting_for_approval:
            capability_flags = {
                Capability.WORKSPACE_WRITE: "--apply-patches",
                Capability.BENCHMARK_EXECUTION: "--run-evaluations",
                Capability.BUILD_EXECUTION: "--run-builds",
                Capability.OWNED_SERVICE_LIFECYCLE: "--manage-services",
            }
            required = OptimizationRunner.required_capabilities(config)
            missing = [
                capability_flags[item]
                for item in required
                if item not in authorizations and item in capability_flags
            ]
            print("Review the proposal, then start a new run with: " + " ".join(missing))
    if result.run_state in {RunState.COMPLETED, RunState.WAITING_FOR_APPROVAL}:
        return 0
    return 1


def _optimize_events(args: argparse.Namespace) -> int:
    ledger = EventLedger(args.events, create_parents=False)
    events = ledger.by_run(args.run_id) if args.run_id else ledger.read_all()
    selected = events[-args.limit :]
    if args.json:
        _print_json([event.to_dict() for event in selected])
    elif not selected:
        print(f"No optimization events recorded in {args.events}.")
    else:
        for event in selected:
            iteration = event.iteration_id or "-"
            print(f"{event.occurred_at}  {event.run_id}  {iteration}  {event.event_type.value}")
    return 0


def _target_resolve(args: argparse.Namespace) -> int:
    resolution = resolve_optimization_config(args.recipe, args.values)
    config = resolution.config
    if config is None:  # pragma: no cover - strict resolution raises first
        raise AssertionError("target resolve did not produce a configuration")
    if config.target is None:
        raise OptimizationRuntimeError("target configuration is required for target resolve")
    require_optimization_execution_lock(config)
    output = args.output.expanduser().resolve()
    if output.parent != resolution.source.parent:
        raise OptimizationConfigError(
            "target resolve output must be in the recipe directory so relative paths "
            "retain their meaning"
        )
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"resolved recipe already exists: {output}")
    output.write_text(dump_resolved_optimization_config(config), encoding="utf-8")
    payload = {
        "recipe": resolution.name,
        "resolved": True,
        "output": str(output),
        "bound_inputs": sorted(resolution.bindings),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Resolved recipe: {resolution.name}")
        print(f"Lock file: {output}")
        print("Bound inputs: " + ", ".join(sorted(resolution.bindings)))
    return 0


def _target_plan(args: argparse.Namespace) -> int:
    resolution = resolve_optimization_config(
        args.recipe, args.values, allow_unresolved=True
    )
    if resolution.config is None:
        _print_unresolved_resolution(resolution, as_json=args.json)
        return 0
    config = resolution.config
    required = OptimizationRunner.baseline_validation_capabilities(config)
    lock_issues = optimization_execution_lock_issues(config)
    payload = {
        "recipe": config.name,
        "source_revision": config.baseline.source_revision,
        "endpoint": config.endpoint,
        "workload_points": len(config.workload_suite.points),
        "required_capabilities": [capability.value for capability in required],
        "launch_argv": list(config.target_launch_argv),
        "resolved": True,
        "bound_inputs": sorted(resolution.bindings),
        "execution_ready": not lock_issues,
        "execution_lock_issues": list(lock_issues),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Target validation: {config.name}")
        print(f"Revision: {config.baseline.source_revision}")
        print(f"Endpoint: {config.endpoint}")
        print(f"Workload points: {len(config.workload_suite.points)}")
        print(f"Execution ready: {'yes' if not lock_issues else 'no'}")
        for issue in lock_issues:
            print(f"Lock issue: {issue}")
        print("Required capabilities: " + ", ".join(item.value for item in required))
        print("\nRead-only plan. No worktree, build, service, or benchmark was created.")
    return 0


def _target_run(args: argparse.Namespace) -> int:
    config = load_optimization_config(args.recipe, args.values)
    authorizations: set[Capability] = set()
    if args.prepare_workspace:
        authorizations.add(Capability.WORKSPACE_WRITE)
    if args.run_evaluations:
        authorizations.add(Capability.BENCHMARK_EXECUTION)
    if args.run_builds:
        authorizations.add(Capability.BUILD_EXECUTION)
    if args.manage_services:
        authorizations.add(Capability.OWNED_SERVICE_LIFECYCLE)
    result = OptimizationRunner().validate_baseline(
        config,
        frozenset(authorizations),
        run_id=args.run_id,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        print(f"Target validation: {result.run_id}")
        print(f"Run UID: {result.run_uid}")
        print(f"Outcome: {result.evaluation.outcome.value}")
        print(f"Artifacts: {result.artifact_dir}")
        print(f"Worktree: {result.workspace_path}")
    return 0 if result.passed else 1


def _print_unresolved_resolution(
    resolution: OptimizationRecipeResolution,
    *,
    as_json: bool,
) -> None:
    payload = {
        "recipe": resolution.name,
        "resolved": False,
        "missing_inputs": list(resolution.missing_inputs),
    }
    if as_json:
        _print_json(payload)
        return
    print(f"Recipe template: {resolution.name}")
    print("Missing required inputs: " + ", ".join(resolution.missing_inputs))
    print("Provide --values or run 'euboulia target resolve' before execution.")


def _print_plan(name: str, plans: Sequence[Any]) -> None:
    print(f"Recipe: {name}")
    print(f"Candidates: {len(plans)}")
    for plan in plans:
        label = "baseline" if plan.ordinal == 0 else "candidate"
        print(f"\n[{plan.ordinal}] {plan.candidate.candidate_id} ({label})")
        print(f"result:  {plan.command.result_path}")
        print(f"command: {shlex.join(plan.command.argv)}")
        if plan.command.manages_service_lifecycle:
            print("safety:  plan only; this command manages a service lifecycle")


def _print_run_summary(ledger: Path, result: RecipeRunResult) -> None:
    print(f"Run: {result.run_id}")
    print(
        f"Experiments: {len(result.experiments)}; accepted={result.accepted}; "
        f"rejected={result.rejected}; failed={result.failed}"
    )
    print(f"Ledger: {ledger}")
    if result.stopped_reason:
        print(f"Stopped: {result.stopped_reason}")


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Command = args.handler
    try:
        return handler(args)
    except (
        AdapterError,
        RecipeSafetyError,
        ConfigError,
        EventLedgerCorruptionError,
        EvaluationError,
        FileExistsError,
        LedgerCorruptionError,
        MemoryConflictError,
        OSError,
        OptimizationConfigError,
        OptimizationRuntimeError,
        PatchCatalogError,
        TargetError,
        TypeError,
        ValueError,
        WorkspaceError,
    ) as exc:
        print(f"euboulia: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
