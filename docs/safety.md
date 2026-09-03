# Safety model

Euboulia can build code, start GPU services, and generate heavy benchmark traffic.
Its safety model therefore treats inspection, workspace mutation, builds, service
ownership, and evaluation as different capabilities.

The central rule is: **Euboulia may control only the exact worktrees, commands, and
SGLang processes created for an explicitly authorized run.**

## Action boundary

| Action | Authorization | Status |
| --- | --- | --- |
| Validate a recipe and inspect a plan | None | Supported |
| Import existing profiler artifacts | None | Supported |
| Run a schema-v1 benchmark client | `run --execute` | Supported |
| Create detached trial worktrees and apply a reviewed change | `--apply-patches` | Supported |
| Run declared build argv | `--run-builds` | Supported |
| Start/readiness-check/stop a fresh local SGLang child | `--manage-services` | Supported |
| Run finite correctness and performance commands | `--run-evaluations` | Supported |
| Generate and iteratively repair CUDA/Triton code | No dedicated authorization exists | Planned; not yet implemented |
| Discover or take over an existing service | No authorization exists | Forbidden |
| Edit the user's branch, commit, push, or deploy | No authorization exists | Forbidden |
| Manage vLLM lifecycle | No authorization exists | Not implemented |
| Start or stop profiling | No authorization exists | Not implemented |

`optimize plan` is read-only. `optimize run` may record its deliberation, but it
pauses before the first missing capability. One flag never implies another, and a
recipe field is not authorization by itself.

## Required review points

Before a run, review:

- model, dataset, workload matrix, cache/request policy, endpoint, GPUs, and cost;
- pinned SGLang/runtime revisions and observed provenance;
- exact source patch and server-argument delta;
- build, launch, readiness, correctness, and benchmark argv;
- timeouts, worktree/artifact paths, objective, repetitions, and regression gates.

After a run, review raw results, failures, noise, provenance drift, per-point
regressions, and semantic accuracy before any separate merge or rollout decision.
A passing gate is not deployment approval.

## Command and process ownership

- Commands are argument arrays and execute without a shell.
- Build and evaluation commands have finite timeouts and scoped environment input.
- A managed target starts in a new process group and receives a signed handle bound
  to its PID, process-group ID, process start identity, run/trial IDs, and argv
  digest.
- Teardown can address only that handle. Readiness failure, benchmark failure,
  interruption, and exceptions all enter the same `finally` teardown path.
- Port scans, `pgrep`, `pkill`, name matching, stale PID files, and killing a process
  because it listens on the configured port are forbidden.
- An external SGLang or vLLM endpoint may receive declared benchmark requests, but
  it is never signaled or reconfigured.

A partial result, timeout, nonzero exit, invalid metric, or teardown failure cannot
produce an accepted verdict.

## Source and filesystem isolation

- Baseline and candidate use separate detached worktrees at the same pinned commit.
- Reviewed patches are checked for absolute/parent paths, `.git` access, symlink
  traversal, binary/symlink creation, size, file/line budgets, and exact applicability.
- The operator's branch and dirty changes are never modified.
- Artifact and ledger paths are resolved before execution and shown in the plan.
- Existing evidence must not be silently overwritten; failures remain inspectable.
- Worktree cleanup is a separate operator action, not an automatic side effect.

Never place an artifact or worktree root at a home directory, filesystem root, model
store, or other valuable shared path.

## Network and resource risk

Managed readiness is restricted to a loopback URL. Euboulia does not scan for a
target. External-service tests require the operator to confirm authorization for the
declared endpoint.

Inference benchmarks can exhaust GPU memory, saturate accelerators and networks,
degrade neighboring workloads, and create substantial cost. Begin in a disposable
environment with bounded concurrency, finite timeouts, and an observability/abort
plan. Do not benchmark production or a third-party service merely because it is
reachable.

## Secrets and datasets

- Do not store credentials in recipes, patches, command output, fixtures, or Git.
- Execution inherits an allowlisted environment plus explicit overrides; artifact
  records list environment keys rather than secret values.
- Prompts, outputs, traces, and profiler captures may contain proprietary or personal
  data. Apply the appropriate access, encryption, retention, and deletion policy.
- Shared examples should use synthetic or approved datasets.

## Evidence integrity

An experiment should bind configuration, commands, timestamps, runtime provenance,
source/change digests, process status, raw outputs, normalized metrics, and verdict.
Rejected and failed trials remain part of the history.

Events reference large artifacts by path, size, and SHA-256. The current local
JSON/JSONL records are auditable but not a cryptographically signed ledger; stronger
artifact manifests, isolated workers, and secret handling are future hardening.
Profile-derived observations remain labeled diagnostic and gate-ineligible.

## Expanding authority

Any future feature that edits unreviewed code, controls profiling, reaches remote
workers, changes deployment state, or handles credentials needs a separate threat
model, narrow authorization, bounded targets, rollback, and durable audit evidence.
Existing flags must not be reused as blanket permission.
