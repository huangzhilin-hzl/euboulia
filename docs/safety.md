# Safety Model

Euboulia is designed to help an operator reason about inference performance
without quietly becoming an infrastructure controller. Its central safety
property is simple: **inspection is the default, benchmark execution is explicit,
and service mutation is outside the MVP.**

## Action boundary

| Action | Default / `--dry-run` | Explicit `--execute` | MVP status |
| --- | --- | --- | --- |
| Validate configuration and gates | Allowed | Allowed | Supported |
| Inspect local tool availability and versions | Allowed | Allowed | Supported |
| Render argument vectors, paths, and trial order | Allowed | Allowed | Supported |
| Invoke the planned benchmark client | No | Allowed | Supported with consent |
| Write results within declared artifact/ledger paths | No | Allowed | Supported with consent |
| Parse results and evaluate gates | From existing data | Allowed | Supported |
| Edit source, weights, or server configuration | No | No | Not permitted |
| Apply the candidate `patch` field | No | No | Not permitted |
| Launch, restart, signal, or kill an inference service | No | No | Not permitted |
| Deploy, promote, push Git changes, or alter infrastructure | No | No | Not permitted |

`run` without `--execute` must remain non-executing. `--dry-run` is an explicit
way to request the same safe inspection behavior. A future feature cannot reuse
`--execute` as blanket permission for a broader class of changes.

## Human checkpoints

There are two mandatory decision points:

1. **Before execution:** inspect the endpoint, model, dataset, prompt/token sizes,
   request rate, concurrency, command arguments, environment, timeout, artifact
   location, and estimated resource impact.
2. **After evaluation:** inspect raw evidence, correctness, noise, regressions,
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
- Every benchmark has a finite timeout. On timeout, Euboulia may terminate only
  the exact benchmark child process it created and its owned descendants.
- Euboulia must never discover, signal, restart, or kill the SGLang/vLLM server
  at the configured endpoint.
- A partial result, nonzero exit, timeout, or parse failure is retained as a
  failed experiment and cannot be promoted to a passing verdict.

## Filesystem safety

- Artifact and ledger paths are resolved before execution and shown in the plan.
- Writes are limited to the declared campaign locations and use unique run
  identities rather than broad cleanup or in-place reuse.
- Existing evidence should not be overwritten silently. Completed records are
  append-only from the operator's perspective.
- Implementations and reviews must defend against `..` traversal, absolute-path
  escapes, symlink swaps, unsafe temporary files, and result filenames supplied
  by a framework.
- Never point an artifact directory at a source tree, home directory, filesystem
  root, model store, or other valuable shared location.

## Network and resource safety

The endpoint is explicit; Euboulia does not scan for a server. The examples use
loopback addresses intentionally. Before targeting a remote environment, confirm
authorization, isolation, rate limits, budget, and an observability/abort plan.

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
argument vector, environment/provenance, timestamps, process status, raw native
result, normalized metrics, and gate verdict. Unknown raw fields are retained so
a parser update can be audited. Failed and rejected records stay visible.

Cryptographic artifact manifests, signed ledgers, isolated workers, and stronger
secret handling are candidates for future hardening; they should not be implied
by an ordinary local JSON/JSONL record today.

## Candidate patches

The schema permits a nullable `patch` field so a candidate can refer to a
proposed change in evidence. In the MVP it is inert metadata: Euboulia neither
interprets nor applies it. A future patch workflow must separately define trusted
sources, sandboxing, review, exact target resolution, approval, rollback, and
provenance.

## Expanding the boundary

Any feature that can change code, a running service, deployment state,
infrastructure, credentials, or production traffic requires a dedicated threat
model and explicit human approval narrower than `--execute`. It also needs least
privilege, preflight checks, a bounded target, rollback, and an immutable audit
record before it can be considered safe.
