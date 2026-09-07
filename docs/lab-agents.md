# Connect agents to your Lab

The Lab service adds real agent identities, connections, messages, and tasks to
the research-room prototype. It is independent of `euboulia serve`, the existing
experiment control plane. Connecting an agent does not authorize GPU experiments
or change the execution rules of existing Euboulia commands.

This first version supports macOS and Linux. Each connector represents one agent;
run multiple connectors with separate configuration directories for multiple
agents on the same machine. A role and capability description describe the
teammate; actual access is determined by the locally configured runtime.

## 1. Start the Lab

Use a checkout containing this feature. During review, the feature branch is
`molou/lab-machine-agents`; its PR targets `molou/inference-world-design`.

```console
git clone --branch molou/lab-machine-agents https://github.com/huangzhilin-hzl/euboulia.git
cd euboulia
uv sync
uv run euboulia lab serve
```

Open <http://127.0.0.1:8773/prototypes/inference-world/>. Select **计算机** or
**团队**, then **登录 Lab**. In another terminal in the same checkout, display the
private management key:

```console
uv run euboulia lab token
```

Paste that key into the login form. The key is stored in `.euboulia/lab/admin.key`.
It gives full Lab access: keep it on the Lab machine. Agent machines receive
their own scoped credentials instead. Browser sessions expire after eight hours
and are invalidated by restarting the service.

To use another state directory, pass the same `--state-dir` to `lab serve` and
`lab token`. Stop the service with Ctrl+C. Identities, inboxes, results, and audit
events persist in the state directory. Restart with the same directory to retain
them. The static demo at port 8772 can remain running separately.

## 2. Create an identity and connect a local Codex agent

The **Agent 与计算机** page shows a single **连接第一位 Agent** action when the
Lab is empty. Local and remote setup guides are available below it. Once agents
have been added, the page shows their roles, connection states, runtimes, and
recent work; use **连接 Agent** above the roster to add another teammate.

Give the agent a name, role, and capability description. The
Lab displays a single-use pairing code valid for ten minutes and a command using
the new agent's ID. The setup panel separates identity creation, environment
connection, and waiting for the connector to come online. Copy the connection
and startup commands separately; replace the workspace placeholder before
running the connection command on the agent machine. Remote SSH instructions
expand in the same panel. Closing the panel does not mark the agent connected;
the roster updates when the connector polls the service.

Open an agent card for its full details, dispatch actions, and pairing controls.
Use **数据与权限** in the page footer for storage and execution boundaries. The
connection workspace supports the same four themes as the rest of the prototype.

Install Codex CLI and log in **on that machine** first. The adapter uses its saved
authentication and does not copy Codex credentials into the Lab. Check the setup
without running a model:

```console
codex --version
codex login status
```

The following uses `prism` as a convenient local directory name. The UI-generated
command uses the agent ID; either is valid. Replace the workspace path with the
directory the agent should work in:

```console
uv run euboulia lab connect \
  --url http://127.0.0.1:8773 \
  --workspace /absolute/path/to/research-repository \
  --config ~/.config/euboulia/agents/prism/agent.json
```

Paste the pairing code when prompted. It is not echoed or placed in shell
history. Pairing saves private connection settings; start the connector next:

```console
uv run euboulia lab agent run \
  --config ~/.config/euboulia/agents/prism/agent.json
```

Keep that terminal running. The Lab shows **可接收工作** after the first poll.
Ctrl+C stops the connector and any invocation it owns. To reconnect later, run
`lab agent run` again with the same config; do not pair again. If the process
crashed, allow up to 45 seconds for its previous connection lease to expire.

Codex executes in **read-only** mode with user config and exec-policy rules
disabled. This requires a recent Codex CLI supporting `exec --json`,
`--ignore-user-config`, and `--ignore-rules` (the command surface was checked with
0.147.0). It starts its own sessions and resumes successful conversations within
the same room/Goal scope. It does not attach to an existing Codex desktop chat.
Read-only mode is not a filesystem read jail: only use a machine and workspace
whose readable data may be used for this research.

## 3. Connect a remote machine

Both Lab HTTP and connector URLs are loopback-only. Use an SSH tunnel; do not
expose the stdlib HTTP service or an unauthenticated runtime port publicly.

If the Lab runs on your laptop, run this **on the laptop**, replacing `research-box`
with your SSH alias:

```console
ssh -N -o ExitOnForwardFailure=yes \
  -R 8773:127.0.0.1:8773 research-box
```

This makes the laptop's Lab available at `127.0.0.1:8773` on the remote machine.
The remote SSH server must allow remote forwarding and retain a loopback bind
(`GatewayPorts no`). If port 8773 is occupied, choose another Lab port with
`lab serve --port PORT` and use that same port throughout the tunnel and URLs.

On the remote machine, install the same Euboulia version and desired runtime,
create an identity in the Lab UI, then run `lab connect` and `lab agent run` as
above with the remote workspace path. No SSH password or private key is uploaded
to Lab. Keep the Lab, tunnel, and connector running; a sleeping laptop is offline.

Alternatively, keep Lab on an always-on server and forward a local port from each
client/worker machine:

```console
ssh -N -o ExitOnForwardFailure=yes \
  -L 8773:127.0.0.1:8773 lab-server
```

Use the same numeric port on both sides because the Lab validates the HTTP Host.
Existing VPN/network reachability alone does not change the loopback-only HTTP
boundary in this version.

## 4. Send messages and tasks

Open an agent's detail panel and choose **发送消息或任务**, or enter a research room
and choose **联系真实 Agent**. The dialog shows the destination and research
context before sending. A task carries an execution request; a message carries
questions or additional information. Both receive an observable response turn.

Each dispatch stores a snapshot of the room, Goal title, objective, completion
criteria, budget description, context, and revision. Later Goal edits do not
change existing requests. The connector processes one invocation at a time.
Additional messages queue, including while it is busy or disconnected; they do
not interrupt a running model turn. Codex sessions are scoped by room, Goal,
and conversation. Other adapters own their memory implementation.

Open the work record to see progress, results, or request cancellation. A queued
request cancels immediately. A running request shows **等待停止确认** until the
connector acknowledges it. A lease expires after 45 seconds without updates;
uncertain work becomes **执行中断 · 需核实** and is never automatically replayed.
Review the remote machine before creating a replacement request.

Use **撤销连接** to invalidate an agent credential and cancel queued work.
In-flight work becomes interrupted. The connector stops its owned process when
it observes revocation; on connection loss, it stops after roughly 30 seconds
without a successful update, plus bounded request/termination time. Local
services or processes it did not start are not managed. Use **重新配对** if a code
expired or credentials were lost; this also invalidates the previous connection.
Preserve the old connector directory and choose a new directory for re-pairing.

## 5. Bring another runtime through an adapter

The `command` adapter launches an explicitly configured local argv array, with
no shell interpolation. It receives one JSON object on stdin:

```json
{
  "agent": {
    "agent_id": "identity-id",
    "name": "Prism",
    "role": "Inspect performance evidence",
    "capabilities": "Read traces"
  },
  "job": {
    "id": "work-id",
    "kind": "task",
    "prompt": "Analyze this trace",
    "context": {
      "room_id": "room-id",
      "room_name": "inference",
      "goal_id": "goal-id",
      "goal_title": "Understand decode latency",
      "goal_context": "The captured Goal snapshot",
      "conversation_id": "room"
    }
  }
}
```

The envelope may contain additional work metadata. Emit UTF-8 JSONL on stdout,
flush progress events, emit a terminal `completed` or `failed` event, then exit:

```jsonl
{"type":"progress","text":"Checking the supplied evidence"}
{"type":"completed","text":"The comparison is inconclusive; rank 1 evidence is missing."}
```

A nonzero process exit is a failure even after a completed event. Do not emit
credentials, private reasoning, or raw tool logs. Stderr is discarded to avoid
accidentally publishing runtime diagnostics. Responses are limited to 32,000
characters, 500 progress events, and 2 MB total stdout per invocation.

Save an argv file **on the agent machine**, for example:

```json
["/usr/bin/python3", "/absolute/path/to/my_agent_bridge.py"]
```

Create a new identity in Lab, then pair it with that adapter:

```console
uv run euboulia lab connect \
  --url http://127.0.0.1:8773 \
  --workspace /absolute/path/to/research-repository \
  --config ~/.config/euboulia/agents/custom-agent/agent.json \
  --runtime command \
  --command-file /absolute/path/to/adapter-argv.json \
  --timeout 600
```

Start it using `lab agent run`. This can bridge an existing agent service or
framework, but arbitrary CLI output is not automatically compatible: the bridge
must implement the envelope/event contract. The bridge controls its own
permissions and memory. Configuring it is explicit authorization to invoke that
program for incoming Lab work; the connector does not sandbox custom programs.

## Current scope

- This is a single-operator Lab. Browser administration and agent credentials are
  separate. Agent credentials can only poll/report that identity's work.
- Research-room/Goal editing and sample discussions still use browser storage.
  Live dispatches and results are durable server records with context snapshots.
  Resetting the local prototype does not erase or cancel live work. Prototype
  Goal pause/complete controls do not control remote execution; cancel live work
  in its own record.
- Role-based automatic routing, Agent-to-Agent delegation, shared multi-user room
  storage, artifact upload, A2A/MCP endpoints, and GPU scheduling are follow-up
  features. Capability descriptions do not grant executable permissions.
- The connector uses outbound polling. It retains an in-flight journal and a
  private `last-result.json` receipt. Restarting does not replay a started job.
  Notification and completion delivery are idempotent; arbitrary runtime side
  effects cannot be guaranteed to occur exactly once.
- Connector-owned Codex conversations persist on that machine. Workspace/session
  migration between machines is not implemented. Successful results are not
  automatically accepted as verified experimental evidence.

## Threat model and rollback

Lab is trusted to send tasks within the operator's configured scope. Pairing
requires a private, expiring single-use code; server-side agent credentials are
hashed. Local credential/state directories use mode 0700 and files use 0600.
Browser writes require a same-origin custom header, management authentication,
and Host/Origin validation; cookies are HttpOnly and SameSite=Strict. No CORS
access or public HTTP binding is provided. Audit records identify create, pair,
dispatch, claim, cancellation, completion, and revoke actions without logging
prompt bodies or credentials.

Revoking an identity removes access without deleting historical work. Stop the
connector locally to stop its owned runtime, and stop `lab serve` to disable the
feature. Preserve state directories for recovery. Model authentication stays
with the runtime provider on the worker machine; no changes are made to the
existing experiment execution and approval mechanisms.
