# Euboulia

Euboulia is a **SGLang-first optimization agent for inference engines and hot
kernels**. It finds the highest-ROI hotspot under a real serving workload, optimizes
that operator in an inner CUDA/Triton loop, integrates it back into SGLang, and uses
end-to-end evidence to decide the next champion.

The project is built for inference and kernel engineers working on scheduling,
attention, MoE, speculative decoding, CUDA, Triton, and backend libraries such as
DeepGEMM. It is not a training agent, a deployment controller, or a general-purpose
coding agent.

## Primary workflow

The product is a nested optimization loop. End-to-end A/B is the outer truth signal;
it is not the whole product.

```text
Outer — SGLang end-to-end
champion SGLang -> workload suite -> NSYS profile -> system/operator attribution
  -> select highest-ROI hotspot -> enter operator loop -> integrate into SGLang
  -> end-to-end A/B -> update champion -> profile the new champion again

Inner — hot operator
operator contract + real shapes -> generate CUDA/Triton candidate -> compile
  -> numerical correctness -> microbenchmark -> NCU analysis -> revise candidate
  -> return the best valid kernel to the outer loop
```

Only a kernel that survives compilation, correctness, and microbenchmark gates may
return to SGLang. Only a change that wins the fixed-scenario end-to-end comparison may
become champion. A person controls execution and any later merge or deployment.

| In scope | Not in scope |
| --- | --- |
| SGLang engine and hot-kernel optimization | Model training or weight optimization |
| Reproducible workload and runtime contracts | Production rollout or infrastructure control |
| NSYS system attribution and NCU kernel analysis | Treating profiled timings as promotion evidence |
| Isolated generation and validation of Python, Triton, and CUDA candidates | Unbounded mutation of the host or user's branch |
| Isolated baseline/candidate evaluation and experiment memory | Claiming causality from one noisy benchmark |

SGLang is the primary managed runtime. vLLM remains available only through the
older external-service benchmark path; managed vLLM optimization is not a current
project goal.

The first north-star scenario is DS-V4-Flash on one 8×H20 node with TP8/CP8/EP8,
MegaMoE, and DSPARK. It must drive the whole loop: strict launch and workload
contracts, cache-clean end-to-end evidence, NSYS ROI attribution, real-shape operator
optimization, integration, A/B, champion update, and re-profile.

## What works today

- Validate versioned experiment and optimization recipes.
- Import PyTorch Chrome traces, NSYS CSV, and NCU CSV.
- Rank bottleneck findings with explainable rules and evidence references.
- Select a hypothesis from a reviewed change catalog.
- Materialize baseline and candidate from the same pinned Git revision in separate
  detached worktrees.
- Apply structured SGLang arguments, exact source patches, or an atomic combination
  of both.
- Build, start, readiness-check, benchmark, and stop only SGLang processes created
  by the current run.
- Evaluate multiple ISL/OSL/concurrency points with correctness and performance
  gates.
- Preserve events, artifacts, accepted results, regressions, and failures.

This is the implemented outer-loop foundation, not the complete target workflow.
The current planner is rule-backed and catalog-driven; operator-task extraction,
CUDA/Triton generation, compile/correctness/microbenchmark/NCU repair, and automatic
champion re-profiling still need to be built.

RSI appears at both loop boundaries: the inner loop uses compiler, correctness,
microbenchmark, and NCU feedback to improve a kernel; the outer loop re-profiles each
new champion so the next optimization target follows the changed system bottleneck.
Experiment memory supports both loops but is not itself the main RSI mechanism.

## Quick start

Euboulia requires Python 3.11 or newer. For development:

```console
git clone <your-euboulia-fork>
cd euboulia
uv sync --extra dev
uv run euboulia doctor
```

Inspect the SGLang optimization example without creating a worktree or starting a
process:

```console
uv run euboulia optimize plan \
  --recipe examples/optimization-sglang.yaml
```

An active managed run has five independent permissions:

```console
uv run euboulia optimize run \
  --recipe your-sglang-optimization.yaml \
  --apply-patches \
  --run-profiles \
  --run-builds \
  --manage-services \
  --run-evaluations
```

These flags authorize only the declared run. They do not permit changes to the
user's branch, adoption of an existing service, commits, pushes, or deployment.

Inspect the resulting trajectory with:

```console
uv run euboulia optimize events \
  --events experiments/your-run/events.jsonl
```

The older external-service recipe flow remains available for benchmark-only use:

```console
uv run euboulia plan --recipe examples/sglang.yaml
uv run euboulia run --recipe examples/sglang.yaml --dry-run
uv run euboulia run --recipe examples/sglang.yaml --execute
```

`plan` and `--dry-run` are inspection-only. The external service must already be
running, and Euboulia never starts or stops it.

## Core concepts

- **Scenario:** pinned model, hardware, SGLang/runtime identity, dataset, workload
  matrix, cache policy, metrics, and acceptance gates.
- **Kernel task:** operator semantics, reference implementation, target hardware,
  real shape distribution, numerical tolerance, and performance objective.
- **Hypothesis:** one explainable engine or kernel change intended to address one
  observed bottleneck.
- **Trial pair:** fresh baseline and candidate processes evaluated under the same
  scenario.
- **Evidence:** raw profiler/benchmark output, runtime provenance, commands, logs,
  normalized metrics, and verdicts.
- **Memory:** a rebuildable index of positive and negative outcomes used to avoid
  repeating known experiments.

Changing the workload while changing the implementation is a capacity experiment,
not evidence of a code speedup. Promotion requires an unprofiled comparison under a
fixed scenario.

## Documentation

- [Architecture](docs/architecture.md): optimization loop, component boundaries,
  trial lifecycle, and evidence model.
- [Optimization runtime](docs/optimization.md): recipe structure, change catalog,
  CLI workflow, and evaluation semantics.
- [Safety model](docs/safety.md): authorization, process ownership, filesystem,
  and evidence rules.
- [Design principles](docs/design-inspirations.md): selected ideas borrowed from
  agent and experiment systems.

## Current limitations

- Active profiling currently uses SGLang's bounded Torch profiler; automatic
  NSYS/NCU escalation is not yet connected.
- Hotspot selection currently stops at a reviewed catalog entry; kernel-task
  extraction and CUDA/Triton candidate generation are not yet implemented.
- Qualification can call an external semantic-evaluation tool through a generic
  command/result contract; task implementations are intentionally not vendored.
- Trials run on one local host; remote/container workers and crash-safe resume are
  not implemented.
- Adaptive windows stop early when recent objective values meet the lane tolerance,
  but confidence still depends on representative points and a sound stability policy.

The near-term direction is deliberately narrow: complete the DS-V4 scenario through
both loops, rather than adding more frameworks or generic agent features.

## Development

```console
uv sync --extra dev
uv run ruff check .
uv run mypy src/euboulia
uv run pytest
uv run euboulia optimize plan --recipe examples/optimization-sglang.yaml
```

Contributions should include the evidence needed to assess any performance claim.
See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Euboulia is available under the [MIT License](LICENSE).
