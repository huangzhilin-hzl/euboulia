# DS-V4-Flash MegaMoE target validation

This is the first concrete SGLang scenario supported end to end by Euboulia. Its
source contract is `ds-v4-flash-dspark-cp8-tp8-ep8_megamoe_test_plan.md`; the
checked-in recipe is `examples/scenarios/dsv4-megamoe.yaml`.

## Preconditions

Run Euboulia inside the declared single-node H20 container. Before execution:

- `/home/admin/model/DeepSeek-V4-Flash-0731` contains the local model;
- `/home/admin/src/dsv4-megamoe/SGLang` is a clean clone containing the exact commit
  selected in the values file;
- `/home/admin/src/dsv4-megamoe/DeepGEMM` is a clean clone containing the exact commit
  selected in the values file;
- `/home/admin/bench_data/dsv4_sharegpt_exact_seed1` contains the six exact-length
  datasets and their `manifest.json`; and
- the result and worktree roots do not already contain the selected run ID.

The checked-in recipe is intentionally unresolved. Create a local values file with the
immutable image reference and exact SGLang commit supplied by the user or deployment
platform:

```yaml
container_image: acr.example/sglang/deepep-base@sha256:<64-hex-digest>
deepgemm_revision: <40-or-64-hex-commit>
model_revision: <40-or-64-hex-model-revision>
sglang_revision: <40-or-64-hex-commit>
```

A container cannot portably infer its own registry digest, so Euboulia never invents
one or treats an image tag as immutable identity.

Generate the exact ShareGPT datasets with the script from the source test plan:

```console
python3 -m euboulia.harnesses.sglang.prepare_sharegpt_exact \
  --source /home/admin/model/ShareGPT_V3_unfiltered_cleaned_split.json \
  --tokenizer /home/admin/model/DeepSeek-V4-Flash-0731 \
  --output-dir /home/admin/bench_data/dsv4_sharegpt_exact_seed1 \
  --lengths 1024 16384 32768 65536 131072 262144 \
  --samples 16 \
  --seed 1
```

## Execution boundary

Inspect the template and its missing bindings without side effects:

```console
uv run euboulia target plan --recipe examples/scenarios/dsv4-megamoe.yaml
```

Bind and validate the execution identity, then inspect the exact launch argv, source
revision, 30 workload points, and required capabilities:

```console
uv run euboulia target resolve \
  --recipe examples/scenarios/dsv4-megamoe.yaml \
  --values dsv4-values.yaml \
  --output examples/scenarios/dsv4-megamoe.lock.yaml

uv run euboulia target plan \
  --recipe examples/scenarios/dsv4-megamoe.lock.yaml
```

The recipe does not hand-maintain model, suite, baseline, or point IDs. Euboulia
generates readable point aliases from ISL/OSL/concurrency/prompt count and computes
the immutable scenario identity from semantic content. Renaming a display alias
therefore cannot disconnect later optimization runs from applicable memory.

After review, execute exactly one baseline (no generated candidate):

```console
uv run euboulia target run \
  --recipe examples/scenarios/dsv4-megamoe.lock.yaml \
  --run-id dsv4-megamoe-$(date +%Y%m%d-%H%M%S) \
  --prepare-workspace \
  --run-builds \
  --manage-services \
  --run-evaluations
```

The lock requires an immutable commit before the detached worktree is created. SGLang
is installed editable from that worktree with `--no-deps`;
DeepGEMM is installed last. Euboulia starts a new process group and can stop only
that signed, owned process. It never discovers or kills an existing server.

The kernel-path gate intentionally produces eight unmerged PyTorch traces. These
can be large; budget disk space before starting. The profiled request is explicitly
non-scoring and precedes the formal matrix.

## Fail-closed gates

Execution stops on any of the following:

- model, TP8, CP8, EP8, MegaMoE, DSPARK, memory, chunking, or CUDA Graph mismatch;
- missing DeepGEMM SM90 MegaMoE API, wrong import path, forbidden Humming state,
  ordinary-MoE fallback, OOM, JIT/symmetric-buffer error, or unreviewed warning;
- fewer than eight H20 GPUs or fewer than eight rank traces containing
  `fp8_mxfp4_mega_moe`;
- a ShareGPT manifest/hash mismatch, non-exact ISL/OSL, incomplete request, failed
  cache flush, non-zero cache hit, or missing per-round server snapshot; or
- an incomplete 5-point TTFT matrix, incomplete 25-point TPOT matrix, or missing
  GSM8K result.

## Output

The run root is
`/home/admin/results/euboulia-dsv4-megamoe/<run-id>/target-validation`.
It contains `resolved-recipe.yaml`, the provenance snapshot, complete owned service logs, raw
startup and kernel-path evidence, per-command evidence, and these canonical files:

- `environment.txt`, `logs/server.log`, `server_startup_summary.md`;
- `raw/server_info_initial.json`, `raw/metrics_initial.prom`,
  `raw/nvidia_smi_initial.csv`, `raw/megamoe_kernel_path_evidence.txt`, and
  `raw/gsm8k_result.json`;
- `summary_rounds.csv`, `summary_best.csv`, `summary.md`;
- `sharegpt_manifest.json` and `result_validation.json`.

Detached worktrees and traces are retained as evidence and are not automatically
removed.
