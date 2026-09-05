# DS-V4-Flash MegaMoE target validation

This is the first concrete SGLang scenario supported end to end by Euboulia. Its
source contract is `ds-v4-flash-dspark-cp8-tp8-ep8_megamoe_test_plan.md`; the
checked-in recipe is `examples/scenarios/dsv4-megamoe.yaml`.

## Preconditions

Run the Euboulia controller locally and configure a private single-node Pod template.
The selected node and template must satisfy `target.hardware` in the resolved recipe and
provide:

- a persistent model volume at `/home/admin/model`, writable when a download is needed,
  with enough space for the selected model;
- the selected downloader (`modelscope` or `huggingface_hub`) installed in the worker
  image, with network access to ModelScope or HF Mirror when the local model is missing;
- the local controller can authenticate to every private repository through its Git
  credential helper or SSH agent; credentials must not appear in values or the lock;
- `lm_eval` with API extras is installed in the image at the exact version selected
  in the values file; and
- volumes, GPU resources, tolerations, image-pull secrets, and other cluster policy
  needed by the selected node.

Before preparing sources or building SGLang, the worker checks the configured model
directory for a readable `config.json`, tokenizer, and weights, including every shard
listed in a weight index. A complete local model is reused without a network request.
A missing model is downloaded on the selected node to the mounted directory. With a
hostPath volume this cache is node-local; symlink targets must also be mounted.

The user must supply the model's actual `owner/repository` ID and download provider;
Euboulia never infers a repository from the local directory name. Both downloaders use
the pinned `model_revision` from the selected provider. Switching providers may require
a different revision; there is no automatic fallback between repositories.

The `preparing_models` phase runs with a configurable timeout (four hours in this
recipe). Download logs and command results are retained under `target-validation/models`.
An interrupted Euboulia download is resumed through the SDK's local download metadata.
Concurrent runs serialize writes to the same model directory. Cached downloads with a
different model ID or revision are rejected; use another directory for that model.
An incomplete pre-existing directory without Euboulia download metadata must be repaired
or replaced with an empty destination before downloading. Model weights remain on the
mounted volume and are not copied into run artifacts.

ModelScope uses its [snapshot download API](https://github.com/modelscope/modelscope/blob/master/modelscope/hub/snapshot_download.py).
HF Mirror uses the [Hugging Face download API](https://huggingface.co/docs/huggingface_hub/en/guides/download)
with `HF_ENDPOINT=https://hf-mirror.com` and an explicit endpoint. Install the selected
SDK in the worker image before running; Euboulia does not install packages implicitly.

The recipe also sets `HF_ENDPOINT=https://hf-mirror.com` explicitly in the performance
harness (including profile warmup and capture) and in `lm_eval` for GSM8K. The pinned
SGLang implementation of `dataset: random` still downloads ShareGPT text, so a complete
local model alone does not make evaluation independent of network access. If another
Hugging Face endpoint is required, change both command environments in the recipe
before regenerating the lock. The model download provider does not change these
dataset settings.

The checked-in recipe is intentionally unresolved. Create a local values file with the
immutable image reference and exact SGLang commit supplied by the user or deployment
platform:

```yaml
container_image: acr.example/sglang/deepep-base@sha256:<64-hex-digest>
deepgemm_repository: https://github.com/example/DeepGEMM.git
deepgemm_ref: refs/heads/my-deepgemm-branch
deepgemm_revision: <40-or-64-hex-commit>
lm_eval_version: <installed-lm-evaluation-harness-version>
model_id: <owner/model-repository>
model_provider: modelscope # or hf_mirror
model_revision: <40-or-64-hex-model-revision>
sglang_repository: https://example.com/team/SGLang.git
sglang_ref: refs/heads/my-sglang-branch
sglang_revision: <40-or-64-hex-commit>
```

A container cannot portably infer its own registry digest, so Euboulia never invents
one or treats an image tag as immutable identity. A source `repository` says where to
fetch, `ref` records the selected branch or tag, and `revision` is the immutable commit
that is actually executed. The two repositories and refs are independent.

## Execution boundary

Inspect the template and its missing bindings without side effects:

```console
uv run euboulia target plan --recipe examples/scenarios/dsv4-megamoe.yaml
```

Set `EXPERIMENT_DIR` to a private directory outside the checkout. Bind and validate the
execution identity, then inspect the exact launch argv, source revision, and 30 workload
points:

```console
uv run euboulia target resolve \
  --recipe examples/scenarios/dsv4-megamoe.yaml \
  --values "$EXPERIMENT_DIR/values.yaml" \
  --output "$EXPERIMENT_DIR/recipe.lock.yaml"

uv run euboulia target plan \
  --recipe "$EXPERIMENT_DIR/recipe.lock.yaml"
```

The recipe does not hand-maintain model, suite, baseline, or point IDs. Euboulia
generates readable point aliases from ISL/OSL/concurrency/prompt count and computes
the immutable scenario identity from semantic content. Renaming a display alias
therefore cannot disconnect later optimization runs from applicable memory.

After review, execute exactly one baseline (no generated candidate):

```console
uv run euboulia target run \
  --recipe "$EXPERIMENT_DIR/recipe.lock.yaml" \
  --executor gpu-worker \
  --node NODE_NAME_OR_INTERNAL_IP \
  --name dsv4-megamoe-baseline
```

The private experiment directory is local-only. `values.yaml` never leaves the
controller. The remote supervisor stages the local checkout and resolved lock in a
new run-specific Pod and records the lock's SHA-256 locally.

The executor and canonical local storage are machine-specific and therefore live in
`~/.config/euboulia/config.yaml`, not in the scenario. Start from
`examples/runtime/kubernetes.yaml` and keep the real Pod template next to that private
config. `--node` accepts either a Kubernetes node name or its InternalIP and is required
for every remote run; Euboulia resolves the value to `spec.nodeName`.

`--name` is optional, non-unique display metadata. Euboulia generates an immutable,
time-sortable ULID `run_uid` used by artifacts, workspaces, service manifests, and
event correlation.

The lock requires an immutable commit for each source. The controller reuses that exact
commit from its validated persistent cache; when absent, it fetches the declared branch
without tags and the locked commit as needed. Cache hits do not contact the origin or
claim a fresh observation of the branch tip. Source preparation failures are recorded as
failed runs before any Pod is created. The controller transfers an exact-revision Git
bundle. The worker creates separate per-run worktrees,
installs SGLang editable with `--no-deps`, and installs DeepGEMM from its own worktree last.
DeepGEMM declares `submodules: true` so its pinned CUTLASS and fmt dependencies are
initialized before the build. Its installer runs with `bash -e -o pipefail` so a failed
wheel build or installation stops the run instead of being hidden by a later command.
After changing the recipe, regenerate the experiment lock before submitting a new run;
existing submission and run artifacts retain the original contract.
Euboulia starts a new process group and can stop only
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
- accelerator identity/count does not match `target.hardware`, or fewer than eight rank
  traces contain `fp8_mxfp4_mega_moe`;
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
- `sources/source-sglang.json` and `sources/source-deepgemm.json` with fetch evidence;
- `profile/summary.json` and `profile/manifest.json`;
- per-point `evaluation.json` and `benchmark-windows.json`;
- the generic `evaluation-summary.json` for the complete lane;
- `euboulia-accuracy.json`, produced directly by external `lm_eval` during
  qualification.

Required artifacts are synchronized before the ephemeral Pod is deleted. With the
default `raw_profiles: on_demand` policy, the exact owned Pod is retained only when its
artifact index contains an unsynchronized raw trace. A synchronization or verification
failure also retains it for recovery:

```console
uv run euboulia target artifacts pull \
  --executor gpu-worker \
  --run-uid <run-uid> \
  --destination /absolute/local/path/raw-snapshot

uv run euboulia target cleanup \
  --executor gpu-worker \
  --run-uid <run-uid>
```
