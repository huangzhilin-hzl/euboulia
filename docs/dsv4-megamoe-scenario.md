# DS-V4-Flash MegaMoE target validation

This is the first concrete SGLang scenario supported end to end by Euboulia. Its
source contract is `ds-v4-flash-dspark-cp8-tp8-ep8_megamoe_test_plan.md`; the
checked-in recipe is `examples/scenarios/dsv4-megamoe.yaml`.

## Preconditions

Run the Euboulia controller locally and configure a worker in the declared single-node
H20 Pod. Before execution, the Pod must provide:

- `/home/admin/model/DeepSeek-V4-Flash-0731` contains the local model;
- `/home/admin/src/dsv4-megamoe/SGLang` is a clean clone containing the exact commit
  selected in the values file;
- `/home/admin/src/dsv4-megamoe/DeepGEMM` is a clean clone containing the exact commit
  selected in the values file;
- `lm_eval` with API extras is installed in the image at the exact version selected
  in the values file; and
- the same Euboulia checkout is available at the executor's `project_dir`.

The checked-in recipe is intentionally unresolved. Create a local values file with the
immutable image reference and exact SGLang commit supplied by the user or deployment
platform:

```yaml
container_image: acr.example/sglang/deepep-base@sha256:<64-hex-digest>
deepgemm_revision: <40-or-64-hex-commit>
lm_eval_version: <installed-lm-evaluation-harness-version>
model_revision: <40-or-64-hex-model-revision>
sglang_revision: <40-or-64-hex-commit>
```

A container cannot portably infer its own registry digest, so Euboulia never invents
one or treats an image tag as immutable identity.

## Execution boundary

Inspect the template and its missing bindings without side effects:

```console
uv run euboulia target plan --recipe examples/scenarios/dsv4-megamoe.yaml
```

Bind and validate the execution identity, then inspect the exact launch argv, source
revision, and 30 workload points:

```console
uv run euboulia target resolve \
  --recipe examples/scenarios/dsv4-megamoe.yaml \
  --values ~/.local/share/euboulia/experiments/dsv4-baseline/values.yaml \
  --output ~/.local/share/euboulia/experiments/dsv4-baseline/recipe.lock.yaml

uv run euboulia target plan \
  --recipe ~/.local/share/euboulia/experiments/dsv4-baseline/recipe.lock.yaml
```

The recipe does not hand-maintain model, suite, baseline, or point IDs. Euboulia
generates readable point aliases from ISL/OSL/concurrency/prompt count and computes
the immutable scenario identity from semantic content. Renaming a display alias
therefore cannot disconnect later optimization runs from applicable memory.

After review, execute exactly one baseline (no generated candidate):

```console
uv run euboulia target run \
  --recipe ~/.local/share/euboulia/experiments/dsv4-baseline/recipe.lock.yaml \
  --executor h20-pod \
  --name dsv4-megamoe-baseline
```

The private experiment directory is local-only. `values.yaml` never leaves the
controller. The remote supervisor stages only the resolved lock in the run-specific
Pod scratch directory and records its SHA-256 locally.

The executor and canonical local storage are machine-specific and therefore live in
`~/.config/euboulia/config.yaml`, not in the scenario. Start from
`examples/runtime/kubernetes.yaml`. The configured `project_dir` must contain the
same Euboulia checkout in the Pod; the model and SGLang paths remain Pod paths in the
scenario.

`--name` is optional, non-unique display metadata. Euboulia generates an immutable,
time-sortable ULID `run_uid` used by artifacts, workspaces, service manifests, and
event correlation.

The lock requires an immutable commit before the detached worktree is created. SGLang
is installed editable from that worktree with `--no-deps`;
DeepGEMM is installed last. Euboulia starts a new process group and can stop only
that signed, owned process. It never discovers or kills an existing server.

`target run` profiles the owned baseline before its unprofiled matrix;
`optimize run --run-profiles` uses the same collector on a separate owned champion
service. Both ask SGLang for three profiled engine steps on
`isl16384-osl256-c1-n1`. The capture requires eight unmerged rank traces and checks
that every trace contains `fp8_mxfp4_mega_moe`. The trace parser streams events into
a 5,000-row summary; after the summary and SHA-256 manifest are durable, the raw
traces are removed because the recipe declares `keep_raw: false`. Failed captures
retain their raw traces for diagnosis. This diagnostic request is never scored.

Optimization iterations use the four-point `fast` lane. Each point gets one warmup
and two to four one-sample measurement windows; evaluation stops after two recent
windows are within 2%. Target validation uses the complete 30-point
`qualification` lane with a 1.5% tolerance and at most five windows. The old fixed
three-round-per-point report path has been removed.

Performance requests use SGLang's standard `random` dataset with fixed ISL/OSL,
`random_range_ratio=0`, and seed 1. SGLang generates the requests at benchmark time;
there is no scenario-specific dataset preparation or manifest format.

## Fail-closed gates

Execution stops on any of the following:

- runtime component provenance or declared hardware model/count mismatch;
- managed-service readiness or generic OpenAI-chat correctness failure;
- fewer than eight H20 GPUs or fewer than eight rank traces containing
  `fp8_mxfp4_mega_moe`;
- an incomplete benchmark request or invalid benchmark result; or
- an incomplete qualification matrix, a point that does not stabilize within its
  window/time budget, or a missing/invalid external accuracy result.

## Output

The local controller creates `<storage.root>/runs/<run-uid>` immediately. `run.json`
and `events.jsonl` retain the control-plane record; `summary.json` is the final result;
`artifact-manifest.json` records local paths plus immutable Kubernetes URIs and
SHA-256 values. `memory.sqlite3` remains a single local database and is never copied
from the Pod.

The automatically synchronized `artifacts/target-validation` snapshot contains
`resolved-recipe.yaml`, the provenance snapshot, the active-profile summary/manifest,
owned service logs, per-command evidence, and these files:

- `logs/server.log` and `runtime-provenance.json`;
- `profile/summary.json` and `profile/manifest.json`;
- per-point `evaluation.json` and `benchmark-windows.json`;
- the generic `evaluation-summary.json` for the complete lane;
- `euboulia-accuracy.json`, produced directly by external `lm_eval` during
  qualification.

Detached worktrees remain in the configured Pod scratch directory. Raw profile traces
stay in the Pod by default and remain addressable through `artifact-manifest.json`.
Pull a complete immutable snapshot explicitly when needed:

```console
uv run euboulia target artifacts pull \
  --executor h20-pod \
  --run-uid <run-uid> \
  --destination /absolute/local/path/raw-snapshot
```
