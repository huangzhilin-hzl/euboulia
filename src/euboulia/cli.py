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
from euboulia.campaign import (
    CampaignRunResult,
    CampaignSafetyError,
    evaluate_existing_results,
    plan_campaign,
    run_campaign,
)
from euboulia.config import ConfigError, load_config
from euboulia.doctor import required_checks_pass, run_doctor
from euboulia.ledger import ExperimentLedger, LedgerCorruptionError
from euboulia.optimization.config import OptimizationConfigError, load_optimization_config
from euboulia.optimization.contracts import Capability, RunState
from euboulia.optimization.evaluator import EvaluationError
from euboulia.optimization.events import EventLedger, EventLedgerCorruptionError
from euboulia.optimization.memory import MemoryConflictError, MemorySchemaError
from euboulia.optimization.planner import PatchCatalogError
from euboulia.optimization.runner import OptimizationRunner, OptimizationRuntimeError
from euboulia.optimization.workspace import WorkspaceError

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

    plan_parser = subparsers.add_parser("plan", help="render a campaign without executing it")
    _add_config_argument(plan_parser)
    plan_parser.add_argument("--run-id", default="preview", help="artifact path label")
    plan_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    plan_parser.set_defaults(handler=_plan)

    run_parser = subparsers.add_parser("run", help="plan or explicitly execute a campaign")
    _add_config_argument(run_parser)
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
        "evaluate", help="apply campaign gates to existing benchmark results"
    )
    _add_config_argument(evaluate_parser)
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
        "plan", help="import profiles and propose reviewed patches without writing"
    )
    _add_config_argument(optimize_plan_parser)
    optimize_plan_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    optimize_plan_parser.set_defaults(handler=_optimize_plan)

    optimize_run_parser = optimize_commands.add_parser(
        "run", help="run until completion or an explicit capability boundary"
    )
    _add_config_argument(optimize_run_parser)
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
    return parser


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, type=Path, help="campaign YAML or JSON")


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
    config = load_config(args.config)
    plans = plan_campaign(config, run_id=args.run_id)
    if args.json:
        _print_json({"campaign": config.name, "plans": [plan.to_dict() for plan in plans]})
    else:
        _print_plan(config.name, plans)
    return 0


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = run_campaign(config, execute=args.execute, run_id=args.run_id)
    if args.json:
        _print_json(result.to_dict())
    elif not result.executed:
        _print_plan(config.name, result.plans)
        print("\nDry run only. No benchmark client was executed; pass --execute after review.")
    else:
        _print_run_summary(config.execution.ledger, result)
    return 2 if result.stopped_reason is not None or result.failed else 0


def _evaluate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    evaluation = evaluate_existing_results(config, args.baseline, args.candidate)
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
    config = load_optimization_config(args.config)
    plan = OptimizationRunner().plan(config)
    if args.json:
        _print_json(plan.to_dict())
    else:
        print(f"Optimization campaign: {plan.campaign} ({plan.framework})")
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
    config = load_optimization_config(args.config)
    authorizations: set[Capability] = set()
    if args.apply_patches:
        authorizations.add(Capability.WORKSPACE_WRITE)
    if args.run_evaluations:
        authorizations.add(Capability.BENCHMARK_EXECUTION)
    result = OptimizationRunner().run(
        config,
        frozenset(authorizations),
        run_id=args.run_id,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        print(f"Optimization run: {result.run_id}")
        print(f"State: {result.run_state.value}")
        print(f"Champion: {result.champion_id}")
        print(f"Outcomes: {len(result.outcomes)}")
        print(f"Events: {result.event_ledger}")
        print(f"Memory: {result.memory}")
        if result.stop_reason:
            print(f"Reason: {result.stop_reason}")
        if result.waiting_for_approval:
            missing: list[str] = []
            if not args.apply_patches:
                missing.append("--apply-patches")
            if not args.run_evaluations:
                missing.append("--run-evaluations")
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


def _print_plan(name: str, plans: Sequence[Any]) -> None:
    print(f"Campaign: {name}")
    print(f"Candidates: {len(plans)}")
    for plan in plans:
        label = "baseline" if plan.ordinal == 0 else "candidate"
        print(f"\n[{plan.ordinal}] {plan.candidate.candidate_id} ({label})")
        print(f"result:  {plan.command.result_path}")
        print(f"command: {shlex.join(plan.command.argv)}")
        if plan.command.manages_service_lifecycle:
            print("safety:  plan only; this command manages a service lifecycle")


def _print_run_summary(ledger: Path, result: CampaignRunResult) -> None:
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
        CampaignSafetyError,
        ConfigError,
        EventLedgerCorruptionError,
        EvaluationError,
        FileExistsError,
        LedgerCorruptionError,
        MemoryConflictError,
        MemorySchemaError,
        OSError,
        OptimizationConfigError,
        OptimizationRuntimeError,
        PatchCatalogError,
        TypeError,
        ValueError,
        WorkspaceError,
    ) as exc:
        print(f"euboulia: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
