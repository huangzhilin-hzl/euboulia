import * as M from "./loop-state.mjs";
const $ = (q) => document.querySelector(q),
  esc = M.escapeHTML;
let state,
  lastSaved = null,
  blocked = false,
  raw = null,
  handler = null,
  zoom = 1,
  filter = "all",
  chatScope = "node",
  chatRole = "lead",
  chatIntent = "discuss",
  mobileChat = false,
  compareId = null,
  backupURL = null,
  formKey = "",
  noticeTimer,
  recoveryForm = false;
try {
  lastSaved = localStorage.getItem(M.KEY);
  state = lastSaved ? M.decode(lastSaved) : M.demo();
} catch (e) {
  state = M.demo();
  raw = lastSaved;
  blocked = true;
  warning("已有数据未能读取，原始记录已保留。请先导出原始备份。" + e.message);
}
function warning(text) {
  $("#storage-warning").hidden = false;
  $("#storage-warning").textContent = text;
}
function save() {
  if (blocked) return false;
  try {
    if (localStorage.getItem(M.KEY) !== lastSaved) {
      blocked = true;
      warning("另一标签页更新了记录。本页停止保存，请导出本页备份后刷新。");
      return false;
    }
    lastSaved = JSON.stringify(state);
    localStorage.setItem(M.KEY, lastSaved);
    return true;
  } catch {
    blocked = true;
    warning("无法继续保存。请立即导出本页备份。");
    return false;
  }
}
function toast(text) {
  clearTimeout(noticeTimer);
  $("#notice").textContent = text;
  noticeTimer = setTimeout(() => ($("#notice").textContent = ""), 4000);
}
function act(fn) {
  if (blocked) {
    toast("请先导出备份并刷新，避免覆盖其他页面。");
    return;
  }
  const before = structuredClone(state);
  try {
    fn();
    save();
    render();
  } catch (e) {
    state = before;
    toast(e.message);
  }
}
const l = () => M.loopOf(state),
  n = () => M.nodeOf(state),
  all = () => M.nodesOf(state);
const tag = (status) =>
  `<span class="status status-${esc(status)}"><i></i>${esc(M.STATUS[status] || status)}</span>`;
const origin = (n) =>
  `<span class="origin">${n.result?.origin === "imported" ? "用户录入 · 未核验" : "演示数据"}</span>`;
const num = (v) =>
  typeof v === "number"
    ? v.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : { pass: "通过", fail: "失败", unknown: "待判断" }[v] || "—";
const roleName = (role, loop = l()) => loop?.roles[role] || M.ROLES[role];
const time = (v) =>
  new Date(v).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
const job = () => state.jobs.filter((j) => j.loopId === state.loopId).at(-1);
const draftKey = () =>
  `${state.loopId}:${chatScope === "node" ? state.nodeId : "loop"}`;
const input = (name, label, value = "", type = "text", required = true) =>
  `<label class="field">${label}<input name="${name}" type="${type}" value="${esc(value)}" ${required ? "required" : ""} ${type === "number" ? 'step="any"' : 'maxlength="4000"'}></label>`;
const area = (name, label, value = "", required = true) =>
  `<label class="field">${label}<textarea name="${name}" rows="3" maxlength="12000" ${required ? "required" : ""}>${esc(value)}</textarea></label>`;
const select = (name, label, options, value) =>
  `<label class="field">${label}<select name="${name}">${options.map(([k, v]) => `<option value="${k}" ${k === value ? "selected" : ""}>${v}</option>`).join("")}</select></label>`;
function openForm(title, html, fn, submit = "保存", key = "") {
  recoveryForm = false;
  formKey = key;
  handler = fn;
  $("#flow-title").textContent = title;
  $("#flow-fields").innerHTML = html;
  $("#flow-submit").textContent = submit;
  $("#flow-error").textContent = "";
  if (key && state.drafts[`form:${key}`]) {
    try {
      const d = JSON.parse(state.drafts[`form:${key}`]);
      for (const e of $("#flow-form").elements)
        if (e.name && d[e.name] !== undefined) {
          if (e.type === "checkbox") e.checked = d[e.name] === true;
          else e.value = d[e.name];
        }
    } catch {}
  }
  $("#flow-dialog").showModal();
  $("#flow-fields input, #flow-fields textarea, #flow-fields select")?.focus();
}
function details(title, html) {
  $("#detail-title").textContent = title;
  $("#detail-content").innerHTML = html;
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
}
function navigate(id) {
  const node = M.nodeOf(state, id);
  if (!node) return;
  state.loopId = node.loopId;
  state.nodeId = id;
  compareId = null;
  $("#detail-dialog").close();
  document.body.classList.remove("nav-open");
  save();
  render();
}
function home() {
  return `<div class="loop-home"><div class="eyebrow">A SPACE FOR BETTER ITERATIONS</div><h1>一个方向，<br>不止一种可能。</h1><p class="home-lead">与 Agent 讨论、尝试、看清结果。<br>把每一次改进，接进同一段演进。</p><form id="direction-form" class="loop-composer"><label for="direction" class="sr-only">想要改进什么</label><textarea id="direction" name="direction" placeholder="想要改进什么？一段代码、一份文档、一种方法…" required maxlength="4000">${esc(state.drafts.home || "")}</textarea><div><span>先描述目标，再约定如何评价</span><button class="button primary">创建 Loop ↗</button></div></form><div class="template-row">${[
    ["answer", "回答与提示词"],
    ["code", "代码与数据"],
    ["writing", "文档与设计"],
  ]
    .map(
      ([key, t]) =>
        `<button data-action="template" data-id="${key}">${t} <span>↗</span></button>`,
    )
    .join(
      "",
    )}</div><div class="section-heading"><h2>继续探索</h2><span>${state.loops.length} 个 Loops</span></div><div class="loop-cards">${state.loops.map((x) => `<button class="loop-card" data-action="loop" data-id="${x.id}"><span class="card-meta">${x.demo ? "演示样例" : "我的 Loop"} · ${esc(x.artifactKind)}</span><h3>${esc(x.title)}</h3><p>${esc(x.objective)}</p><div>${M.nodesOf(state, x.id).length} 个节点<span>${x.mode === "debate" ? "多角色对抗" : "单角色迭代"} ↗</span></div></button>`).join("")}</div><p class="quiet home-note">演示 Agent 可响应讨论与介入操作；当前不调用模型或执行真实任务。</p></div>`;
}
function sidebar() {
  return state.loops
    .map(
      (x) =>
        `<button class="loop-link ${state.loopId === x.id ? "selected" : ""}" data-action="loop" data-id="${x.id}"><span class="loop-symbol">↻</span><span>${esc(x.title)}<small>${M.nodesOf(state, x.id).filter((n) => n.result).length} 份结果 · ${x.mode === "debate" ? "对抗模式" : "自由迭代"}</small></span></button>`,
    )
    .join("");
}
function execution() {
  const j = job();
  if (!j)
    return `<div class="execution-idle"><span class="local-dot"></span>从任意节点继续 · 讨论随时可用 <span>每轮留下计划、产物和判断</span></div>`;
  const pending = j.pending.length;
  return `<section class="execution-bar"><div class="execution-title"><strong>${j.status === "running" ? "演示运行中" : j.status === "paused" ? "已暂停" : j.reason || "本轮已结束"}</strong><span>${j.used} / ${j.maxRounds} 轮 · ${j.minutes} 分钟上限${pending ? ` · ${pending} 条指令待应用` : ""}</span><div>${j.status === "running" ? `<button data-action="pause" data-id="${j.id}" class="text-button">Ⅱ 暂停</button>` : j.status === "paused" ? `<button data-action="resume" data-id="${j.id}" class="text-button">▷ 继续</button>` : ""}${["running", "paused"].includes(j.status) ? `<button data-action="stop" data-id="${j.id}" class="text-button">结束</button>` : ""}</div></div><div class="phase-track">${M.PHASES.map((p, i) => `<span class="${i < j.phase ? "complete" : i === j.phase && j.status === "running" ? "current" : ""}"><i>${i < j.phase ? "✓" : i + 1}</i>${p}</span>`).join("")}</div>${pending && !["running", "paused"].includes(j.status) ? `<p class="quiet">本轮已停止，排队指令尚未执行，可在讨论中另开分支。</p>` : ""}</section>`;
}
function graph() {
  const nodes = all(),
    g = M.layout(nodes),
    hidden = (id) =>
      filter === "path" && !ancestors(n()).includes(id) && id !== state.nodeId;
  return `<div class="graph-heading"><div><h2>Loop 演进</h2><span>选择节点，查看它的计划、证据与讨论</span></div><div class="graph-tools"><button class="${filter === "all" ? "chosen" : ""}" data-action="graph-filter" data-id="all">全部</button><button class="${filter === "path" ? "chosen" : ""}" data-action="graph-filter" data-id="path">当前路径</button><button data-action="zoom-out" aria-label="缩小演进图">−</button><span>${Math.round(zoom * 100)}%</span><button data-action="zoom-in" aria-label="放大演进图">＋</button><button data-action="fit" aria-label="适应画布">⛶</button></div></div><div class="graph-viewport" id="graph-viewport" tabindex="0" aria-label="Loop 演进图，可横向滚动"><div class="graph-space" style="width:${g.width * zoom}px;height:${g.height * zoom}px"><div class="graph-canvas" style="width:${g.width}px;height:${g.height}px;transform:scale(${zoom})"><svg width="${g.width}" height="${g.height}" class="graph-edges" aria-hidden="true">${g.positions
    .filter((p) => M.nodeOf(state, p.id).parentId)
    .map((p) => {
      const parent = g.positions.find(
        (x) => x.id === M.nodeOf(state, p.id).parentId,
      );
      return `<path class="${hidden(p.id) ? "dim" : ""}" d="M${parent.x + 184},${parent.y + 44} C${parent.x + 207},${parent.y + 44} ${p.x - 25},${p.y + 44} ${p.x},${p.y + 44}"/>`;
    })
    .join(
      "",
    )}</svg>${[...new Set(g.positions.map((p) => p.depth))].map((d) => `<span class="round-label" style="left:${d * 224 + 28}px">${d === 0 ? "起点" : `演进深度 ${d}`}</span>`).join("")}${g.positions
    .map((p) => {
      const node = M.nodeOf(state, p.id),
        c = node.snapshot.criteria[0];
      return `<button class="graph-node ${node.id === state.nodeId ? "selected" : ""} ${hidden(node.id) ? "dim" : ""}" style="left:${p.x}px;top:${p.y}px" data-action="node" data-id="${node.id}" aria-pressed="${node.id === state.nodeId}" aria-label="查看节点：${esc(node.title)}"><span class="node-meta"><span>${node.id.replace("node-", "N")}${node.result ? ` · ${node.result.origin === "demo" ? "示例" : "录入"}` : ""}</span>${node.id === l().currentId ? "<b>当前方案</b>" : tag(node.status)}</span><strong>${esc(node.title)}</strong><span class="node-foot">${node.result ? `${esc(c.name)} <b>${num(node.result.values[c.id])}${esc(c.unit)}</b>` : `${esc(roleName(node.role))} · ${node.debate.length} 条观点`}</span></button>`;
    })
    .join(
      "",
    )}</div></div></div><div class="graph-legend"><span><i class="legend-kept"></i>已采纳</span><span><i class="legend-ready"></i>待验证</span><span><i class="legend-rejected"></i>未采纳 / 有反例</span><span>可滚动 · 节点与右侧讨论联动</span></div>`;
}
function ancestors(node) {
  const ids = [];
  let cur = node;
  while (cur?.parentId) {
    ids.push(cur.parentId);
    cur = M.nodeOf(state, cur.parentId);
  }
  return ids;
}
function trend() {
  const records = all().filter((x) => x.result),
    criterion = l().criteria[0];
  if (criterion.kind === "check") return "";
  const matching = records.filter(
    (x) =>
      M.contractKey(x.snapshot) ===
      M.contractKey({ context: l().context, criteria: l().criteria }),
  );
  const provenance = n()?.result?.origin || "demo",
    points = matching.filter((x) => x.result.origin === provenance);
  if (!points.length)
    return '<p class="quiet">当前评价条件下还没有可绘制的结果。</p>';
  const vals = points.map((x) => x.result.values[criterion.id]),
    lo = Math.min(...vals),
    hi = Math.max(...vals),
    range = hi - lo || 1;
  const xy = points.map((x, i) => ({
    x: 30 + (i * 400) / Math.max(1, points.length - 1),
    y: 84 - ((x.result.values[criterion.id] - lo) * 60) / range,
    node: x,
  }));
  return `<div class="trend-card"><div><h3>${esc(criterion.name)}的变化</h3><span class="quiet">${criterion.direction === "higher" ? "越高越好" : "越低越好"} · ${provenance === "demo" ? "演示" : "用户录入"} · 相同评价条件</span></div><svg viewBox="0 0 460 116" class="trend" role="img" aria-label="${esc(criterion.name)}变化趋势"><path d="M30 92H435" class="chart-axis"/><path d="${xy.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ")}" class="trend-line"/>${xy.map((p) => `<circle cx="${p.x}" cy="${p.y}" r="${p.node.id === state.nodeId ? 6 : 4}" class="${p.node.status === "rejected" ? "failed-point" : ""}"/><text x="${p.x}" y="${p.y - 11}" text-anchor="middle">${num(p.node.result.values[criterion.id])}</text><text x="${p.x}" y="110" text-anchor="middle">${p.node.id.replace("node-", "N")}</text>`).join("")}</svg><div class="trend-links">${points.map((x) => `<button class="text-button" data-action="node" data-id="${x.id}">${x.id.replace("node-", "N")} · ${esc(x.title)}</button>`).join("")}</div><p class="quiet">高分节点可能未通过必要条件；图中保留被拒绝的结果。</p></div>`;
}
function nodeDetail() {
  const node = n(),
    parent = M.nodeOf(state, node.parentId),
    a = M.assess(node);
  return `<section class="node-inspector"><div class="section-heading"><div><span class="eyebrow">${node.id.replace("node-", "N")} · ${parent ? `来自 ${esc(parent.title)}` : "初始记录"}</span><h2>${esc(node.title)}</h2></div>${tag(node.status)}</div><p class="plan-text">${esc(node.plan)}</p>${node.directives.length ? `<div class="steer-note">↳ 已应用的介入：${node.directives.map((x) => esc(x.text)).join("；")}</div>` : ""}${node.result ? `<div class="result-values">${node.snapshot.criteria.map((c) => `<button data-action="cite" data-id="${c.id}"><span>${esc(c.name)}${c.required ? " · 必要条件" : ""}</span><strong>${num(node.result.values[c.id])}<small>${esc(c.unit)}</small></strong></button>`).join("")}</div><div class="result-verdict">${origin(node)}<span class="${a.pass ? "quiet" : "warning-text"}">${esc(a.reason)}</span></div><p>${esc(node.result.summary)}</p>${node.decision ? `<div class="decision-note">${node.decision.verdict === "keep" ? "✓ 采纳依据" : "↳ 保留的失败原因"}：${esc(node.decision.reason)}</div>` : ""}` : `<p class="quiet">尚无本节点的评价结果 · 约定 v${node.snapshot.version}</p>`}<div class="inspector-actions"><button class="button primary" data-action="fork">↳ 从这里继续</button>${node.result ? `<button class="button" data-action="artifact">查看产物与证据</button><button class="text-button" data-action="decide">记录取舍</button><button class="text-button" data-action="memory">沉淀经验</button>` : `${["ready", "interrupted"].includes(node.status) ? '<button class="button" data-action="run">▷ 演示验证</button>' : '<button class="button" data-action="view" data-id="plans">查看计划讨论</button>'}<button class="text-button" data-action="import-result">录入结果</button>`}</div></section>`;
}
function plans() {
  const node = n(),
    list = all().filter(
      (x) => x.parentId === node.parentId && x.parentId !== null,
    );
  return `<div class="view-heading"><h2>先让不同观点碰一碰</h2><p>提出计划、互相质疑、保留答辩。选择验证之后，结果仍需单独验收。</p></div><div class="mode-strip"><span>${l().mode === "debate" ? "多角色对抗 · 观点与权限分离" : "单角色迭代 · 可随时切换对抗模式"}</span><button class="text-button" data-action="pair">从当前节点提出两个方案 ＋</button></div><div class="plan-columns">${(list.length
    ? list
    : [node]
  )
    .map(
      (x) =>
        `<article class="proposal-card ${x.id === node.id ? "selected" : ""}"><header><span class="role-avatar role-${x.role}">${esc(roleName(x.role).slice(0, 1))}</span><div><span class="quiet">${esc(roleName(x.role))}</span><h3><button data-action="node" data-id="${x.id}">${esc(x.title)}</button></h3></div>${tag(x.status)}</header><p>${esc(x.plan)}</p><div class="debate-log">${
          x.debate
            .filter((d) => d.kind !== "proposal")
            .map(
              (d) =>
                `<div class="debate-entry"><span>${esc(roleName(d.role))} · ${{ critique: "质疑", rebuttal: "答辩", select: "选择" }[d.kind]}${d.demo ? " · 演示" : ""}</span><p>${esc(d.text)}</p></div>`,
            )
            .join("") ||
          '<p class="quiet">还没有质疑。将担心的假设与失败条件具体写下来。</p>'
        }</div><div class="card-actions">${x.result ? `<button class="text-button" data-action="node" data-id="${x.id}">查看结果与判断 ↗</button>` : x.status === "proposed" ? `<button class="button" data-action="debate" data-id="${x.id}" data-kind="critique">提出质疑</button>` : x.status === "challenged" ? `<button class="button" data-action="debate" data-id="${x.id}" data-kind="rebuttal">补充答辩</button>${x.debate.some((d) => d.kind === "rebuttal") ? `<button class="button primary" data-action="debate" data-id="${x.id}" data-kind="select">选择验证</button>` : ""}` : ["ready", "interrupted"].includes(x.status) ? `<button class="button primary" data-action="run" data-id="${x.id}">▷ 演示验证</button>` : ""}${!x.result && !["running", "paused"].includes(x.status) ? `<button class="text-button" data-action="revise-plan" data-id="${x.id}">修改计划</button>` : ""}</div></article>`,
    )
    .join(
      "",
    )}</div><div class="authority-strip"><strong>角色如何配合</strong><span>协调者组织选择</span><span>方案角色相互质疑</span><span>执行者留下产物</span><span>观察者记录过程问题</span><span>审阅者检查证据</span><button class="text-button" data-action="view" data-id="contract">配置角色 ↗</button></div>`;
}
function results() {
  const records = all().filter((x) => x.result),
    node = n(),
    ref =
      records.find((x) => x.id === compareId) ||
      records.find((x) => x.id !== node.id),
    c = M.comparison(ref, node);
  return `<div class="view-heading"><h2>结果与取舍</h2><p>保留收益，也保留未满足的条件。所有判断都能回到产物。</p></div><div class="results-table-wrap"><table><thead><tr><th>节点 / 产物</th><th>结果</th><th>来源与状态</th></tr></thead><tbody>${records.map((x) => `<tr class="${x.id === node.id ? "selected" : ""}"><td><button class="text-button" data-action="node" data-id="${x.id}">${x.id.replace("node-", "N")} · ${esc(x.title)}</button></td><td>${x.snapshot.criteria.map((c) => `${esc(c.name)}：${num(x.result.values[c.id])} ${esc(c.unit)}`).join("<br>")}</td><td>${origin(x)}<br>${tag(x.status)}</td></tr>`).join("") || '<tr><td colspan="3">还没有结果。可以演示验证，或录入自己的产物与评价。</td></tr>'}</tbody></table></div>${
    node.result
      ? `<div class="compare-box"><label>对照节点 <select id="compare-node">${records
          .filter((x) => x.id !== node.id)
          .map(
            (x) =>
              `<option value="${x.id}" ${x.id === ref?.id ? "selected" : ""}>${esc(x.title)}</option>`,
          )
          .join(
            "",
          )}</select></label><span>→ 当前：${esc(node.title)}</span><p>${esc(c.reason)}</p>${c.ok ? `<div class="compare-values">${c.values.map((v) => `<div><span>${esc(v.name)}</span><strong>${num(v.left)} → ${num(v.right)}</strong><small>${v.delta !== null ? `差值 ${v.delta > 0 ? "+" : ""}${num(v.delta)}` : "按必要条件逐项判断"}</small></div>`).join("")}</div>` : ""}</div>`
      : ""
  }${nodeDetail()}`;
}
function knowledge() {
  const sources = state.sources.filter((x) => x.loopId === state.loopId);
  return `<div class="view-heading"><h2>线索与经验</h2><p>外部方法带着出处进入；本地尝试带着适用条件留下。</p></div><div class="section-heading"><h3>参考资料</h3><button class="button" data-action="source">＋ 添加资料</button></div><div class="knowledge-list">${sources.map((r) => `<article class="source-card"><span class="quiet">${esc(r.version)} · 待验证线索</span><h3><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)} ↗</a></h3><p>${esc(r.note)}</p><button class="text-button" data-action="source-fork" data-id="${r.id}">带着这条线索继续 ↗</button></article>`).join("") || '<p class="empty-inline">添加论文、文档或案例；当前原型不会自动获取网页内容。</p>'}</div><div class="section-heading"><h3>可回引的经验</h3><span>来自全部 Loops</span></div>${state.memories.map((m) => `<article class="memory-card"><span class="tag ${m.status === "contested" ? "warning" : ""}">${m.status === "contested" ? "存在反例" : "经验草稿"} · ${m.origin === "demo" ? "演示" : "用户录入"}</span><h3>${esc(m.title)}</h3><p>${esc(m.claim)}</p><p class="condition-note">适用条件：${esc(m.conditions)}</p><p class="quiet">${M.contractKey(m.snapshot) === M.contractKey(n().snapshot) ? "条件一致，仍需判断适用性。" : "当前条件不同，引用后需要复验。"}</p><div class="card-actions"><button class="text-button" data-action="node" data-id="${m.nodeId}">回到证据</button><button class="text-button" data-action="use-memory" data-id="${m.id}">引用到当前节点</button><button class="text-button" data-action="counterexample" data-id="${m.id}">记录反例</button></div>${m.history.length ? `<details><summary>判定历史 · ${m.history.length}</summary>${m.history.map((h) => `<p>${esc(h.reason)}</p>`).join("")}</details>` : ""}</article>`).join("") || '<p class="empty-inline">从一份结果中记录经验，保留它成立的条件。</p>'}`;
}
function contract() {
  const loop = l();
  return `<div class="view-heading"><h2>这段 Loop 的共同约定</h2><p>目标、产物和评价方式可以用于代码、内容、研究或其他迭代任务。旧节点始终保留旧约定。</p></div><div class="toolbar"><span class="tag">版本 ${loop.version}</span><button class="button" data-action="edit-contract">编辑后续约定</button></div><dl class="context-grid"><dt>目标</dt><dd>${esc(loop.objective)}</dd><dt>产物</dt><dd>${esc(loop.artifactKind)}</dd><dt>评价条件</dt><dd>${esc(loop.context)}</dd><dt>协作方式</dt><dd>${loop.mode === "debate" ? "多角色对抗：提出 → 质疑 → 答辩 → 选择 → 验证" : "单角色迭代：计划 → 尝试 → 评价 → 再迭代"}</dd></dl><h3>评价标准</h3><div class="criteria-list">${loop.criteria.map((c) => `<div><strong>${esc(c.name)}</strong><span>${{ metric: "数值指标", rubric: "0–100 评分", check: "通过 / 失败 / 待判断" }[c.kind]}</span><span>${c.kind === "check" ? "逐项检查" : `${c.direction === "higher" ? "越高越好" : "越低越好"}${c.target !== null ? ` · 目标 ${c.target} ${esc(c.unit)}` : ""}`}</span><span class="tag">${c.required ? "必须满足" : "用于比较"}</span></div>`).join("")}</div><h3 class="roles-title">角色分工</h3><div class="role-grid">${["lead", "researcher", "championA", "championB", "executor", "monitor", "auditor"].map((r) => `<div><span class="role-avatar role-${r}">${esc(roleName(r).slice(0, 1))}</span><strong>${esc(roleName(r))}</strong><small>${{ lead: "组织阶段与候选选择", researcher: "整理起点和外部线索", championA: "提出并捍卫一种方案", championB: "提出替代方案与质疑", executor: "产生可检查的产物", monitor: "提醒目标偏离或过程异常", auditor: "检查评价依据与必要条件" }[r]}</small></div>`).join("")}</div><p class="quiet">角色名称可配置。原型用规则演示角色回应；不表示已启动多个模型或远程 Agent。</p>`;
}
function chat() {
  const loop = l(),
    node = n(),
    messages = state.messages.filter(
      (m) =>
        m.loopId === loop.id && (chatScope === "loop" || m.nodeId === node.id),
    );
  return `<aside class="conversation ${mobileChat ? "mobile-open" : ""}" aria-label="Agent 讨论"><header><div><span class="chat-live-dot"></span><h2>一起往下想</h2></div><button class="icon-button chat-close" data-action="chat-close" aria-label="关闭讨论区">×</button><p>演示 Agent · 运行时也能讨论</p></header><div class="chat-scope"><label>讨论范围<select id="chat-scope"><option value="node" ${chatScope === "node" ? "selected" : ""}>当前节点 · ${esc(node.title)}</option><option value="loop" ${chatScope === "loop" ? "selected" : ""}>整个 Loop</option></select></label></div><div class="chat-messages" id="chat-messages">${
    messages
      .map((m) => {
        const suggestion = state.suggestions.find(
          (p) => p.id === m.suggestionId,
        );
        return `<article class="chat-message ${m.role === "you" ? "from-you" : ""}"><div class="message-by"><span class="role-avatar role-${m.role}">${esc(roleName(m.role).slice(0, 1))}</span><strong>${esc(roleName(m.role))}</strong><small>${m.demo ? "演示回应" : m.role === "you" ? "" : "记录"} · ${time(m.at)}</small></div>${chatScope === "loop" && m.nodeId ? `<button class="scope-reference" data-action="node" data-id="${m.nodeId}">↳ ${esc(M.nodeOf(state, m.nodeId)?.title)}</button>` : ""}<p>${esc(m.text)}</p>${suggestion ? `<div class="suggestion"><span>${suggestion.status === "pending" ? "建议的下一步" : suggestion.status === "queued" ? "已排入下一轮 · 尚未执行" : "已应用到新节点"}</span><p>${esc(suggestion.text)}</p>${suggestion.status === "pending" ? `<div><button data-action="apply" data-id="${suggestion.id}" data-mode="branch">另开分支 ↗</button>${state.jobs.some((j) => j.loopId === loop.id && ["running", "paused"].includes(j.status)) ? `<button data-action="apply" data-id="${suggestion.id}" data-mode="next">下一轮应用</button>` : ""}</div>` : suggestion.status === "queued" ? `<button data-action="queued-fork" data-id="${suggestion.id}">改为独立分支 ↗</button>` : `<button data-action="node" data-id="${suggestion.appliedNodeId}">查看应用节点 ↗</button>`}</div>` : ""}</article>`;
      })
      .join("") ||
    '<div class="chat-empty">从一个问题开始。可以讨论计划、引用结果，或给下一轮一个新方向。</div>'
  }</div><form id="chat-form" class="chat-form"><div class="chat-controls"><select id="chat-role" aria-label="讨论角色">${Object.keys(
    M.ROLES,
  )
    .filter((r) => r !== "you")
    .map(
      (r) =>
        `<option value="${r}" ${r === chatRole ? "selected" : ""}>@ ${esc(roleName(r))}</option>`,
    )
    .join("")}</select><select id="chat-intent" aria-label="讨论方式">${[
    ["discuss", "讨论"],
    ["challenge", "提出质疑"],
    ["steer", "调整下一轮"],
    ["plan", "提出新方案"],
  ]
    .map(
      ([k, t]) =>
        `<option value="${k}" ${k === chatIntent ? "selected" : ""}>${t}</option>`,
    )
    .join(
      "",
    )}</select></div><label for="chat-text" class="sr-only">与 Agent 讨论</label><textarea id="chat-text" name="message" required maxlength="4000" placeholder="对结果有疑问？或想让下一轮换个方向…">${esc(state.drafts[draftKey()] || "")}</textarea><div class="chat-send"><span>规则演示 · 未接真实模型</span><button class="send-button" aria-label="发送讨论">↑</button></div></form></aside>`;
}
function primaryControl() {
  const running = job(),
    node = n();
  if (running && ["running", "paused"].includes(running.status))
    return `<button class="button primary" data-action="node" data-id="${running.nodeId}">查看运行节点 ↗</button>`;
  if (node.result || node.status === "baseline")
    return '<button class="button primary" data-action="fork">↳ 从这里迭代</button>';
  if (["proposed", "challenged"].includes(node.status))
    return '<button class="button primary" data-action="view" data-id="plans">讨论这份计划 ↗</button>';
  return '<button class="button primary" data-action="run">▷ 演示运行</button>';
}
function workspace() {
  const loop = l(),
    node = n(),
    tabs = [
      ["evolution", "演进"],
      ["plans", "计划讨论"],
      ["results", "结果"],
      ["knowledge", "资料与经验"],
      ["contract", "约定"],
    ];
  return `<div class="loop-page-head"><div><span class="eyebrow">${esc(loop.artifactKind)} <span> / </span> ${loop.mode === "debate" ? "多角色对抗" : "单角色迭代"}</span><h1>${esc(loop.title)}</h1><p>${esc(loop.objective)}</p></div><div class="loop-head-actions"><button class="button" data-action="pair">＋ 提出方案</button>${primaryControl()}</div></div><div class="loop-workspace"><div class="loop-stage">${execution()}<nav class="loop-tabs" aria-label="Loop 视图">${tabs.map(([key, title]) => `<button data-action="view" data-id="${key}" class="${state.view === key ? "selected" : ""}" aria-current="${state.view === key ? "page" : "false"}">${title}</button>`).join("")}</nav><div class="loop-view">${{ evolution: () => `${graph()}${nodeDetail()}${trend()}`, plans, results, knowledge, contract }[state.view]()}</div></div>${chat()}</div><button class="chat-mobile-trigger button primary" data-action="chat-open">讨论 · ${esc(roleName(chatRole))} ↗</button>`;
}
function render() {
  const active = document.activeElement,
    id = active?.id,
    pos = active?.selectionStart,
    end = active?.selectionEnd;
  const scroll = $("#chat-messages")?.scrollTop,
    graphX = $("#graph-viewport")?.scrollLeft,
    graphY = $("#graph-viewport")?.scrollTop;
  $("#loop-list").innerHTML = sidebar();
  $("#loop-count").textContent = state.loops.length;
  $("#breadcrumb").innerHTML =
    `<button data-action="home">我的工作台</button>${l() ? `<span>/</span><span>${esc(l().title)}</span>` : ""}`;
  $("#content").innerHTML = l() && n() ? workspace() : home();
  if (id && !active.isConnected) {
    const el = document.getElementById(id);
    el?.focus({ preventScroll: true });
    if (el?.setSelectionRange && pos !== null) el.setSelectionRange(pos, end);
  }
  if (scroll !== undefined && $("#chat-messages"))
    $("#chat-messages").scrollTop = scroll;
  if (graphX !== undefined && $("#graph-viewport")) {
    $("#graph-viewport").scrollLeft = graphX;
    $("#graph-viewport").scrollTop = graphY;
  }
}
function criteriaEditor(criteria) {
  return `<div class="criteria-editor"><p class="quiet">支持数值、0–100 评分、检查项；最多 8 项。勾选“必须满足”的项目不能被其他高分抵消。</p>${criteria
    .map(
      (c, i) =>
        `<fieldset class="criterion-row"><legend>标准 ${i + 1}</legend><input type="hidden" name="cid-${i}" value="${esc(c.id)}">${input(`name-${i}`, "名称", c.name)}${select(
          `kind-${i}`,
          "类型",
          [
            ["metric", "数值"],
            ["rubric", "评分"],
            ["check", "检查项"],
          ],
          c.kind,
        )}${select(
          `direction-${i}`,
          "方向",
          [
            ["higher", "越高越好"],
            ["lower", "越低越好"],
          ],
          c.direction,
        )}${input(`unit-${i}`, "单位", c.unit, "text", false)}${input(`target-${i}`, "目标（可留空）", c.target ?? "", "number", false)}<label class="check-label"><input name="required-${i}" type="checkbox" ${c.required ? "checked" : ""}>必须满足</label></fieldset>`,
    )
    .join(
      "",
    )}<button type="button" class="text-button" data-action="add-criterion">＋ 添加评价标准</button></div>`;
}
function criteriaFrom(f) {
  const result = [];
  for (let i = 0; f.has(`cid-${i}`); i++) {
    if (!String(f.get(`name-${i}`)).trim()) continue;
    const kind = f.get(`kind-${i}`);
    result.push({
      id: f.get(`cid-${i}`),
      name: f.get(`name-${i}`),
      kind,
      direction: f.get(`direction-${i}`),
      unit: kind === "check" ? "" : f.get(`unit-${i}`),
      target:
        kind === "check" || f.get(`target-${i}`) === ""
          ? null
          : Number(f.get(`target-${i}`)),
      required: f.has(`required-${i}`),
    });
  }
  return M.validateCriteria(result);
}
function newLoop(template = "custom", direction = "") {
  const t = structuredClone(M.TEMPLATES[template] || M.TEMPLATES.custom);
  openForm(
    "开始一个 Loop",
    `${input("title", "Loop 名称", direction.slice(0, 180) || t.title)}${area("objective", "你希望实现什么", direction || t.objective)}${input("artifactKind", "这段过程会产生什么", t.artifactKind)}${area("context", "如何保证各轮评价可比较", t.context, false)}${select(
      "mode",
      "协作方式",
      [
        ["solo", "单角色迭代"],
        ["debate", "多角色对抗 · 先质疑，再验证"],
      ],
      t.mode,
    )}<details open><summary>评价标准</summary>${criteriaEditor(t.criteria)}</details>`,
    (f) => {
      M.createLoop(state, {
        title: f.get("title"),
        objective: f.get("objective"),
        artifactKind: f.get("artifactKind"),
        context: f.get("context"),
        criteria: criteriaFrom(f),
        mode: f.get("mode"),
      });
      document.body.classList.remove("nav-open");
    },
    "创建 Loop",
    `new:${template}`,
  );
}
function forkForm(parent = n(), preset = {}) {
  openForm(
    "从这里探索另一条路径",
    `<p class="dialog-intro">起点：${esc(parent.title)}。新节点保留当前约定与来源，旧结果不变。</p>${input("title", "方案名称", preset.title || "")}${area("plan", "这一步想尝试什么", preset.plan || "")}${select(
      "role",
      "提出方案的角色",
      [
        ["championA", roleName("championA")],
        ["championB", roleName("championB")],
        ["lead", roleName("lead")],
      ],
      "championA",
    )}`,
    (f) =>
      M.fork(state, parent.id, {
        title: f.get("title"),
        plan: f.get("plan"),
        role: f.get("role"),
        sourceId: preset.sourceId,
      }),
    "创建分支",
    `fork:${parent.id}`,
  );
}
function runForm(node = n()) {
  if (!["ready", "interrupted"].includes(node.status) || node.result) {
    state.nodeId = node.id;
    state.view = "plans";
    save();
    render();
    toast("先选择一份可验证的计划；已有结果可从这里继续分支。");
    return;
  }
  const loop = M.loopOf(state, node.loopId);
  openForm(
    "给这轮探索一个边界",
    `<p class="dialog-intro">${esc(node.title)} · 演示执行，不处理真实文件。${loop.mode === "debate" ? "下一轮生成的新计划会重新进入质疑与答辩。" : ""}</p><div class="form-grid">${input("rounds", "最多几轮", loop.maxRounds, "number")}${input("minutes", "最多几分钟", loop.minutes, "number")}</div><p class="quiet">约 7 秒一轮。必要条件失败时提前返回；讨论和介入始终可用。</p>`,
    (f) =>
      M.start(state, node.id, {
        maxRounds: Number(f.get("rounds")),
        minutes: Number(f.get("minutes")),
      }),
    "开始演示",
    `run:${node.id}`,
  );
}
function resultForm() {
  const node = n();
  openForm(
    "录入产物与评价",
    `<p class="dialog-intro">用户录入 · 未核验。原型不会读取产物中的路径或执行内容。</p><button class="button" type="button" data-action="result-json">从 JSON 填入</button><div class="form-grid">${node.snapshot.criteria
      .map((c) =>
        c.kind === "check"
          ? select(
              c.id,
              c.name,
              [
                ["unknown", "待判断"],
                ["pass", "通过"],
                ["fail", "失败"],
              ],
              "unknown",
            )
          : input(c.id, `${esc(c.name)} ${esc(c.unit)}`, "", "number"),
      )
      .join(
        "",
      )}</div>${area("summary", "评价说明")}${area("artifact", "产物内容 / 原始证据引用")}`,
    (f) => {
      const values = {};
      for (const c of node.snapshot.criteria)
        values[c.id] = c.kind === "check" ? f.get(c.id) : Number(f.get(c.id));
      M.importEvaluation(state, node.id, {
        values,
        artifact: f.get("artifact"),
        summary: f.get("summary"),
      });
    },
    "保存评价",
    `result:${node.id}`,
  );
}
function exportBackup() {
  const text = raw || JSON.stringify(state, null, 2);
  if (backupURL) URL.revokeObjectURL(backupURL);
  backupURL = URL.createObjectURL(
    new Blob([text], { type: "application/json" }),
  );
  details(
    "保存 Loop 备份",
    `<p>包含节点、讨论、产物、资料与经验。${raw ? "这是无法读取的原始数据。" : ""}</p><label>完整 JSON<textarea id="backup-json" rows="10" readonly>${esc(text)}</textarea></label><div class="card-actions"><a class="button primary" href="${backupURL}" download="euboulia-loops.json">下载 JSON</a><button class="button" data-action="copy-backup">复制完整内容</button></div>`,
  );
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-action]");
  if (!b) return;
  const { action, id, kind, mode } = b.dataset;
  if (action === "close-dialog") {
    $("#flow-dialog").close();
    return;
  }
  if (action === "close-detail") {
    $("#detail-dialog").close();
    return;
  }
  if (action === "home") {
    act(() => {
      state.loopId = null;
      state.nodeId = null;
      document.body.classList.remove("nav-open");
    });
    return;
  }
  if (action === "nav") {
    document.body.classList.toggle("nav-open");
    return;
  }
  if (action === "chat-open" || action === "chat-close") {
    mobileChat = action === "chat-open";
    render();
    return;
  }
  if (action === "new" || action === "template") {
    newLoop(id);
    return;
  }
  if (action === "loop") {
    navigate(M.loopOf(state, id)?.currentId);
    return;
  }
  if (action === "node") {
    navigate(id);
    return;
  }
  if (action === "view") {
    act(() => (state.view = id));
    return;
  }
  if (action === "graph-filter") {
    filter = id;
    render();
    return;
  }
  if (["zoom-in", "zoom-out", "fit"].includes(action)) {
    zoom =
      action === "fit"
        ? Math.min(
            1,
            Math.max(
              0.4,
              ($("#graph-viewport")?.clientWidth || 700) /
                M.layout(all()).width,
            ),
          )
        : Math.min(
            1.5,
            Math.max(0.4, zoom + (action === "zoom-in" ? 0.1 : -0.1)),
          );
    render();
    return;
  }
  if (action === "fork") {
    forkForm();
    return;
  }
  if (action === "run") {
    runForm(id ? M.nodeOf(state, id) : n());
    return;
  }
  if (action === "pair") {
    act(() => {
      M.proposePair(state, state.nodeId);
      state.view = "plans";
    });
    return;
  }
  if (action === "debate") {
    const node = M.nodeOf(state, id);
    if (kind === "select") {
      act(() => M.debateStep(state, id, kind));
      return;
    }
    openForm(
      kind === "critique" ? "提出一个具体质疑" : "回应这份质疑",
      `<p class="dialog-intro">${esc(node.title)}。写下自己的观点；留空可生成带有演示标记的示例。</p>${area("text", kind === "critique" ? "担心哪条假设或失败条件" : "用什么证据或调整回应", "", false)}`,
      (f) => M.debateStep(state, id, kind, f.get("text")),
      kind === "critique" ? "记录质疑" : "记录答辩",
      `${kind}:${id}`,
    );
    return;
  }
  if (action === "revise-plan") {
    const node = M.nodeOf(state, id);
    openForm(
      "修改计划",
      `${area("plan", "计划", node.plan)}<p class="quiet">旧计划保存在历史中，修改后重新讨论。已完成的产物请另开分支。</p>`,
      (f) => M.revisePlan(state, id, f.get("plan")),
      "保存计划",
      `plan:${id}`,
    );
    return;
  }
  if (action === "pause" || action === "resume" || action === "stop") {
    act(() => M[action](state, id));
    return;
  }
  if (action === "apply") {
    act(() => M.applySuggestion(state, id, mode));
    return;
  }
  if (action === "queued-fork") {
    act(() => {
      const p = state.suggestions.find((p) => p.id === id);
      if (p?.status !== "queued") throw Error("这条指令已经应用。");
      for (const j of state.jobs)
        j.pending = j.pending.filter((x) => x.suggestionId !== id);
      p.status = "pending";
      M.applySuggestion(state, id, "branch");
    });
    return;
  }
  if (action === "cite") {
    chatScope = "node";
    chatIntent = "discuss";
    state.drafts[draftKey()] =
      `关于 ${n().id.replace("node-", "N")}「${n().title}」的${n().snapshot.criteria.find((c) => c.id === id)?.name}（${num(n().result.values[id])}），`;
    save();
    mobileChat = true;
    render();
    $("#chat-text")?.focus();
    return;
  }
  if (action === "import-result") {
    resultForm();
    return;
  }
  if (action === "result-json") {
    $("#result-input").click();
    return;
  }
  if (action === "artifact") {
    const node = n();
    details(
      "产物与原始证据",
      `${origin(node)}<h3>${esc(node.title)}</h3><pre class="artifact-text">${esc(node.result.artifact)}</pre><h3>执行 / 评价快照</h3><pre class="artifact-text">${esc(JSON.stringify(node.execution || { plan: node.plan, snapshot: node.snapshot }, null, 2))}</pre>`,
    );
    return;
  }
  if (action === "decide") {
    openForm(
      "这一步如何取舍",
      `${select(
        "verdict",
        "判断",
        [
          ["keep", "采纳为当前方案"],
          ["reject", "暂不采纳，保留这条路径"],
        ],
        "reject",
      )}${area("reason", "依据与适用范围", n().decision?.reason || "")}<p class="quiet">必要条件未满足时无法采纳。演示判断仍保留演示来源。</p>`,
      (f) => M.decide(state, state.nodeId, f.get("verdict"), f.get("reason")),
      "记录判断",
      `decision:${state.nodeId}`,
    );
    return;
  }
  if (action === "source") {
    openForm(
      "保存一条外部线索",
      `${input("title", "资料名称")}${input("url", "原文链接", "", "url")}${input("version", "日期或版本")}${area("note", "与当前任务有什么关系")}`,
      (f) => M.addSource(state, Object.fromEntries(f)),
      "保存资料",
      `source:${state.loopId}`,
    );
    return;
  }
  if (action === "source-fork") {
    const r = state.sources.find((x) => x.id === id);
    forkForm(n(), {
      title: `验证：${r.title}`.slice(0, 180),
      plan: r.note,
      sourceId: id,
    });
    return;
  }
  if (action === "memory") {
    openForm(
      "把这次发现留下来",
      `${input("title", "经验名称")}${area("claim", "可以参考的经验")}${area("conditions", "成立的条件", n().snapshot.context)}`,
      (f) => M.saveMemory(state, state.nodeId, Object.fromEntries(f)),
      "保存经验草稿",
      `memory:${state.nodeId}`,
    );
    return;
  }
  if (action === "use-memory") {
    act(() => M.useMemory(state, state.nodeId, id));
    return;
  }
  if (action === "counterexample") {
    openForm(
      "记录反例",
      area("reason", "在哪些条件下不再成立"),
      (f) => M.verdictMemory(state, id, "contested", f.get("reason")),
      "保留反例",
    );
    return;
  }
  if (action === "edit-contract") {
    const loop = l();
    openForm(
      "修改后续约定",
      `${area("context", "评价条件", loop.context)}${select(
        "mode",
        "协作模式",
        [
          ["solo", "单角色迭代"],
          ["debate", "多角色对抗"],
        ],
        loop.mode,
      )}${criteriaEditor(loop.criteria)}<details><summary>角色名称</summary><div class="form-grid">${Object.keys(
        M.ROLES,
      )
        .filter((r) => r !== "you")
        .map((r) => input(`role-${r}`, M.ROLES[r], loop.roles[r]))
        .join("")}</div></details>`,
      (f) =>
        M.updateContract(state, loop.id, {
          context: f.get("context"),
          criteria: criteriaFrom(f),
          mode: f.get("mode"),
          roles: Object.fromEntries(
            Object.keys(M.ROLES)
              .filter((r) => r !== "you")
              .map((r) => [r, f.get(`role-${r}`)]),
          ),
        }),
      "更新后续约定",
      `contract:${loop.id}`,
    );
    return;
  }
  if (action === "add-criterion") {
    try {
      const criteria = criteriaFrom(new FormData($("#flow-form")));
      if (criteria.length >= 8) throw Error("最多 8 项标准。");
      let serial = criteria.length + 1;
      while (criteria.some((c) => c.id === `custom-${serial}`)) serial++;
      criteria.push({
        id: `custom-${serial}`,
        name: "新标准",
        kind: "check",
        direction: "higher",
        unit: "",
        target: null,
        required: true,
      });
      $(".criteria-editor").outerHTML = criteriaEditor(criteria);
    } catch (error) {
      $("#flow-error").textContent = error.message;
    }
    return;
  }
  if (action === "export") {
    exportBackup();
    return;
  }
  if (action === "copy-backup") {
    navigator.clipboard
      .writeText($("#backup-json").value)
      .then(() => toast("完整备份已复制"))
      .catch(() => {
        $("#backup-json").select();
        toast("请按复制快捷键保存完整内容。");
      });
    return;
  }
  if (action === "import-backup") {
    $("#backup-input").click();
    return;
  }
  if (action === "about") {
    details(
      "Loop 工作台 · v0.5",
      `<p>面向代码、内容、研究等迭代任务的本地交互原型。Agent 讨论、提案和运行由规则演示；尚未接入真实模型、文件执行或自动网页检索。</p><p>研究分支是产品记录，不创建 Git checkout。评价由演示或你录入，不能替代真实验收。</p><div class="card-actions"><button class="button" data-action="export">导出备份</button><button class="button" data-action="import-backup">导入备份</button></div><p><a href="research.html">打开 v0.4 研究工作台与原数据 ↗</a></p><p><a href="team.html">历史研究室 ↗</a></p><p>参考：<a href="https://docs.weco.ai/using-weco/steerability" target="_blank" rel="noopener noreferrer">Weco 的分支与介入</a> · <a href="https://amazon-science.github.io/ammo/AMMO_arXiv_Paper.pdf" target="_blank" rel="noopener noreferrer">AMMO 的对抗提案与验收</a></p>`,
    );
    return;
  }
  if (action === "search") {
    details(
      "搜索 Loop",
      `<label>名称、计划或目标<input type="search" id="loop-search" placeholder="搜索 Loops 和节点…"></label><div id="search-results"></div>`,
    );
    $("#loop-search").focus();
    return;
  }
});
document.addEventListener("submit", (e) => {
  if (e.target.id === "direction-form") {
    e.preventDefault();
    newLoop("custom", new FormData(e.target).get("direction"));
    return;
  }
  if (e.target.id === "chat-form") {
    e.preventDefault();
    act(() =>
      M.submitMessage(state, {
        text: new FormData(e.target).get("message"),
        nodeId: chatScope === "node" ? state.nodeId : null,
        role: chatRole,
        intent: chatIntent,
      }),
    );
    const panel = $("#chat-messages");
    if (panel) panel.scrollTop = panel.scrollHeight;
    return;
  }
  if (e.target.id === "flow-form") {
    e.preventDefault();
    if (blocked && !recoveryForm) {
      $("#flow-error").textContent = "请先导出记录并刷新。";
      return;
    }
    const before = structuredClone(state);
    try {
      handler(new FormData(e.target));
      if (recoveryForm) {
        blocked = false;
        raw = null;
        lastSaved = localStorage.getItem(M.KEY);
        $("#storage-warning").hidden = true;
      }
      delete state.drafts[`form:${formKey}`];
      save();
      $("#flow-dialog").close();
      render();
    } catch (error) {
      state = before;
      $("#flow-error").textContent = error.message;
    }
  }
});
document.addEventListener("input", (e) => {
  if (blocked) return;
  if (e.target.id === "chat-text") {
    state.drafts[draftKey()] = e.target.value;
    save();
  }
  if (e.target.id === "direction") {
    state.drafts.home = e.target.value;
    save();
  }
  if (e.target.closest("#flow-form") && formKey) {
    const values = {};
    for (const el of $("#flow-form").elements)
      if (el.name)
        values[el.name] = el.type === "checkbox" ? el.checked : el.value;
    state.drafts[`form:${formKey}`] = JSON.stringify(values);
    save();
  }
  if (e.target.id === "loop-search") {
    const query = e.target.value.trim().toLowerCase();
    $("#search-results").innerHTML =
      state.nodes
        .filter((x) =>
          (x.title + x.plan + M.loopOf(state, x.loopId).title)
            .toLowerCase()
            .includes(query),
        )
        .slice(0, 30)
        .map(
          (x) =>
            `<button class="search-result" data-action="node" data-id="${x.id}">${esc(x.title)}<small>${esc(M.loopOf(state, x.loopId).title)}</small></button>`,
        )
        .join("") || "<p>没有匹配的记录。</p>";
  }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "chat-scope") {
    chatScope = e.target.value;
    render();
  }
  if (e.target.id === "chat-role") chatRole = e.target.value;
  if (e.target.id === "chat-intent") chatIntent = e.target.value;
  if (e.target.id === "compare-node") {
    compareId = e.target.value;
    render();
  }
});
$("#backup-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    if (file.size > 5000000) throw Error("备份上限 5 MB。");
    const restored = M.decode(await file.text());
    $("#detail-dialog").close();
    openForm(
      "恢复这份 Loop 备份",
      `<p>${restored.loops.length} 个 Loops · ${restored.nodes.length} 个节点 · ${restored.messages.length} 条讨论。</p><p>替换当前 Loop 数据，旧版研究工作台不受影响。运行中的演示会标为中断。</p><label class="check-label"><input type="checkbox" name="confirm" required>我已导出需要保留的内容，同意替换</label>`,
      (f) => {
        if (!f.has("confirm")) throw Error("请确认替换。");
        state = restored;
      },
      "恢复备份",
    );
    recoveryForm = true;
  } catch (e) {
    toast(e.message);
  }
});
$("#result-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    if (file.size > 256000) throw Error("结果 JSON 上限 256 KB。");
    const data = JSON.parse(await file.text());
    if (
      data.snapshot &&
      M.contractKey(data.snapshot) !== M.contractKey(n().snapshot)
    )
      throw Error("JSON 的评价条件与当前节点不一致。");
    M.validateValues(n().snapshot.criteria, data.values);
    if (typeof data.artifact !== "string" || typeof data.summary !== "string")
      throw Error("需要 artifact 与 summary 文本。");
    for (const [key, value] of Object.entries({
      ...data.values,
      artifact: data.artifact,
      summary: data.summary,
    })) {
      const field = $("#flow-form").elements.namedItem(key);
      if (field) field.value = value;
    }
    toast("已填入，请检查后保存。");
  } catch (e) {
    $("#flow-error").textContent = e.message;
  }
});
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "k") {
    e.preventDefault();
    document.querySelector('[data-action="search"]').click();
  }
  if (e.key === "Escape") {
    const wasOpen = mobileChat;
    mobileChat = false;
    document.body.classList.remove("nav-open");
    if (wasOpen) render();
  }
});
window.addEventListener("storage", (e) => {
  if (e.key === M.KEY && e.newValue !== lastSaved) {
    blocked = true;
    warning("另一标签页更新了记录。本页停止保存，请先导出再刷新。");
  }
});
$("#detail-dialog").addEventListener("close", () => {
  if (backupURL) {
    URL.revokeObjectURL(backupURL);
    backupURL = null;
  }
});
setInterval(() => {
  if (!blocked && M.tick(state)) {
    save();
    render();
  }
}, 450);
render();
