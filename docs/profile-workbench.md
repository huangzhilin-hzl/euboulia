# Profile workbench

Open a run in the local console and choose **Profile**, or visit
`/profiles?run=<run-uid>`. The workbench reads real artifacts belonging to that
run. It also discovers retained replay captures and recovery snapshots under the
run directory. A failed run can contain a valid diagnostic capture; that capture
is not evidence that baseline qualification passed.

## Explore the evidence

1. Select a capture, then a recorded phase, module subtree, and/or forward step.
   The stage overview reports the fraction of indexed GPU events with an explicit
   phase assignment. Unassigned events remain independently selectable. The
   module tree includes parent paths; selecting a parent includes its descendants.
   Select a timeline rank. The default view shows GPU kernels. Change the
   activity filter to inspect CUDA APIs, CPU operators, communication kernels,
   memory transfers, or recorded annotations.
2. Hover over a timeline event to see its name, rank, start time, and duration.
   Zoom and move the time window to separate short kernels. An expandable event
   list provides keyboard access to the same event inspector.
3. Hotspots collapse exact kernel signatures across ranks. The default ranking
   uses the largest per-rank cumulative duration, not a sum of rank clocks. The
   heatmap shows each rank's cumulative milliseconds, normalized within that row;
   expand a row for rank counts, means, and activity shares. Search and sort do not
   change denominators. Selecting a heatmap cell opens a real sample event.
   Hotspots cover the complete selected phase/module/step scope; the timeline's
   time-window slider only controls the timeline.
4. Select an individual event to inspect launch/external/request/batch IDs,
   enclosing ranges in the same thread, explicit flow endpoints, native arguments,
   recorded stacks, and shapes. The inspector keeps the full kernel signature
   behind a disclosure instead of filling the page with template arguments.
5. Save an optimization hypothesis with its evidence, validation method, and
   rejection criterion. Saving creates an immutable local draft; it does not
   execute an experiment or promote a candidate.

GPU activity shares use the sum of kernel, communication, and memory activity
for the same rank and selected phase/module/step. CPU activity shares use their
own category and rank in that scope. The
search filter does not change these denominators. Neither is a fraction of
request latency: streams can overlap and CPU ranges can nest. Communication
classification uses kernel-name patterns and cannot separate communication from
computation inside a fused kernel. Cross-rank clocks are marked unverified.

The benefit calculator is a manually supplied Amdahl assumption:
`latency_after / latency_before = 1 - f + f / local_speedup`. Its `f` is an
assumed critical-path fraction, never a hotspot percentage copied from the table.
Actual latency and throughput gains require an unprofiled A/B comparison.

## Capture a useful collection

The existing managed target validation and optimization paths use
`optimization.profiling`. The DSV4 example now retains raw traces, records CPU and
GPU activities, and captures three workload points twice, with multiple request
waves. Previously resolved lock files are immutable and keep their old policy;
resolve a new recipe to apply these settings.

```yaml
optimization:
  profiling:
    provider: sglang_torch
    workload_point: isl16384-osl256-c1-n1
    workload_points:
      - isl1024-osl1024-c1-n1
      - isl262144-osl1024-c16-n16
    repetitions: 2
    request_waves: 4
    purpose: diagnostic
    warmup_runs: 1
    start_step: 1
    num_steps: 3
    activities: [CPU, GPU]
    semantic_scopes: true
    with_stack: false
    record_shapes: false
    merge_profiles: false
    keep_raw: true
    expected_rank_traces: 8
```

`workload_point` remains the primary point used by the optimization planner.
`workload_points` adds up to twelve unique declared points. `repetitions` is 1–10
independent captures per point. Each capture has a fresh output directory and its
own warmup, manifest, raw traces, and summary. The planner continues to consume the
primary capture; it does not pool different workloads or repetitions.

`request_waves` is 1–32. The profiler workload sends
`max(declared_num_prompts, concurrency * request_waves)` requests, without changing
the qualification workload. The manifest and workload digest record this actual
request count, dataset, benchmark parameters, source revision, and recipe digest.
A wave count is a load-size setting, not a guarantee that a bounded capture saw
all those requests.

Warm up the exact workload before capture. Keep model/source revisions, hardware,
parallelism, request distribution, seed, cache policy, and CUDA Graph settings
fixed when comparing traces. `start_step` and `num_steps` count engine steps, not
requests or necessarily decode tokens. Repetitions repeat that window; they do
not automatically advance through inference phases. Inspect recorded phase
markers before calling a window prefill, decode, or complete. Long chunked
prefill may need a different start step or longer window to reach decode.

For a separate code-understanding capture, choose `purpose: understanding`,
`activities: [CPU, GPU]`, `with_stack: true`, and `record_shapes: true`. Purpose
labels the evidence; the flags explicitly control collection. Stack/shape
collection can perturb execution and memory use. Keep diagnostic captures short
and use unprofiled qualification for the performance verdict. Missing annotations
or source stacks remain missing in the UI; kernel names are not source locations.

Raw byte and free-disk budgets are enforced per capture. A collection multiplies
capture time and retained bytes; plan storage for the entire collection. Export
must settle before its rank count, byte budget, and checksums are accepted.

The existing validation execution order is unchanged: build/start, diagnostic
capture, unprofiled qualification, stop. This PR adds analysis of baseline
artifacts and richer collection in that path. It does not add a separate
post-baseline GPU job scheduler or automatically recapture an old run from the UI.

## Semantic capture and attribution

`semantic_scopes: true` requires CPU + GPU activities and a managed
`python -m sglang.launch_server` target. Only the profiling launch uses
`euboulia.profilers.sglang_launcher`. A spawn-compatible import hook wraps
`ModelRunner.forward`; it does not edit the pinned SGLang checkout. Old resolved
recipes retain their old settings. The DSV4 template enables this option for new
resolutions. No old trace gains annotations retroactively.

The wrapper emits `euboulia::{...}` Torch annotations containing the exact
`ForwardMode`, a per-model-runner profiled forward sequence (`target:N` or
`draft:N`), and batch size. These step IDs are not request IDs or scheduler engine
step numbers. During an active profiler call only, transient module hooks record
Python module paths and available class-definition file/line metadata. Hooks are
removed on return or exception. The off-profile path bypasses hook registration.

Capture instrumentation can perturb Python execution and compilation, so this is
code-understanding/diagnostic evidence, never a performance verdict. CUDA Graph
replay may retain phase/step attribution through the graph launch but generally
has no inner Python module calls. Missing graph-node/module links stay unknown;
we do not disable graphs or apportion fused kernels to requests. Module definition
locations are not kernel implementation locations. Unsupported worker execution
paths that bypass `ModelRunner.forward` remain unannotated.

The index recognizes explicit phase/module/step fields, canonical Euboulia scope
annotations, and SGLang's recorded `step[MODE bs=N ...]` annotations. CPU events
inherit containing scopes in the same file/process/thread. GPU events inherit
only through a unique recorded CUDA launch correlation in the same raw file;
GPU timestamps need not lie inside the CPU scope. Reused launch IDs and conflicting
crossing scopes do not establish an attribution. Kernel-name guesses, request/batch
ID matches, and time adjacency are not used to assign a phase.

Each event inspector lists the actual annotation and launch event IDs supporting
its assignment. Findings distinguish observed facts from unverified bottleneck
hypotheses and unverified experimental gains. Stage summaries keep per-rank
cumulative GPU activity, the union of GPU activity intervals, and first-to-last
GPU event span separate. None is automatically a request's latency or critical
path. Coverage counts indexed GPU events, not requests or complete inference
phases, and is explicitly partial when files are missing or the index is capped.

## Retention and indexing

Capture and synchronization have independent retention controls:

- `keep_raw: true` retains worker-side raw traces after a durable summary and
  manifest have been written.
- Host runtime `storage.sync.raw_profiles: always` synchronizes those traces with the
  normal artifact pull. The Kubernetes runtime example uses this setting.
- `on_demand` omits all nested `profile/raw` directories during normal sync,
  including collection windows. Explicit artifact pull can retrieve a complete
  snapshot while the remote artifacts still exist. It cannot restore evicted raw
  files or recover a deleted Pod without another retained copy.

```text
runs/<run-uid>/
  artifacts/target-validation/
    profile/{manifest.json,summary.json,raw/}
    profile-captures/<point>/window-<n>/profile/{manifest.json,summary.json,raw/}
  profile-analysis/<capture-id>/hypotheses/<content-hash>.json
<storage-root>/profile-indexes/<run-uid>/<input-signature>.sqlite
```

The run artifacts remain the source of truth. The local SQLite index is a
disposable cache, built asynchronously from checksummed raw files; paths, manifest
content, sizes, modification times, and index version identify the cache. Raw
traces are streamed into the index and are never rewritten. Relative nanosecond
positions avoid passing large absolute timestamps to JavaScript as imprecise
numbers. The cache retains duration events, timing, scope, correlations, and flow
records. It supports Chrome `X` duration events and paired `B/E` ranges.

Queries return at most 2,500 events by default. A capture index is capped at two
million duration events and six million input records. The UI explicitly reports
truncation; narrow the window when a response reaches its limit. The timeline
renders at most 36 lanes and the accessible list at most 150 events per response.
The hotspot API returns the 5,000 largest groups and the table displays its first
80 filtered groups after collapsing ranks; search narrows this list. Phase,
module-subtree and step filters run in SQL before ranking/capping; their GPU
shares use all matching activity in the denominator. At the 5,000-row cap the
UI explicitly asks for a narrower scope. Raw traces remain available for
full exploration. Missing raw files, checksum failures, malformed traces, absent
stacks/shapes, and summary-only captures remain explicit states.

Hypothesis drafts are stored under the run, independently of the disposable
index. The server restricts requests to known runs, confines artifact paths, uses
bounded queries, and protects draft writes with the existing local control header.

## Perfetto and further analysis

**Perfetto** opens the selected raw trace in the public browser viewer through its
origin-checked PING/PONG and buffer handoff. This is a user-triggered action; simply
viewing the workbench makes no request to Perfetto. Files larger than 256 MiB use
the download path. For restricted traces, download and open them in an approved
local/self-hosted viewer. The workbench does not bundle Perfetto, Nsight Systems,
or Nsight Compute and does not claim SM occupancy, memory bandwidth, or a
validated end-to-end critical path from Torch timing alone.

References:

- [PyTorch profiler](https://docs.pytorch.org/docs/main/profiler)
- [SGLang profiling](https://docs.sglang.io/docs/developer_guide/benchmark_and_profiling)
- [Perfetto embedding API](https://perfetto.dev/docs/visualization/embedding-api-reference)
- [Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)
- [Nsight Compute profiling guide](https://docs.nvidia.com/nsight-compute/ProfilingGuide/index.html)
