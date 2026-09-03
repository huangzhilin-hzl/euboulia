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
| `models` | Target and optional draft model identities, paths, revisions, and manifests |
| `endpoint` | Loopback endpoint used by the managed target and evaluator |
| `workload_suite` | Dataset/request-rate policy and named ISL/OSL/concurrency points |
| `benchmark` | Benchmark mode and typed parameters |
| `baseline` | Baseline identity, pinned SGLang revision, and declared target parameters |
| `target` | SGLang build, launch environment/argv, GPUs, readiness, runtime, and serving identity |
| `optimization.profiles` | Imported profile artifacts and formats |
| `optimization.planner` | Reviewed catalog and proposal/deduplication policy |
| `optimization.workspace` | Repository, detached-worktree root, and patch limits |
| `optimization.evaluation` | Correctness/performance commands, objective, repetitions, and suite gates |
| `optimization.budget` | Iteration, wall-time, failure, patience, and profile-size limits |
| `execution` | Artifact directory, event ledger, experiment ledger, and memory database |

Paths are resolved relative to the recipe. Commands are argv arrays, never shell
strings. Environment changes are explicit key/value mappings.

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

`target.serving` separately records backend and speculative-decoding identity so a
launch cannot silently drift from the scenario declaration.

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
