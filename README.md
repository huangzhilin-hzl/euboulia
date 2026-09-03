# Euboulia

Euboulia is an **evidence-driven, human-governed tuning agent for SGLang and
vLLM**. It turns a declared workload, a finite set of candidates, and explicit
acceptance gates into reproducible benchmark evidence. A person remains in
control of execution and of every operational decision that follows.

> [!IMPORTANT]
> Euboulia is early-stage software. The schema-v1 `run` command remains
> non-executing unless `--execute` is supplied and never applies its `patch`
> metadata. The schema-v2/v3 `optimize` runtime accepts only reviewed change and
> target declarations. With no `target`, it retains the external-service,
> patch-only path. With an explicit SGLang `target`, it may manage only fresh
> services it starts itself, under separate workspace, benchmark, lifecycle, and
> optional build authorizations. Managed vLLM lifecycle is not implemented.

## What it does

The schema-v1 recipe path:

- validates a versioned YAML recipe containing one baseline and one or more
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
- maps findings to a reviewed change catalog while deduplicating prior outcomes;
- materializes reviewed args-only, patch-only, or composite SGLang candidates in
  per-trial detached worktrees;
- builds, starts, checks, evaluates, and finally stops fresh baseline and candidate
  SGLang processes when a managed target is explicitly declared and authorized;
- evaluates correctness once per fresh service, then benchmarks every declared
  ISL/OSL/concurrency point and applies point-aware promotion gates;
- captures declared-versus-observed image, Python, CUDA, framework, and extension
  provenance before a managed trial; and
- records typed events plus positive and negative outcomes in a rebuildable
  SQLite memory index.

Euboulia does not claim that a faster single run is a production-safe tuning.
The operator owns the test environment, capability grants, statistical standard,
and final decision. A managed target is only an ephemeral child of one run; it is
not authority over an existing or production service.

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
environment used to execute a recipe. Euboulia does not install a framework.
Schema-v1 and target-absent schema-v2/v3 runs use an existing external service;
the managed optimization path can launch only a declared local SGLang target.

## Quick start: inspect first

The repository includes readable, supported YAML configurations for
[SGLang](examples/sglang.yaml) and [vLLM](examples/vllm.yaml). YAML is a first-
class input format, not merely a template; Euboulia validates it and normalizes
it to its typed/JSON data model internally.

```console
uv run euboulia doctor
uv run euboulia plan --recipe examples/sglang.yaml
uv run euboulia run --recipe examples/sglang.yaml --dry-run
```

`plan` and `--dry-run` show the candidate commands, result locations, and gates.
They do not invoke a benchmark or touch the configured service.
`--config` remains a compatibility alias for existing scripts, but new commands
and documentation use `--recipe`.

After reviewing the endpoint, workload, generated commands, resource budget,
and artifact destination, execution must be authorized explicitly:

```console
uv run euboulia run --recipe examples/sglang.yaml --execute
```

The service at the configured endpoint is externally managed and must already
be ready. Euboulia invokes benchmark clients against it; it never assumes
permission to manage that service.

Existing results can be compared and the append-only history inspected with:

```console
uv run euboulia evaluate \
  --recipe examples/vllm.yaml \
  --baseline experiments/baseline.json \
  --candidate experiments/candidate.json
uv run euboulia history --ledger experiments/ledger.jsonl
```

Use paths produced by your recipe rather than the illustrative result paths
above.

## Iterative optimization: profile to memory

The schema-v2/v3 examples are safe to inspect as checked in. The SGLang example
is a schema-v3 GLM-5.3-Flash H20 TP8 template:

```console
uv run euboulia optimize plan --recipe examples/optimization-sglang.yaml
uv run euboulia optimize plan --recipe examples/optimization-vllm.yaml
```

`optimize plan` reads the declared trace and reviewed change catalog, runs the rule
analyzer, and prints proposals. It creates no artifact directory, event log,
memory database, worktree, or subprocess. The included patch, paths, and target
commands are illustrative; replace them and pin `baseline.source_revision` before
active use. The SGLang evaluator commands call reusable harnesses shipped with
Euboulia.

Cookbook and recipe conversion happens before this boundary. The operator turns
upstream guidance into a reviewed target declaration and change catalog;
Euboulia does not translate prose into launch commands or source edits.

An active run first executes the same deliberation and pauses at the capability
boundary by default:

```console
uv run euboulia optimize run --recipe your-optimization.yaml
```

With no `target`, schema v2/v3 keeps the external-service patch-only behavior. After
review, it requires the two existing capabilities:

```console
uv run euboulia optimize run \
  --recipe your-optimization.yaml \
  --apply-patches \
  --run-evaluations
```

These flags authorize only detached-worktree mutation and finite evaluator
commands. They do not authorize control of the external service.

The SGLang managed example also declares a build, so its active form requires two
additional, independent capabilities:

```console
uv run euboulia optimize run \
  --recipe examples/optimization-sglang.yaml \
  --apply-patches \
  --run-evaluations \
  --manage-services \
  --run-builds
```

The managed runner creates separate baseline and candidate worktrees at the same
pinned revision. It builds, starts, waits for readiness, evaluates, and stops each
side independently; teardown runs in `finally`. It may stop only the exact child
process group it created. It never searches for or kills an external process.
Managed lifecycle is SGLang-first; do not add `target.provider: vllm`.

For SGLang, the runner passes the target model/served name, endpoint, and each
declared workload point (token lengths, concurrency, prompt count, request rate,
dataset, warmups, repetitions, and point-specific result path) to two shared
modules. The correctness module performs one deterministic native or OpenAI-chat
smoke request per fresh baseline/candidate service. The benchmark module invokes
`sglang.benchmark.serving`, requires every request to complete, discards full
warmup runs, and records median metrics across measured repetitions. This keeps
recipe conversion focused on target launch arguments and reviewed changes; users
do not need to copy benchmark scripts into each model recipe.

The smoke check is not semantic accuracy. As in InferenceX, expensive model-task
evaluation belongs to a separate policy: run it when onboarding a model/runtime
combination and for candidates whose changes may affect numerics. A built-in
champion-only accuracy tier is follow-on work, so the MVP does not pretend the
smoke response proves model quality.

Inspect the append-only trajectory with:

```console
uv run euboulia optimize events \
  --events experiments/optimization-vllm/events.jsonl
```

## Recipe shape

The vocabulary is deliberately small: a **recipe** is the reusable experiment
protocol, a **run** is one execution of that protocol, a **trial** is one
baseline or candidate measurement inside a run, and a **change set** is the
reviewed source/argument change under test. Automatic optimization produces
change sets; it does not introduce a second configuration concept.

Each recipe declares:

- a framework-neutral workload and an existing endpoint;
- a benchmark mode and base arguments;
- candidates, with the first item serving as the baseline;
- a correctness gate plus a directional performance gate; and
- scoped artifact, ledger, timeout, and environment settings.

In schema v1, candidate `parameters` are benchmark-client knobs. They are not
instructions to mutate server flags, and the optional `patch` field remains inert
evidence metadata. Schema v2 uses separate `profiles`, reviewed change catalog,
`workspace`, `evaluation`, and `budget` sections plus an optional top-level
`target`; it never reinterprets a v1 patch as active input. Schema v3 replaces the
ambiguous single `workload` with `models`, `endpoint`, and `workload_suite.points`,
adds typed `target.runtime` and `target.serving`, and uses per-point baseline and
promotion mappings. Schema v2 remains load-compatible as a normalized one-point
suite. Catalog entries may be args-only, patch-only, or composite.
Changing request rate, concurrency, token lengths, or another load dimension is
a **capacity-search experiment**, not evidence of a code speedup. A code or
server-tuning claim must hold model, workload, hardware, and measurement policy
constant between baseline and candidate.

Paths in a recipe are resolved relative to that recipe file. The supplied
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

The original recipe flow remains intentionally narrow:

```text
YAML/JSON config -> validation and plan -> framework adapter -> reviewed argv
-> opt-in benchmark execution -> result parser -> evidence ledger -> gates
-> human decision
```

Adapters contain framework CLI differences. The planner, data model, ledger,
and gate evaluator remain framework-neutral. The iterative flow is:

```text
Profiler -> Analyzer -> Planner -> approval -> Isolated Target Trial -> Evaluator
    ^                                                                      |
    +--------------------- Event Ledger + Memory --------------------------+
```

The event stream is independent of the existing experiment ledger. Profiled
values can diagnose a bottleneck but are structurally ineligible for a promotion
gate; the evaluator requires a separate unprofiled result. Read
[Architecture](docs/architecture.md) for component and data-flow details and
[Safety](docs/safety.md) for the execution boundary. The exact mechanisms
borrowed from OpenHands, SWE-agent, Aider, Optuna, MLflow, LangGraph, and
InferenceX are
documented in [Design inspirations](docs/design-inspirations.md).

The end-to-end schema-v2 lifecycle and configuration are covered in
[Iterative optimization runtime](docs/optimization.md).

## Evidence discipline

A credible recipe should:

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

MVP scope: static SGLang/vLLM recipes; imported profiler normalization;
rule-backed bottleneck analysis and reviewed change planning; append-only events;
explicit state and resource budgets; exact patch validation; fail-fast gates;
structured memory; the external-service patch-only compatibility path; and an
owned, local SGLang lifecycle with separate baseline/candidate worktrees and
processes. The SGLang path also includes runtime provenance validation and reusable
smoke/multi-point fixed-length serving harnesses with complete-request validation,
median aggregation, and point-aware promotion.

Likely follow-on work includes managed vLLM, interleaved GPU trial scheduling,
confidence intervals and statistical pruning, container/remote workspaces, LLM
planner/editor adapters, Optuna search-policy and MLflow tracking adapters,
crash-safe resume, and approved canary workflows. Silent service adoption or
mutation is not a roadmap goal.

## Development

```console
uv sync --extra dev
uv run ruff check .
uv run mypy src/euboulia
uv run pytest
uv run euboulia plan --recipe examples/vllm.yaml
uv run euboulia optimize plan --recipe examples/optimization-sglang.yaml
uv run euboulia optimize plan --recipe examples/optimization-vllm.yaml
```

Contributions should include the evidence needed to evaluate performance claims.
See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Euboulia is available under the [MIT License](LICENSE).
