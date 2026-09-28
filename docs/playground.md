# Molou GPU Playground

Run a local editor against a dedicated Kubernetes Pod on a selected cluster/node.
Choose a physical GPU index, edit a complete Python script, and see streamed stdout,
stderr, package installation output, exit codes, and failures in Console Output.
CuTe DSL, TileLang, and Python/PyTorch starters are included. The interface works
offline without a frontend build or CDN.

## Start

```console
uv sync --extra dev
mkdir -p ~/.config/euboulia
cp examples/playground.yaml ~/.config/euboulia/playground.yaml
cp examples/playground-pod.yaml ~/.config/euboulia/playground-pod.yaml
# Edit these two local files with your kubeconfig, context, image and runtime.
uv run euboulia playground --open
```

An alternate configuration and port can be selected:

```console
uv run euboulia playground --config /private/path/playground.yaml --port 8766 --open
```

The server binds only to `127.0.0.1`. Starting it does not create a Pod. In the UI:

1. Choose **Cluster** and **Node**, then **Connect GPU pod**. Node choices display
   their InternalIP (ExternalIP or node name if unavailable); Pod placement still
   uses the original node name. Targets come from the cluster or the local `nodes`
   allowlist. Creating a session is asynchronous;
   image-pull failures and other Pod status information remain in the local record.
2. Choose **GPU index** from the Pod's actual `nvidia-smi` inventory. The workbench
   displays memory use and GPU utilization; the refresh button updates them.
3. Select a profile, edit `solution.py`, and click **Run** or press Cmd/Ctrl+Enter.
   Click the fixed two-line **Arguments** preview or **Edit** to open a separate
   editor, for example `--seq_len_k 128 --num_heads 128 --label "test run"`.
   The editor accepts one option per line or pasted shell-style backslash
   continuations. **Apply changes** (Cmd/Ctrl+Enter) saves to the profile draft and
   updates the preview; **Cancel**, the close button, or Esc discards unapplied edits.
   Apply does not run the script; use **Run** from the workbench when ready.
   Backslash-newline continuations (including CRLF) are
   removed outside single quotes, preserving escaped backslashes and literal quoted
   values. Arguments are split using POSIX quoting and passed directly to Python's
   `sys.argv`; shell expansion and shell
   commands are not evaluated. Leave the field empty to use script defaults.
   Arguments are saved with each profile's browser draft and restored from run history.
   The script must call its own kernel and print the results. There is no hidden
   challenge harness or correctness oracle; starters call `torch.testing.assert_close`.
   Console Output records the cluster, actual Pod host IP, node, namespace/Pod and
   container. It also shows the venv path, Python/base interpreter, image package
   inheritance, creation/reuse status and key GPU package versions. Before script
   execution it prints the working directory, GPU visibility and quoted Python
   command inside the Pod. The process also inherits image/profile environment
   settings; arbitrary environment variables are not copied into logs.
4. Use **Stop** to cancel the current setup or script. **Release pod** deletes only
   this session's owned Pod when no run is active. Source and output stay local.

Each profile keeps a browser draft. Import/Download support local Python files.
Run history shows each run's node IP and a single-line Python command preview.
Hover over the command to see the recorded working directory, GPU visibility and
full command, including the actual interpreter and script paths. Runs without an
execution record show a labeled preview. Selecting a run restores its exact
submitted source snapshot, arguments and output. Repeated
runs on one session reuse its Pod and virtual environments, with one active run
per session. Different nodes can run concurrently.

## Cluster and GPU sharing

`clusters` is keyed by a friendly name. `context` and optional `kubeconfig` are
always passed explicitly to kubectl; the tool never switches the user's current
context. Paths resolve relative to the config file. Private host configuration is
separate from Euboulia's existing `config.yaml` executor configuration.

The current GPU access mode is `shared-nvidia-runtime`. It requires an existing,
administrator-enabled way for the Pod to access the node's NVIDIA devices without
exclusive GPU allocation. The generic template uses `runtimeClassName: nvidia`.
Some clusters instead require an approved host-device template with explicit device
mounts. Put those requirements and any admission labels in the local Pod template.
The application preserves them and replaces Pod identity, node assignment, command,
restart policy and GPU visibility. It does not reconfigure cluster GPU operators.

The template must contain one worker container and no extended resource requests
or limits (including `nvidia.com/gpu`, `nvidia.com/B300`, or custom GPU resources).
This avoids accidentally turning a shared session into an exclusive reservation.
The example sets both the memory request and limit to 128 GiB for large-tensor
benchmarks and clusters that require equal memory requests and limits. Adjust
both together to suit your workloads and node capacity. Restart the playground
after editing the template; existing Pods retain their original resources.
Namespace creation and image registry access must already be configured. Minimal RBAC needs
Pod create/get/delete and pods/exec in the configured namespace, plus node list
unless a local node allowlist is provided.
Resolving IP labels for an allowlist needs node get permission; without it, the
configured node names remain available as fallback labels.

All physical GPUs must be visible for inventory. Selecting index 3 resolves its
UUID, then sets `CUDA_VISIBLE_DEVICES=GPU-...` for the script. That selected card is
`cuda:0` inside PyTorch. Each execution verifies the index/UUID pair again and fails
if the mapping changed. It never silently falls back to another device. MIG slices
and automatic device-plugin time-slice assignment are not supported by this mode.

Sharing provides neither memory quotas nor performance/fault isolation. CUDA device
visibility is an execution convention for your own code, not a security sandbox;
scripts can change their environment. Run only trusted code. Do not expose this
local HTTP service to a shared network. Other Pods are never exec targets, stopped,
or modified. Cleanup checks the session label, node, namespace and immutable Pod UID,
then uses an API deletion UID precondition.

See NVIDIA's [device visibility documentation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html)
and [GPU sharing documentation](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
for runtime requirements and isolation limits.

## Environments and persistence

The image needs Linux, Python 3.10+, `venv`, `pip`, `nvidia-smi`, a compatible CUDA
development toolkit and GPU-enabled PyTorch for the included starters. Profiles are
independent Python venvs keyed by package specification and interpreter version.
`system_site_packages: true` reuses the image's PyTorch/CUDA packages; set it false
and include all needed packages for a self-contained venv. `packages` accepts pip
requirement strings; pin versions for repeatability. Set package mirrors using the
trusted Pod template's pip environment variables or a prepared image.
Image interpreters that already live in a venv are supported: their package
directories and DSL `.pth` bootstrap files are also inherited when this option is on.
Profile `env` can set runtime-specific values such as `LD_LIBRARY_PATH` or `CUDA_HOME`.
These values stay local and are passed only to that profile's remote processes.
GPU visibility and Python path isolation remain managed by the workbench. For an
image with all DSLs already installed, set `packages: []` and keep
`system_site_packages: true` to run without a network dependency.

On first use, the worker creates a venv and installs missing profile dependencies.
A completion marker is written only after success. Interrupted setup is rebuilt;
an environment lock prevents competing installers. Every run records resolved
package versions. An existing environment is reused until the profile changes or
the Pod is released. The default `emptyDir` keeps environments for the Pod lifetime;
it does not survive deletion. The tool keeps session directories separate even if
the template uses a PVC.

Remote files live under `<workdir>/<session-id>/`:

```text
venvs/<environment-hash>/
runs/<run-id>/solution.py
runs/<run-id>/cancel
```

Canonical local records live under `storage`:

```text
sessions/<session-id>.json
runs/<run-id>/run.json
runs/<run-id>/solution.py
runs/<run-id>/events.jsonl
runs/<run-id>/environment.json
runs/<run-id>/execution.json
```

Only script sources, structured console events, and environment metadata are
automatically retained locally. Arbitrary files written by scripts stay in the Pod.
Browser closure does not stop runs. Ctrl+C requests cancellation before server exit;
Pods remain available for reuse or explicit release. After a clean restart, **Connect
GPU pod** revalidates the stored Pod UID and inventory and reuses its environments.
If a run was interrupted or the Pod template changed, release/recreate the session
before continuing. Historical code/logs remain readable. A transport failure puts the session into recovery
mode; release it before retrying. If deletion fails, ownership records are retained.

Setup and execution have separate deadlines. Cancellation uses a run-specific marker
watched by the remote supervisor, which terminates only its own process group,
including child processes. A failed connection is reported as unconfirmed cancellation,
never as success. A remote timeout still bounds disconnected execution. Output is
streamed without line buffering, capped at `max_output_bytes` per run, and paged by
byte cursor. After the cap, pipes keep draining so excessive output cannot deadlock
the worker. The browser retains a bounded output window; complete retained events
up to the configured cap remain on disk.

The included starter APIs follow [CuTe DSL](https://docs.nvidia.com/cutlass/latest/media/docs/pythonDSL/quick_start.html)
and [TileLang](https://github.com/tile-ai/tilelang). Compatibility still depends on
the image, driver and configured package versions; install/compile errors appear
in the console with the real exit status.

## Validation

```console
uv run ruff check .
uv run mypy src/euboulia
uv run pytest
```

Tests cover config validation, exclusive-resource rejection, immutable Pod ownership,
HTTP origin/Host/token protection, actual Python venv reuse and streaming, stderr and
exit codes, cancellation, timeout, output caps, session serialization and history.
They use a fake GPU inventory for local tests; actual kernel validation requires a
live CUDA Pod.


## NCU and NSYS text reports

Choose **Run**, **NCU**, or **NSYS** beside the language/environment selector. The
source, script arguments, chosen GPU UUID and venv are reused. **Profiler → Edit**
opens a separate multiline editor; enter profiler options only (not the tool name,
Python command or script arguments). Each environment keeps independent mode/options
in its browser draft. History restores source, script arguments, mode, profiler
arguments and the optional **Export SASS** setting.

NCU defaults to `--set detailed --launch-count 1`. Set a filter such as
`--kernel-name 'regex:MyKernel'` or `--nvtx --nvtx-include 'measure/'` to select your
kernel. Launch skip/count counts matching kernel launches, not Python warmup
iterations. Clock control is fixed to `none` on shared GPUs. The command wraps the
complete target argv, preserving quoted arguments. The current editor still runs
a single Python file; profiling does not upload project dependencies.

NSYS defaults to `--trace=cuda,nvtx --sample=none --cpuctxsw=none`. For scripts using
CUDA Profiler APIs, add `--capture-range=cudaProfilerApi --capture-range-end=stop`.
The argument editor lists supported long options. Both `--name value` and
`--name=value` work, including quoted values and shell-style line continuations.
Output, target, GPU, config-file and attach options are managed by the tool and
cannot be overridden. CPU sampling/context-switch collection, when enabled, is
restricted to `process-tree`. Unsupported options fail before submission; options
unsupported by the installed profiler version report the tool's error in Console.

The Pod image must provide `ncu` and/or `nsys` on PATH, compatible with its GPU and
driver. These are image tools, not pip dependencies; a local profile may configure
PATH for an existing installation. Every profile run records the binary path,
version and capture/export commands. Missing tools, counter-permission failures
and missing reports are reported without silently falling back to normal Run.
Sharing GPU resources affects metrics and profiler replay affects timing; use
normal Run for ordinary benchmark timings.

After capture, NCU imports its report to produce `details.txt` and optionally
`sass.txt`. NSYS runs `nsys stats` for kernel, CUDA API and memory-operation time
summaries to produce `stats.txt`. Choose the text file in the **Output** selector.
The page previews the first 256 KiB; **Expand** opens the full saved text in a
large selectable viewer. **Copy all saved text** and **Download text**
read the entire saved file. Exported text is separate from the bounded Console
stream. Each file is capped at `max_report_bytes` (default 32 MiB); incomplete,
failed or size-limited exports are explicitly marked **Partial**. Copy/download
contains only the saved bytes when an export is partial.

Text is streamed to private local files under
`<storage>/runs/<run-id>/reports/`, independently checksummed, and retained across
server restarts and Pod deletion. `profiling.json` stores capture/export status,
version and raw report locations; `run.json` also records each text file's size,
status and checksum. Capture may succeed while export fails; the two states are
shown separately and a failed export marks the overall run failed. A size-limited
export marks its text partial even if the profiler itself succeeded.

Raw `report.ncu-rep` / `report.nsys-rep` stay under the run directory in the Pod.
Their paths appear below the output pane, labeled **Pod only**. Copy those paths
for manual retrieval with your usual kubectl tooling if you need the native GUI;
text statistics do not replace the NSYS interactive timeline. Releasing the Pod
warns about these raw files. Files already saved locally remain available.

`profiling_timeout_seconds` defaults to 900 seconds; `export_timeout_seconds`
defaults to 300 seconds per text export. They are separate from the normal
`run_timeout_seconds`. Stop/timeout during capture sends SIGINT to this run's
process group, allows up to 15 seconds to finalize, then terminates remaining owned
processes. A stopped capture does not automatically start text exports; any raw
report finalized before exit is listed for manual retrieval. Stop during export
retains any text received so far as partial. Connection loss keeps partial local
text and the existing recovery-required Pod workflow.
