# Euboulia

Euboulia is an optimization agent for SGLang inference engines and their hot
operators. It turns profiling evidence into bounded optimization experiments and
uses reproducible end-to-end results to decide whether a change is worth keeping.

It focuses on inference performance. Model training, production deployment, and
general-purpose coding are outside its scope.

## How it thinks

Euboulia follows five principles:

1. **Measure before changing.** Optimization starts from serving evidence, not an
   assumed hotspot.
2. **Keep the scenario fixed.** Model, hardware, runtime, workload, and evaluation
   policy form the experiment contract.
3. **Separate diagnosis from judgment.** Profilers locate opportunities; an
   unprofiled baseline/candidate comparison decides promotion.
4. **Isolate every attempt.** Sources, services, Pods, commands, and artifacts are
   owned by one run and cannot be confused with another run.
5. **Remember outcomes by content.** Stable digests connect equivalent experiments;
   display names and generated run UIDs do not determine memory equivalence.

The optimization process has two feedback loops:

```text
SGLang loop
profile -> locate bottleneck -> propose change -> integrate -> end-to-end A/B
   ^                                                        |
   +---------------- profile the new winner ----------------+

Operator loop
real shapes -> generate/revise -> compile -> correctness -> benchmark -> analyze
```

A faster isolated kernel is not automatically a faster inference engine. Only a
correct change that improves the fixed end-to-end scenario can become the next
champion.

## Typical workflow

1. Describe the model, runtime, workload, metrics, and acceptance policy in a
   versioned recipe.
2. Keep machine-specific values and Kubernetes configuration outside the repository.
3. Resolve the recipe into an immutable experiment lock.
4. Inspect the resolved plan.
5. Run it on a user-selected node.
6. Review the local summary, evidence, and memory before starting another iteration.

Euboulia creates a dedicated Pod in the configured namespace, stages the locked run,
collects verified results locally, and deletes only the Pod owned by that run. If
required evidence cannot be synchronized, the Pod is retained for explicit recovery.

## Quick start

Euboulia requires Python 3.11 or newer:

```console
git clone <your-euboulia-fork>
cd euboulia
uv sync --extra dev
uv run euboulia doctor
```

Inspect a recipe without creating a workspace, service, or Pod:

```console
uv run euboulia target plan \
  --recipe examples/scenarios/dsv4-megamoe.yaml
```

Set `EXPERIMENT_DIR` to a private directory outside the checkout. Bind private values
and create the lock used for execution:

```console
uv run euboulia target resolve \
  --recipe examples/scenarios/dsv4-megamoe.yaml \
  --values "$EXPERIMENT_DIR/values.yaml" \
  --output "$EXPERIMENT_DIR/recipe.lock.yaml"
```

Configure an executor in `~/.config/euboulia/config.yaml`, with its real Pod template
kept outside Git. Start the local experiment console:

```console
uv run euboulia serve --open
```

Submit the lock from the console, or from another terminal:

```console
uv run euboulia target submit \
  --recipe "$EXPERIMENT_DIR/recipe.lock.yaml" \
  --executor gpu-worker \
  --node NODE_NAME_OR_INTERNAL_IP \
  --name baseline
```

The loopback-only console shows the task, current execution phase, owned Pod state,
result synchronization, logs, and historical artifacts. Its queue is durable, and
running controllers continue independently if the page or server is closed. Canonical
records remain under `<storage.root>`.

## Documentation

- [Optimization guide](docs/optimization.md): recipes, execution, evaluation, and
  experiment memory.
- [Architecture](docs/architecture.md): system boundaries and feedback loops.
- [Safety model](docs/safety.md): authorization, isolation, and ownership rules.
- [DSV4 scenario](docs/dsv4-megamoe-scenario.md): the concrete reference scenario.

## Development

```console
uv sync --extra dev
uv run ruff check .
uv run mypy src/euboulia
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Euboulia is available under the [MIT License](LICENSE).
