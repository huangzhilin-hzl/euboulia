# Safety Model

Euboulia is designed to help an operator reason about inference performance
without quietly becoming an infrastructure controller. Its central safety
property is simple: **inspection is the default, every side-effect class is
explicit, external services remain untouched, and managed SGLang processes are
limited to children created and owned by the current run.**

## Action boundary

| Action | Inspection default | Narrow authorization | Status |
| --- | --- | --- | --- |
| Validate configuration, gates, trace, and reviewed change catalog | Allowed | None needed | Supported |
| Render argument vectors, paths, and trial order | Allowed | None needed | Supported |
| Invoke a schema-v1 benchmark client | No | `run --execute` | Supported |
| Import existing profiler artifacts | Allowed | None needed | Supported |
| Start or stop server profiling | No | None exists | Not permitted |
| Prepare and check a patch in a detached worktree | No | Active optimize run | Supported |
| Apply a reviewed optimization patch in that worktree | No | `--apply-patches` | Supported |
| Run finite preflight/correctness/benchmark commands | No | `--run-evaluations` | Supported |
| Run declared build argv in pinned baseline/candidate worktrees | No | `--run-builds` | Supported only when `target.build.commands` is declared |
| Start, readiness-check, and stop a fresh local SGLang child | No | `--manage-services` | Supported only for an explicit schema-v2/v3 `target` |
| Apply a schema-v1 candidate `patch` field | No | None exists | Not permitted |
| Edit the user's branch or auto-commit/push | No | None exists | Not permitted |
| Discover, adopt, restart, signal, or kill an external inference service | No | None exists | Not permitted |
| Manage a vLLM service | No | None exists | Not permitted in the MVP |
| Deploy, promote, or alter infrastructure | No | None exists | Not permitted |

`run` without `--execute` must remain non-executing. `--dry-run` is an explicit
way to request the same safe inspection behavior. A future feature cannot reuse
`--execute` as blanket permission for a broader class of changes.

Likewise, `optimize plan` is zero-write and zero-process. `optimize run` records
its deliberation but pauses before a worktree is created unless all capabilities
required by the configuration are present. Workspace, benchmark, owned service,
and build authorization do not imply one another. None authorizes external
service control, external model calls, deployment, or mutation of the user's
branch.

## Human checkpoints

There are three mandatory decision points:

1. **Before execution:** inspect the endpoint, model, dataset, prompt/token sizes,
   request rate, concurrency, command arguments, environment, timeout, artifact
   location, and estimated resource impact.
2. **Before an optimization trial:** inspect the pinned revision, exact patch and
   digest, structured server-argument delta, changed paths and budgets, build,
   launch, readiness, shutdown, correctness, and benchmark argv, objective,
   per-point baseline/promotion policy, GPU IDs, image/dependency provenance,
   profiler separation, workspace root, and cost limit.
3. **After evaluation:** inspect raw evidence, correctness, noise, regressions,
   and provenance before making any separate server or deployment change.

A passing gate is a recommendation under the declared experiment, not rollout
authorization.

## Command and process safety

- Commands are structured argument vectors and run without a shell. User values
  must never be concatenated into an executable string.
- Adapters select known benchmark modules and make every generated argument
  visible during planning.
- Execution inherits only the required environment plus explicitly declared
  string overrides. Sensitive values should be redacted in displays and records.
- Every build and benchmark has a finite timeout. On timeout, Euboulia may
  terminate only the exact child process it created and its owned descendants.
- A managed target records its exact SGLang child/process-group identity at
  creation. Readiness timeout, evaluation failure, interruption, and exceptions
  all enter `finally` teardown for that identity.
- Euboulia must never use port scans, `pgrep`, `pkill`, name matching, a stale PID
  file, or the configured endpoint to discover or kill a process. An external
  SGLang/vLLM service is never signaled.
- The managed MVP does not start or stop vLLM.
- A partial result, nonzero exit, timeout, or parse failure is retained as a
  failed experiment and cannot be promoted to a passing verdict.
- SGLang/vLLM `--profile` and `--profile-*` controls are rejected by serving
  adapters because they mutate server profiler state and perturb measurements.

## Filesystem safety

- Artifact and ledger paths are resolved before execution and shown in the plan.
- Writes are limited to the declared recipe locations and use unique run
  identities rather than broad cleanup or in-place reuse.
- Existing evidence should not be overwritten silently. Completed records are
  append-only from the operator's perspective.
- Implementations and reviews must defend against `..` traversal, absolute-path
  escapes, symlink swaps, unsafe temporary files, and result filenames supplied
  by a framework.
- Never point an artifact directory at a source tree, home directory, filesystem
  root, model store, or other valuable shared location.
- The external-service path gives each proposal a detached candidate worktree.
  Managed mode creates separate baseline and candidate worktrees at the same
  pinned revision. Patch paths cannot be absolute, contain `..`/`.git`, traverse
  symlinks, create a symlink or binary object, or exceed byte/file/line budgets.
- Worktrees and failed patch evidence are not automatically deleted. The operator
  removes them later through a separately reviewed maintenance action.

## Network and resource safety

The endpoint is explicit; Euboulia does not scan for a server. Managed target
readiness is restricted to localhost or a loopback IP address. The external-
service recipe examples also use loopback intentionally. Before benchmarking a
remote external environment, confirm authorization, isolation, rate limits,
budget, and an observability/abort plan.

Benchmark traffic can exhaust GPU memory, saturate accelerators and networks,
increase latency for other tenants, or create significant cloud cost. Begin with
a disposable environment and small concurrency. Do not benchmark production or
a third-party endpoint merely because it is reachable.

## Secrets, prompts, and datasets

- Keep tokens and credentials out of YAML, candidate parameters, patches, shell
  history, fixtures, raw-result uploads, and pull requests.
- Environment allowlisting and display redaction are preferred to inheriting an
  entire interactive environment into evidence.
- Prompts, model outputs, request dumps, traces, and profiler captures may contain
  personal, proprietary, or regulated data. Apply access control, encryption,
  retention, and deletion rules appropriate to that data.
- Use synthetic or approved datasets in examples and shared reproductions.

## Evidence integrity

An experiment record should bind together the validated configuration, generated
argument vector, expected/observed runtime provenance, timestamps, process status,
per-workload-point raw results, normalized metrics, and suite gate verdict.
Unknown raw fields are retained so
a parser update can be audited. Failed and rejected records stay visible.

Cryptographic artifact manifests, signed ledgers, isolated workers, and stronger
secret handling are candidates for future hardening; they should not be implied
by an ordinary local JSON/JSONL record today.

Optimization stage events live in a separate append-only ledger. Large traces,
patches, diffs, logs, and metrics are referenced by path, size, and SHA-256 rather
than embedded. SQLite memory is a derived index and may be deleted/rebuilt; it is
not the canonical audit record. Profile observations are permanently labeled
`profile_diagnostic` and `gate_eligible=false`.

## Reviewed changes

Schema v1 permits a nullable `patch` field so a candidate can refer to a proposed
change in evidence. It remains inert metadata forever under that schema.

Schema v2/v3 uses a separate reviewed change catalog. Cookbook or recipe conversion
is the operator's responsibility. The planner cannot write code or invent a
launch option; it selects an args-only, patch-only, or composite entry whose
trigger matches a finding. The workspace treats that result as untrusted, repeats
exact-apply validation at the mutation boundary, and edits only its detached
candidate worktree. Euboulia never commits the patch, touches the user's dirty
changes, or treats an accepted benchmark as deployment approval.

## Expanding the boundary

Any feature that can change code, a running service, deployment state,
infrastructure, credentials, or production traffic requires a dedicated threat
model and explicit human approval narrower than `--execute`. It also needs least
privilege, preflight checks, a bounded target, rollback, and an immutable audit
record before it can be considered safe.
