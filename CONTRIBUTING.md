# Contributing to Euboulia

Thank you for helping make inference tuning more reproducible and accountable.
Bug fixes, adapters, tests, documentation, experiment fixtures, and critical
review of performance claims are all valuable contributions.

## Development setup

Euboulia supports Python 3.11 and newer. Install the project and development
tools with uv:

```console
git clone <your-euboulia-fork>
cd euboulia
uv sync --extra dev
uv run euboulia doctor
uv run pytest
```

Before opening a pull request, run:

```console
uv run ruff check .
uv run mypy src/euboulia
uv run pytest
uv run euboulia plan --config examples/sglang.yaml
uv run euboulia plan --config examples/vllm.yaml
```

Planning commands must not require a live inference service.

## Contribution workflow

1. Search existing issues and pull requests before starting substantial work.
2. Open an issue for a new public interface, safety-boundary change, framework
   integration, or large architectural change.
3. Work in a focused branch and keep unrelated formatting or refactors out of
   the change.
4. Add tests for behavior and update the relevant documentation or example.
5. Open a small, reviewable pull request explaining the problem, design, risks,
   and validation performed.

Please use the repository's private security reporting channel instead of a
public issue for suspected vulnerabilities; see [SECURITY.md](SECURITY.md).

## Evidence required for performance work

A performance claim or adapter change should include enough information for a
reviewer to reproduce and challenge it:

- exact Euboulia and SGLang/vLLM commits or versions;
- model, hardware, accelerator software, operating system, and topology;
- the complete campaign configuration and generated benchmark command;
- raw and normalized baseline/candidate results;
- warmup policy, trial count, run ordering, and observed variance;
- correctness results evaluated before performance results; and
- every intentional difference between the baseline and candidate workloads.

Do not present a single favorable run as a general optimization. Retain failed
and rejected runs, and say when an apparent gain falls within normal noise.
Changing concurrency, request rate, token lengths, or another workload dimension
is capacity-search evidence, not a code speedup. Claims about code or server
tuning must keep model, workload, hardware, framework build, and measurement
policy constant.

## Safety requirements

Changes must preserve Euboulia's human-governed execution boundary:

- `run` remains non-executing unless the operator supplies `--execute`;
- subprocess commands are structured argument vectors, never interpolated shell
  strings;
- the executor may manage only the benchmark process it starts, never an
  existing inference service;
- source, model weights, server configuration, patches, deployment state, and
  infrastructure are not modified by the MVP;
- filesystem writes stay inside the declared artifact and ledger locations;
- timeouts, endpoints, and environment changes remain explicit and reviewable;
  and
- secrets and prompt data must not appear in tests, fixtures, logs, or pull
  requests.

Any proposal to expand that boundary needs an explicit threat model, approval
step, audit record, least-privilege design, and rollback plan.

## Pull request checklist

- [ ] The change is focused and its user-visible behavior is documented.
- [ ] New behavior has tests, including failure and malformed-input cases.
- [ ] `ruff`, `mypy`, and `pytest` pass on the supported Python versions.
- [ ] Example YAML remains valid under `schema_version: 1`.
- [ ] Framework-specific behavior is contained in an adapter where practical.
- [ ] Performance claims include reproducible evidence and correctness results.
- [ ] The change does not weaken dry-run, approval, secret, process, or
      filesystem boundaries.

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
