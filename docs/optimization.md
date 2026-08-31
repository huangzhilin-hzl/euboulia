# Iterative optimization runtime

The schema-v2 runtime turns offline profiler evidence into a bounded candidate
trial. It is intentionally an evidence loop, not an autonomous infrastructure
operator.

## The loop

```text
Profile -> Analyze -> Plan -> Approve -> Patch -> Evaluate -> Remember
   ^                                                           |
   +-----------------------------------------------------------+
```

1. **Profile** imports declared PyTorch Chrome, Nsight Systems, or Nsight
   Compute exports. It does not call a server profiling endpoint.
2. **Analyze** normalizes activities and applies conservative bottleneck rules.
   Every finding includes confidence, evidence, and caveats.
3. **Plan** maps finding categories to exact, human-reviewed patch catalog
   entries. It does not generate free-form edits.
4. **Approve** is a hard state boundary. Configuration can request work but
   cannot authorize itself.
5. **Patch** creates a fresh detached worktree at the pinned revision, performs
   static policy checks, runs `git apply --check`, rechecks at the write boundary,
   and retains the resulting diff.
6. **Evaluate** runs finite argv commands in preflight, correctness, and
   unprofiled performance order. Any failure stops later tiers.
7. **Remember** appends canonical events and indexes both successful and failed
   outcomes in SQLite for context-filtered recall and duplicate rejection.

## Read-only and active commands

```console
euboulia optimize plan --config optimization.yaml
euboulia optimize run --config optimization.yaml
```

The first command is zero-write and zero-process. The second records its
trajectory and pauses at `waiting_for_approval`. Active evaluation requires both:

```console
euboulia optimize run \
  --config optimization.yaml \
  --apply-patches \
  --run-evaluations
```

The permissions do not imply one another:

| Capability | Flag | Permits | Does not permit |
| --- | --- | --- | --- |
| `workspace_write` | `--apply-patches` | Apply the checked catalog patch inside a new detached worktree | User-branch edits, commit, push, cleanup, service config |
| `benchmark_execution` | `--run-evaluations` | Run the declared finite preflight/correctness/benchmark argv | Shell strings, persistent service ownership, profiler endpoints, deployment |

Both are required because the first runner has no crash-safe `resume` command;
it never leaves a half-authorized applied patch expecting an implicit later step.

## Configuration groups

`schema_version: 2` has five optimization groups:

- `profiles`: explicit imported artifact paths and formats. NSYS imports also
  name the official report such as `cuda_gpu_kern_sum`.
- `planner`: the reviewed patch catalog, proposal count, and duplicate policy.
- `workspace`: source repository, external worktree root, pinned baseline, and
  patch byte/file/line limits.
- `evaluation`: objective direction and baseline, metrics output path, and
  ordered command tiers. Commands are YAML argv arrays, never shell strings.
- `budget`: iteration, elapsed-time, consecutive-failure, no-improvement, and
  profile-size limits.

Each tier carries `warmups` and `repetitions`. The runner exposes those values to
the finite harness as `EUBOULIA_WARMUPS` and `EUBOULIA_REPETITIONS`; the harness
owns repetition and aggregation and must write the declared metrics JSON inside
the worktree. Correctness commands communicate failure through a non-zero exit.

See [the vLLM example](../examples/optimization-vllm.yaml) for the complete
shape. Its patch, repository, revision, commands, and baseline are illustrative
and must be replaced before active use.

## Evidence and state

The run state is explicit:

```text
planned -> iterating -> waiting_for_approval | completed | stopped | failed
```

An iteration follows:

```text
created -> profiling -> analyzing -> planning -> waiting_for_approval
        -> preparing_workspace -> applying_patch -> evaluating
        -> recording_memory -> accepted | rejected | invalid | failed
```

Skipping approval or correctness is an illegal transition. The reference
baseline remains immutable. An accepted proposal becomes the champion, then the
initial runner stops because its imported profile is stale; the next run must
use a fresh champion profile.

The two ledgers have different contracts:

- the original experiment JSONL contains only schema-v1 benchmark `Experiment`
  snapshots;
- schema-v2 `events.jsonl` contains typed pipeline transitions and artifact
  references.

`memory.sqlite3` is a derived index, not a source of truth. Recall filters by
framework revision, host fingerprint, model, workload digest, and evaluation
policy digest. Negative outcomes remain queryable so the planner does not repeat
a failed patch blindly.

## Profiler evidence is not reward

All imported observations and analyses are permanently labeled
`measurement_lane=profile_diagnostic` and `gate_eligible=false`. SGLang and vLLM
adapter arguments beginning with `--profile` are rejected. A candidate can be
accepted only from the separate evaluator path with `profiler_trial=false`.

This distinction matters because tracing, counter collection, shape/stack
capture, and NCU replay can materially perturb timing. A profiler points to a
hypothesis; it does not measure the promotion reward.

## Current limits

The runtime does not yet:

- start, monitor, restart, or prove ownership of an SGLang/vLLM service;
- generate patches with an LLM or repair a failed patch;
- aggregate paired/interleaved GPU trials or compute confidence intervals;
- resume an interrupted side effect from the event log;
- use containers or remote GPU workers; or
- deploy, commit, push, or clean retained worktrees.

These are deliberate adapter boundaries. See
[Design inspirations](design-inspirations.md) for the OpenHands, SWE-agent,
Aider, Optuna, MLflow, and LangGraph mechanisms that shaped them.
