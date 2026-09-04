"""Command-line interface for Euboulia."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from euboulia import __version__
from euboulia.adapters import AdapterError
from euboulia.control import ControlError, ControlStore, TaskManager
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
from euboulia.progress import write_run_progress
from euboulia.recipe import (
    ConfigError,
    RecipeRunResult,
    RecipeSafetyError,
    evaluate_recipe_results,
    load_recipe,
    plan_recipe,
    run_recipe,
)
from euboulia.remote import (
    KubernetesTargetSupervisor,
    RemoteConfigError,
    RemoteExecutionError,
    load_host_runtime_config,
    with_worker_storage,
    write_worker_artifact_index,
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
    run_parser.add_argument("--name", help="optional human-readable run name")
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

    serve_parser = subparsers.add_parser(
        "serve", help="run the loopback-only experiment control plane and web console"
    )
    serve_parser.add_argument(
        "--runtime-config",
        type=Path,
        help="local runtime config (default: ~/.config/euboulia/config.yaml)",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="loopback bind address")
    serve_parser.add_argument("--port", type=int, default=8765, help="local HTTP port")
    serve_parser.add_argument(
        "--max-parallel",
        type=_positive_integer,
        default=2,
        help="maximum concurrently running target tasks",
    )
    serve_parser.add_argument("--open", action="store_true", help="open the console in a browser")
    serve_parser.set_defaults(handler=_serve)

    optimize_parser = subparsers.add_parser(
        "optimize", help="run the schema-v2 iterative optimization pipeline"
    )
    optimize_commands = optimize_parser.add_subparsers(dest="optimize_command", required=True)

    optimize_plan_parser = optimize_commands.add_parser(
        "plan", help="inspect the bounded active-profile and optimization plan"
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
    optimize_run_parser.add_argument("--name", help="optional human-readable run name")
    optimize_run_parser.add_argument(
        "--apply-patches",
        action="store_true",
        help="authorize writes only inside a fresh detached worktree",
    )
    optimize_run_parser.add_argument(
        "--run-profiles",
        action="store_true",
        help="authorize bounded SGLang profile start/stop and trace capture",
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
    optimize_events_parser.add_argument("--run-uid")
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
    target_run_parser.add_argument("--name", help="optional human-readable run name")
    target_run_parser.add_argument(
        "--executor",
        help="Kubernetes executor name from the local runtime config",
    )
    target_run_parser.add_argument(
        "--node",
        help="Kubernetes node name or InternalIP (required with --executor)",
    )
    target_run_parser.add_argument(
        "--runtime-config",
        type=Path,
        help="local runtime config (default: ~/.config/euboulia/config.yaml)",
    )
    target_run_parser.add_argument("--internal-run-uid", help=argparse.SUPPRESS)
    target_run_parser.add_argument("--internal-artifacts-root", type=Path, help=argparse.SUPPRESS)
    target_run_parser.add_argument("--internal-workspace-root", type=Path, help=argparse.SUPPRESS)
    target_run_parser.add_argument(
        "--internal-source-bundles-root",
        type=Path,
        help=argparse.SUPPRESS,
    )
    target_run_parser.add_argument("--controller-run-uid", help=argparse.SUPPRESS)
    target_run_parser.add_argument("--json", action="store_true")
    target_run_parser.set_defaults(handler=_target_run)

    target_submit_parser = target_commands.add_parser(
        "submit", help="persist a target run for the local control plane"
    )
    _add_recipe_argument(target_submit_parser)
    target_submit_parser.add_argument("--executor", required=True)
    target_submit_parser.add_argument("--node", required=True)
    target_submit_parser.add_argument("--name")
    target_submit_parser.add_argument("--runtime-config", type=Path)
    target_submit_parser.add_argument("--json", action="store_true")
    target_submit_parser.set_defaults(handler=_target_submit)

    target_list_parser = target_commands.add_parser(
        "list", help="list submitted and historical target runs"
    )
    target_list_parser.add_argument("--runtime-config", type=Path)
    target_list_parser.add_argument("--limit", type=_positive_integer, default=50)
    target_list_parser.add_argument("--json", action="store_true")
    target_list_parser.set_defaults(handler=_target_list)

    target_show_parser = target_commands.add_parser(
        "show", help="show one submitted target run and its local evidence"
    )
    target_show_parser.add_argument("run_uid")
    target_show_parser.add_argument("--runtime-config", type=Path)
    target_show_parser.add_argument("--json", action="store_true")
    target_show_parser.set_defaults(handler=_target_show)

    target_cancel_parser = target_commands.add_parser(
        "cancel", help="cancel one queued or running target run"
    )
    target_cancel_parser.add_argument("run_uid")
    target_cancel_parser.add_argument("--runtime-config", type=Path)
    target_cancel_parser.add_argument("--json", action="store_true")
    target_cancel_parser.set_defaults(handler=_target_cancel)

    target_artifacts_parser = target_commands.add_parser(
        "artifacts", help="retrieve retained artifacts from a remote target run"
    )
    target_artifact_commands = target_artifacts_parser.add_subparsers(
        dest="target_artifact_command", required=True
    )
    target_artifacts_pull_parser = target_artifact_commands.add_parser(
        "pull", help="pull a complete immutable run snapshot, including raw profiles"
    )
    target_artifacts_pull_parser.add_argument("--run-uid", required=True)
    target_artifacts_pull_parser.add_argument("--executor", required=True)
    target_artifacts_pull_parser.add_argument("--runtime-config", type=Path)
    target_artifacts_pull_parser.add_argument("--destination", required=True, type=Path)
    target_artifacts_pull_parser.add_argument("--json", action="store_true")
    target_artifacts_pull_parser.set_defaults(handler=_target_artifacts_pull)

    target_cleanup_parser = target_commands.add_parser(
        "cleanup", help="delete one retained Pod after verifying its local ownership record"
    )
    target_cleanup_parser.add_argument("--run-uid", required=True)
    target_cleanup_parser.add_argument("--executor", required=True)
    target_cleanup_parser.add_argument("--runtime-config", type=Path)
    target_cleanup_parser.add_argument("--json", action="store_true")
    target_cleanup_parser.set_defaults(handler=_target_cleanup)
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
    plans = plan_recipe(config)
    if args.json:
        _print_json({"recipe": config.name, "plans": [plan.to_dict() for plan in plans]})
    else:
        _print_plan(config.name, plans)
    return 0


def _run(args: argparse.Namespace) -> int:
    config = load_recipe(args.recipe)
    result = run_recipe(config, execute=args.execute, name=args.name)
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


def _serve(args: argparse.Namespace) -> int:
    from euboulia.server import serve

    serve(
        runtime_config=args.runtime_config,
        host=args.host,
        port=args.port,
        max_parallel=args.max_parallel,
        open_browser=args.open,
    )
    return 0


def _optimize_plan(args: argparse.Namespace) -> int:
    resolution = resolve_optimization_config(args.recipe, args.values, allow_unresolved=True)
    if resolution.config is None:
        _print_unresolved_resolution(resolution, as_json=args.json)
        return 0
    config = resolution.config
    plan = OptimizationRunner().plan(config)
    if args.json:
        _print_json(plan.to_dict())
    else:
        print(f"Optimization recipe: {plan.recipe} ({plan.framework})")
        profile_plan = plan.profile_plan
        print(
            "Profile: "
            f"{profile_plan['provider']} point={profile_plan['workload_point']} "
            f"steps={profile_plan['num_steps']} keep_raw={profile_plan['keep_raw']}"
        )
        print(
            "Required capabilities: "
            + ", ".join(capability.value for capability in plan.required_capabilities)
        )
        print("Proposals: generated after the active profile is captured and analyzed")
        print("\nRead-only plan. No event, memory, worktree, command, or service was created.")
    return 0


def _optimize_run(args: argparse.Namespace) -> int:
    config = load_optimization_config(args.recipe, args.values)
    authorizations: set[Capability] = set()
    if args.apply_patches:
        authorizations.add(Capability.WORKSPACE_WRITE)
    if args.run_evaluations:
        authorizations.add(Capability.BENCHMARK_EXECUTION)
    if args.run_profiles:
        authorizations.add(Capability.PROFILE_EXECUTION)
    if args.run_builds:
        authorizations.add(Capability.BUILD_EXECUTION)
    if args.manage_services:
        authorizations.add(Capability.OWNED_SERVICE_LIFECYCLE)
    result = OptimizationRunner().run(
        config,
        frozenset(authorizations),
        name=args.name,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        if result.name is not None:
            print(f"Name: {result.name}")
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
                Capability.PROFILE_EXECUTION: "--run-profiles",
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
    events = ledger.by_run(args.run_uid) if args.run_uid else ledger.read_all()
    selected = events[-args.limit :]
    if args.json:
        _print_json([event.to_dict() for event in selected])
    elif not selected:
        print(f"No optimization events recorded in {args.events}.")
    else:
        for event in selected:
            iteration = event.iteration_id or "-"
            print(f"{event.occurred_at}  {event.run_uid}  {iteration}  {event.event_type.value}")
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
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"resolved recipe already exists: {output}")
    _write_private_text(
        output,
        dump_resolved_optimization_config(config, destination=output),
    )
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
    resolution = resolve_optimization_config(args.recipe, args.values, allow_unresolved=True)
    if resolution.config is None:
        _print_unresolved_resolution(resolution, as_json=args.json)
        return 0
    config = resolution.config
    profile_plan = OptimizationRunner().plan(config).profile_plan
    lock_issues = optimization_execution_lock_issues(config)
    payload = {
        "recipe": config.name,
        "source_revision": config.baseline.source_revision,
        "sources": {
            name: {
                "repository": source.repository,
                "ref": source.ref,
                "revision": source.revision,
                "submodules": source.submodules,
            }
            for name, source in sorted(config.sources.items())
        },
        "endpoint": config.endpoint,
        "workload_points": len(config.workload_suite.points),
        "profile_plan": dict(profile_plan),
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
        for source_name, source in sorted(config.sources.items()):
            print(f"Source {source_name}: {source.repository} {source.ref} @ {source.revision}")
        print(f"Endpoint: {config.endpoint}")
        print(f"Workload points: {len(config.workload_suite.points)}")
        print(
            "Profile: "
            f"{profile_plan['provider']} point={profile_plan['workload_point']} "
            f"steps={profile_plan['num_steps']} keep_raw={profile_plan['keep_raw']}"
        )
        print(f"Execution ready: {'yes' if not lock_issues else 'no'}")
        for issue in lock_issues:
            print(f"Lock issue: {issue}")
        print("\nRead-only plan. No worktree, build, service, profile, or benchmark was created.")
    return 0


def _target_run(args: argparse.Namespace) -> int:
    config = load_optimization_config(args.recipe, args.values)
    internal_storage_values = (
        args.internal_run_uid,
        args.internal_artifacts_root,
        args.internal_workspace_root,
    )
    if any(value is not None for value in internal_storage_values) and not all(
        value is not None for value in internal_storage_values
    ):
        raise RemoteConfigError("internal worker storage arguments must be supplied together")
    if args.internal_source_bundles_root is not None and args.internal_run_uid is None:
        raise RemoteConfigError("internal source bundles require internal worker storage")
    internal_values = (*internal_storage_values, args.internal_source_bundles_root)
    if args.executor is not None and any(value is not None for value in internal_values):
        raise RemoteConfigError("--executor cannot be combined with internal worker arguments")
    if args.controller_run_uid is not None and args.executor is None:
        raise RemoteConfigError("--controller-run-uid requires --executor")
    if args.executor is not None and args.node is None:
        raise RemoteConfigError("--node is required with --executor")
    if args.executor is None and args.node is not None:
        raise RemoteConfigError("--node requires --executor")
    if args.executor is not None:
        require_optimization_execution_lock(config)
        runtime = load_host_runtime_config(args.runtime_config)
        remote = KubernetesTargetSupervisor(
            runtime.executor(args.executor),
            runtime.storage,
        ).run(
            config,
            name=args.name,
            node=args.node,
            run_uid=args.controller_run_uid,
        )
        if args.json:
            _print_json(remote.to_dict())
        else:
            if remote.name is not None:
                print(f"Name: {remote.name}")
            print(f"Run UID: {remote.run_uid}")
            print(f"Status: {remote.status}")
            print(
                f"Pod: {remote.namespace}/{remote.pod} node={remote.node} cleanup={remote.cleanup}"
            )
            print(f"Local records: {remote.local_run_dir}")
            print(f"Remote artifacts: {remote.remote_run_dir}")
            if remote.sync is not None:
                print(
                    "Artifact sync: "
                    f"local={remote.sync.local_dir} remote={remote.sync.remote_dir} "
                    f"added={remote.sync.added} modified={remote.sync.modified} "
                    f"deleted={remote.sync.deleted}"
                )
            if remote.error is not None:
                print(f"Error: {remote.error}")
        return 0 if remote.passed else 1

    run_uid = args.internal_run_uid
    if run_uid is not None:
        config = with_worker_storage(
            config,
            artifacts_root=args.internal_artifacts_root,
            workspace_root=args.internal_workspace_root,
        )
    try:
        result = OptimizationRunner(
            source_bundles_root=args.internal_source_bundles_root,
        ).validate_baseline(config, name=args.name, run_uid=run_uid)
    except KeyboardInterrupt:
        if run_uid is not None:
            write_run_progress(
                config.execution.artifacts_dir,
                run_uid,
                status="cancelled",
                phase="cancelled",
                detail="worker interrupted by its owning controller",
            )
        raise
    except Exception as exc:
        if run_uid is not None:
            write_run_progress(
                config.execution.artifacts_dir,
                run_uid,
                status="failed",
                phase="failed",
                detail=f"{type(exc).__name__}: {exc}"[:512],
            )
        raise
    finally:
        if run_uid is not None:
            write_worker_artifact_index(config.execution.artifacts_dir / run_uid, run_uid)
    if args.json:
        _print_json(result.to_dict())
    else:
        if result.name is not None:
            print(f"Name: {result.name}")
        print(f"Run UID: {result.run_uid}")
        print(f"Profile: {result.profile.profile_id}")
        print(f"Outcome: {result.evaluation.outcome.value}")
        print(f"Artifacts: {result.artifact_dir}")
        print(f"Worktree: {result.workspace_path}")
    return 0 if result.passed else 1


def _target_submit(args: argparse.Namespace) -> int:
    manager = TaskManager(args.runtime_config)
    run = manager.submit(
        recipe=args.recipe,
        executor=args.executor,
        node=args.node,
        name=args.name,
    )
    if args.json:
        _print_json(run.to_dict())
    else:
        print(f"Submitted: {run.run_uid}")
        print(f"Status: {run.status}")
        print(f"Recipe lock: {run.recipe_path}")
        print("The task starts when `euboulia serve` is running.")
    return 0


def _target_list(args: argparse.Namespace) -> int:
    manager = TaskManager(args.runtime_config)
    runs = manager.store.list(limit=args.limit)
    if args.json:
        _print_json({"runs": [run.to_dict() for run in runs]})
    else:
        if not runs:
            print("No submitted target runs.")
        for run in runs:
            label = run.name or run.recipe_name
            print(f"{run.run_uid}  {run.status:<12} {run.phase:<22} {label}")
    return 0


def _target_show(args: argparse.Namespace) -> int:
    from euboulia.server import ControlApplication

    manager = TaskManager(args.runtime_config)
    try:
        detail = ControlApplication(manager).run_detail(args.run_uid)
    except KeyError as exc:
        raise ControlError(f"unknown run: {args.run_uid}") from exc
    if args.json:
        _print_json(detail)
    else:
        run = detail["run"]
        if not isinstance(run, dict):  # pragma: no cover - application contract
            raise ControlError("invalid control-plane run record")
        print(f"Run UID: {run['run_uid']}")
        print(f"Name: {run['name'] or run['recipe_name']}")
        print(f"Status: {run['status']} / {run['phase']}")
        print(f"Infrastructure: {run['infrastructure_state']}")
        print(f"Artifacts: {run['artifact_state']}")
        if run.get("detail"):
            print(f"Detail: {run['detail']}")
    return 0


def _target_cancel(args: argparse.Namespace) -> int:
    manager = TaskManager(args.runtime_config)
    run = manager.cancel(args.run_uid)
    if args.json:
        _print_json(run.to_dict())
    else:
        print(f"Run UID: {run.run_uid}")
        print(f"Status: {run.status}")
        print(run.detail or "Cancellation requested.")
    return 0


def _target_artifacts_pull(args: argparse.Namespace) -> int:
    runtime = load_host_runtime_config(args.runtime_config)
    sync = KubernetesTargetSupervisor(
        runtime.executor(args.executor),
        runtime.storage,
    ).pull_snapshot(args.run_uid, args.destination)
    if args.json:
        _print_json(sync.to_dict())
    else:
        print(
            f"Artifact sync: local={sync.local_dir} remote={sync.remote_dir} "
            f"added={sync.added} modified={sync.modified} deleted={sync.deleted}"
        )
    return 0


def _target_cleanup(args: argparse.Namespace) -> int:
    runtime = load_host_runtime_config(args.runtime_config)
    pod = KubernetesTargetSupervisor(
        runtime.executor(args.executor),
        runtime.storage,
    ).cleanup(args.run_uid)
    control = ControlStore(runtime.storage.root)
    if control.get(pod.run_uid) is not None:
        control.update(
            pod.run_uid,
            infrastructure_state="pod_deleted",
            detail="owned Pod deleted after terminal run",
        )
    payload = {
        "run_uid": pod.run_uid,
        "namespace": runtime.executor(args.executor).namespace,
        "pod": pod.name,
        "pod_uid": pod.uid,
        "deleted": True,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Deleted owned Pod: {payload['namespace']}/{pod.name} uid={pod.uid}")
    return 0


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


def _write_private_text(path: Path, value: str) -> None:
    """Create a local-only experiment input without group or world access."""

    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not parent_existed:
        path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


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
    if result.name is not None:
        print(f"Name: {result.name}")
    print(f"Run UID: {result.run_uid}")
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
        ControlError,
        RecipeSafetyError,
        RemoteExecutionError,
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
