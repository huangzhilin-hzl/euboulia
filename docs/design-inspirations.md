# Design inspirations

Euboulia borrows narrow, proven mechanisms from successful agent and experiment
systems. It does not embed a general-purpose agent framework in the optimization
runtime. The source of truth remains Euboulia's own typed events and evidence.

| Project | Mechanism adopted by Euboulia | Deliberate boundary |
| --- | --- | --- |
| [OpenHands](https://docs.openhands.dev/sdk/arch/conversation) | Immutable events, an append-only event log, resumable projections, and an isolated workspace abstraction | No unrestricted shell ACI and no dependency on the OpenHands runtime |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/trajectories.md) | Replayable action/observation trajectories, bounded tool output, timeouts, and explicit failure retention | Performance agents receive domain actions, not a general terminal |
| [Aider](https://aider.chat/docs/usage/modes.html) | Separate the hypothesis-producing architect from the patch materializer; run lint and tests immediately after an edit | Patches use exact application; there is no fuzzy edit fallback or automatic commit |
| [Optuna](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html) | Trial states, ask/tell-shaped search policies, hard budgets, duplicate rejection, and fail-fast pruning | Optuna may become an adapter, but it is not the audit log or scheduler |
| [MLflow](https://mlflow.org/docs/latest/ml/tracking/) | Separate small run metadata and metrics from large content-addressed artifacts; model parent run and child trials | Tracking is a one-way projection and cannot decide or block a trial |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | Checkpoint-oriented thinking, deterministic replay, and explicit pause/resume boundaries | The first runtime uses a small explicit state machine instead of adding LangGraph |
| [InferenceX](https://github.com/SemiAnalysisAI/InferenceX) | A shared serving benchmark driver, fixed workload identity, fail-closed request accounting, and a separate semantic-eval path | Euboulia keeps local process ownership and per-candidate gates; it does not import InferenceX's CI matrix, deployment assumptions, or model-specific eval policy |

## Components, not branding

The resulting loop is:

```text
imported profile
      |
      v
Profiler -> Analyzer -> HypothesisPlanner -> approval
                                           |
                                           v
                              Reviewed ChangeWorkspace
                                           |
                                           v
                      RuntimeProvenance -> TargetController
                                           |
                                           v
                 correctness once -> unprofiled point matrix
                                           |
                                           v
                             EventLedger + Memory index
                                           |
                                           +---- next iteration
```

Each arrow crosses a typed protocol. This makes the built-in rule components
replaceable without changing the state machine. A later LLM planner, Optuna
search policy, MLflow sink, container workspace, or remote GPU evaluator can be
added as an adapter.

## Why the event log is separate

The existing experiment ledger contains only benchmark `Experiment` snapshots.
Optimization events use a separate append-only log so old recipe readers stay
compatible. An event contains IDs and small JSON data; trace files, patches,
stdout, and benchmark output remain artifacts referenced by path, size, and
SHA-256 digest.

Memory is a derived SQLite index over completed iterations. It stores both
positive and negative outcomes and can be rebuilt from evidence. It is useful for
deduplicating proposals and recalling relevant results, but it never replaces the
event log as the audit record.

## Safety and evidence rules

1. Profile captures diagnose a candidate but are ineligible for promotion.
2. A reviewed change is still untrusted input. Structured arguments are validated;
   a patch must pass path, symlink, size, file-count, line-count, base-revision,
   and exact-apply checks.
3. Managed baseline and candidate trials use separate detached Git worktrees at
   the same pinned revision; the user's branch is never edited or automatically
   committed.
4. Checks are fail-fast and ordered from cheap to expensive: preflight and
   correctness once per fresh service, then an unprofiled benchmark for every
   declared workload point.
5. The reference baseline is immutable. An accepted candidate becomes the
   current champion, and the next candidate is compared with that champion.
6. Planning, workspace writes, evaluator commands, owned service lifecycle,
   optional builds, and any external model call are separate capabilities.
   Configuration may request a capability but cannot authorize itself.

## Why benchmark and semantic accuracy are separate

InferenceX centralizes serving measurement instead of maintaining a benchmark
script per model, while lm-eval tasks and thresholds live on a separate path.
Euboulia adopts that separation. Its built-in SGLang performance harness consumes
the framework-neutral workload contract, verifies complete request accounting,
and aggregates repeated measurements. Its built-in correctness command is only a
fast endpoint/output-shape smoke check.

Semantic accuracy is intentionally not inferred from that smoke response. Model
task selection, answer extraction, tokenizer/chat-template behavior, and score
thresholds are versioned evaluation policy. In the MVP they remain an external
reviewed command. A later accuracy component can reuse lm-eval-style tasks, but it
must have an explicit onboarding or champion-promotion state rather than forcing
the same expensive suite into every mechanically equivalent candidate trial.

## Deferred integrations

The MVP owns a narrowly scoped local SGLang lifecycle: fresh baseline and
candidate children are built, started, checked, evaluated, and finally stopped.
It does not adopt an existing SGLang process and does not manage vLLM. Managed
vLLM, interleaved GPU scheduling, statistical pruning, container/remote
workspaces, Optuna policies, and MLflow tracking remain later integrations.
