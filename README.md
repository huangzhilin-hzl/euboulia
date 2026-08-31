# Euboulia

Euboulia is an **evidence-driven, human-governed tuning agent for SGLang and
vLLM**. It turns a declared workload, a finite set of candidates, and explicit
acceptance gates into reproducible benchmark evidence. A person remains in
control of execution and of every operational decision that follows.

> [!IMPORTANT]
> Euboulia is early-stage software. The schema-v1 `run` command remains
> non-executing unless `--execute` is supplied and never applies its `patch`
> metadata. The schema-v2 `optimize` runtime can apply an exact, reviewed catalog
> patch only inside a fresh detached Git worktree, and only when both
> `--apply-patches` and `--run-evaluations` are supplied. Neither path launches,
> restarts, kills, deploys, or promotes an SGLang/vLLM service.

## What it does

The schema-v1 campaign path:

- validates a versioned YAML campaign containing one baseline and one or more
  candidates;
- renders framework-specific benchmark commands without shell interpolation;
- executes only the benchmark client, and only after explicit authorization;
- retains raw output, normalized metrics, configuration, and verdicts as
  experiment evidence;
- checks correctness before deciding whether a performance result passes; and
- records rejected and failed experiments instead of hiding inconvenient data.

The iterative runtime adds a second, separately authorized loop:

- imports PyTorch Chrome traces, Nsight Systems CSV, and Nsight Compute CSV as
  diagnostic-only evidence;
- classifies compute, memory, occupancy, launch, synchronization, transfer,
  communication, CPU-submission, and underutilization signals with explainable
  rules;
- maps findings to a reviewed patch catalog while deduplicating prior outcomes;
- checks and applies exact patches in per-trial detached worktrees;
- evaluates preflight, correctness, and unprofiled performance in fail-fast
  order; and
- records typed events plus positive and negative outcomes in a rebuildable
  SQLite memory index.

Euboulia does not claim that a faster single run is a production-safe tuning.
The operator owns the test environment, the running inference service, the
statistical standard, and the final decision.

## Euboulia and Entelechy

The two names describe different responsibilities:

- **Euboulia deliberates.** It plans controlled experiments, collects evidence,
  applies declared gates, and produces an auditable recommendation.
- **Entelechy, when present, may realize an approved decision.** A downstream
  system can consume Euboulia's portable evidence and handle rollout or other
  operational work.

Euboulia has no runtime, import, service, or control-plane dependency on
Entelechy. It is useful as a standalone project. In short: **Euboulia
recommends; Entelechy may realize.**

## Installation

Euboulia requires Python 3.11 or newer. For development with
[uv](https://docs.astral.sh/uv/):

```console
git clone <your-euboulia-fork>
cd euboulia
uv sync --extra dev
uv run euboulia doctor
```

To install the command from a local checkout instead:

```console
uv tool install .
euboulia doctor
```

SGLang or vLLM and their benchmark dependencies must be installed in the
environment used to execute a campaign. Euboulia does not install or start an
inference server on the operator's behalf.

## Quick start: inspect first

The repository includes readable, supported YAML configurations for
[SGLang](examples/sglang.yaml) and [vLLM](examples/vllm.yaml). YAML is a first-
class input format, not merely a template; Euboulia validates it and normalizes
it to its typed/JSON data model internally.

```console
uv run euboulia doctor
uv run euboulia plan --config examples/sglang.yaml
uv run euboulia run --config examples/sglang.yaml --dry-run
```

`plan` and `--dry-run` show the candidate commands, result locations, and gates.
They do not invoke a benchmark or touch the configured service.

After reviewing the endpoint, workload, generated commands, resource budget,
and artifact destination, execution must be authorized explicitly:

```console
uv run euboulia run --config examples/sglang.yaml --execute
```

The service at the configured endpoint is externally managed and must already
be ready. Euboulia invokes benchmark clients against it; it never assumes
permission to manage that service.

Existing results can be compared and the append-only history inspected with:

```console
uv run euboulia evaluate \
  --config examples/vllm.yaml \
  --baseline experiments/baseline.json \
  --candidate experiments/candidate.json
uv run euboulia history --ledger experiments/ledger.jsonl
```

Use paths produced by your campaign rather than the illustrative result paths
above.

## Iterative optimization: profile to memory

The schema-v2 example is safe to inspect as checked in:

```console
uv run euboulia optimize plan --config examples/optimization-vllm.yaml
```

`optimize plan` reads the declared trace and patch catalog, runs the rule
analyzer, and prints proposals. It creates no artifact directory, event log,
memory database, worktree, or subprocess. The included patch and commands are
illustrative; replace them and pin `baseline.source_revision` before active use.

An active run first executes the same deliberation and pauses at the capability
boundary by default:

```console
uv run euboulia optimize run --config your-optimization.yaml
```

After reviewing the proposal, repository, exact patch digest, path/line budgets,
correctness commands, finite benchmark harness, baseline, metric, and resource
budget, both active capabilities must be granted explicitly:

```console
uv run euboulia optimize run \
  --config your-optimization.yaml \
  --apply-patches \
  --run-evaluations
```

These flags authorize only detached-worktree mutation and finite evaluator
commands. They do not authorize a persistent service lifecycle. Inspect the
append-only trajectory with:

```console
uv run euboulia optimize events \
  --events experiments/optimization-vllm/events.jsonl
```

## Campaign shape

Each campaign declares:

- a framework-neutral workload and an existing endpoint;
- a benchmark mode and base arguments;
- candidates, with the first item serving as the baseline;
- a correctness gate plus a directional performance gate; and
- scoped artifact, ledger, timeout, and environment settings.

In schema v1, candidate `parameters` are benchmark-client knobs. They are not
instructions to mutate server flags, and the optional `patch` field remains inert
evidence metadata. Schema v2 uses separate `profiles`, `planner`, `workspace`,
`evaluation`, and `budget` sections; it never reinterprets a v1 patch as active
input. See the annotated example files for both schemas.
Changing request rate, concurrency, token lengths, or another load dimension is
a **capacity-search experiment**, not evidence of a code speedup. A code or
server-tuning claim must hold model, workload, hardware, and measurement policy
constant between baseline and candidate.

Paths in a campaign are resolved relative to that campaign file. The supplied
examples therefore place evidence under the repository-level `experiments/`
directory. A run is conceptually organized as follows (the exact filenames may
evolve while the schema is young):

```text
experiments/
├── ledger.jsonl
└── <run-id>/
    ├── plan-and-config-snapshot
    ├── baseline/
    │   ├── raw-benchmark-output
    │   └── normalized-metrics
    ├── candidates/<candidate-id>/
    │   ├── raw-benchmark-output
    │   └── normalized-metrics
    └── verdict-and-evidence
```

Treat a completed run directory as immutable evidence. Do not use it as a
workspace for source changes.

## Architecture

The original campaign flow remains intentionally narrow:

```text
YAML/JSON config -> validation and plan -> framework adapter -> reviewed argv
-> opt-in benchmark execution -> result parser -> evidence ledger -> gates
-> human decision
```

Adapters contain framework CLI differences. The planner, data model, ledger,
and gate evaluator remain framework-neutral. The iterative flow is:

```text
Profiler -> Analyzer -> Planner -> approval -> Patch Workspace -> Evaluator
    ^                                                               |
    +---------------- Event Ledger + Memory -------------------------+
```

The event stream is independent of the existing experiment ledger. Profiled
values can diagnose a bottleneck but are structurally ineligible for a promotion
gate; the evaluator requires a separate unprofiled result. Read
[Architecture](docs/architecture.md) for component and data-flow details and
[Safety](docs/safety.md) for the execution boundary. The exact mechanisms
borrowed from OpenHands, SWE-agent, Aider, Optuna, MLflow, and LangGraph are
documented in [Design inspirations](docs/design-inspirations.md).

The end-to-end schema-v2 lifecycle and configuration are covered in
[Iterative optimization runtime](docs/optimization.md).

## Evidence discipline

A credible campaign should:

1. compare baseline and candidates on the same model, prompts, token lengths,
   arrival policy, hardware, and server state unless that variable is the
   experiment itself;
2. record framework, driver, accelerator, model, and Euboulia versions;
3. run warmups and enough repeated trials to characterize noise;
4. require correctness before interpreting performance;
5. state whether a metric is maximized or minimized and predeclare regression
   tolerance; and
6. retain failures and raw results alongside accepted results.

The runtime provides evidence, lifecycle, and gate mechanics. It does not turn
an underpowered benchmark into a statistically valid conclusion.

## Roadmap

Implemented now: static SGLang/vLLM campaigns; imported profiler normalization;
rule-backed bottleneck analysis and patch planning; an append-only optimization
event stream; explicit state and resource budgets; exact patch validation in a
detached worktree; fail-fast correctness/performance evaluation; and structured
long-term memory.

Likely follow-on work includes an owned-service `TargetController`, repeated
interleaved GPU trials, confidence intervals and statistical pruning,
container/remote workspaces, LLM planner/editor adapters, Optuna search-policy
and MLflow tracking adapters, crash-safe resume, and approved canary workflows.
Service control will require its own capability, ownership proof, rollback, and
audit trail; silent service mutation is not a roadmap goal.

## Development

```console
uv sync --extra dev
uv run ruff check .
uv run mypy src/euboulia
uv run pytest
uv run euboulia plan --config examples/vllm.yaml
uv run euboulia optimize plan --config examples/optimization-vllm.yaml
```

Contributions should include the evidence needed to evaluate performance claims.
See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Euboulia is available under the [MIT License](LICENSE).
