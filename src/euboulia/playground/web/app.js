"use strict";
const $ = (id) => document.getElementById(id);
const choices = window.PlaygroundChoices;
choices.init();
const activeStates = new Set(["queued", "verifying_gpu", "preparing_environment", "running", "cancelling", "profiling", "exporting", "finalizing"]);
let config, session = null, selectedRun = null, cursor = 0, state = {sessions: [], runs: []};
let currentProfile = "", polling = false, busy = false, historyKey = "", outputSize = 0, nodeRequest = 0;
const nodeLabels = new Map();
let argumentsTarget = "arguments", currentMode = "run", profilerDraft = {}, reportKey = "";
function nodeLabel(cluster, node) { return nodeLabels.get(`${cluster}/${node}`) || node; }

async function api(path, body) {
  const options = body === undefined ? {} : {method: "POST", headers: {
    "Content-Type": "application/json", "X-Playground-Token": config.token}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}
function error(message) { $("error").textContent = message; $("error").hidden = false; }
async function action(fn) {
  if (busy) return;
  busy = true; controls(); $("error").hidden = true;
  try { await fn(); } catch (e) { error(e.message); }
  finally { busy = false; controls(); }
}
function options(select, values) {
  select.replaceChildren(...values.map(([value, label]) => new Option(label, value)));
  choices.sync();
}
function draftKey(profile) { return `molou-playground-draft-${profile}`; }
function argumentLines(text) {
  const lines = text ? text.split("\n").length : 0;
  return `${lines} ${lines === 1 ? "line" : "lines"}`;
}
function updateArguments() {
  const text = $("arguments").value;
  $("arguments-count").textContent = argumentLines(text);
  $("arguments-preview-text").textContent = text ? text.split("\n").slice(0, 2).join("\n") : "No arguments · Click to edit";
  $("arguments-preview").classList.toggle("empty", !text);
  $("arguments-preview").setAttribute("aria-label", `Edit script arguments, ${argumentLines(text)}`);
}
function updateArgumentsEditor() {
  const text = $("arguments-expanded").value;
  $("arguments-editor-count").textContent = `${argumentLines(text)} · ${text.length} / 8192 characters`;
}
function openArguments(target = "arguments") {
  argumentsTarget = target;
  const profiler = target === "profiler-arguments";
  $("arguments-title").textContent = profiler ? `Edit ${currentMode.toUpperCase()} arguments` : "Edit script arguments";
  $("arguments-expanded").setAttribute("aria-label", profiler ? "Expanded profiler arguments" : "Expanded script arguments");
  $("arguments-help").textContent = profiler ? `One option per line; quote values containing spaces. Supported: ${(config.profiler_options[currentMode] || []).join(", ")}. GPU, target and output paths are managed automatically.` : "One option per line, or paste backslash continuations. Quote values containing spaces.";
  $("arguments-expanded").value = $(target).value;
  updateArgumentsEditor();
  $("arguments-dialog").showModal();
  $("arguments-expanded").focus();
}
function applyArguments() {
  $(argumentsTarget).value = $("arguments-expanded").value;
  updateArguments(); updateProfiler(); saveDraft();
  $("arguments-dialog").close();
}
function saveDraft() {
  try {
    localStorage.setItem(draftKey(currentProfile), $("code").value);
    localStorage.setItem(`${draftKey(currentProfile)}-arguments`, $("arguments").value);
    profilerDraft.mode = currentMode;
    profilerDraft[currentMode] = $("profiler-arguments").value;
    localStorage.setItem(`${draftKey(currentProfile)}-profiling`, JSON.stringify(profilerDraft));
    $("saved").textContent = "Draft saved";
  }
  catch (_) { $("saved").textContent = "Draft not saved · download a copy"; }
}
function setProfile(name, code, argumentsText) {
  currentProfile = name; $("profile").value = name;
  let draft = null, savedArguments = "";
  try {
    draft = localStorage.getItem(draftKey(name));
    savedArguments = localStorage.getItem(`${draftKey(name)}-arguments`) || "";
  } catch (_) { /* storage can be disabled */ }
  $("code").value = code ?? draft ?? config.profiles.find((p) => p.name === name).code;
  $("arguments").value = argumentsText ?? savedArguments;
  profilerDraft = {...config.profiler_defaults, mode: "run"};
  try { Object.assign(profilerDraft, JSON.parse(localStorage.getItem(`${draftKey(name)}-profiling`) || "{}")); } catch (_) { /* use defaults */ }
  // Upgrade the old default without changing any custom filters or profiler options.
  if (profilerDraft.ncu.trim().replace(/\s+/g, " ") === "--set detailed --launch-count 1") {
    profilerDraft.ncu = config.profiler_defaults.ncu;
  }
  delete profilerDraft.sass;
  setMode(profilerDraft.mode in config.profiler_defaults ? profilerDraft.mode : "run");
  updateArguments();
  highlight(); updateCursor(); choices.sync();
}
function setMode(mode) {
  currentMode = mode; $("mode").value = mode;
  $("profiler-arguments").value = profilerDraft[mode] ?? config.profiler_defaults[mode];
  updateProfiler(); choices.sync();
}
function updateProfiler() {
  $("profiling-settings").hidden = currentMode === "run";
  $("ncu-reports").hidden = currentMode !== "ncu";
  $("profiler-preview-text").textContent = $("profiler-arguments").value || "Default tool options · Click to edit";
  $("run").textContent = currentMode === "run" ? "▶ Run" : `▶ ${currentMode.toUpperCase()}`;
  $("profiling-hint").textContent = currentMode === "ncu" ? "Filter kernels with --kernel-name 'regex:Sm100SimpleCopyKernel'. The default regex:.* matches all names; --launch-count 1 captures the first match." : "CUDA / NVTX timeline collection. Text statistics are saved locally.";
}
function restoreProfiling(run) {
  const mode = run.mode || "run";
  profilerDraft[mode] = run.profiler_arguments ?? config.profiler_defaults[mode];
  setMode(mode);
}
function resetResults() {
  reportKey = ""; options($("result-view"), [["console", "Console"]]);
  $("report-text").textContent = ""; $("raw-report-details").hidden = true;
  showResult();
}
async function showResult() {
  const name = $("result-view").value, rid = selectedRun, isReport = name !== "console";
  $("console").hidden = isReport; $("report-text").hidden = !isReport;
  $("report-toolbar").hidden = !isReport;
  $("copy-report").disabled = true; $("expand-report").disabled = true;
  if (!isReport || !rid) return;
  const record = state.runs.find(r => r.id === rid)?.reports?.[name];
  const url = `/api/runs/${rid}/reports/${name}`;
  $("download-report").href = url;
  $("download-report").download = `${rid.slice(0,8)}-${name}`;
  try {
    const value = await api(`${url}?preview=1`);
    if (rid !== selectedRun || name !== $("result-view").value) return;
    $("report-text").textContent = value.text;
    const status = record?.status || "partial";
    $("report-info").className = status === "ready" ? "" : "partial";
    $("report-info").textContent = `${status === "ready" ? "Complete" : "Partial / still exporting"} · ${value.bytes.toLocaleString()} bytes saved locally${record?.truncated ? " · Size limit reached; remaining text was not saved" : ""}${value.bytes > 256*1024 ? " · Preview: first 256 KiB; copy/download includes all saved text" : ""}`;
    $("copy-report").disabled = status === "streaming";
    $("expand-report").disabled = status === "streaming";
  } catch (e) { error(e.message); }
}
async function updateResults(run) {
  const previous = $("result-view").value;
  const views = [["console", "Console"], ...Object.keys(run.reports || {}).map(n => [n, {"details.txt":"NCU Details", "sass.txt":"SASS", "stats.txt":"NSYS Stats"}[n] || n])];
  if (JSON.stringify([...$("result-view").options].map(o => o.value)) !== JSON.stringify(views.map(c => c[0]))) {
    options($("result-view"), views);
    if (views.some(c => c[0] === previous)) $("result-view").value = previous;
  }
  choices.sync();
  const raw = run.profiling?.remote_reports || [];
  $("raw-report-details").hidden = !raw.length;
  $("raw-reports").textContent = `Raw report · Pod only · deleted when Pod is released\n${raw.join("\n")}\nCollection: ${run.profiling?.collection_status} · Text export: ${run.profiling?.export_status}`;
  const key = JSON.stringify([run.id,run.reports,$("result-view").value]);
  if (key !== reportKey) { reportKey = key; await showResult(); }
}
function highlight() {
  const code = $("code").value;
  $("line-numbers").textContent = Array.from({length: code.split("\n").length}, (_, i) => i + 1).join("\n");
  const output = document.createDocumentFragment();
  const regex = /#[^\n]*|(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|\b(?:import|from|as|def|class|return|if|else|elif|for|in|while|with|try|except|raise|assert|and|or|not|True|False|None|pass|break|continue|lambda)\b|\b\d+(?:\.\d+)?\b/g;
  let last = 0;
  for (const match of code.matchAll(regex)) {
    output.append(document.createTextNode(code.slice(last, match.index)));
    const span = document.createElement("span"), token = match[0];
    span.className = token.startsWith("#") ? "comment" : /^["']/.test(token) ? "str" : /^\d/.test(token) ? "num" : "kw";
    span.textContent = token; output.append(span); last = match.index + token.length;
  }
  output.append(document.createTextNode(code.slice(last) + "\n"));
  $("highlight").firstElementChild.replaceChildren(output); syncScroll();
}
function syncScroll() {
  $("highlight").scrollTop = $("code").scrollTop;
  $("highlight").scrollLeft = $("code").scrollLeft;
  $("line-numbers").scrollTop = $("code").scrollTop;
}
function updateCursor() {
  const lines = $("code").value.slice(0, $("code").selectionStart).split("\n");
  $("cursor").textContent = `Ln ${lines.length}, Col ${lines.at(-1).length + 1}`;
}
function clearConsole() { $("console").replaceChildren(); outputSize = 0; }
function output(stream, text) {
  if (outputSize > 2 * 1024 * 1024) {
    $("console").firstChild?.remove();
    outputSize = $("console").textContent.length;
  }
  $("console").querySelector(".console-empty")?.remove();
  const span = document.createElement("span"); span.className = stream; span.textContent = text;
  $("console").append(span); outputSize += text.length;
  if ($("autoscroll").checked) $("console").scrollTop = $("console").scrollHeight;
}
function badge(id, value) { $(id).textContent = value.replaceAll("_", " "); $(id).className = `badge ${value}`; }
function activeRun() { return state.runs.find((r) => r.session === session?.id && activeStates.has(r.status)); }
function controls() {
  const running = activeRun(), ready = session?.status === "ready";
  $("run").disabled = busy || !ready || !!running || !$("gpu").value;
  $("stop").disabled = busy || !running || running.status === "cancelling";
  $("connect").disabled = busy || !$("node").value || session?.status === "starting";
  $("release").disabled = busy || !session || !!running || ["starting", "releasing", "released"].includes(session.status);
  $("refresh").disabled = busy || !ready;
  $("gpu").disabled = busy || !ready || !!running;
  choices.sync();
}
function renderSession() {
  badge("session-status", session?.status || "Disconnected");
  $("session-details").hidden = !session; $("session-empty").hidden = !!session;
  $("pod-namespace").textContent = session?.namespace || "";
  const podName = session?.pod || "";
  if ($("pod-name").textContent !== podName) {
    const split = podName.lastIndexOf("-") + 1;
    $("pod-name").replaceChildren(podName.slice(0, split), document.createElement("wbr"), podName.slice(split));
    $("pod-name").title = podName;
  }
  $("session-error").hidden = !session?.error;
  $("session-error").textContent = session?.error || "";
  $("target-info").textContent = session ? `${session.cluster} / ${nodeLabel(session.cluster, session.node)}` : "Select your cluster and node to get started";
  const old = $("gpu").value, gpus = session?.gpus || [];
  const desired = gpus.map((g) => [String(g.index), `GPU ${g.index} · ${g.name}`]);
  if (JSON.stringify([...$("gpu").options].map(o => [o.value,o.text])) !== JSON.stringify(desired.length ? desired : [["", "Connect to select GPU"]])) {
    options($("gpu"), desired.length ? desired : [["", "Connect to select GPU"]]);
    if (gpus.some(g => String(g.index) === old)) $("gpu").value = old;
  }
  gpuInfo(); controls();
}
function gpuInfo() {
  const gpu = session?.gpus.find(g => String(g.index) === $("gpu").value);
  $("gpu-info").textContent = gpu ? `${gpu.memory_used_mb} / ${gpu.memory_total_mb} MiB · ${gpu.utilization}% util` : "";
  $("gpu").title = gpu?.uuid || "Select a physical GPU index";
}
async function loadNodes() {
  const request = ++nodeRequest, cluster = $("cluster").value;
  session = null; renderSession();
  options($("node"), [["", "Loading nodes…"]]); controls();
  const response = await api(`/api/clusters/${encodeURIComponent(cluster)}/nodes`);
  if (request !== nodeRequest) return;
  for (const n of response.nodes) nodeLabels.set(`${cluster}/${n.name}`, n.ip || n.name);
  options($("node"), response.nodes.map(n => [n.name, (n.ip || n.name) + (n.ready === false ? " (NotReady)" : "")]));
  for (const option of $("node").options) option.title = option.value;
  historyKey = "";
  if (!response.nodes.length) options($("node"), [["", "No nodes available"]]);
  selectSession();
}
function selectSession() {
  session = state.sessions.find(s => s.cluster === $("cluster").value && s.node === $("node").value && s.status !== "released") || null;
  renderSession();
}
function shellQuote(value) {
  return /^[a-zA-Z0-9_@%+=:,./-]+$/.test(value) ? value : "'" + value.replaceAll("'", "'\"'\"'") + "'";
}
function historyCommand(run) {
  const execution = run.execution;
  const preview = ["python", "-u", "solution.py", ...(run.args || [])].map(shellQuote).join(" ");
  if (!execution) return {preview, detail: "Command preview (execution not recorded):\n" + preview};
  return {
    preview,
    detail: `Working directory: ${execution.cwd}\nCUDA_VISIBLE_DEVICES=${shellQuote(execution.env.CUDA_VISIBLE_DEVICES)}\nCommand (inside Pod): ${execution.argv.map(shellQuote).join(" ")}`,
  };
}
function historyDate(createdAt) {
  const date = new Date(createdAt * 1000), pad = n => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
const relativeTime = new Intl.RelativeTimeFormat("en", {numeric: "always"});
function historyAge(createdAt, now = Date.now()) {
  const elapsed = (now - createdAt * 1000) / 1000, seconds = Math.abs(elapsed);
  if (seconds < 60) return "Just now";
  const [unit, size] = seconds < 3600 ? ["minute", 60] : seconds < 86400 ? ["hour", 3600] : ["day", 86400];
  return relativeTime.format((elapsed < 0 ? 1 : -1) * Math.floor(seconds / size), unit);
}
function updateHistoryTimes() {
  const now = Date.now();
  // Update only the age labels, preserving list scroll, selection and keyboard focus.
  for (const age of $("history").querySelectorAll(".run-age")) {
    const text = historyAge(Number(age.dataset.createdAt), now);
    if (age.textContent !== text) age.textContent = text;
  }
}
function renderHistory() {
  $("run-count").textContent = state.runs.length;
  const key = JSON.stringify(state.runs.map(r => [r.id,r.status,r.created_at,r.node_ip,r.args,r.execution])) + selectedRun;
  if (key === historyKey) return;
  historyKey = key;
  if (!state.runs.length) return;
  $("history").replaceChildren(...state.runs.map(r => {
    const b = document.createElement("button"); b.className = r.id === selectedRun ? "selected" : "";
    const title = document.createElement("span"); title.className = "run-title";
    const name = document.createElement("span"); name.textContent = `${(r.mode || "run").toUpperCase()} · ${r.profile} · GPU ${r.gpu_index}`;
    const status = document.createElement("span"); status.className = "status-dot"; status.textContent = r.status === "succeeded" ? "✓" : r.status === "failed" ? "×" : "·";
    title.append(name,status); const detail = document.createElement("small"); detail.className = "run-meta";
    const created = document.createElement("time"), date = new Date(r.created_at * 1000);
    created.className = "run-created"; created.dateTime = date.toISOString();
    created.textContent = historyDate(r.created_at); created.title = date.toString();
    const summary = document.createElement("span"); summary.className = "run-meta-summary";
    const age = document.createElement("span"); age.className = "run-age";
    age.dataset.createdAt = String(r.created_at); age.textContent = historyAge(r.created_at);
    const outcome = document.createElement("span"); outcome.textContent = `· ${r.status.replaceAll("_", " ")}`;
    summary.append(age,outcome); detail.append(created,summary);
    const target = document.createElement("span"); target.className = "run-target";
    target.textContent = `Node ${r.node_ip || nodeLabel(r.cluster, r.node)}`;
    const command = document.createElement("code"); command.className = "run-command";
    const invocation = historyCommand(r);
    command.textContent = invocation.preview; command.title = invocation.detail;
    b.append(title,target,command,detail);
    b.title = `${r.cluster} / ${r.node}\nNode IP: ${r.node_ip || "Not recorded"}\nGPU ${r.gpu_index}: ${r.gpu_uuid}\n\n${invocation.detail}`;
    b.onclick = () => action(async () => {
      selectedRun = r.id; cursor = 0; clearConsole(); resetResults();
      const saved = await api(`/api/runs/${r.id}/code`);
      if (config.profiles.some(p => p.name === r.profile)) { saveDraft(); setProfile(r.profile, saved.code, r.arguments || ""); }
      else { $("code").value = saved.code; $("arguments").value = r.arguments || ""; updateArguments(); highlight(); }
      restoreProfiling(r); renderHistory(); await readEvents();
    });
    return b;
  }));
}
async function readEvents() {
  if (!selectedRun) return;
  const rid = selectedRun;
  const response = await api(`/api/runs/${rid}/events?after=${cursor}`);
  if (rid !== selectedRun) return;
  cursor = response.next;
  for (const event of response.events) {
    if (event.kind === "output") output(event.stream, event.data);
    else if (event.kind === "phase") output("phase", `\n› ${event.phase.replaceAll("_", " ")}\n`);
    else if (event.kind === "result") output("system", `\n[${event.status}${event.exit_code !== null ? ` · exit ${event.exit_code}` : ""}]\n`);
  }
  const r = response.run; badge("run-status", r.status);
  const index = state.runs.findIndex(item => item.id === r.id);
  if (index !== -1) state.runs[index] = r;
  await updateResults(r);
  const end = r.finished_at || Date.now()/1000;
  $("run-detail").textContent = `${r.id.slice(0,8)} · ${r.profile} · GPU ${r.gpu_index} · ${(end-r.created_at).toFixed(1)}s`;
}
async function poll() {
  if (polling || !config) return;
  polling = true;
  try {
    state = await api("/api/state"); selectSession(); renderHistory(); await readEvents();
  } catch (e) { error(`Connection: ${e.message}`); }
  finally { polling = false; }
}
async function run() {
  if ($("run").disabled) return;
  await action(async () => {
    saveDraft();
    const r = await api("/api/runs", {session:session.id, profile:currentProfile, gpu_index:Number($("gpu").value), code:$("code").value, arguments:$("arguments").value, mode:currentMode, profiler_arguments:currentMode === "run" ? "" : $("profiler-arguments").value});
    state.runs.unshift(r); selectedRun = r.id; cursor = 0; clearConsole(); resetResults(); badge("run-status", "queued"); renderHistory();
  });
}
$("error").onclick = () => { $("error").hidden = true; };
$("cluster").onchange = () => action(loadNodes);
$("node").onchange = selectSession;
$("gpu").onchange = () => { gpuInfo(); controls(); };
$("connect").onclick = () => action(async () => {
  session = await api("/api/sessions", {cluster:$("cluster").value,node:$("node").value});
  state.sessions = [...state.sessions.filter(s => s.id !== session.id),session]; renderSession();
});
$("release").onclick = () => action(async () => {
  const raw = state.runs.some(r => r.session === session.id && r.profiling?.remote_reports?.length);
  if (raw && !confirm("This Pod contains raw profiler reports. Releasing it deletes those files. Locally saved text and history remain. Release Pod?")) return;
  const s = await api(`/api/sessions/${session.id}/release`, {});
  state.sessions = state.sessions.map(item => item.id === s.id ? s : item); session = null; renderSession();
});
$("refresh").onclick = () => action(async () => {
  session = await api(`/api/sessions/${session.id}/refresh`, {});
  state.sessions = state.sessions.map(s => s.id === session.id ? session : s); renderSession();
});
$("profile").onchange = () => { saveDraft(); setProfile($("profile").value); };
$("code").addEventListener("input", () => { highlight(); updateCursor(); saveDraft(); });
$("arguments-preview").onclick = () => openArguments();
$("arguments-expand").onclick = () => openArguments();
$("profiler-preview").onclick = () => openArguments("profiler-arguments");
$("profiler-edit").onclick = () => openArguments("profiler-arguments");
$("mode").onchange = () => {
  profilerDraft[currentMode] = $("profiler-arguments").value;
  setMode($("mode").value); saveDraft();
};
$("result-view").onchange = () => { reportKey = ""; showResult(); };
$("expand-report").onclick = () => action(async () => {
  const rid = selectedRun, name = $("result-view").value;
  if (!rid || name === "console") return;
  $("report-title").textContent = name;
  $("report-dialog-info").textContent = $("report-info").textContent.replace(/ · Preview:.*/, "");
  $("report-expanded").textContent = "Loading saved text…";
  $("report-copy").disabled = true;
  $("report-dialog").showModal();
  $("report-expanded").scrollTop = 0; $("report-expanded").scrollLeft = 0;
  const response = await fetch(`/api/runs/${rid}/reports/${name}`);
  if (!response.ok) throw new Error("Could not read saved report");
  $("report-expanded").textContent = await response.text();
  $("report-copy").disabled = false;
});
$("report-close").onclick = () => $("report-dialog").close();
for (const viewer of document.querySelectorAll(".report-viewer")) {
  viewer.addEventListener("keydown", e => {
    if (e.key.toLowerCase() !== "a" || !(e.metaKey || e.ctrlKey) || e.altKey) return;
    e.preventDefault();
    const range = document.createRange(); range.selectNodeContents(viewer);
    const selection = window.getSelection();
    selection.removeAllRanges(); selection.addRange(range);
  });
}
for (const button of [$("report-copy"), $("copy-report")]) {
  const labelElement = button.querySelector(".copy-label"), label = labelElement.textContent;
  const accessibleLabel = button.getAttribute("aria-label"), title = button.title;
  let feedbackTimer, copying = false;
  function feedback(status, text) {
    button.dataset.copyState = status; labelElement.textContent = text;
    button.setAttribute("aria-label", text);
  }
  button.setAttribute("aria-live", "polite");
  // Clipboard feedback is local to this button; it must not flash all run controls.
  button.onclick = async () => {
    if (copying) return;
    const rid = selectedRun, name = $("result-view").value;
    if (!rid || name === "console") return;
    copying = true;
    clearTimeout(feedbackTimer);
    button.disabled = true; button.setAttribute("aria-busy", "true");
    feedback("copying", "Copying…");
    try {
      const response = await fetch(`/api/runs/${rid}/reports/${name}`);
      if (!response.ok) throw new Error("Could not read saved report");
      const text = await response.text();
      try { await navigator.clipboard.writeText(text); }
      catch (_) { throw new Error("Clipboard is unavailable. Select text to copy, or download the report."); }
      feedback("copied", "Copied");
    } catch (e) {
      feedback("failed", "Copy failed"); button.title = e.message;
      error(e.message);
    } finally {
      copying = false;
      button.disabled = false; button.removeAttribute("aria-busy");
      feedbackTimer = setTimeout(() => {
        labelElement.textContent = label; delete button.dataset.copyState; button.title = title;
        if (accessibleLabel === null) button.removeAttribute("aria-label");
        else button.setAttribute("aria-label", accessibleLabel);
      }, button.dataset.copyState === "failed" ? 4000 : 2000);
    }
  };
}
$("arguments-expanded").addEventListener("input", updateArgumentsEditor);
$("arguments-apply").onclick = applyArguments;
$("arguments-cancel").onclick = () => $("arguments-dialog").close();
$("arguments-close").onclick = () => $("arguments-dialog").close();
$("code").addEventListener("scroll", syncScroll);
$("code").addEventListener("click", updateCursor);
$("code").addEventListener("keyup", updateCursor);
$("code").addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault(); const t = $("code"); t.setRangeText("    ",t.selectionStart,t.selectionEnd,"end"); highlight(); saveDraft();
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    if ($("arguments-dialog").open) applyArguments();
    else if (!$("report-dialog").open && !choices.isOpen()) run();
  }
});
$("run").onclick = run;
$("stop").onclick = () => action(async () => {
  const r = activeRun(); if (r) { const updated = await api(`/api/runs/${r.id}/cancel`, {}); Object.assign(r,updated); }
});
$("reset").onclick = () => { if (confirm("Replace this draft and arguments with the example?")) { setProfile(currentProfile, config.profiles.find(p => p.name === currentProfile).code, ""); saveDraft(); } };
$("clear").onclick = clearConsole;
$("import").onclick = () => $("file").click();
$("file").onchange = () => action(async () => {
  const file = $("file").files[0]; if (!file) return;
  if (file.size > 256*1024) throw new Error("Script is limited to 256 KiB");
  $("code").value = await file.text(); highlight(); saveDraft(); $("file").value = "";
});
$("download").onclick = () => {
  const url = URL.createObjectURL(new Blob([$("code").value], {type:"text/x-python"}));
  const a = document.createElement("a"); a.href = url; a.download = "solution.py"; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
};
function initPanelLayout() {
  const workbench = $("workbench"), resizer = $("panel-resizer"), expand = $("expand-editor");
  const storageKey = "molou-playground-editor-share", defaultShare = 0.7;
  let share = defaultShare, drag = null;
  try {
    const saved = Number(localStorage.getItem(storageKey));
    if (saved > 0 && saved < 1) share = saved;
  } catch (_) { /* resizing works without storage */ }
  const bounds = () => {
    const total = workbench.clientHeight - resizer.offsetHeight;
    return {total, min: 320, max: Math.max(320, total - 180)};
  };
  function updateAria() {
    if (workbench.classList.contains("editor-expanded")) return;
    const {total, min, max} = bounds();
    if (!total) return;
    const percent = Math.round($("editor-panel").offsetHeight / total * 100);
    $("output-panel").classList.toggle("compact-output", $("output-panel").clientHeight < 260);
    resizer.setAttribute("aria-valuemin", String(Math.ceil(min / total * 100)));
    resizer.setAttribute("aria-valuemax", String(Math.floor(max / total * 100)));
    resizer.setAttribute("aria-valuenow", String(percent));
    resizer.setAttribute("aria-valuetext", `Code editor ${percent} percent`);
  }
  function applyShare() {
    workbench.style.setProperty("--editor-share", `${share}fr`);
    workbench.style.setProperty("--output-share", `${1 - share}fr`);
    updateAria();
  }
  function resize(height) {
    const {total, min, max} = bounds();
    if (total <= 0) return;
    share = Math.max(min, Math.min(max, height)) / total;
    applyShare();
  }
  function save() {
    try { localStorage.setItem(storageKey, String(share)); } catch (_) { /* optional preference */ }
  }
  function finishDrag(cancel = false) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (cancel) { share = previous.share; applyShare(); }
    else save();
    document.body.classList.remove("resizing-panels");
    if (resizer.hasPointerCapture(previous.id)) resizer.releasePointerCapture(previous.id);
  }
  resizer.onpointerdown = e => {
    if (e.button !== 0 || drag) return;
    e.preventDefault(); resizer.focus({preventScroll:true});
    drag = {id:e.pointerId, y:e.clientY, height:$("editor-panel").offsetHeight, share};
    resizer.setPointerCapture(e.pointerId);
    document.body.classList.add("resizing-panels");
  };
  resizer.onpointermove = e => {
    if (drag?.id === e.pointerId) resize(drag.height + e.clientY - drag.y);
  };
  resizer.onpointerup = e => { if (drag?.id === e.pointerId) finishDrag(); };
  resizer.onpointercancel = e => { if (drag?.id === e.pointerId) finishDrag(true); };
  resizer.onlostpointercapture = () => finishDrag();
  resizer.ondblclick = () => { share = defaultShare; applyShare(); save(); };
  resizer.onkeydown = e => {
    const step = e.shiftKey ? 80 : 24, height = $("editor-panel").offsetHeight;
    if (e.key === "ArrowUp") resize(height - step);
    else if (e.key === "ArrowDown") resize(height + step);
    else if (e.key === "Home") resize(bounds().min);
    else if (e.key === "End") resize(bounds().max);
    else return;
    e.preventDefault(); save();
  };
  function setExpanded(expanded) {
    workbench.classList.toggle("editor-expanded", expanded);
    expand.textContent = expanded ? "Restore" : "Expand";
    expand.setAttribute("aria-pressed", String(expanded));
    expand.setAttribute("aria-label", expanded ? "Restore code editor size" : "Expand code editor");
    expand.title = expanded ? "Restore code editor size · Esc" : "Expand code editor";
    updateAria();
  }
  expand.onclick = () => setExpanded(!workbench.classList.contains("editor-expanded"));
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    if (drag) finishDrag(true);
    else if (!document.querySelector("dialog[open]") && !choices.isOpen()) setExpanded(false);
  });
  new ResizeObserver(updateAria).observe(workbench);
  applyShare();
}
initPanelLayout();
async function init() {
  try {
    config = await api("/api/config");
    options($("cluster"),config.clusters.map(c => [c.name,c.name]));
    options($("profile"),config.profiles.map(p => [p.name,p.label]));
    setProfile(config.profiles[0].name);
    state = await api("/api/state"); renderHistory();
    await action(loadNodes); setInterval(poll,800); setInterval(updateHistoryTimes,15000);
  } catch (e) { error(e.message); }
}
init();
