# Design principles

Euboulia borrows a few mechanisms from agent and experiment systems, but it does not
embed their runtimes. Its source of truth is its own typed configuration, events,
artifacts, and verdicts.

## Principles used today

| Principle | Influence | Euboulia boundary |
| --- | --- | --- |
| Append-only action/observation history | [OpenHands](https://docs.openhands.dev/sdk/arch/conversation), [SWE-agent](https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/trajectories.md) | Typed optimization events and retained failures; no unrestricted agent shell |
| Separate hypothesis from materialization | [Aider](https://aider.chat/docs/usage/modes.html) | The planner selects a reviewed change; the workspace independently validates and applies it |
| Explicit trial state and budgets | [Optuna](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html) | Bounded iterations, failures, wall time, duplicate rejection, and accepted/rejected outcomes |
| Metadata separate from large artifacts | [MLflow](https://mlflow.org/docs/latest/ml/tracking/) | Events contain identifiers and digests; traces, patches, logs, and raw results remain files |
| Pause at capability boundaries | [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | A small explicit state machine pauses for missing authority; crash-safe resume is not yet claimed |
| Fixed workload identity and fail-closed accounting | [InferenceX](https://github.com/SemiAnalysisAI/InferenceX) | Shared SGLang harnesses, complete-request validation, separate correctness, and unprofiled promotion evidence |

These are implementation choices, not branding. OpenHands, SWE-agent, Aider,
Optuna, MLflow, LangGraph, and InferenceX are not runtime dependencies.

## Why a small state machine

The target workflow has two finite state machines:

```text
outer: run -> NSYS -> attribute -> choose ROI -> integrate -> A/B -> champion -> run
inner: define + shapes -> generate -> compile -> correct -> microbench -> NCU -> revise
```

An explicit state machine makes authorization, failure, and teardown behavior easy to
test. General agent orchestration becomes useful only when Euboulia adds competing
planners, remote workers, resumable long-running trials, or richer human interaction.
Adding a framework before those needs would obscure the experiment contract.

## Why events and memory are separate

The event ledger is the append-only trajectory: what was attempted, in which state,
with which evidence. SQLite memory is a derived projection optimized for questions
such as “has this change already failed for the same scenario?” It can be rebuilt and
must never replace the event/artifact record.

This separation supports both measured feedback loops:

- evidence updates future hypothesis selection;
- positive and negative results both matter;
- duplicates can be rejected; and
- no result silently rewrites history.

The inner loop improves kernel candidates from compiler/test/performance feedback;
the outer loop re-profiles each champion and changes the next optimization target.
Memory makes both loops more efficient, but it is not a substitute for either
measurement cycle and is not model-weight training.

## What should remain domain-specific

Euboulia's value lies in inference knowledge, not generic tool calling. The following
should stay first-class concepts even if their implementations later become plugins:

- SGLang runtime and workload identity;
- attention, MoE, speculative-decoding, communication, and kernel bottlenecks;
- operator semantics and real serving-shape distributions;
- compile, numerical-correctness, microbenchmark, and NCU feedback as one inner loop;
- profile-versus-promotion evidence separation;
- isolated baseline/candidate lifecycle;
- point-aware performance and correctness gates; and
- exact ownership of GPU service processes.

Future LLM planners, search policies, tracking sinks, and remote workers should adapt
to these contracts rather than replace them.
