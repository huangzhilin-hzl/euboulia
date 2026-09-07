import {
  STORAGE_KEY,
  LEGACY_STORAGE_KEY,
  SEED_GOAL,
  currentGoal,
  scopeKey,
  inScope,
  enterRoom,
  createResearchRoom,
  createResearchGoal,
  updateGoalStatus,
  reviseResearchGoal,
  MEMBERS,
  ARTIFACTS,
  createRoom,
  restoreRoom,
  sendMessage,
  chooseDirection,
  updateTask,
  setGpuOnline,
} from "./room-state.mjs";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const paths = {
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  chat: '<path d="M5 5h14v11H9l-4 4V5Z"/>',
  activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  users:
    '<circle cx="9" cy="8" r="3"/><path d="M3 20v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 5"/>',
  computer:
    '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 21h8m-4-5v5"/>',
  book: '<path d="M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1Zm0 0v14"/>',
  panel:
    '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  file: '<path d="M5 3h9l5 5v13H5V3Zm9 0v6h5M8 13h8m-8 4h5"/>',
  trace: '<path d="M3 17h3V7h5v10h4V4h3v13h3"/>',
  compare: '<path d="M4 8h15m-4-4 4 4-4 4M20 16H5m4-4-4 4 4 4"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.file}</svg>`;
const avatar = (id) => {
  const m = MEMBERS.find((x) => x.id === id) || MEMBERS[0];
  return `<span class="avatar ${m.color}">${m.initials}</span>`;
};
const person = (id) => MEMBERS.find((x) => x.id === id)?.name || id;
const status = {
  done: "已交付",
  cancelled: "已取消",
  running: "进行中",
  queued: "待接力",
  paused: "已暂停",
  blocked: "等待算力连接",
};
let state;
try {
  state = restoreRoom(
    localStorage.getItem(STORAGE_KEY) ||
      localStorage.getItem(LEGACY_STORAGE_KEY),
  );
} catch {
  state = createRoom();
}
let page = state.page,
  panel = null,
  refs = [],
  noticeTimer,
  lastFocus = null;
function notice(text) {
  $("#notice").textContent = text;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => ($("#notice").textContent = ""), 5500);
}
function persist() {
  state.page = page;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    notice("浏览器存储不可用，当前改动仅保留到页面关闭。");
  }
}
function reference(id) {
  const a = ARTIFACTS[id];
  return a
    ? `<button class="reference" data-artifact="${id}">${icon(id === "trace" ? "trace" : id === "compare" ? "compare" : "file")}<span>${esc(a.kind)} · ${esc(a.title)}</span><span>↗</span></button>`
    : "";
}
function resultCard() {
  return `<button class="inline-result" data-artifact="compare" aria-label="打开 MHC 候选与 B0 实验对比"><div class="result-caption">${icon("compare")} TPOT · DSV4 / H20 / C1 <span>查看对比 ↗</span></div><div class="result-numbers"><strong class="old">41.8<small>ms</small></strong><span class="arrow">→</span><strong>38.6<small>ms</small></strong><span class="metric-gain">↓ 7.7%</span></div><div class="result-footer"><span>B0 → MHC-01 · 示例数据</span><span>正确性待补 · 暂不晋升</span></div></button>`;
}
function messageMarkup(m) {
  if (m.author === "system")
    return `<div class="system-message" id="${esc(m.id)}">${esc(m.text)}<div class="reference-row">${m.refs.map(reference).join("")}</div></div>`;
  const member = MEMBERS.find((x) => x.id === m.author);
  return `<article class="message" id="${esc(m.id)}">${avatar(m.author)}<div class="message-body"><div class="message-meta"><strong>${esc(person(m.author))}</strong>${member?.role.includes("Agent") ? '<span class="agent-tag">AGENT</span>' : ""}<time>${esc(m.time)}</time><span class="message-actions"><button data-reply="${esc(m.id)}">回复</button><button data-task-from="${esc(m.id)}">交给队友</button>${m.derivedGoal || m.kind === "goal" ? `<button data-goal="${esc(m.derivedGoal || m.goal)}">查看目标 ↗</button>` : `<button data-goal-from="${esc(m.id)}">转为目标</button>`}</span></div><p>${esc(m.text)}</p><div class="reference-row">${m.refs.map(reference).join("")}${m.task ? `<button class="reference" data-task="${esc(m.task)}">✓ 查看工作</button>` : ""}</div>${m.kind === "result" ? resultCard() : ""}</div></article>`;
}
function decisionMarkup() {
  return state.decision
    ? ""
    : `<section class="decision"><div class="decision-label"><span>◌</span> 需要你的方向</div><h3>下一步，先验证哪条线索？</h3><p>Atlas 和 Prism 已把两条路径准备好。选一个重点，团队接着推进。</p><div class="decision-buttons"><button class="button primary" data-direction="ranks">补采多 rank <span>↗</span></button><button class="button" data-direction="mhc">验证 MHC 在高并发的收益</button><button class="text-button muted" data-action="custom-direction">我有别的想法</button></div></section>`;
}
function conversation() {
  const messages = state.messages.filter((m) => inScope(state, m));
  if (!messages.length)
    return `<div class="empty"><div class="empty-icon">#</div><h2>从一个具体问题开始</h2><p>贴一段代码、引用已有证据，或者说说你想验证什么。这个研究室的讨论和工作会保留在一起。</p><button class="button" data-action="new-goal">把问题整理成目标</button></div>`;
  return `<div class="conversation"><div class="day-rule">今天 · 演示研究记录</div>${messages.map(messageMarkup).join("")}${currentGoal(state)?.id === SEED_GOAL && currentGoal(state).status === "active" ? decisionMarkup() : ""}</div>`;
}
function taskMarkup(t) {
  return `<button class="task-row" data-task="${esc(t.id)}"><span class="task-symbol ${esc(t.state)}">${{ done: "✓", running: "◌", paused: "Ⅱ", blocked: "!", queued: "·", cancelled: "−" }[t.state]}</span><span><strong>${esc(t.title)}</strong><small>${esc(person(t.owner))} · ${t.gpu ? "H20 × 8 / Pod" : "Computer 上的分析工作"}</small></span><span class="task-state">${status[t.state]}</span>${avatar(t.owner)}</button>`;
}
function work() {
  const tasks = state.tasks.filter((t) => inScope(state, t));
  return `<div class="page-body"><div class="page-intro"><div><h2>工作在这里接力</h2><p>从讨论中产生，带着上下文继续。</p></div><button class="button" data-action="new-task">${icon("plus")}交给队友</button></div><div class="filter-row">${[
    ["all", "全部"],
    ["running", "进行中"],
    ["queued", "待接力"],
    ["blocked", "受阻"],
    ["paused", "已暂停"],
    ["done", "已交付"],
    ["cancelled", "已取消"],
  ]
    .map(
      ([v, l]) =>
        `<button data-filter="${v}" class="${state.filter === v ? "active" : ""}">${l}</button>`,
    )
    .join("")}</div>${
    tasks
      .filter((t) => state.filter === "all" || t.state === state.filter)
      .map(taskMarkup)
      .join("") ||
    '<div class="empty"><p>这个视图还没有工作。你可以从讨论中发起。</p></div>'
  }</div>`;
}
function artifacts() {
  const cited = new Set(
    state.messages.filter((m) => inScope(state, m)).flatMap((m) => m.refs),
  );
  const entries = Object.entries(ARTIFACTS).filter(
    ([id]) =>
      currentGoal(state)?.id === SEED_GOAL ||
      currentGoal(state)?.refs.includes(id) ||
      cited.has(id),
  );
  return `<div class="page-body"><div class="page-intro"><div><h2>能继续工作的产物</h2><p>每份证据都能回到原来的讨论，也能被下一项工作引用。</p></div></div>${
    !entries.length
      ? '<div class="empty"><p>还没有产物。你引用的证据会随工作一起保留。</p></div>'
      : `<div class="artifact-grid">${entries
          .map(
            ([id, a]) =>
              `<button class="artifact-card" data-artifact="${id}"><span class="eyebrow">${esc(a.kind)} · DEMO</span><strong>${esc(a.title)}</strong><p>${esc(a.summary)}</p><small>${esc(a.ref)} ↗</small></button>`,
          )
          .join("")}</div>`
  }</div>`;
}
function mapView() {
  if (currentGoal(state)?.id !== SEED_GOAL) return goalMap();
  return `<div class="page-body"><div class="page-intro"><div><h2>看看工作如何连接</h2><p>研究关系概览 · 点击打开对应证据或工作。</p></div></div><div class="relationship-map"><button class="map-node" data-action="context">把 DSV4 的解码再快一点<small>研究方向 · 正确性优先</small></button><span class="map-edge"></span><div class="map-split"><button class="map-node" data-artifact="compare">已有基线与 MHC 候选<small>无 profile 比较 · 正确性待补</small></button><button class="map-node" data-artifact="trace">Decode 等待线索<small>单 rank 观察 · 原因待验证</small></button></div><span class="map-edge"></span><button class="map-node" data-action="show-work">${state.decision === "ranks" ? "补齐多 rank 证据" : state.decision === "mhc" ? "验证 C1 / C16 的收益" : "两条路径，等待研究方向"}<small>${state.decision ? "来自你的决定 · 已进入工作队列" : "讨论决定下一步"}</small></button></div><p class="map-note">这张图从研究记录中整理，当前为示例概览。<br>精确任务依赖、版本和输入契约在执行层维护。</p><p class="map-note"><a href="network.html" target="_blank" rel="noopener">查看 v0.1 任务依赖原型 ↗</a></p></div>`;
}
function membersPage() {
  return `<div class="page-body"><div class="page-intro"><div><h2>认识一起工作的队友</h2><p>职责、上下文和经验跟着队友保留；运行时放在详情里。</p></div></div><div class="team-grid">${MEMBERS.map((m) => `<button class="person-card" data-member="${m.id}"><div class="person-meta">${avatar(m.id)}<div><strong>${m.name}</strong><p>${m.role}</p></div></div><p>${{ julian: "确定方向，判断哪些证据值得继续追。", atlas: "连接代码、环境与可复现的系统实验。", prism: "检查性能证据，区分观察和解释。", lin: "把热点分析变成值得验证的 kernel 假设。" }[m.id]}</p><div class="tag-row"><span class="tag">${m.computer}</span><span class="tag">${m.status} · 演示</span></div></button>`).join("")}</div></div>`;
}
function computersPage() {
  return `<div class="page-body"><div class="page-intro"><div><h2>队友工作的地方</h2><p>Computer 承载 Agent 的代码、工具和记忆；GPU 实验通过 Pod 执行。</p></div></div><div class="computer-grid"><button class="computer-card" data-computer="local">${icon("computer")}<strong>Local Mac</strong><p>Atlas 的持续工作环境<br>代码与上下文就近保留</p><div class="tag-row"><span class="tag">Codex runtime</span><span class="tag">在线 · 演示</span></div></button><button class="computer-card" data-computer="lab">${icon("computer")}<strong>Lab connector</strong><p>Prism 的持续工作环境<br>经 Kubernetes 连接 H20 算力池</p><div class="tag-row"><span class="tag">Claude runtime</span><span class="tag">Computer 在线 · 演示</span></div></button></div><div class="memory-card"><div class="eyebrow">GPU 执行环境</div><h3>H20 × 8 <span class="tag">${state.gpuOnline ? "已连接" : "连接中断"} · 演示</span></h3><p>${state.gpuOnline ? "独占测量，基线与候选沿用同一 workload。" : "相关 GPU 工作暂停派发。真实系统必须先核实已有 Pod，再恢复调度。"}</p><button class="button" data-action="toggle-gpu">演示${state.gpuOnline ? "算力连接中断" : "连接恢复"}</button></div></div>`;
}
function memoryPage() {
  return `<div class="page-body"><div class="page-intro"><div><h2>让下一次少走弯路</h2><p>保留适用条件、证据和反例，队友在新工作中按需引用。</p></div></div><article class="memory-card"><div class="eyebrow">团队经验 · 示例</div><h3>低并发下的收益，不能直接外推到高并发</h3><p>DSV4 / H20 上的历史反例提示，C1 的收益可能在 C16 消失。MHC 候选需要同时测量两种并发，才能讨论适用范围。</p><div class="tag-row"><span class="tag">DSV4</span><span class="tag">H20</span><span class="tag">C1 → C16</span><span class="tag">有反例</span></div><button class="button" data-artifact="memory">打开证据与适用范围 ↗</button><button class="button" data-action="use-memory">带进当前研究室</button></article>${state.savedMemory ? '<article class="memory-card"><div class="eyebrow">新草稿 · 尚未复现</div><h3>MHC 候选：TPOT 降低 7.7%</h3><p>示例数据，仅适用于当前 C1 测量。正确性和 C16 证据待补；不能作为已验证经验使用。</p><button class="button" data-artifact="compare">查看来源</button></article>' : ""}</div>`;
}
function activityPage() {
  return `<div class="page-body"><div class="page-intro"><div><h2>回来的时候，知道该看哪里</h2><p>聚合发现、阻塞和你的决定。底层运行记录按需打开。</p></div></div>${!state.decision && state.goals.find((g) => g.id === SEED_GOAL)?.status === "active" ? `<div class="activity-item"><small>dsv4-on-h20 · 需要你的方向</small>两条路径已经准备好：补采多 rank，或验证 MHC。<br><button class="button" data-action="attention">回到讨论</button></div>` : ""}${state.messages
    .slice()
    .reverse()
    .filter((m) => m.author === "system" || m.kind)
    .map(
      (m) =>
        `<div class="activity-item"><small>${esc(m.time)} · ${esc(state.rooms.find((r) => r.id === m.room)?.name || m.room)}${m.goal ? " / " + esc(state.goals.find((g) => g.id === m.goal)?.title || "") : " / 自由讨论"} · ${m.author === "system" ? "研究记录" : esc(person(m.author))}</small>${esc(m.text)}<div class="reference-row">${m.refs.map(reference).join("")}</div><button class="text-button" data-message-link="${esc(m.id)}">回到讨论 ↗</button></div>`,
    )
    .join("")}</div>`;
}
function saveDraft() {
  state.drafts[scopeKey(state)] = {
    text: $("#message").value,
    refs: [...refs],
    asTask: $("#as-task").checked,
    owner: $("#task-owner").value,
  };
  persist();
}
function loadDraft() {
  const d = state.drafts[scopeKey(state)] || {};
  $("#message").value = typeof d.text === "string" ? d.text : "";
  refs = Array.isArray(d.refs) ? d.refs.filter((id) => ARTIFACTS[id]) : [];
  $("#as-task").checked = Boolean(d.asTask);
  $("#task-owner").value = MEMBERS.some((m) => m.id === d.owner)
    ? d.owner
    : "atlas";
  renderComposer();
}
function renderComposer() {
  $("#context-chips").innerHTML = refs
    .map(
      (id) =>
        `<span class="reference">${icon("file")}${esc(ARTIFACTS[id].title)}<button type="button" data-remove-ref="${id}" aria-label="移除引用 ${esc(ARTIFACTS[id].title)}">×</button></span>`,
    )
    .join("");
  $("#send").disabled = !$("#message").value.trim();
  const inactive = currentGoal(state) && currentGoal(state).status !== "active";
  $("#as-task").disabled = Boolean(inactive);
  if (inactive) $("#as-task").checked = false;
  $("#task-owner").hidden = !$("#as-task").checked;
  $("#message").placeholder = currentGoal(state)
    ? `围绕这个目标继续讨论，@ 队友或引用证据…`
    : "先聊一个问题，也可以把讨论转为目标…";
}
function render() {
  $$("[data-icon]").forEach((el) => (el.innerHTML = icon(el.dataset.icon)));
  $("#member-list").innerHTML = MEMBERS.map(
    (m) =>
      `<button class="member-button" data-member="${m.id}">${avatar(m.id)}<span><strong>${m.name}</strong><small>${m.role}</small></span><i class="presence ${m.id === "lin" ? "away" : m.id === "prism" ? "busy" : ""}"></i></button>`,
  ).join("");
  $$(".rail-button[data-page]").forEach((b) => {
    const active =
      b.dataset.page === page ||
      (b.dataset.page === "rooms" && page === "room");
    b.classList.toggle("active", active);
    b.setAttribute("aria-current", active ? "page" : "false");
  });
  $("#room-total").textContent = state.rooms.length;
  $("#room-list").innerHTML = state.rooms
    .map(
      (r) =>
        `<button class="room-link ${r.id === state.room && page === "room" ? "selected" : ""}" data-room="${esc(r.id)}"><span class="hash">#</span><span>${esc(r.name)}</span><small>${state.goals.filter((g) => g.room === r.id && g.status === "active").length || ""}</small></button>`,
    )
    .join("");
  const room = state.rooms.find((r) => r.id === state.room);
  const goal = currentGoal(state);
  $("#room-name").textContent =
    page === "room"
      ? room.name
      : {
          rooms: "全部研究室",
          members: "团队",
          computers: "计算机",
          memory: "经验",
          activity: "动态",
        }[page];
  $("#room-heading").textContent =
    page === "room"
      ? goal?.title || room.name
      : {
          rooms: "今天，想推进什么问题？",
          members: "人和 Agent，共享一个研究室",
          computers: "连接工作环境，接着往下做",
          memory: "把一次研究变成下一次的起点",
          activity: "研究室正在发生什么",
        }[page];
  $("#room-controls").hidden = page !== "room";
  $("#new-goal-trigger").hidden = page !== "room";
  $('.header-actions [data-action="context"]').hidden = page !== "room";
  $("#goal-strip").hidden = !goal;
  $("#goal-strip").innerHTML = goal
    ? `<button class="text-button" data-room="${esc(state.room)}">← 全部目标</button><span class="tag">${goalStatus[goal.status]}</span><span>${esc(person(goal.owner))} 负责</span><button class="text-button" data-action="goal-details">目标详情 ↗</button>`
    : "";
  $("#scope-context").hidden = state.view === "overview";
  $("#scope-context").innerHTML =
    `<button data-action="context">${esc(goal ? "目标上下文" : "研究室自由讨论 · 未关联目标")}${icon("chevron")}</button>${goal?.id === SEED_GOAL && goal.status === "active" && !state.decision ? '<button class="needs-direction" data-action="attention">◌ 1 个方向待定 ↗</button>' : ""}`;
  $("#task-count").textContent = state.tasks.filter((t) =>
    inScope(state, t),
  ).length;
  $$(".room-tabs button").forEach((b) => {
    const selected = b.dataset.view === state.view;
    b.setAttribute("aria-selected", selected);
    b.tabIndex = selected ? 0 : -1;
  });
  $("#content").innerHTML =
    page === "room"
      ? (
          {
            overview: roomOverview,
            conversation,
            tasks: work,
            artifacts,
            map: mapView,
          }[state.view] || conversation
        )()
      : {
          rooms: roomsPage,
          members: membersPage,
          computers: computersPage,
          memory: memoryPage,
          activity: activityPage,
        }[page]();
  $("#content").setAttribute("role", page === "room" ? "tabpanel" : "region");
  if (page === "room")
    $("#content").setAttribute("aria-labelledby", `tab-${state.view}`);
  else $("#content").removeAttribute("aria-labelledby");
  $("#composer-wrap").hidden = page !== "room" || state.view !== "conversation";
  $("#suggestions").hidden =
    currentGoal(state)?.id !== SEED_GOAL ||
    currentGoal(state)?.status !== "active";
  renderComposer();
  persist();
}
function showPanel(type, html) {
  lastFocus = document.activeElement;
  $("#inspector-type").textContent = type;
  $("#inspector-content").innerHTML = html;
  $("#inspector").hidden = false;
  $(".shell").classList.add("with-panel");
  $("#inspector").scrollTop = 0;
  $('#inspector [data-action="close-panel"]').focus({ preventScroll: true });
}
function closePanel() {
  $("#inspector").hidden = true;
  $(".shell").classList.remove("with-panel");
  panel = null;
  if (lastFocus?.isConnected) lastFocus.focus({ preventScroll: true });
}
const meta = (rows) =>
  `<div class="meta-list">${rows.map(([a, b]) => `<div class="meta-row"><span>${esc(a)}</span><span>${esc(b)}</span></div>`).join("")}</div>`;
function openArtifact(id) {
  const a = ARTIFACTS[id];
  if (!a) return;
  panel = { kind: "artifact", id };
  let body = "";
  if (id === "compare")
    body = `${[
      ["TPOT ↓", "41.8", "38.6", "ms", "7.7%"],
      ["吞吐 ↑", "191.4", "207.3", "tok/s", "8.3%"],
    ]
      .map(
        ([label, b, c, unit, gain]) =>
          `<section class="compare-metric"><div><span>${label}</span><span>${gain} 改善</span></div><div class="bar-row baseline"><span>B0</span><span class="bar-track"><i style="width:${(Number(b) / Math.max(Number(b), Number(c))) * 100}%"></i></span><span>${b}</span></div><div class="bar-row"><span>MHC-01</span><span class="bar-track"><i style="width:${(Number(c) / Math.max(Number(b), Number(c))) * 100}%"></i></span><span>${c}</span></div><p>单位 ${unit} · 固定演示值</p></section>`,
      )
      .join("")}${meta([
      ["工作负载", "16K 输入 / 256 输出 / C1"],
      ["运行环境", "H20 × 8 · SGLang"],
      ["测量通道", "无 profiling · 示例"],
      ["正确性", "待补齐"],
      ["结论", "候选保留，尚不能晋升"],
    ])}<div class="callout">缺少真实重复测量与置信区间。这里的差值只用于演示界面，不能作为真实性能结论。</div><button class="button" data-action="save-memory">${state.savedMemory ? "已保存经验草稿" : "保存为经验草稿"}</button>`;
  if (id === "trace")
    body = `<div class="trace"><div class="trace-scale"><span>12 ms</span><span>14 ms</span><span>16 ms</span></div><div class="trace-line"><span>rank 0</span><div class="trace-bands"><i>GEMM</i><button data-action="cite-window" aria-label="引用 rank 0 的 12–16 ms 等待窗口">等待 · 引用</button><i>MoE</i></div></div><div class="trace-line"><span>rank 1</span><div class="trace-bands"><i class="wait">缺少采样</i></div></div></div><p>示意时间线 · 不是原始 trace</p><div class="document-block"><strong>观察</strong><br>rank 0 在 decode 阶段存在等待。<br><br><strong>还不能证明</strong><br>等待由通信算法、计算偏斜还是输入不均造成。<br><br><strong>下一份证据</strong><br>相同 workload 的多 rank 采样与计算区间对齐。</div>`;
  if (id === "patch")
    body = `<div class="document-block"><strong>要验证的假设</strong><br>合并相邻处理，可能减少 MHC 中间张量访存。<br><br><strong>涉及的改动</strong><br>处理边界、张量生命周期和 kernel 调用次数。当前没有附加真实代码 diff。<br><br><strong>放弃条件</strong><br>数值不通过，或独立端到端收益消失。</div><button class="button" data-task="correctness">查看正确性工作 ↗</button>`;
  if (id === "memory")
    body = `${meta([
      ["适用条件", "DSV4 / H20 / 当前 workload"],
      ["支持证据", "历史比较 · 示例"],
      ["反例", "C16 出现回退 · 示例"],
      ["使用方式", "验证同时覆盖 C1 与 C16"],
    ])}<div class="document-block">这是一条带边界的研究提醒。模型、硬件、并发或 kernel 版本改变时，应重新验证。</div>`;
  showPanel(
    a.kind,
    `<div class="eyebrow">${esc(a.ref)} · DEMO</div><h2>${esc(a.title)}</h2><p>${esc(a.summary)}</p>${body}<button class="button primary" data-cite="${id}">引用到讨论 ${icon("plus")}</button><button class="button" data-origin="${id}">回到原讨论 ↗</button>`,
  );
}
function openTask(id) {
  const t = state.tasks.find((t) => t.id === id);
  if (!t) return;
  if (!inScope(state, t) || page !== "room")
    navigate(t.room, t.goal || null, "tasks");
  panel = { kind: "task", id };
  showPanel(
    "工作 · 本地演示",
    `<div class="eyebrow">${esc(t.id)}</div><h2>${esc(t.title)}</h2><p>${esc(t.description)}</p>${meta(
      [
        ["负责人", person(t.owner)],
        ["当前状态", status[t.state]],
        [
          "关联目标",
          state.goals.find((g) => g.id === t.goal)?.title || "研究室自由讨论",
        ],
        ["执行环境", t.gpu ? "H20 × 8 / 隔离 Pod" : "Computer"],
        ["来源", t.source ? "研究室消息" : "已有研究记录"],
        ...(t.goal
          ? [
              [
                "依据的目标说明",
                `第 ${t.goalRevision || 1} 版（当前第 ${state.goals.find((g) => g.id === t.goal)?.revision || 1} 版）`,
              ],
            ]
          : []),
      ],
    )}<div class="reference-row">${t.refs.map(reference).join("")}</div>${t.state === "blocked" ? '<div class="callout">H20 连接中断。真实执行恢复前须核对原 Pod；此原型只演示阻塞与回到队列。</div>' : ""}<div class="decision-buttons">${t.state === "queued" ? `<button class="button primary" data-task-action="start" data-id="${esc(t.id)}">演示开始</button>` : ""}${["queued", "running", "blocked"].includes(t.state) ? `<button class="button" data-task-action="pause" data-id="${esc(t.id)}">暂停演示任务</button>` : ""}${t.state === "paused" ? `<button class="button primary" data-task-action="resume" data-id="${esc(t.id)}">恢复演示任务</button>` : ""}${t.state === "running" ? `<button class="button primary" data-deliver="${esc(t.id)}">记录交付</button>` : ""}${!["done", "cancelled"].includes(t.state) ? `<button class="text-button" data-task-action="cancel" data-id="${esc(t.id)}">取消任务</button>` : ""}<button class="button" data-steer-task="${esc(t.id)}">补充方向 ↗</button>${t.source ? `<button class="text-button" data-message-link="${esc(t.source)}">回到来源消息 ↗</button>` : ""}</div>${t.result ? `<div class="document-block"><strong>交付说明 · 人工记录</strong><p>${esc(t.result)}</p></div>` : ""}`,
  );
}
function openMember(id) {
  const m = MEMBERS.find((m) => m.id === id);
  if (!m) return;
  panel = { kind: "member", id };
  const agent = m.role.includes("Agent");
  showPanel(
    "队友",
    `${avatar(m.id)}<h2>${m.name}</h2><p>${m.role}</p>${meta([
      ["状态", `${m.status} · 演示`],
      ["工作环境", m.computer],
      ...(agent
        ? [
            ["运行时", m.id === "atlas" ? "Codex" : "Claude"],
            ["上下文", "研究室、源代码、已引用证据"],
            ["经验", "适用条件和反例随工作引用"],
            [
              "今日 token",
              m.id === "atlas" ? "24k / 100k · 示例" : "16k / 80k · 示例",
            ],
          ]
        : []),
    ])}<p>${agent ? "身份与记忆保留在工作环境中；运行时选择与会话重启放在管理层。" : "人类同样可以提出、接手和交付工作。"}</p><button class="button primary" data-mention-person="${id}">@ ${m.name} 参与讨论</button>`,
  );
}
function openComputer(id) {
  panel = { kind: "computer", id };
  const local = id === "local";
  showPanel(
    "Computer · 演示",
    `${icon("computer")}<h2>${local ? "Local Mac" : "Lab connector"}</h2><p>为队友提供持续的工作环境。</p>${meta(
      [
        ["连接状态", "在线 · 演示"],
        ["队友", local ? "Atlas" : "Prism"],
        ["运行时", local ? "Codex" : "Claude"],
        ["可用上下文", "项目目录 / 研究记录 / 经验"],
        [
          "GPU 工作",
          local ? "不在此 Computer 上运行" : "通过 Kubernetes 创建 Pod",
        ],
      ],
    )}<button class="button" data-member="${local ? "atlas" : "prism"}">查看队友 ↗</button><div class="callout">该页面使用固定示例，未连接真实机器，也不会生成连接命令或更改权限。</div>`,
  );
}
function contextPanel() {
  if (currentGoal(state)) return openGoalDetails();
  const room = state.rooms.find((r) => r.id === state.room);
  panel = { kind: "context" };
  showPanel(
    "研究室上下文",
    `<h2>${esc(room.name)}</h2><p>${esc(room.description || "在这里一起推进研究问题。")}</p><div class="document-block">${esc(room.context || "尚未设置模型、硬件或工作负载。新目标不会继承其他研究室的场景。")}</div><p>新目标会保存这份上下文的快照；每个目标的讨论、工作和结论独立保留。</p><button class="button primary" data-action="new-goal">发起目标 ↗</button>`,
  );
}

function cite(id) {
  if (!ARTIFACTS[id]) return;
  closePanel();
  page = "room";
  state.view = "conversation";
  if (!refs.includes(id)) refs.push(id);
  render();
  saveDraft();
  $("#message").focus();
}
function moveToMessage(id) {
  const m = state.messages.find((m) => m.id === id);
  if (!m) return;
  saveDraft();
  enterRoom(state, m.room, m.goal || null);
  page = "room";
  state.view = "conversation";
  loadDraft();
  render();
  closePanel();
  const el = document.getElementById(id);
  el?.scrollIntoView({ block: "center" });
  el?.classList.add("highlight");
  setTimeout(() => el?.classList.remove("highlight"), 2000);
}
function search() {
  const query = $("#search-input").value.toLowerCase().trim();
  const results = [
    ...state.rooms.map((r) => ({
      label: r.name,
      detail: `研究室 · ${r.description}`,
      type: "room",
      id: r.id,
    })),
    ...state.goals.map((g) => ({
      label: g.title,
      detail: `目标 · ${goalStatus[g.status]} · ${state.rooms.find((r) => r.id === g.room)?.name}`,
      type: "goal",
      id: g.id,
    })),
    ...state.tasks.map((t) => ({
      label: t.title,
      detail: `工作 · ${status[t.state]} · ${roomName(t.room)} / ${state.goals.find((g) => g.id === t.goal)?.title || "自由讨论"}`,
      type: "task",
      id: t.id,
    })),
    ...Object.entries(ARTIFACTS).map(([id, a]) => ({
      label: a.title,
      detail: a.kind,
      type: "artifact",
      id,
    })),
    ...state.messages
      .filter((m) => m.author !== "system")
      .map((m) => ({
        label: m.text,
        detail: `讨论 · ${person(m.author)} · ${roomName(m.room)} / ${state.goals.find((g) => g.id === m.goal)?.title || "自由讨论"}`,
        type: "message",
        id: m.id,
      })),
  ]
    .filter(
      (x) => !query || `${x.label} ${x.detail}`.toLowerCase().includes(query),
    )
    .slice(0, 18);
  $("#search-results").innerHTML = results.length
    ? results
        .map(
          (x) =>
            `<button class="search-result" data-search-type="${x.type}" data-search-id="${esc(x.id)}"><small>${esc(x.detail)}</small>${esc(x.label.slice(0, 105))}</button>`,
        )
        .join("")
    : '<div class="empty"><p>没有匹配结果。试试“等待”“MHC”或队友名字。</p></div>';
}
function openSearch() {
  lastFocus = document.activeElement;
  $("#search-dialog").showModal();
  $("#search-input").value = "";
  search();
  $("#search-input").focus();
}
function inputText(
  text,
  { asTask = false, owner = "atlas", citations = [] } = {},
) {
  if (asTask && currentGoal(state) && currentGoal(state).status !== "active")
    throw Error("请先恢复目标，再交办工作。");
  page = "room";
  state.view = "conversation";
  $("#message").value = text;
  $("#as-task").checked = asTask;
  $("#task-owner").value = owner;
  refs = [...new Set([...refs, ...citations])];
  render();
  saveDraft();
  closePanel();
  $("#message").focus();
}
function showMention() {
  const term =
    $("#message")
      .value.match(/@([a-z]*)$/i)?.[1]
      ?.toLowerCase() || "";
  $("#mention-menu").hidden = false;
  $("#mention-menu").innerHTML = MEMBERS.filter((m) =>
    m.name.toLowerCase().startsWith(term),
  )
    .map(
      (m) =>
        `<button type="button" data-mention-person="${m.id}">${avatar(m.id)}${m.name}<small>${m.role.includes("Agent") ? "Agent" : "人类"}</small></button>`,
    )
    .join("");
}
function attention() {
  saveDraft();
  enterRoom(state, "dsv4", SEED_GOAL);
  page = "room";
  state.view = "conversation";
  loadDraft();
  render();
  closePanel();
  $(".decision")?.scrollIntoView({ block: "center" });
  if (state.decision)
    notice("当前没有待定研究方向。新增阻塞可在“工作”中查看。");
}
const actions = {
  search: openSearch,
  "new-room": () => openFlow("room"),
  "new-goal": () => openFlow("goal"),
  "close-flow": () => $("#flow-dialog").close(),
  "goal-details": openGoalDetails,
  "free-discussion": () => navigate(state.room, null, "conversation"),
  "close-dialog": () => $("#search-dialog").close(),
  "close-panel": closePanel,
  sidebar: () => $(".sidebar").classList.toggle("show"),
  context: contextPanel,
  reset: () => {
    state = createRoom();
    page = "rooms";
    refs = [];
    $(".sidebar").classList.remove("show");
    closePanel();
    loadDraft();
    render();
    notice("本地演示已重置。");
  },
  "custom-direction": () => inputText("我想调整下一步："),
  "new-task": () => inputText("", { asTask: true }),
  "show-work": () => {
    state.view = "tasks";
    render();
  },
  "seed-kernel": () =>
    inputText("@Atlas 帮我分析这个 kernel 的访存，先提出可验证的假设。", {
      asTask: true,
    }),
  "toggle-gpu": () => {
    setGpuOnline(state, !state.gpuOnline);
    render();
    notice(
      state.gpuOnline
        ? "演示连接已恢复；GPU 工作回到队列。"
        : "演示连接中断；相关 GPU 工作显示受阻。",
    );
  },
  "save-memory": () => {
    state.savedMemory = true;
    persist();
    openArtifact("compare");
    notice("已保存为未验证的经验草稿，保留缺失证据。");
  },
  "use-memory": () => cite("memory"),
  "cite-window": () => {
    cite("trace");
    $("#message").value =
      "@Prism 请解释 rank 0 在 12–16 ms 的等待，先核对其他 rank 的对应窗口。";
    $("#task-owner").value = "prism";
    renderComposer();
    saveDraft();
  },
  mention: () => {
    $("#message").focus();
    if (!$("#message").value.endsWith("@")) $("#message").value += "@";
    showMention();
    renderComposer();
  },
  attach: () => {
    panel = { kind: "attach" };
    showPanel(
      "引用证据",
      `<h2>把证据带进下一步</h2><p>队友收到消息时，也能定位到同一个上下文。</p>${Object.entries(
        ARTIFACTS,
      )
        .map(
          ([id, a]) =>
            `<button class="task-row" data-cite="${id}"><span><strong>${esc(a.title)}</strong><small>${esc(a.kind)} · ${esc(a.ref)}</small></span><span>＋</span></button>`,
        )
        .join("")}`,
    );
  },
  attention,
};
document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const d = b.dataset;
  try {
    if (d.page) {
      saveDraft();
      page = d.page;
      $(".sidebar").classList.remove("show");
      closePanel();
      render();
    } else if (d.room) {
      navigate(d.room);
    } else if (d.goal) {
      const goal = state.goals.find((g) => g.id === d.goal);
      if (goal) navigate(goal.room, goal.id);
    } else if (d.goalFrom) {
      const origin = state.messages.find((m) => m.id === d.goalFrom);
      if (origin) {
        if (!inScope(state, origin)) navigate(origin.room, origin.goal || null);
        openFlow("goal", origin.id);
      }
    } else if (d.messageLink) moveToMessage(d.messageLink);
    else if (d.goalAction) {
      if (d.goalAction === "complete") openFlow("conclusion", state.goal);
      else if (d.goalAction === "revise") openFlow("revise", state.goal);
      else {
        updateGoalStatus(state, state.goal, d.goalAction);
        render();
        openGoalDetails();
      }
    } else if (d.deliver) openFlow("delivery", d.deliver);
    else if (d.goalExample) {
      $("#goal-prompt").value =
        d.goalExample === "bottleneck"
          ? "弄清当前推理场景的主要瓶颈，给出有证据的解释和下一步建议。"
          : "验证一个优化改动是否有效，检查正确性与不同负载下的表现。";
      saveFlowDraft();
      $("#goal-prompt").focus();
    } else if (d.view) {
      if (d.view === "overview") {
        navigate(state.room);
        return;
      }
      state.view = d.view;
      closePanel();
      render();
    } else if (d.artifact) openArtifact(d.artifact);
    else if (d.member) openMember(d.member);
    else if (d.computer) openComputer(d.computer);
    else if (d.task) openTask(d.task);
    else if (d.action) actions[d.action]?.();
    else if (d.direction) {
      chooseDirection(state, d.direction);
      render();
      $("#content").scrollTop = $("#content").scrollHeight;
      notice("已记录研究方向，并创建带证据引用的演示工作。");
    } else if (d.preset) {
      if (d.preset === "ranks")
        inputText(
          "@Prism 先查 rank 间等待，补齐多 rank 证据后再决定是否改通信 kernel。",
          { asTask: true, owner: "prism", citations: ["trace"] },
        );
      else if (d.preset === "compare") openArtifact("compare");
      else attention();
    } else if (d.cite) cite(d.cite);
    else if (d.origin) {
      const m = state.messages.find((m) => m.refs.includes(d.origin));
      if (m) moveToMessage(m.id);
    } else if (d.removeRef) {
      refs = refs.filter((id) => id !== d.removeRef);
      renderComposer();
      saveDraft();
    } else if (d.reply || d.taskFrom) {
      const m = state.messages.find((m) => m.id === (d.reply || d.taskFrom));
      if (m)
        inputText(`@${person(m.author)} 关于「${m.text.slice(0, 38)}」：`, {
          asTask: Boolean(d.taskFrom),
          owner: MEMBERS.some((p) => p.id === m.author) ? m.author : "atlas",
          citations: m.refs,
        });
    } else if (d.mentionPerson) {
      const m = MEMBERS.find((m) => m.id === d.mentionPerson);
      if (m) {
        const v = $("#message").value;
        inputText(
          /@[a-z]*$/i.test(v)
            ? v.replace(/@[a-z]*$/i, `@${m.name} `)
            : `${v}${v ? " " : ""}@${m.name} `,
          { asTask: $("#as-task").checked, owner: m.id },
        );
        $("#mention-menu").hidden = true;
      }
    } else if (d.filter) {
      state.filter = d.filter;
      render();
    } else if (d.taskAction) {
      updateTask(state, d.id, d.taskAction);
      render();
      openTask(d.id);
    } else if (d.steerTask) {
      const t = state.tasks.find((t) => t.id === d.steerTask);
      if (t)
        inputText(`@${person(t.owner)} 关于「${t.title}」，我想补充：`, {
          citations: t.refs,
        });
    } else if (d.searchType) {
      $("#search-dialog").close();
      if (d.searchType === "task") openTask(d.searchId);
      else if (d.searchType === "artifact") openArtifact(d.searchId);
      else if (d.searchType === "room") navigate(d.searchId);
      else if (d.searchType === "goal") {
        const goal = state.goals.find((g) => g.id === d.searchId);
        if (goal) navigate(goal.room, goal.id);
      } else moveToMessage(d.searchId);
    }
  } catch (error) {
    notice(error.message);
  }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  let m;
  try {
    m = sendMessage(state, {
      text: $("#message").value,
      refs,
      asTask: $("#as-task").checked,
      owner: $("#task-owner").value,
    });
  } catch (error) {
    notice(error.message);
    return;
  }
  if (!m) return;
  $("#message").value = "";
  $("#as-task").checked = false;
  $("#mention-menu").hidden = true;
  refs = [];
  saveDraft();
  render();
  $("#content").scrollTop = $("#content").scrollHeight;
  $("#message").focus();
});
function matchAssignee() {
  const name = $("#message")
    .value.match(/^@([a-z]+)\s/i)?.[1]
    ?.toLowerCase();
  const member = MEMBERS.find((m) => m.name.toLowerCase() === name);
  if (member) $("#task-owner").value = member.id;
}
$("#message").addEventListener("input", () => {
  matchAssignee();
  renderComposer();
  saveDraft();
  if (/@[a-z]*$/i.test($("#message").value)) showMention();
  else $("#mention-menu").hidden = true;
});
$("#as-task").addEventListener("change", () => {
  matchAssignee();
  renderComposer();
  saveDraft();
});
$("#task-owner").addEventListener("change", saveDraft);
$("#message").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
  if (e.key === "Escape") $("#mention-menu").hidden = true;
});
$("#search-input").addEventListener("input", search);
$(".room-tabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const tabs = $$(".room-tabs button");
  let i = tabs.indexOf(document.activeElement);
  i =
    e.key === "Home"
      ? 0
      : e.key === "End"
        ? tabs.length - 1
        : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  if (tabs[i].dataset.view === "overview") navigate(state.room);
  else {
    state.view = tabs[i].dataset.view;
    render();
  }
  tabs[i].focus();
});
document.addEventListener("keydown", (e) => {
  if ($("#appearance-dialog")?.open || $("#flow-dialog").open) return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (!$("#search-dialog").open) openSearch();
  }
  if (e.key === "Escape" && !$("#search-dialog").open) {
    closePanel();
    $("#mention-menu").hidden = true;
    $(".sidebar").classList.remove("show");
  }
});
const goalStatus = { active: "研究中", paused: "已暂停", completed: "已结束" };
const roomName = (id) => state.rooms.find((r) => r.id === id)?.name || id;
let flow = null;
function navigate(room, goal = null, view = null) {
  saveDraft();
  enterRoom(state, room, goal);
  if (view) state.view = view;
  page = "room";
  closePanel();
  $(".sidebar").classList.remove("show");
  loadDraft();
  render();
  $("#content").scrollTop = 0;
}
function goalCard(g) {
  const tasks = state.tasks.filter((t) => t.goal === g.id);
  const done = tasks.filter((t) => t.state === "done").length;
  return `<button class="goal-card" data-goal="${esc(g.id)}"><div class="goal-card-top"><span class="goal-dot ${esc(g.status)}"></span><span>${goalStatus[g.status]}</span><small>${esc(person(g.owner))} 负责</small></div><h3>${esc(g.title)}</h3><p>${esc(g.status === "completed" ? g.conclusion : g.criterion || "完成标准待讨论，可先梳理问题与证据。")}</p><div class="goal-card-foot"><span>${tasks.length ? `${done} / ${tasks.length} 项工作已交付` : "还没有工作，先从讨论开始"}</span><span>进入目标 ↗</span></div></button>`;
}
function roomsPage() {
  const active = state.goals.filter((g) => g.status === "active");
  return `<div class="page-body rooms-home"><div class="entry-intro"><div><span class="eyebrow">JULIAN’S LAB · 共享研究空间</span><p>进入一个研究室，接着推进已有目标；也可以为新的问题留出空间。</p></div><button class="button primary" data-action="new-room">${icon("plus")} 创建研究室</button></div><div class="entry-summary"><span><strong>${state.rooms.length}</strong> 个研究室</span><span><strong>${active.length}</strong> 个目标正在研究</span><span>人和 Agent，一起把问题弄清楚。</span></div><div class="room-grid">${state.rooms
    .map((r) => {
      const goals = state.goals.filter((g) => g.room === r.id);
      const latest = goals.filter((g) => g.status === "active").at(-1);
      return `<button class="room-entry" data-room="${esc(r.id)}"><div class="room-entry-top"><span class="room-entry-icon">#</span><span>${goals.filter((g) => g.status === "active").length} 个目标研究中</span></div><h2>${esc(r.name)}</h2><p>${esc(r.description || "新的研究空间，等一个值得探索的问题。")}</p><div class="room-entry-context">${esc(r.context || "从问题开始，逐步补齐上下文")}</div><div class="room-entry-foot"><span>${latest ? "最近目标 · " + esc(latest.title) : "可以先讨论，也可以直接发起目标"}</span><span>进入 ↗</span></div></button>`;
    })
    .join(
      "",
    )}</div><div class="entry-note">研究室保留团队与上下文，目标记录每一次研究的进展和结论。<br>当前为本地交互原型，队友与资源使用示例配置。</div></div>`;
}
function roomOverview() {
  const room = state.rooms.find((r) => r.id === state.room);
  const goals = state.goals.filter((g) => g.room === state.room);
  const open = goals.filter((g) => g.status !== "completed");
  const completed = goals.filter((g) => g.status === "completed");
  const general = state.messages.filter(
    (m) => m.room === state.room && !m.goal && m.author !== "system",
  );
  return `<div class="page-body room-overview"><div class="room-welcome"><span class="eyebrow">共享研究室</span><p>${esc(room.description || "把值得研究的问题带进来，和队友一起推进。")}</p><div class="tag-row">${MEMBERS.map((m) => `<button class="room-person" data-member="${m.id}">${avatar(m.id)}${m.name}</button>`).join("")}</div></div>${!goals.length ? `<section class="goal-empty"><span class="empty-icon">◎</span><h2>你想研究什么？</h2><p>一句目标、一段观察或一个疑问，都可以是起点。<br>先建立目标，随着研究逐步补齐完成标准。</p><button class="button primary" data-action="new-goal">${icon("plus")} 发起第一个目标</button></section>` : `<div class="section-title"><h2>正在推进 <span>${open.length}</span></h2><button class="text-button" data-action="new-goal">＋ 发起目标</button></div><div class="goal-grid">${open.map(goalCard).join("") || '<p class="muted">当前目标都已结束。新的问题出现时，随时开始。</p>'}</div>${completed.length ? `<div class="section-title"><h2>研究记录 <span>${completed.length}</span></h2></div><div class="goal-grid">${completed.map(goalCard).join("")}</div>` : ""}`}<button class="free-discussion" data-action="free-discussion">${icon("chat")}<span><strong>先聊聊，不急着确定目标</strong><small>${general.length ? `${general.length} 条讨论 · ` + esc(general.at(-1).text.slice(0, 60)) : "分享一个想法，之后可以把讨论转为目标"}</small></span><span>↗</span></button></div>`;
}
function goalMap() {
  const goal = currentGoal(state);
  if (!goal)
    return '<div class="empty"><h2>从一个研究目标开始</h2><p>目标产生后，可以在这里追溯工作和讨论的联系。</p><button class="button primary" data-action="new-goal">发起目标</button></div>';
  const tasks = state.tasks.filter((t) => inScope(state, t));
  return `<div class="page-body"><div class="page-intro"><div><h2>这次研究如何推进</h2><p>由实际的目标、工作与引用生成；不表示任务执行依赖。</p></div></div><div class="relationship-map"><button class="map-node" data-action="goal-details">${esc(goal.title)}<small>${goalStatus[goal.status]} · ${esc(person(goal.owner))}</small></button><span class="map-edge"></span><div class="goal-map-tasks">${tasks.map(taskMarkup).join("") || '<p class="muted">可以先讨论，再交办具体工作。</p>'}</div><div class="reference-row">${goal.refs.map(reference).join("")}</div></div></div>`;
}
function openGoalDetails() {
  const goal = currentGoal(state);
  if (!goal) return contextPanel();
  panel = { kind: "goal", id: goal.id };
  const history = (goal.history || []).length
    ? `<details class="flow-details"><summary>历史目标说明 · ${goal.history.length} 次修改</summary>${goal.history
        .map(
          (r) =>
            `<div class="document-block"><strong>第 ${r.revision} 版 · ${esc(r.title)}</strong><p>${esc(r.prompt)}</p>${meta(
              [
                ["完成标准", r.criterion || "待讨论"],
                ["预算与边界", r.budget || "未设置"],
              ],
            )}</div>`,
        )
        .join("")}</details>`
    : "";
  const unfinished = state.tasks.filter(
    (t) => t.goal === goal.id && !["done", "cancelled"].includes(t.state),
  ).length;
  showPanel(
    "研究目标",
    `<div class="eyebrow">${esc(roomName(goal.room))} · ${goalStatus[goal.status]} · 第 ${goal.revision || 1} 版</div><h2>${esc(goal.title)}</h2><p>${esc(goal.prompt)}</p>${meta(
      [
        ["负责人", person(goal.owner)],
        ["完成标准", goal.criterion || "待讨论；尚未制定验收条件"],
        ["预算与边界", goal.budget || "尚未设置，不代表无限执行授权"],
      ],
    )}<div class="document-block"><strong>继承的研究上下文</strong><p>${esc(goal.context || "尚未绑定模型、硬件或工作负载。")}</p></div><div class="reference-row">${goal.refs.map(reference).join("")}</div>${goal.source ? `<button class="button" data-message-link="${esc(goal.source)}">回到发起目标的讨论 ↗</button>` : ""}${goal.conclusion ? `<div class="document-block"><strong>${goal.status === "completed" ? "研究结论" : "上次结束时的结论"} · 人工记录</strong><p>${esc(goal.conclusion)}</p></div>` : ""}<div class="decision-buttons">${goal.status === "active" ? '<button class="button" data-goal-action="pause">暂停研究</button>' : goal.status === "paused" ? '<button class="button primary" data-goal-action="resume">恢复研究</button>' : '<button class="button" data-goal-action="reopen">重新开放目标</button>'}${goal.status !== "completed" ? '<button class="button" data-goal-action="revise">补充目标说明</button><button class="button" data-goal-action="complete">结束研究并记录结论</button>' : ""}</div><p>${unfinished ? `还有 ${unfinished} 项工作未结束。结束研究前请先交付或取消这些工作。` : "工作记录会随目标保留。研究结束不等于性能验收通过。"}</p>${history}`,
  );
}
function openFlow(kind, id = null) {
  saveDraft();
  const room = state.rooms.find((r) => r.id === state.room);
  flow = {
    kind,
    id,
    key: `${kind}:${kind === "room" ? "new" : id || state.room}`,
  };
  const d = state.forms[flow.key] || {};
  const source =
    kind === "goal" && id ? state.messages.find((m) => m.id === id) : null;
  let fields = "";
  if (kind === "room") {
    fields = `<p>给一个长期研究方向留出共同工作的空间。</p><label class="field">研究室名称<input name="name" required maxlength="48" placeholder="例如：长上下文推理" value="${esc(d.name || "")}" autofocus></label><label class="field">简单介绍 <span>选填</span><textarea name="description" rows="2" maxlength="240" placeholder="这个研究室关注什么？">${esc(d.description || "")}</textarea></label><details class="flow-details" ${d.context ? "open" : ""}><summary>添加研究上下文 · 选填</summary><label class="field">模型、代码或资源线索<textarea name="context" rows="3" maxlength="1000" placeholder="已有的模型、代码库、硬件和工作负载…">${esc(d.context || "")}</textarea></label></details><p class="form-note">演示中沿用 Julian、Atlas、Prism 与 Lin 四位队友。资源线索只用于保存上下文。</p>`;
  } else if (kind === "goal") {
    const selected = [...new Set([...(d.refs || []), ...(source?.refs || [])])];
    fields = `<p>在 <strong>${esc(room.name)}</strong> 中发起研究。先说清想弄明白什么，实施路线可以边做边完善。</p>${source ? `<div class="source-preview"><small>来自 ${esc(person(source.author))} 的讨论 · 原消息与证据会保留</small><p>${esc(source.text.slice(0, 180))}</p></div>` : ""}<label class="field">你想研究什么？<textarea id="goal-prompt" name="prompt" rows="4" required maxlength="4000" placeholder="例如：弄清 DSV4 在 H20 上的 decode 瓶颈，先复用已有证据。" autofocus>${esc(d.prompt ?? source?.text ?? "")}</textarea></label><div class="goal-examples"><span>也可以从这里开始</span><button type="button" data-goal-example="bottleneck">理解瓶颈 ↗</button><button type="button" data-goal-example="change">验证改动 ↗</button></div><details class="flow-details" ${d.criterion || d.budget || selected.length ? "open" : ""}><summary>完成标准、边界和证据 · 选填</summary><label class="field">怎样算有结果？<textarea name="criterion" rows="2" maxlength="2000" placeholder="解释清楚一个原因，或验证某项改动的效果…">${esc(d.criterion || "")}</textarea></label><label class="field">预算与研究边界<input name="budget" maxlength="240" placeholder="例如：先分析已有 trace，暂不启动 GPU 实验" value="${esc(d.budget || "")}"></label><fieldset class="evidence-choices"><legend>带入已有证据 · 演示样例</legend>${Object.entries(
      ARTIFACTS,
    )
      .map(
        ([key, a]) =>
          `<label><input type="checkbox" name="refs" value="${key}" ${selected.includes(key) ? "checked" : ""} ${source?.refs.includes(key) ? "disabled" : ""}>${esc(a.title)}${source?.refs.includes(key) ? " · 原讨论引用" : ""}</label>`,
      )
      .join(
        "",
      )}</fieldset></details><div class="goal-start-options"><label class="field">负责接力的队友<select name="owner">${MEMBERS.map((m) => `<option value="${m.id}" ${(d.owner || source?.author || "atlas") === m.id ? "selected" : ""}>${m.name} · ${m.role}</option>`).join("")}</select></label><label class="checkbox-field"><input type="checkbox" name="firstTask" ${d.firstTask !== false ? "checked" : ""}>创建第一项工作：梳理证据与下一步</label></div><p class="form-note">继承的上下文：${esc(room.context || "尚未设置，可以从整理问题开始。")}</p>`;
  } else if (kind === "revise") {
    const goal = state.goals.find((g) => g.id === id);
    fields = `<p>补齐研究问题、完成标准或边界。修改会留在历史记录里；已有工作继续保留原说明，新交办的工作引用更新后的说明。</p><label class="field">研究问题<textarea name="prompt" rows="3" required maxlength="4000" autofocus>${esc(d.prompt ?? goal.prompt)}</textarea></label><label class="field">怎样算有结果？<textarea name="criterion" rows="3" maxlength="2000">${esc(d.criterion ?? goal.criterion)}</textarea></label><label class="field">预算与研究边界<input name="budget" maxlength="240" value="${esc(d.budget ?? goal.budget)}"></label>`;
  } else {
    const task = state.tasks.find((t) => t.id === id);
    const goal = state.goals.find((g) => g.id === id);
    fields = `<p>${esc(kind === "delivery" ? task?.title : goal?.title)}</p><label class="field">${kind === "delivery" ? "交付说明" : "研究结论"}<textarea name="result" rows="6" required maxlength="4000" autofocus placeholder="记录做了什么、依据是什么、还有哪些不确定；也可以说明尚未得出结论。">${esc(d.result || "")}</textarea></label><p class="form-note">这是人工填写的本地记录，不会自动生成性能判定或真实实验结果。</p>`;
  }
  $("#flow-title").textContent = {
    room: "创建研究室",
    goal: "发起目标",
    revise: "补充目标说明",
    delivery: "记录任务交付",
    conclusion: "结束研究",
  }[kind];
  $("#flow-fields").innerHTML = fields;
  $("#flow-submit").textContent = {
    room: "创建并进入",
    goal: "开始研究",
    revise: "保存目标说明",
    delivery: "保存交付",
    conclusion: "保存结论并结束",
  }[kind];
  $("#flow-error").textContent = "";
  $("#flow-dialog").showModal();
  $("#flow-fields [autofocus]")?.focus();
}
function saveFlowDraft() {
  if (!flow) return;
  const form = new FormData($("#flow-form"));
  state.forms[flow.key] = {
    ...Object.fromEntries(form),
    refs: form.getAll("refs"),
    firstTask: form.has("firstTask"),
  };
  persist();
}
$("#flow-form").addEventListener("input", saveFlowDraft);
$("#flow-form").addEventListener("change", saveFlowDraft);
$("#flow-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!flow) return;
  saveFlowDraft();
  const d = state.forms[flow.key];
  try {
    const { kind, id, key } = flow;
    if (kind === "room") createResearchRoom(state, d);
    else if (kind === "goal") createResearchGoal(state, { ...d, source: id });
    else if (kind === "revise") reviseResearchGoal(state, id, d);
    else if (kind === "delivery") updateTask(state, id, "complete", d.result);
    else updateGoalStatus(state, id, "complete", d.result);
    delete state.forms[key];
    $("#flow-dialog").close();
    flow = null;
    page = "room";
    closePanel();
    loadDraft();
    render();
    if (kind === "delivery") openTask(id);
    if (["conclusion", "revise"].includes(kind)) openGoalDetails();
    $(".sidebar").classList.remove("show");
    notice(
      {
        room: "研究室已创建，可以先讨论，也可以发起目标。",
        goal: "目标已建立，讨论与工作会独立保留。",
        revise: "目标说明已保存，已有工作的版本与状态保留。",
        delivery: "交付说明已保存到对应目标的讨论。",
        conclusion: "研究已结束，结论与原始工作记录已保留。",
      }[kind],
    );
  } catch (error) {
    $("#flow-error").textContent = error.message;
  }
});

loadDraft();
render();
