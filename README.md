# Euboulia

Euboulia is an **evidence-driven, human-governed tuning agent for SGLang and
vLLM**. It turns a declared workload, a finite set of candidates, and explicit
acceptance gates into reproducible benchmark evidence. A person remains in
control of execution and of every operational decision that follows.

> [!IMPORTANT]
> Euboulia is early-stage software. The MVP plans and evaluates experiments; it
> does not edit source or configuration, apply patches, launch, restart, kill,
> deploy, or promote an SGLang/vLLM service. `run` is non-executing unless
> `--execute` is supplied.

## What it does

- validates a versioned YAML campaign containing one baseline and one or more
  candidates;
- renders framework-specific benchmark commands without shell interpolation;
- executes only the benchmark client, and only after explicit authorization;
- retains raw output, normalized metrics, configuration, and verdicts as
  experiment evidence;
- checks correctness before deciding whether a performance result passes; and
- records rejected and failed experiments instead of hiding inconvenient data.

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

## Campaign shape

Each campaign declares:

- a framework-neutral workload and an existing endpoint;
- a benchmark mode and base arguments;
- candidates, with the first item serving as the baseline;
- a correctness gate plus a directional performance gate; and
- scoped artifact, ledger, timeout, and environment settings.

Candidate `parameters` are benchmark-client knobs. They are not instructions to
mutate server flags. The optional `patch` field is evidence metadata in the MVP
and is never applied. See the annotated example files for the complete schema.
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

The core flow is intentionally narrow:

```text
YAML/JSON config -> validation and plan -> framework adapter -> reviewed argv
-> opt-in benchmark execution -> result parser -> evidence ledger -> gates
-> human decision
```

Adapters contain framework CLI differences. The planner, data model, ledger,
and gate evaluator remain framework-neutral. Read
[Architecture](docs/architecture.md) for component and data-flow details and
[Safety](docs/safety.md) for the execution boundary.

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

The MVP provides the ledger and gate mechanics. It does not turn an
underpowered benchmark into a statistically valid conclusion.

## Roadmap

The initial scope is YAML validation, environment diagnostics, plan/dry-run,
explicit benchmark execution, SGLang/vLLM adapters, normalized metrics,
correctness/performance gates, and experiment history.

Likely follow-on work includes repeated-trial statistics and confidence
intervals, profiler and trace ingestion, evidence-backed candidate generation,
and optional integrations for approved canary or rollout workflows. Any future
patch proposal or service-control integration must add a separate approval,
threat model, rollback story, and audit trail; silent service mutation is not a
roadmap goal.

## Development

```console
uv sync --extra dev
uv run ruff check .
uv run mypy src/euboulia
uv run pytest
uv run euboulia plan --config examples/vllm.yaml
```

Contributions should include the evidence needed to evaluate performance claims.
See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Euboulia is available under the [MIT License](LICENSE).
