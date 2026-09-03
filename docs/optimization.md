# Iterative optimization runtime

The schema-v2/v3 runtime turns imported profiler evidence and a reviewed change
catalog into bounded experiments. It is an evidence loop, not a Cookbook parser,
a deployment controller, or an unrestricted coding agent.

## MVP boundary

The operator owns recipe interpretation. If a Cookbook, tuning guide, incident
note, or upstream recommendation motivates an experiment, the operator converts
it into two reviewed inputs before Euboulia sees it:

1. an optional `target` declaration describing one locally owned SGLang process;
2. a change-catalog entry containing structured server arguments, an exact patch,
   or both.

Euboulia validates and executes those declarations. It does not scrape a
Cookbook, infer hidden setup steps, translate prose into commands, or generate a
free-form patch. This boundary makes the executable input small enough to review
and hash.

Managed lifecycle is **SGLang-first** in the MVP. A `target` block with
`provider: vllm` is not supported and must not be described as managed. vLLM
remains supported by the existing externally managed service paths.

## Two execution modes

The top-level `target` block is optional and selects the lifecycle boundary:

| Configuration | Service ownership | Allowed reviewed change | Baseline |
| --- | --- | --- | --- |
| `target` absent | An already-running external service; Euboulia never starts, signals, or stops it | Patch-only, evaluated by finite commands in a detached candidate worktree | v2 scalar `evaluation.baseline_value` or v3 `baseline.metric_values` keyed by workload point |
| `target.provider: sglang` | Fresh local processes created and owned by this run | Args-only, patch-only, or composite | Rebuilt and remeasured from the pinned baseline worktree |

Leaving out `target` is a compatibility feature, not permission to discover or
take over a process at the declared `endpoint`. Evaluation commands may send requests
to that endpoint, but the service must already be ready and remains untouched.

## The loop

```text
Imported profile -> Analyze -> Plan reviewed change -> Approve
       ^                                             |
       |                                             v
       +--- Event ledger + Memory <- Gate <- Isolated trial pair
```

1. **Profile** imports declared PyTorch Chrome, Nsight Systems, or Nsight
   Compute exports. It does not call a server profiling endpoint.
2. **Analyze** normalizes activities and applies conservative bottleneck rules.
   Every finding includes confidence, evidence, and caveats.
3. **Plan** maps a finding to a pre-reviewed change-catalog entry.
4. **Approve** is a hard boundary. Configuration requests side effects but never
   authorizes them.
5. **Materialize** creates isolated worktrees at the exact pinned revision and
   applies only the selected structured change.
6. **Evaluate** runs correctness once for the fresh service, then unprofiled
   performance for every workload point, and fails closed on missing, invalid, or
   non-finite evidence.
7. **Remember** appends canonical events and indexes accepted, rejected, invalid,
   and failed outcomes in rebuildable SQLite memory.

Profile-derived timing is permanently diagnostic-only. It can select a
hypothesis, but it cannot promote one.

## Reviewed change catalog

Despite the historical `patch_catalog` configuration field name, a catalog entry
is a reviewed **change**, with one of three shapes:

```yaml
# Args-only
server_args:
  set:
    "--chunked-prefill-size": 4096
  remove: []
```

```yaml
# Patch-only
patch: ../patches/reviewed-change.diff
```

```yaml
# Composite: one atomic hypothesis
patch: ../patches/reviewed-change.diff
server_args:
  set:
    "--mem-fraction-static": 0.82
  remove: []
```

Server option names must be canonical `--kebab-case` tokens. A value is a string,
finite number, or `null` for a valueless switch; disabling an option uses the
explicit `remove` list. The runner never turns these mappings into a shell string.
A composite change is accepted or rejected as a whole, so its evidence cannot be
misattributed to only the patch or only the argument.

See the checked-in
[SGLang configuration](../examples/optimization-sglang.yaml) and
[change catalog](../examples/catalogs/sglang-changes.yaml). They are safe to use
with `optimize plan`; their repository, revisions, local model, patches, and
build/launch commands are illustrative and must be replaced before active
execution. The SGLang correctness and performance commands are reusable built-in
harnesses rather than per-model scripts.

## Shared SGLang evaluation harnesses

The runner translates the declared model, workload suite, evaluation tier, and active target
handle into a stable environment contract for every evaluator command:

| Environment input | Source |
| --- | --- |
| `EUBOULIA_TARGET_ENDPOINT`, `EUBOULIA_MODEL`, `EUBOULIA_MODEL_SERVED_NAME` | Active service handle and `models.target` |
| `EUBOULIA_WORKLOAD_NAME`, `EUBOULIA_WORKLOAD_POINT_ID` | Suite and current point identity |
| `EUBOULIA_INPUT_TOKENS`, `EUBOULIA_OUTPUT_TOKENS` | Current point shape |
| `EUBOULIA_CONCURRENCY`, `EUBOULIA_NUM_PROMPTS`, `EUBOULIA_REQUEST_RATE`, `EUBOULIA_DATASET` | Current point and suite load policy |
| `EUBOULIA_WARMUPS`, `EUBOULIA_REPETITIONS` | Current evaluation tier |
| `EUBOULIA_METRICS_PATH` | Declared evaluator result path |

`python -m euboulia.harnesses.sglang.correctness` performs a cheap deterministic
request against either SGLang's native `/generate` API or its OpenAI-compatible
chat API. It is deliberately a per-trial smoke gate: it detects launch, routing,
response-shape, and empty-output failures before an expensive measurement. It is
not a semantic accuracy claim.

`python -m euboulia.harnesses.sglang.benchmark` runs the upstream
`sglang.benchmark.serving` client with a random fixed-length workload,
`request-rate=inf`, bounded concurrency, deterministic sampling, and cache flush.
It discards the configured number of complete warmup runs, validates that every
request completed in every sample, and writes the median of common finite numeric
metrics across measured repetitions. Missing requests, invalid throughput,
non-zero client exit, or malformed JSON fail the trial closed.

This split follows the useful part of InferenceX's design: one shared benchmark
driver and a separate correctness/eval path, rather than copying both into every
model recipe. Full semantic accuracy remains a declared external evaluator in
the MVP. It should be run when onboarding a model/framework/quantization path and
again for a promoted candidate when the change can affect numerics; the cheap
smoke gate still runs for every baseline and candidate. A champion-only accuracy
tier and task thresholds require an explicit evaluator state transition and are
deferred instead of being disguised as the current correctness tier.

## Managed SGLang trial pair

For each managed trial, baseline and candidate are independent materializations
of `baseline.source_revision`. They are never two launches from one dirty source
tree and never two measurements from one long-lived server:

```text
baseline worktree (pinned revision)
  -> optional reviewed build
  -> start owned SGLang process
  -> wait for declared loopback readiness URL
  -> correctness once -> measure every baseline workload point
  -> finally stop owned process

candidate worktree (same pinned revision)
  -> apply args-only / patch-only / composite change
  -> optional reviewed build
  -> start a new owned SGLang process
  -> wait for declared loopback readiness URL
  -> correctness once -> unprofiled performance for every workload point
  -> finally stop owned process

paired point metrics -> suite gate -> evidence and memory
```

The baseline must be stopped successfully before the candidate is launched. A
readiness timeout, early process exit, command failure, parse failure, gate
failure, interruption, or exception still enters teardown through `finally`.
Failure to stop an owned process is fail-closed and can never update the champion.
Every managed iteration receives fresh baseline and candidate processes; target
handles are never reused across roles or iterations. Tier `warmups` and
`repetitions` are consumed by the shared benchmark harness as discarded full runs
and measured full runs for each point against the current process. The runner
stores each metrics file under a point-specific path; it never starts a new service
merely to change ISL/OSL/concurrency. Runner-level repeated or
interleaved process pairs remain follow-on work rather than an implied behavior.

When `target.build.commands` is present, the same declared argv build sequence is
run separately in each pinned worktree. With no `build` block, the build phase is
a no-op; source isolation and fresh process ownership still apply.

## Capabilities and CLI

`optimize plan` remains zero-write and zero-process. `optimize run` records its
deliberation and pauses at `waiting_for_approval` before the first undeclared side
effect. Capabilities do not imply one another:

| Capability | CLI flag | Required when | Permits | Does not permit |
| --- | --- | --- | --- | --- |
| `workspace_write` | `--apply-patches` | Every active optimization trial | Create isolated worktrees and materialize the reviewed change | User-branch edits, commit, push, deployment |
| `benchmark_execution` | `--run-evaluations` | Every active optimization trial | Run declared finite correctness and benchmark argv | Service ownership, profiler control, shell strings |
| `owned_service_lifecycle` | `--manage-services` | `target` is present | Start, readiness-check, and stop only services created by this run | Discovering, adopting, restarting, or killing an external process |
| `build_execution` | `--run-builds` | `target.build.commands` is non-empty | Run the declared finite build argv in pinned worktrees | Arbitrary shell, package publishing, host cleanup |

An external-service patch-only run therefore uses:

```console
euboulia optimize run \
  --recipe external-service.yaml \
  --apply-patches \
  --run-evaluations
```

A managed SGLang run without build commands additionally uses
`--manage-services`. A managed configuration such as the checked-in example,
which declares build commands, requires all four flags:

```console
euboulia optimize run \
  --recipe examples/optimization-sglang.yaml \
  --apply-patches \
  --run-evaluations \
  --manage-services \
  --run-builds
```

These flags authorize only the reviewed plan for that run. A YAML field, detected
GPU, reachable port, or prior authorization cannot silently grant a capability.

## Exact configuration shape

`schema_version: 3` contains the following groups:

- `models`: the target model plus optional external draft artifacts, each with a
  stable ID, path, served name, revision, and optional weights-manifest digest;
- `endpoint`: benchmark base URL, separate from model and workload identity;
- `workload_suite`: dataset/request-rate policy and one or more explicitly named
  ISL/OSL/concurrency/prompt-count points;
- `benchmark`: benchmark mode, base argv inputs, result filename, and typed
  parameters;
- `baseline`: candidate identity, pinned Git revision, and baseline target
  arguments;
- optional top-level `target`: SGLang launch argv/environment, loopback readiness
  URL and polling bounds, GPU IDs, shutdown timeout, optional build argv, typed
  runtime provenance, backend selections, and speculative-decoding declaration;
- `optimization.profiles`: explicit imported artifacts and formats;
- `optimization.planner`: reviewed catalog path, proposal limit, and duplicate
  policy;
- `optimization.workspace`: source repository, external worktree root, timeouts,
  and patch byte/file/line limits;
- `optimization.evaluation`: objective, direction, metrics path, ordered
  correctness/performance tiers, warmups, repetitions, primary workload points,
  and per-point promotion/regression thresholds;
- `optimization.budget`: iteration, elapsed-time, failure, patience, and imported
  profile-size limits; and
- `execution`: artifact directory, event ledger, experiment ledger, and memory
  index, all resolved relative to the configuration file.

Schema v2 remains accepted and is normalized internally into one target model and
one workload point. New configurations should use v3 because it makes model,
workload, runtime, and serving identity independently auditable.

### Runtime and speculative identity

`target.runtime.expected` records the container image/digest and component
versions or revisions for Python, SGLang, Torch, CUDA, NCCL, Triton, FlashInfer,
DeepGEMM, DeepEP, `sgl-kernel`, or other named dependencies. Before a managed
baseline starts, Euboulia writes `runtime-provenance.json`, compares fields it can
observe, and fails before launch on an observed mismatch when
`capture.fail_on_mismatch` is enabled. A container digest is declared-only in the
local MVP unless the execution environment can expose it; `require_observed`
controls whether an unobservable field is itself fatal.

`target.serving.speculative` distinguishes an embedded draft (`model_ref` points
to `models.target`) from an external draft (`model_ref` points to
`models.drafts`). For an enabled external algorithm, the referenced draft path
must also occur in the exact launch argv. With `algorithm: "off"`, any
`--speculative-*` launch flag is rejected as declaration drift.

All commands are YAML argv arrays with explicit environment deltas. Shell command
strings, pipes, redirections, command substitution, and implicit interactive
setup are outside the schema.

## Evidence and state

The event ledger records runtime provenance, target materialization, argument application, worktree
preparation, patch application, build, service start/readiness/stop, evaluation,
verdict, champion update, and memory recording. Events identify baseline versus
candidate and link to artifacts without storing secret environment values.

The managed state path makes baseline and candidate teardown visible. It does not
consider an evaluation complete until the corresponding owned process has been
stopped. `memory.sqlite3` remains a derived query index; the append-only event and
artifact records are the audit source of truth.

## Process and filesystem invariants

- Launch, build, correctness, and benchmark commands use structured argv with
  `shell=False` semantics.
- A target controller records the exact child/process-group identity it created.
  It may signal that owned identity only.
- Port scans, `pgrep`, `pkill`, name-based process matching, PID-file adoption,
  and killing a process merely because it listens on the configured port are
  forbidden.
- Baseline and candidate worktrees begin at the same pinned commit. Patches never
  touch the operator's branch and remain subject to path, size, mode, symlink,
  changed-file, and changed-line checks.
- Failed worktrees and evidence are retained for review; cleanup is a separate
  operator action.

## Current limits

The MVP does not:

- convert SGLang Cookbook material into target or change declarations;
- manage a vLLM service;
- provide a built-in semantic accuracy suite or champion-only accuracy gate;
- generate or repair free-form patches with an LLM;
- take over an already-running SGLang process;
- resume an interrupted side effect from the event log;
- use containers or remote GPU workers;
- establish statistical confidence from an underpowered recipe/run; or
- deploy, commit, push, promote, or clean retained worktrees.

See [Safety](safety.md) for the authorization and ownership model and
[Design inspirations](design-inspirations.md) for the mechanisms that informed
the loop.
