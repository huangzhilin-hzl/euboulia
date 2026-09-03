# SGLang optimization runtime

The target runtime closes two loops: an outer SGLang end-to-end optimization loop and
an inner hot-operator optimization loop. The current implementation already provides
much of the outer experiment foundation; the inner CUDA/Triton loop is the main build
target, not an optional extension.

Use schema v3 for new work. Schema v2 is compatibility input, and schema v1 belongs
to the older benchmark-only flow.

## Primary workflow

### Outer loop: SGLang end to end

1. Start the current champion under a pinned runtime contract.
2. Execute the declared workload suite.
3. Capture NSYS evidence and attribute time to SGLang stages and operator kernels.
4. Select the hotspot with the highest expected end-to-end ROI.
5. Construct and run the inner operator loop.
6. Integrate the best valid operator candidate back into an isolated SGLang tree.
7. Run an unprofiled, fixed-scenario baseline/candidate comparison.
8. Promote the winner to champion and profile that champion again.

The last step is mandatory: once a hotspot becomes faster, the dominant bottleneck
may move elsewhere.

### Inner loop: hot operator

1. Extract the operator definition, reference implementation, real serving shapes,
   dtypes/layouts, target GPU, numerical tolerance, and objective.
2. Generate or revise a CUDA/Triton implementation in an isolated workspace.
3. Compile it and feed build failures back into the next revision.
4. Compare numerical output against the reference over required shapes and edge cases.
5. Microbenchmark valid candidates over the observed shape distribution.
6. Run NCU on useful candidates, diagnose stalls, occupancy, memory traffic, and
   instruction mix, then revise again.
7. Return only the best correct candidate to the outer integration step.

Microbenchmark improvement is necessary but insufficient. SGLang end-to-end A/B is
the promotion truth because launch overhead, fusion, scheduling, communication, and
shape frequency can erase an isolated kernel win.

## Required inputs

A runnable outer optimization needs:

1. **Scenario contract:** model/runtime identity, endpoint, workload points, metrics,
   repetitions, and promotion policy.
2. **Profile evidence:** PyTorch Chrome, Nsight Systems CSV, or Nsight Compute CSV
   exported from the relevant workload.
3. **Pinned source:** an SGLang repository and baseline Git revision.
4. **Change input:** currently a reviewed catalog entry; eventually the validated
   output of the operator loop.

The operator loop additionally needs:

- an executable reference implementation and numerical contract;
- real shapes and their workload frequency, not only synthetic square shapes;
- compiler/toolchain and target-GPU identity;
- correctness cases and tolerance policy; and
- microbenchmark and NCU measurement rules.

Euboulia currently does not extract that kernel task or generate the implementation.
The reviewed catalog is a safe bootstrap for exercising the outer loop while those
inner-loop capabilities are built.

## Current executable slice

```text
import existing profile -> rule-ranked finding -> reviewed catalog change
  -> authorization -> isolated baseline/candidate trial -> suite gate -> memory
```

- Profile data is diagnostic and can select a hypothesis.
- Baseline and candidate performance is measured without profiling.
- Every failure and rejection is retained; the planner can use that memory to avoid
  repeating the same change in the same context.
- Active NSYS capture, real-shape extraction, kernel generation/repair, and automatic
  champion re-profile are not yet connected.

## Inspect before execution

The checked-in SGLang example is illustrative. Replace its model paths, revisions,
runtime identity, source repository, launch arguments, and workload before an active
run.

```console
uv run euboulia optimize plan \
  --recipe examples/optimization-sglang.yaml
```

`optimize plan` imports and analyzes the declared profile, proposes a catalog entry,
and prints the plan. It creates no worktree and starts no process.

Running without all required permissions records the deliberation and pauses at the
capability boundary:

```console
uv run euboulia optimize run --recipe your-scenario.yaml
```

## Execute a managed SGLang trial

```console
uv run euboulia optimize run \
  --recipe your-scenario.yaml \
  --apply-patches \
  --run-builds \
  --manage-services \
  --run-evaluations
```

Permissions are independent:

| Flag | Permits |
| --- | --- |
| `--apply-patches` | Create isolated worktrees and materialize the selected reviewed change |
| `--run-builds` | Run only the finite argv commands declared in `target.build` |
| `--manage-services` | Start, readiness-check, and stop only SGLang processes created by this run |
| `--run-evaluations` | Run the declared finite correctness and benchmark commands |

Omit `--run-builds` only when the recipe has no build commands. None of these flags
permits an edit to the user's branch, adoption of an existing service, commit, push,
or deployment.

## Schema v3 structure

| Section | Purpose |
| --- | --- |
| `inputs` | Typed values that a reusable template requires before execution |
| `models` | Target and optional draft model identities, paths, revisions, and manifests |
| `endpoint` | Loopback endpoint used by the managed target and evaluator |
| `workload_suite` | Dataset/request-rate policy and named ISL/OSL/concurrency points |
| `benchmark` | Benchmark mode and typed parameters |
| `baseline` | Baseline identity, pinned SGLang revision, and optional external-target parameters |
| `target` | SGLang build, native launch options, declared hardware, GPUs, readiness, and runtime identity |
| `optimization.profiles` | Imported profile artifacts and formats |
| `optimization.planner` | Reviewed catalog and proposal/deduplication policy |
| `optimization.workspace` | Repository, detached-worktree root, and patch limits |
| `optimization.evaluation` | Correctness/performance commands, objective, repetitions, and suite gates |
| `optimization.budget` | Iteration, wall-time, failure, patience, and profile-size limits |
| `execution` | Artifact directory, event ledger, experiment ledger, and memory database |

Paths are resolved relative to the recipe. Build and evaluation commands are argv
arrays, never shell strings. Environment changes are explicit key/value mappings.

### Template resolution and execution lock

A reusable recipe may declare values that must be supplied by the user:

```yaml
inputs:
  container_image:
    type: container_digest
    required: true
  sglang_revision:
    type: git_commit
    required: true

baseline:
  source_revision: ${sglang_revision}

target:
  runtime:
    expected:
      container:
        image: ${container_image}
      components:
        sglang:
          revision: ${sglang_revision}
```

Input references must occupy an entire YAML scalar. Supported types are `string`,
`integer`, `number`, `boolean`, `git_commit`, `container_digest`, and `sha256`.
`git_commit` requires a full 40- or 64-character hexadecimal commit;
`container_digest` requires an immutable `repository@sha256:...` reference. All-zero
commit and digest placeholders are rejected.

`target plan` and `optimize plan` may inspect an unresolved template and report its
missing inputs. Active runs reject missing bindings before creating events, memory,
worktrees, or artifacts. Bind values directly with `--values`, or create a lock recipe:

```console
uv run euboulia target resolve \
  --recipe scenario.yaml \
  --values h20-values.yaml \
  --output scenario.lock.yaml

uv run euboulia target run \
  --recipe scenario.lock.yaml \
  --prepare-workspace --run-builds --manage-services --run-evaluations
```

The lock file must be written beside its template so relative paths keep the same
meaning. It contains concrete values and no `inputs` section. Managed schema-v3 runs
also require an image digest, a full baseline Git commit, and a matching pinned SGLang
runtime revision. Each model must provide either a full revision commit or a non-zero
weights-manifest SHA-256. Source-backed runtime components must declare `dirty: false`;
reviewed candidate patches remain separate experiment inputs. Every active run writes
the exact bound document to
`<artifacts>/<run-id>/resolved-recipe.yaml` (or the target-validation subdirectory for
`target run`); configuration digests and memory identity are calculated from that
resolved content, not from template text or the values file.

### Structured SGLang launch

The recipe declares SGLang launch intent as typed options instead of interleaved argv
tokens:

```yaml
target:
  launch:
    python: python3
    python_options: [-u]
    module: sglang.launch_server
    bind_host: 0.0.0.0  # optional; defaults to the endpoint host
    options:
      --tp-size: 8
      --mem-fraction-static: 0.88
      --trust-remote-code: true
    env:
      SGLANG_ENABLE_JIT_DEEPGEMM: "1"
```

`true` emits a bare flag, `false` or `null` omits it, and a string or number emits a
flag/value pair. `extra_argv` is the explicit escape hatch for positional or repeated
arguments. `--model-path` and `--served-model-name` come from `models.target`; `--port`
comes from `endpoint`; and `--host` defaults to the endpoint host unless `bind_host`
explicitly declares a different listen address. Those generated options cannot be
overridden in `launch.options` or `launch.extra_argv`.

The executable must be Python, the module must be an approved SGLang server entrypoint,
and `python_options` is currently limited to `[]` or `[-u]`.

Options are normalized by long-option name and compiled to an internal argv tuple only
at the execution boundary. This keeps semantic identity stable when YAML key order
changes while preserving `subprocess` execution without a shell. The removed
`target.launch.argv` field is rejected rather than interpreted as a compatibility form.

### Identity and memory recall

Schema v3 separates four identity roles:

| Role | Representation | Memory behavior |
| --- | --- | --- |
| Display alias | Optional `name` | Never enters a semantic digest |
| Content identity | Versioned `spec_digest` plus model/workload/protocol/runtime/hardware digests | Exact recall |
| Execution identity | Generated `run_uid` (`run_id` compatibility name) | Audit and artifact lineage only |
| Compatibility | Structured hard/soft facets and a hard-facet digest | Cross-workload RSI recall |

Model, suite, baseline, and point names are optional. The `id` field is not accepted.
A point without `name` gets a deterministic display key such as
`isl16384-osl256-c1-n1`. Renaming a name, including every reference to it in
baseline or promotion policy, does not
change `spec_digest`. Changing tokens, concurrency, model revision, executable
commands, policy, runtime, or hardware does.

Memory first recalls exact `spec_digest` matches. It then supplies compatible
hard-facet matches to analysis as transfer evidence, while duplicate-change
rejection uses exact matches only. This prevents a failure on a merely similar
workload from permanently suppressing a valid experiment in the current scenario.
Hard facets include framework, model content, declared accelerator topology,
derived launch semantics, container identity, and runtime-component ABI.
SQLite memory is a disposable projection of canonical events and artifacts. A
database whose schema version does not match the runtime is dropped and rebuilt
empty; no in-place schema migration or legacy-ID fallback is performed.

See [examples/optimization-sglang.yaml](../examples/optimization-sglang.yaml) for a
complete shape.

## Bootstrap change catalog

Until the operator loop emits validated candidates, one catalog entry represents one
pre-reviewed hypothesis. The supported forms are:

```yaml
# Server arguments only
server_args:
  set:
    "--chunked-prefill-size": 4096
  remove: []
```

```yaml
# Source patch only
patch: ../patches/reviewed-kernel-change.diff
```

```yaml
# Atomic source + configuration hypothesis
patch: ../patches/reviewed-kernel-change.diff
server_args:
  set:
    "--mem-fraction-static": 0.82
  remove: []
```

Argument names must be canonical long options. Valueless switches use `null`, while
disabled options belong in `remove`. Composite changes receive one verdict; their
effect cannot be attributed to only half of the change.

The workspace validates the base revision, patch bytes, paths, file modes, symlink
traversal, changed-file/line budgets, and exact `git apply --check` result before
writing to the detached candidate tree.

## Managed trial semantics

For every iteration:

1. Create a baseline worktree at `baseline.source_revision`.
2. Build it when `target.build.commands` is present.
3. Start a fresh SGLang process and wait for the declared loopback readiness URL.
4. Run correctness once and evaluate every workload point.
5. Stop the owned baseline process.
6. Create a second worktree at the same revision and apply the selected change.
7. Build, start, evaluate, and stop the candidate independently.
8. Apply per-point and suite gates, record the verdict, and update memory.

Changing ISL, OSL, or concurrency does not restart the service; workload points share
the same fresh process for that side of the trial. A later iteration receives new
baseline and candidate processes.

Any readiness, build, command, parsing, or teardown failure invalidates the trial.
The runner still attempts exact owned-process teardown in `finally`.

## Evaluation contract

The runner supplies evaluator commands with explicit environment values for:

- active endpoint and model/served name;
- suite and point identity;
- input/output tokens, concurrency, prompt count, dataset, and request rate;
- warmup and repetition counts; and
- metrics output path.

The built-in SGLang correctness harness checks routing, response shape, and non-empty
output. This is a smoke check, not semantic accuracy. Scenario-specific commands may
run task evaluation such as GSM8K when a model, quantization path, or
numerics-affecting kernel changes; the runtime does not yet provide a generic
champion-only accuracy state.

The performance harness runs repeated samples, validates complete requests and
finite metrics, and emits normalized results for the gate. Dataset-specific harness
rules may additionally enforce exact lengths, cache state, or evidence snapshots.

## Promotion policy

A credible suite declares:

- one objective metric and whether it is minimized or maximized;
- primary workload points;
- minimum improvement on primary points;
- maximum allowed regression on other points;
- noise tolerance; and
- whether every declared point must produce valid evidence.

Profile measurements are always gate-ineligible. Baseline and candidate must keep
the model, prompts, hardware, runtime identity, request policy, and measurement rules
fixed unless one of those is the variable under test.

## Runtime provenance

`target.runtime.expected` can pin the image and components such as Python, SGLang,
Torch, CUDA, NCCL, Triton, FlashInfer, DeepGEMM, DeepEP, and `sgl-kernel`. The runner
captures observable state before launch and can fail on a mismatch.

Some values, such as a container digest, may be declared but unobservable from a
local process. `capture.require_observed` decides whether that absence is fatal;
Euboulia never labels an unobserved value as verified.

`target.launch.options` is the single author-facing source for SGLang switches,
including backend and speculative-decoding flags. Euboulia derives normalized
`launch_facets` from `--*-backend`, `--speculative-*`, parallelism, quantization,
and cache-format options for compatibility recall. Unrecognized options still
participate in the full scenario digest through the compiled launch argv, so a new
SGLang switch cannot silently collapse two distinct executions into one identity.
For a managed schema-v3 target, non-empty `baseline.target_parameters` is rejected
to prevent a second, drifting copy of launch state.

## Evidence and inspection

```text
<artifacts>/
├── events.jsonl
├── memory.sqlite3
└── <run-id>/
    └── evaluations/<trial-id>/

<workspace-root>/<run-id>/<iteration-id>/
├── baseline/{worktree,evidence}/
└── candidate/{worktree,evidence}/
```

Inspect events with:

```console
uv run euboulia optimize events \
  --events <artifacts>/events.jsonl
```

The event log and linked artifacts are the audit source. SQLite is a rebuildable
query index. Failed worktrees are retained for inspection and require a separate,
operator-controlled cleanup action.

## External-service compatibility

When `target` is absent, Euboulia can evaluate reviewed patch-only changes against an
already-running service. It may send requests to the configured endpoint but never
discovers, starts, restarts, signals, or stops that service. This path supports older
SGLang/vLLM workflows; new engine optimization work should prefer managed SGLang.

## Gap to the primary workflow

- NSYS/NCU artifacts are imported rather than captured at the correct loop stage.
- ROI selection does not yet combine end-to-end contribution, call frequency,
  optimization headroom, and implementation cost.
- A hotspot is not yet converted into a kernel task with reference semantics and
  real serving shapes.
- CUDA/Triton generation is not connected to compile-error repair, numerical
  correctness, microbenchmark, and NCU-guided iteration.
- Validated kernels are not automatically integrated into SGLang, and a promoted
  champion is not automatically re-profiled to select the next hotspot.
- Scenario-specific accuracy harnesses exist, but semantic accuracy lacks a generic
  champion-only state.
- Trial scheduling is local and sequential; remote workers, interleaved pairs,
  confidence intervals, and crash-safe resume remain future work.
- Euboulia ends at evidence and a recommendation. Merge, rollout, and production
  observation belong to separate systems.

See [Architecture](architecture.md) for component contracts and [Safety](safety.md)
for the execution boundary.
