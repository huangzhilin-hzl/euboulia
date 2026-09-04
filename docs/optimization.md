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
   fast/qualification lanes, stability budgets, and promotion policy.
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
authorization -> owned champion service -> bounded SGLang Torch profile
  -> streaming summary -> rule-ranked finding -> reviewed catalog change
  -> isolated baseline/candidate trial -> unprofiled suite gate -> memory
```

- Profile data is diagnostic and can select a hypothesis.
- Baseline and candidate performance is measured without profiling.
- Every failure and rejection is retained; the planner can use that memory to avoid
  repeating the same change in the same context.
- Each new optimization run profiles the current reviewed champion. Active NSYS/NCU
  escalation, real-shape extraction, and kernel generation/repair are not yet connected.

## Inspect before execution

The checked-in SGLang example is illustrative. Replace its model paths, revisions,
runtime identity, source repository, launch arguments, and workload before an active
run.

```console
uv run euboulia optimize plan \
  --recipe examples/optimization-sglang.yaml
```

`optimize plan` prints the selected profile workload, bounded step window, disk budget,
retention policy, and required capabilities. It does not profile, analyze, propose,
create a worktree, or start a process.

Running without all required permissions records the run boundary and pauses before
the active profile or any other declared side effect:

```console
uv run euboulia optimize run --recipe your-scenario.yaml
```

## Execute a managed SGLang trial

```console
uv run euboulia optimize run \
  --recipe your-scenario.yaml \
  --apply-patches \
  --run-profiles \
  --run-builds \
  --manage-services \
  --run-evaluations
```

Permissions are independent:

| Flag | Permits |
| --- | --- |
| `--apply-patches` | Create isolated worktrees and materialize the selected reviewed change |
| `--run-profiles` | Call `/start_profile` and `/stop_profile` on the owned SGLang champion and capture bounded traces |
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
| `optimization.profiling` | Active SGLang profile point, step window, trace checks, size limits, and retention |
| `optimization.planner` | Reviewed catalog and proposal/deduplication policy |
| `optimization.workspace` | Repository, detached-worktree root, and patch limits |
| `optimization.evaluation` | Correctness/performance commands, lanes, adaptive stability, external accuracy contract, objective, and suite gates |
| `optimization.budget` | Iteration, wall-time, failure, and patience limits |
| `execution` | Artifact directory, event ledger, experiment ledger, and memory database |

Paths are resolved relative to the recipe. Commands are compiled to argument arrays
and never passed through a shell. Environment changes are explicit key/value mappings.

### Active profile and trace retention

```yaml
optimization:
  profiling:
    provider: sglang_torch
    workload_point: isl16k-osl256-c1
    warmup_runs: 1
    start_step: 1
    num_steps: 3
    activities: [GPU]
    with_stack: false
    record_shapes: false
    max_raw_bytes: 8589934592
    min_free_disk_bytes: 17179869184
    max_summary_rows: 5000
    keep_raw: false
```

The runtime checks free space before capture, asks SGLang for a finite profiler step
window, and hashes every per-rank trace. Chrome trace events are decoded incrementally
and aggregated by activity/name/device/rank/phase, so the full decompressed JSON is
never held in memory. With `keep_raw: false`, raw traces are removed only after the
bounded summary and manifest are durable. A failed capture keeps raw evidence for
diagnosis. Profile-derived values remain diagnostic and cannot pass a promotion gate.

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
  sglang_repository:
    type: string
    required: true
  sglang_ref:
    type: string
    required: true

sources:
  sglang:
    repository: ${sglang_repository}
    ref: ${sglang_ref}
    revision: ${sglang_revision}
    submodules: true

baseline:
  source_revision: ${sglang_revision}

target:
  runtime:
    expected:
      container:
        image: ${container_image}
      components:
        sglang:
          source: sglang
          revision: ${sglang_revision}

optimization:
  workspace:
    source: sglang
```

Input references must occupy an entire YAML scalar. Supported types are `string`,
`integer`, `number`, `boolean`, `git_commit`, `container_digest`, and `sha256`.
`git_commit` requires a full 40- or 64-character hexadecimal commit;
`container_digest` requires an immutable `repository@sha256:...` reference. All-zero
commit and digest placeholders are rejected.

Each managed Git source has an independent repository, full branch/tag ref, and
immutable revision. The ref preserves selection intent; execution always checks out
the revision. Build commands can address dependency worktrees with placeholders such
as `{source.deepgemm}`. Repository credentials remain in the Git credential helper or
executor-mounted secrets and are rejected when embedded in an HTTP URL.

`target plan` and `optimize plan` may inspect an unresolved template and report its
missing inputs. Active runs reject missing bindings before creating events, memory,
worktrees, or artifacts. Bind values directly with `--values`, or create a lock recipe:

```console
uv run euboulia target resolve \
  --recipe scenario.yaml \
  --values ~/.local/share/euboulia/experiments/gpu-baseline/values.yaml \
  --output ~/.local/share/euboulia/experiments/gpu-baseline/recipe.lock.yaml

uv run euboulia target run \
  --recipe ~/.local/share/euboulia/experiments/gpu-baseline/recipe.lock.yaml \
  --executor gpu-worker \
  --node NODE_NAME_OR_INTERNAL_IP
```

Values and lock files are private experiment inputs. Keep them under a local experiment
directory outside the checkout, with one directory per experiment; both common filename
forms are also ignored by Git as a second line of defense. `target resolve` creates the
lock with mode `0600` and rebases source-relative references, so the lock no longer has
to sit beside its template. It contains concrete values and no `inputs` section.

The recommended local layout is:

```text
~/.config/euboulia/config.yaml
~/.local/share/euboulia/experiments/<experiment>/values.yaml
~/.local/share/euboulia/experiments/<experiment>/recipe.lock.yaml
~/.local/share/euboulia/runs/<run-uid>/
~/.local/share/euboulia/memory.sqlite3
```

Managed schema-v3 runs also require an image digest, a full baseline Git commit, and a
matching pinned SGLang runtime revision. Each model must provide either a full revision
commit or a non-zero weights-manifest SHA-256. Source-backed runtime components must
declare `dirty: false`; declared accelerator model/count and local node count are checked
generically against the captured host inventory.

`target run` always creates a fresh detached worktree, runs declared build commands
when present, owns the SGLang service lifecycle, captures the configured profile, and
executes the qualification evaluation. These are fixed command semantics rather than
separate authorization flags.

For remote execution, the namespace, Pod template, and canonical storage are host
policy, not scenario content. Put them in `~/.config/euboulia/config.yaml` (see
`examples/runtime/kubernetes.yaml`) and select the executor with `--executor`. Pass the
node name or InternalIP through `--node` for each run; it is never stored in static
configuration. Pod templates contain cluster-specific resources, mounts, tolerations,
and secret references, so they stay next to the user's private runtime config. Reuse an
executor for experiments with the same runtime resource profile; define another executor
and template when the accelerator type or cluster policy changes.

The local supervisor generates `run_uid`, creates a uniquely named Pod in exactly the
configured namespace, and records the Pod UID before executing anything. It transfers
the local checkout and fully resolved recipe, but never the values file or host runtime
config. On success or failure it applies the configured artifact sync policy and verifies
the resulting manifest. It writes `run.json`, `events.jsonl`, `summary.json`, and
`artifact-manifest.json` under `<storage.root>/runs/<run-uid>`. If nothing remains
remote-only, it deletes the Pod with a Kubernetes UID precondition. With
`raw_profiles: on_demand`, a Pod is retained only when its artifact index actually
contains an unsynchronized raw profile. Every Pod operation checks the exact namespace,
name, UID, run annotation, and ownership labels. Euboulia never searches for or mutates
unrelated Pods.

If transfer or verification fails, the owned Pod is retained. A later controller can use
the local `run_uid` record to retrieve it, then explicitly clean it up:

```console
uv run euboulia target artifacts pull \
  --executor gpu-worker \
  --run-uid <run-uid> \
  --destination /absolute/local/path/recovery

uv run euboulia target cleanup \
  --executor gpu-worker \
  --run-uid <run-uid>
```

Pre-reviewed candidate patches remain separate experiment inputs. Every active run writes
the exact bound document to
`<artifacts>/<run-uid>/resolved-recipe.yaml` (or the target-validation subdirectory for
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
| Execution identity | Generated time-sortable ULID `run_uid` | Audit and artifact lineage only |
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
database whose schema version or columns do not match the runtime is dropped and
rebuilt empty; no in-place schema migration or legacy-ID fallback is performed.

Every `run` command creates a new `run_uid`, even when multiple executions share the
same optional `--name`. Resume is deliberately unavailable until event replay can
restore service, workspace, budget, and iteration state safely; a repeated display
name never implies resume or overwrite.

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

1. Fetch declared sources into the worker cache and create isolated baseline
   worktrees at their locked revisions.
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
- measurement phase/window plus one-sample harness controls; and
- metrics output path.

The built-in SGLang correctness harness checks routing, response shape, and non-empty
output. This is a smoke check, not semantic accuracy. Qualification may declare one
external accuracy command and a JSON result contract (`path`, dotted `metric`,
`direction`, and `threshold`). The external tool owns datasets, prompts, scoring,
and task-specific dependencies; Euboulia only executes it and evaluates the result.
`{endpoint}`, `{served_name}`, `{model_path}`, `{workspace}`, and `{result_path}`
are expanded as argv values without a shell.

External accuracy commands use a structured declaration rather than hand-written
argv. All tool switches stay together under `options`; a mapping value is compiled
to the comma-separated `key=value` form used by tools such as lm-eval:

```yaml
accuracy:
  command:
    name: lm-eval-gsm8k
    executable: python3
    module: lm_eval
    timeout_seconds: 7200
    options:
      --model: local-chat-completions
      --apply_chat_template: true
      --model_args:
        model: "{served_name}"
        base_url: "{endpoint}/v1/chat/completions"
        num_concurrent: 64
      --tasks: gsm8k
      --output_path: "{result_path}"
  result:
    path: accuracy.json
    metric: results.gsm8k.exact_match,flexible-extract
    direction: maximize
    threshold: 0.8
```

The performance harness emits one normalized sample per invocation. The generic
evaluator performs warmups, records objective windows, and stops as soon as the
configured number of recent windows are within the relative tolerance. A point
fails closed if it does not stabilize before `max_windows` or `max_seconds`.
Dataset-specific harness rules may additionally enforce exact lengths, cache state,
or evidence snapshots.

The `fast` lane is used for baseline/candidate iterations and should contain a small
representative point set. The `qualification` lane must contain `all` workload
points and is used by target validation; only qualification runs the external
accuracy command. This follows the same separation used by InferenceX: standardized
serving measurements are distinct from lm-evaluation-harness task evaluation.

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
captures observable state before launch and can fail on a mismatch. A component with
`source: <name>` is observed from that run's isolated source worktree rather than a
fixed image path.

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
└── <run-uid>/
    └── evaluations/<trial-id>/

<workspace-root>/<run-uid>/<iteration-id>/
├── baseline/{worktree,evidence}/
└── candidate/{worktree,evidence}/
```

Inspect events with:

```console
uv run euboulia optimize events \
  --events <artifacts>/events.jsonl \
  --run-uid <run-uid>
```

The event log and linked artifacts are the audit source. SQLite is a rebuildable
query index. Failed worktrees are retained for inspection and require a separate,
operator-controlled cleanup action.

## Gap to the primary workflow

- Bounded Torch traces are captured at the champion-profile stage, but automatic
  NSYS/NCU escalation is not yet implemented.
- ROI selection does not yet combine end-to-end contribution, call frequency,
  optimization headroom, and implementation cost.
- A hotspot is not yet converted into a kernel task with reference semantics and
  real serving shapes.
- CUDA/Triton generation is not connected to compile-error repair, numerical
  correctness, microbenchmark, and NCU-guided iteration.
- Validated kernels are not automatically integrated into SGLang, and a promoted
  champion is not automatically re-profiled to select the next hotspot.
- Target validation supports one Kubernetes worker with automatic local result
  persistence. Iterative optimization remains local and sequential; remote optimizer
  workers, interleaved pairs, confidence intervals, and crash-safe resume remain
  future work.
- Euboulia ends at evidence and a recommendation. Merge, rollout, and production
  observation belong to separate systems.

See [Architecture](architecture.md) for component contracts and [Safety](safety.md)
for the execution boundary.
