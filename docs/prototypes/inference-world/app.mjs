import {
  STATUS,
  createWorld,
  byId,
  inputBlockers,
  eligibility,
  startTask,
  finishTask,
  addHypothesis,
  addDependency,
  upstream,
  downstream,
  levels,
  setBudget,
  evaluatePractice,
  promotePractice,
  rollbackPractice,
  trialPractice,
} from "./model.mjs";
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const fmt = (n) => n.toLocaleString("en-US");
const TYPE = {
  strategy: "策略",
  benchmark: "性能验证",
  profile: "诊断采样",
  analysis: "分析",
  proposal: "假设",
  build: "物化",
  verdict: "判定",
  experience: "经验",
};
let world = createWorld(),
  page = "network",
  view = "tasks",
  detail = "task",
  zoom = 0.75,
  list = false,
  actor = "prism",
  positions = new Map(),
  graphWidth = 0,
  graphHeight = 0;
const messageDrafts = {};
let hypothesisOrigin = "A0";
function notice(text) {
  $("#notice").textContent = text;
}
function act(fn) {
  try {
    fn();
    render();
  } catch (error) {
    notice(error.message);
  }
}
function choose(id, center = false) {
  world.selected = id;
  detail = "task";
  actor = byId(world, id).owner || "prism";
  render();
  if (center) centerSelected();
}
function badge(status) {
  return `<span class="badge ${esc(status)}">${esc(STATUS[status] || { candidate: "待复现", verified: "已复现 · 演示", contested: "出现反例" }[status] || status)}</span>`;
}
function projections() {
  if (view === "tasks")
    return world.tasks.map((t) => ({ ...t, key: t.id, links: t.deps }));
  const branches = world.tasks.filter(
    (t) => t.type === "benchmark" && t.id !== "B0",
  );
  if (view === "lineage")
    return [
      {
        key: "root",
        id: "B0",
        title: "B0 · 原始基线",
        status: "completed",
        links: [],
        note: "固定参照，不改写",
      },
      ...branches.map((t) => ({
        key: t.id,
        id: t.id,
        title: `候选 ${t.id.slice(1)} · ${t.result?.regressed ? "回退" : t.status === "completed" ? "已测量" : t.status === "running" ? "验证中" : t.status === "failed" ? "尝试失败" : "待验证"}`,
        status: t.status,
        links: [
          t.parentBenchmark && t.parentBenchmark !== "B0"
            ? t.parentBenchmark
            : "root",
        ],
        note: "继承 " + (t.parentBenchmark || "B0") + " · 对照根 B0",
      })),
    ];
  return [
    {
      key: "evidence",
      id: "A0",
      title: "热点与证据缺口",
      status: "completed",
      links: [],
      note: "观察不是结论",
    },
    ...world.tasks
      .filter((t) => t.type === "experience")
      .map((t) => ({
        key: t.id,
        id: t.id,
        title: t.result ? "经验草案" : "待汇合的证据",
        status: t.status,
        links: ["evidence"],
        note: "判定 + 候选 Profile",
      })),
    {
      key: "practice",
      id: "A0",
      title: `Practice ${world.practice.active}`,
      status: "pending",
      links: world.tasks
        .filter((t) => t.type === "experience")
        .map((t) => t.id),
      note: "仅经独立评测与试用后晋升",
    },
  ];
}
function renderGraph() {
  const data = projections(),
    taskLevels = view === "tasks" ? levels(world) : null;
  const depth = new Map();
  function depthOf(n) {
    if (depth.has(n.key)) return depth.get(n.key);
    const value = n.links.length
      ? 1 +
        Math.max(...n.links.map((k) => depthOf(data.find((x) => x.key === k))))
      : 0;
    depth.set(n.key, value);
    return value;
  }
  const cols = new Map();
  data.forEach((n) => {
    const l = taskLevels?.get(n.id) ?? depthOf(n);
    if (!cols.has(l)) cols.set(l, []);
    cols.get(l).push(n);
  });
  const rows = Math.max(...[...cols.values()].map((c) => c.length));
  graphWidth = Math.max(560, cols.size * 188 + 40);
  graphHeight = Math.max(440, rows * 132 + 125);
  positions = new Map();
  for (const [level, nodes] of cols)
    nodes.forEach((n, i) =>
      positions.set(n.key, {
        x: 24 + level * 188,
        y: 85 + (graphHeight - 125 - nodes.length * 132) / 2 + i * 132,
      }),
    );
  const linked =
    view === "tasks"
      ? new Set([
          world.selected,
          ...upstream(world, world.selected),
          ...downstream(world, world.selected),
        ])
      : new Set(data.map((n) => n.id));
  $("#nodes").innerHTML =
    [...cols.keys()]
      .map(
        (l) =>
          `<span class="column-label" style="left:${24 + l * 188}px">${view === "tasks" ? ["锁定", "测量", "理解", "假设", "物化", "验证", "判定", "沉淀"][l] || "继续" : view === "lineage" ? (l ? "候选版本" : "原始参照") : ["观察", "经验草案", "团队方法"][l]}</span>`,
      )
      .join("") +
    data
      .map((n) => {
        const pos = positions.get(n.key),
          t = byId(world, n.id),
          why = inputBlockers(world, t);
        return `<button class="node ${n.status} ${n.id === world.selected ? "selected" : ""} ${linked.has(n.id) ? "" : "dim"}" data-node="${n.id}" style="left:${pos.x}px;top:${pos.y}px" aria-pressed="${n.id === world.selected}" aria-label="${esc(n.key + " " + n.title + "，" + STATUS[n.status])}" title="${esc((n.note || t.note || "") + " · " + (why[0] || STATUS[n.status]))}"><span class="node-top"><span>${esc(n.key.toUpperCase())} · ${esc(TYPE[t.type])}</span><i></i></span><strong>${esc(n.title)}</strong><small>${esc(view === "tasks" ? why[0] || (world.members.find((m) => m.id === t.owner)?.name || "可认领") + " · " + STATUS[t.status] : n.note)}</small></button>`;
      })
      .join("");
  $("#edges").setAttribute("viewBox", `0 0 ${graphWidth} ${graphHeight}`);
  $("#edges").setAttribute("width", graphWidth);
  $("#edges").setAttribute("height", graphHeight);
  $("#edges").innerHTML =
    '<defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto"><path d="M0 0 L6 3 L0 6" fill="none" stroke="#7298ae" stroke-width="1"/></marker></defs>' +
    data
      .flatMap((n) =>
        n.links.map((k) => {
          const a = positions.get(k),
            b = positions.get(n.key),
            src = data.find((x) => x.key === k),
            active = linked.has(n.id) && linked.has(src.id);
          const path =
            b.x - a.x > 200
              ? `M${a.x + 156} ${a.y + 50} C${a.x + 172} ${a.y + 50} ${a.x + 165} 60 ${a.x + 190} 60 L${b.x - 28} 60 C${b.x - 10} 60 ${b.x - 22} ${b.y + 50} ${b.x} ${b.y + 50}`
              : `M${a.x + 156} ${a.y + 50} C${a.x + 174} ${a.y + 50} ${b.x - 18} ${b.y + 50} ${b.x} ${b.y + 50}`;
          return `<path class="edge ${active ? "active" : "dim"} ${view === "memory" ? "memory" : ""}" d="${path}" marker-end="url(#arrow)"/>`;
        }),
      )
      .join("");
  $("#view-label").textContent = {
    tasks: "任务 DAG · 实线表示产物依赖",
    lineage: "版本谱系 · 继承关系，不代表任务顺序",
    memory: "经验关联 · 虚线表示来源；验证后才可复用",
  }[view];
  $("#task-list").innerHTML =
    `<table><thead><tr><th>任务</th><th>状态</th><th>输入</th></tr></thead><tbody>${world.tasks.map((t) => `<tr><td><button data-select="${t.id}">${t.id} · ${esc(t.title)}</button></td><td>${badge(t.status)}</td><td>${esc(t.deps.join("、") || "无")}</td></tr>`).join("")}</tbody></table>`;
  $("#graph-viewport").hidden = list;
  $("#task-list").hidden = !list;
  applyZoom();
}
function applyZoom() {
  const w = $("#graph-world");
  w.style.width = `${graphWidth * zoom}px`;
  w.style.height = `${graphHeight * zoom}px`;
  for (const s of ["#nodes", "#edges"]) {
    $(s).style.transform = `scale(${zoom})`;
    $(s).style.transformOrigin = "0 0";
  }
  w.classList.toggle("compact", zoom < 0.8);
  $("#zoom-value").textContent = `${Math.round(zoom * 100)}%`;
}
function fit() {
  const width = $("#graph-viewport").clientWidth;
  zoom = Math.min(1, Math.max(0.6, (width - 16) / graphWidth));
  applyZoom();
  $("#graph-viewport").scrollTo(0, 0);
}
function centerSelected() {
  const p =
    positions.get(world.selected) ||
    [...positions.entries()].find(([key]) => key === "root")?.[1];
  if (p) {
    const vp = $("#graph-viewport");
    vp.scrollTo({
      left: (p.x + 78) * zoom - vp.clientWidth / 2,
      top: (p.y + 50) * zoom - vp.clientHeight / 2,
    });
  }
}
function renderInspector() {
  const t = byId(world, world.selected);
  $("#inspector-heading").innerHTML =
    `<div class="eyebrow">${t.id} / ${TYPE[t.type].toUpperCase()}</div><h2>${esc(t.title)}</h2>${badge(t.status)}`;
  $$("[data-detail]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.detail === detail)),
  );
  if (detail === "discussion") {
    const messages = world.messages[t.id] || [];
    $("#inspector-content").innerHTML =
      `<p>讨论绑定 ${t.id}。想法可以共同修订；提交结构化假设后才会长出验证分支。</p><div class="message"><strong>上下文提示 · 非 Agent 回复</strong><p>${esc(t.note || "请说明观察、预期改变和否定条件。")}</p></div>${messages.map((m) => `<div class="message"><strong>Julian · ${t.id}</strong><p>${esc(m)}</p></div>`).join("")}<label for="discussion-input">补充你的判断</label><textarea id="discussion-input" placeholder="例如：先补采多 rank trace，确认等待来源。">${esc(messageDrafts[t.id] || "")}</textarea><div class="actions"><button id="post-comment">保存讨论</button><button id="discussion-hypothesis">转为假设</button></div><p class="hint">这里只记录本页演示，不发送给真实成员或 Agent。</p>`;
    return;
  }
  if (detail === "evidence") {
    $("#inspector-content").innerHTML =
      `<p>所有产物引用都是演示占位；未连接本地 run，也没有真实 trace。</p><span class="field-label">当前尝试的产物</span>${t.result ? `<code class="artifact">${esc(t.result.artifact || t.result.error)}</code>${t.result.explanation ? `<p>${esc(t.result.explanation)}</p>` : ""}${t.result.regressed ? '<div class="blocker">测量完成，但候选性能回退。任务完成不等于优化成功。</div>' : ""}` : '<div class="artifact">初始示例 / 尚无本页生成产物</div>'}<span class="field-label">尝试历史 · 不覆盖旧记录</span>${t.history.length ? t.history.map((h) => `<div class="history-row">Attempt ${h.attempt} · ${esc(STATUS[h.status])}<br>${esc(h.owner)} ${h.resource ? " / " + esc(h.resource) : ""}<br>${esc(h.result.explanation || h.result.error || h.result.artifact)}</div>`).join("") : '<p class="hint">在本页提交一次结果后，历史会显示在这里。</p>'}`;
    return;
  }
  const m = world.members.find((m) => m.id === t.owner);
  $("#inspector-content").innerHTML =
    `<p>${esc(t.note || "交付固定版本的产物，让下游在同一事实之上继续工作。")}</p><span class="field-label">已锁定输入 ${t.deps.length ? "· " + t.deps.length : ""}</span>${t.deps.length ? t.deps.map((id) => `<div class="input-row"><button data-select="${id}">${id} · ${esc(TYPE[byId(world, id).type])}</button><span>${esc(STATUS[byId(world, id).status])}</span></div>`).join("") : '<p class="hint">起点任务，不依赖其他产物。</p>'}<div class="inspector-meta"><div><small>准入</small><span>${{ human: "真人", agent: "Agent", either: "真人 / Agent" }[t.admission]}</span></div><div><small>方法版本</small><span>${esc(t.practiceVersion)}</span></div><div><small>资源要求</small><span>${t.gpu ? "Pod · H20 × 8" : "无 GPU"}</span></div><div><small>尝试</small><span>${t.attempt ? "#" + t.attempt : "尚未执行"}</span></div></div>${t.status === "running" ? `<span class="field-label">当前负责人</span><p>${esc(m?.name)}${t.resource ? " / " + esc(t.resource) + " · 独占租约" : ""}</p><div class="actions"><button class="primary" data-finish="success">演示交付</button>${t.type === "benchmark" ? '<button data-finish="regression">演示回退</button>' : ""}<button data-finish="failure">演示失败</button></div><p class="hint">固定结算 2,000 演示 token；产物验收在此被简化为手动推进。</p>` : t.status === "completed" ? `<div class="success-note">产物已验收，下游可按各自条件接力。</div>${t.result?.explanation ? `<div class="blocker">${esc(t.result.explanation)}</div>` : ""}${t.type === "experience" ? '<button class="primary wide" id="next-round">从当前 champion 开启下一轮</button><p class="hint">保留 B0 参照，锁定父候选并重新采样。</p>' : ""}<button id="open-evidence" class="wide">查看产物与历史 →</button>` : `<label for="member-select">由谁认领${t.status === "failed" ? "并重试" : ""}</label><select id="member-select">${world.members.map((m) => `<option value="${m.id}" ${m.id === actor ? "selected" : ""}>${esc(m.name)} · ${m.kind === "human" ? "真人" : m.runtime}${m.online ? "" : " · 离线"}</option>`).join("")}</select><div id="admission-reasons"></div><div class="actions"><button class="primary wide" id="start-task">演示认领${t.status === "failed" ? "并重试" : ""}</button></div>`}`;
  renderAdmission();
}
function renderAdmission() {
  if (!$("#admission-reasons")) return;
  const blockers = eligibility(world, world.selected, actor);
  $("#admission-reasons").innerHTML = blockers.length
    ? blockers.map((b) => `<div class="blocker">${esc(b)}</div>`).join("")
    : '<div class="success-note">条件满足 · 可创建一次新尝试</div>';
  $("#start-task").disabled = !!blockers.length;
}
function renderResources() {
  const t = byId(world, world.selected);
  $("#resources-page").innerHTML =
    `<div class="section-intro"><h2>持久成员，可更换的执行环境</h2><span class="badge">${world.members.length} 位演示成员</span></div><p class="hint">Computer 运行 Agent 控制进程；下方 GPU 池负责 Pod 执行。切换状态只影响本页新尝试。</p><div class="cards-grid">${world.members.map((m) => `<article class="panel"><div class="member-head"><span class="avatar">${m.name.slice(0, 1)}</span><div><strong>${esc(m.name)}</strong><small>${m.kind === "human" ? "真人 · " + m.runtime : "Agent · " + m.runtime + " · " + m.computer}</small></div><button data-member="${m.id}" aria-pressed="${m.online}">${m.online ? "在线" : "离线"}</button></div>${m.kind === "agent" ? `<div class="budget-line"><span>已用 ${fmt(m.used)} / 日预算 ${fmt(m.limit)}</span><span>预留 ${fmt(m.reserved)}</span></div><div class="meter"><span class="used" style="width:${Math.min(100, (m.used / (m.limit || 1)) * 100)}%"></span><span class="reserved" style="width:${Math.min(100, (m.reserved / (m.limit || 1)) * 100)}%"></span></div><form class="budget-form" data-budget="${m.id}"><label for="budget-${m.id}">日 token 上限</label><input id="budget-${m.id}" type="number" min="0" step="1" value="${m.limit}" required><button type="submit">更新</button></form>` : '<p class="hint" style="margin:18px 0 0">可提出、讨论、认领与交付。管理员维护目标和团队默认方法。</p>'}</article>`).join("")}</div><article class="panel"><div class="section-intro"><h2>GPU 资源池与独占租约</h2><span class="badge">演示拓扑</span></div><div class="table-scroll"><table><thead><tr><th>资源池</th><th>GPU</th><th>连接</th><th>执行占用</th><th>对当前 GPU 需求</th></tr></thead><tbody>${world.resources.map((r) => `<tr><td>${r.id}</td><td>${r.model} × ${r.gpus}</td><td><button data-resource="${r.id}" aria-pressed="${r.online}">${r.online ? "在线" : "离线"}</button></td><td>${r.lease ? `<button data-select="${r.lease}">Pod / ${r.lease} →</button>` : "空闲"}</td><td>${!r.online ? "连接不可用" : r.lease ? "已有独占租约" : r.model !== "H20" ? "GPU 型号不匹配" : "可匹配 H20 × 8"}</td></tr>`).join("")}</tbody></table></div><p class="hint" style="margin:14px 0 0">连接离线不代表 Pod 已结束；原有租约继续保留。生产调度还需核对拓扑、软件和干扰域。</p></article><article class="panel"><h3>当前任务 ${t.id} · ${esc(t.title)}</h3><p>检查依赖、成员类型、在线状态、日预算与空闲资源。</p><div class="matching">${world.members
      .map((m) => {
        const blockers = eligibility(world, t.id, m.id);
        return `<span class="${blockers.length ? "no" : ""}">${esc(m.name)}：${esc(blockers.length ? blockers.join("；") : "可认领")}</span>`;
      })
      .join(
        "",
      )}</div><div class="actions"><button data-select="${t.id}">回到任务 →</button></div></article>`;
}
function renderLearning() {
  const p = world.practice,
    e = p.evaluation;
  $("#learning-page").innerHTML =
    `<div class="rsi-header"><article class="panel"><div class="eyebrow">RECURSIVE IMPROVEMENT</div><h2>让下一轮做得更好，也要拿出证据</h2><p>优化系统 → 提炼经验 → 改进团队方法。经验数量不会自动转化为能力；新方法需要独立留出评测、有限试用与可回滚发布。</p><div class="practice-flow"><span>试验</span>→<span>经验草案</span>→<span>复现</span>→<strong>Practice ${p.active}</strong></div><p class="hint">演示管理员视角。任务创建时固定方法版本，晋升仅影响新任务。</p></article><article class="panel"><div class="eyebrow">VERSIONED PRACTICE</div><h3>候选 v2 · 先检查证据覆盖，再排方案</h3><p>拟改动：分析 checklist + 上下文模板。验收标准保持固定。</p><div class="actions"><button id="evaluate-pass">演示评测通过</button><button id="evaluate-fail">演示评测回退</button></div>${e ? `<div class="stat-pair"><div><strong>${e.old}/12</strong><small>v1 正确决策</small></div><div><strong>${e.new}/12</strong><small>v2 正确决策</small></div></div><p class="hint">${esc(e.budget)}。${esc(e.label)}。<br>评测状态：${e.passed ? "通过" : "存在回退"} · 试用：${p.trial === "passed" ? "已通过演示试用" : "尚未通过"}</p>` : '<p class="hint" style="margin-top:18px">尚无评测；不能晋升。下方操作生成固定演示结果，不调用评测器。</p>'}<div class="actions"><button id="trial-practice" ${!e?.passed || p.active === "v2" ? "disabled" : ""}>演示有限试用</button><button id="promote-practice" class="primary" ${!e?.passed || p.trial !== "passed" || p.active === "v2" ? "disabled" : ""}>晋升 v2</button><button id="rollback-practice" ${p.active === "v1" ? "disabled" : ""}>回滚到 v1</button></div></article></div><article class="panel"><div class="section-intro"><h2>团队经验 · 保留适用范围与反例</h2><span class="badge">${world.experiences.length} 条演示记录</span></div><p>完成“经验与下一轮”任务会留下草案；复现通过后才标记为已验证。反例会重新打开问题。</p>${world.experiences.length ? world.experiences.map((x) => `<div class="experience-card"><div class="eyebrow">${x.id}</div><h3>${esc(x.title)}</h3>${badge(x.status)}<p style="margin-top:9px">范围：DSV4 / H20 / 本轮锁定场景。失效触发：runtime 或拓扑变化、相反证据。<br>支持：${x.reproduced ? "演示复现产物" : "待补"} · 反例：${x.status === "contested" ? "演示反例已关联" : "尚无已记录反例，不表示不存在"}</p><div class="actions"><button data-select="${x.source}">来源 ${x.source} →</button><button data-verify-experience="${x.id}" ${x.status !== "candidate" ? "disabled" : ""}>演示复现通过</button><button data-contest-experience="${x.id}" ${x.status === "contested" ? "disabled" : ""}>关联演示反例</button></div></div>`).join("") : '<div class="empty">本页尚未交付经验任务。完成 V1 与 P1，再交付 E1，可观察第一条草案生成。</div>'}</article><article class="panel"><h3>方法发布历史</h3>${p.history.length ? p.history.map((h, i) => `<div class="history-row">${i + 1} · ${h.from} → ${h.to} · ${esc(h.reason || "独立评测与有限试用通过（演示）")}</div>`).join("") : '<p class="hint">还没有发布记录。每次晋升和回滚都保留在这里。</p>'}</article>`;
}
function render() {
  const titles = {
    network: [
      "让每次试验，连接下一次突破",
      "沿任务网络观察工作如何分叉、汇合，并成为团队可复用的经验。",
    ],
    resources: [
      "人、Agent 和算力，各就其位",
      "任务找到合适的成员，GPU 尝试再领取隔离资源。",
    ],
    learning: [
      "经验不是终点，是下一轮的起点",
      "只有经过复现和对照评测，团队的新做法才成为默认版本。",
    ],
  };
  $("#page-title").textContent = titles[page][0];
  $("#page-subtitle").textContent = titles[page][1];
  for (const key of ["network", "resources", "learning"])
    $("#" + key + "-page").hidden = page !== key;
  $$("[data-page]").forEach((b) =>
    b.dataset.page === page
      ? b.setAttribute("aria-current", "page")
      : b.removeAttribute("aria-current"),
  );
  $$("[data-view]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.view === view)),
  );
  $("#graph-revision").textContent =
    `图 v${world.revision} · ${world.tasks.length} 任务`;
  $("#latest-event").textContent = world.events[0].text;
  $(".goal-progress span").style.width =
    `${(world.tasks.filter((t) => t.status === "completed").length / world.tasks.length) * 100}%`;
  $(".goal-progress").setAttribute(
    "title",
    `${world.tasks.filter((t) => t.status === "completed").length} / ${world.tasks.length} 任务已交付`,
  );
  renderGraph();
  renderInspector();
  renderResources();
  renderLearning();
}
function openHypothesis(title = "", source = "human", origin = "A0") {
  hypothesisOrigin = origin;
  $("#hypothesis-dialog p").textContent =
    `从 ${origin} 派生验证子图，父候选固定为 ${world.champion}，同时保留根 B0 参照。`;
  $("#hypothesis-title").value = title.slice(0, 90);
  $("#hypothesis-source").value = source;
  $("#hypothesis-dialog").showModal();
}
$("#add-hypothesis").onclick = () => openHypothesis();
$("#hypothesis-form").onsubmit = (e) => {
  e.preventDefault();
  act(() => {
    const id = addHypothesis(
      world,
      $("#hypothesis-title").value,
      $("#hypothesis-source").value,
      hypothesisOrigin,
    );
    $("#hypothesis-dialog").close();
    page = "network";
    view = "tasks";
    choose(id, true);
    notice(`已创建 ${id} 与 5 个验证任务；它们仍需要定稿和认领。`);
  });
};
$("#edit-edge").onclick = () => {
  const options = world.tasks
    .map((t) => `<option value="${t.id}">${t.id} · ${esc(t.title)}</option>`)
    .join("");
  $("#edge-from").innerHTML = options;
  $("#edge-to").innerHTML = options;
  $("#edge-from").value = world.selected;
  $("#edge-to").value = "H2";
  $("#edge-error").textContent = "";
  $("#edge-dialog").showModal();
};
$("#edge-form").onsubmit = (e) => {
  e.preventDefault();
  try {
    addDependency(world, $("#edge-from").value, $("#edge-to").value);
    $("#edge-dialog").close();
    render();
    notice("依赖已提交，图版本已递增。");
  } catch (error) {
    $("#edge-error").textContent = error.message;
  }
};
$("#toggle-list").onclick = () => {
  list = !list;
  $("#toggle-list").setAttribute("aria-pressed", String(list));
  renderGraph();
};
$("#zoom-in").onclick = () => {
  zoom = Math.min(1.4, zoom + 0.1);
  applyZoom();
};
$("#zoom-out").onclick = () => {
  zoom = Math.max(0.6, zoom - 0.1);
  applyZoom();
};
$("#zoom-reset").onclick = fit;
$("#reset").onclick = () => {
  world = createWorld();
  page = "network";
  view = "tasks";
  detail = "task";
  actor = "prism";
  Object.keys(messageDrafts).forEach((k) => delete messageDrafts[k]);
  notice("已重置本页演示。");
  render();
  fit();
};
// Event delegation keeps regenerated controls usable without accumulating listeners.
document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.page) {
    page = b.dataset.page;
    notice("");
    render();
  }
  if (b.dataset.view) {
    view = b.dataset.view;
    notice("");
    renderGraph();
    fit();
  }
  if (b.dataset.node) choose(b.dataset.node);
  if (b.dataset.select) {
    page = "network";
    choose(b.dataset.select, true);
  }
  if (b.dataset.detail) {
    detail = b.dataset.detail;
    renderInspector();
  }
  if (b.dataset.close) $("#" + b.dataset.close).close();
  if (b.dataset.finish)
    act(() => {
      finishTask(world, world.selected, b.dataset.finish);
      notice(world.events[0].text);
    });
  if (b.id === "start-task")
    act(() => {
      startTask(world, world.selected, actor);
      notice(world.events[0].text);
    });
  if (b.id === "next-round")
    openHypothesis("下一轮：验证瓶颈转移后的优化方向", "joint", world.selected);
  if (b.id === "open-evidence") {
    detail = "evidence";
    renderInspector();
  }
  if (b.id === "post-comment") {
    const value = $("#discussion-input").value.trim();
    if (!value) {
      notice("先写下一个判断，再保存讨论。");
      return;
    }
    (world.messages[world.selected] ??= []).push(value);
    messageDrafts[world.selected] = "";
    renderInspector();
    notice("讨论已保存在本页。");
  }
  if (b.id === "discussion-hypothesis") {
    const value =
      $("#discussion-input").value.trim() ||
      (world.messages[world.selected] || []).at(-1) ||
      "";
    openHypothesis(value, "joint");
  }
  if (b.dataset.member)
    act(() => {
      const m = world.members.find((m) => m.id === b.dataset.member);
      m.online = !m.online;
      notice(
        `${m.name} 已${m.online ? "上线" : "离线"}（演示）；既有尝试保持原状态。`,
      );
    });
  if (b.dataset.resource)
    act(() => {
      const r = world.resources.find((r) => r.id === b.dataset.resource);
      r.online = !r.online;
      notice(
        `${r.id} 连接已${r.online ? "恢复" : "断开"}（演示）；已有租约保持。`,
      );
    });
  if (b.id === "evaluate-pass" || b.id === "evaluate-fail")
    act(() => {
      evaluatePractice(world, b.id === "evaluate-pass");
      notice(world.events[0].text);
    });
  if (b.id === "trial-practice")
    act(() => {
      trialPractice(world);
      notice("有限试用已通过（演示）。可以晋升；既有任务固定版本。");
    });
  if (b.id === "promote-practice")
    act(() => {
      promotePractice(world);
      notice(world.events[0].text);
    });
  if (b.id === "rollback-practice")
    act(() => {
      rollbackPractice(world);
      notice(world.events[0].text);
    });
  if (b.dataset.verifyExperience || b.dataset.contestExperience)
    act(() => {
      const x = world.experiences.find(
        (x) =>
          x.id === (b.dataset.verifyExperience || b.dataset.contestExperience),
      );
      x.status = b.dataset.verifyExperience ? "verified" : "contested";
      if (b.dataset.verifyExperience) x.reproduced = true;
      notice("经验状态已更新（演示），适用范围和来源保留。");
    });
});
document.addEventListener("change", (e) => {
  if (e.target.id === "member-select") {
    actor = e.target.value;
    renderAdmission();
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "discussion-input")
    messageDrafts[world.selected] = e.target.value;
});
document.addEventListener("submit", (e) => {
  const id = e.target.dataset.budget;
  if (!id) return;
  e.preventDefault();
  act(() => {
    setBudget(world, id, Number($("#budget-" + id).value));
    notice("日预算已更新（演示），超额会阻止新的认领。");
  });
});
let drag = null;
const vp = $("#graph-viewport");
vp.addEventListener("pointerdown", (e) => {
  if (e.target.closest("button") || e.pointerType === "touch") return;
  drag = { x: e.clientX, y: e.clientY, left: vp.scrollLeft, top: vp.scrollTop };
  vp.classList.add("dragging");
  vp.setPointerCapture(e.pointerId);
});
vp.addEventListener("pointermove", (e) => {
  if (drag) {
    vp.scrollLeft = drag.left + drag.x - e.clientX;
    vp.scrollTop = drag.top + drag.y - e.clientY;
  }
});
function endDrag() {
  drag = null;
  vp.classList.remove("dragging");
}
vp.addEventListener("pointerup", endDrag);
vp.addEventListener("pointercancel", endDrag);
render();
requestAnimationFrame(fit);
