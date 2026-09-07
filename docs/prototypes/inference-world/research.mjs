import {
  STORAGE_KEY,
  createDemo,
  decodeState,
  createStudy,
  studyOf,
  branchOf,
  runsOf,
  latestRun,
  forkBranch,
  addMessage,
  reviseProtocol,
  importResult,
  comparable,
  addSource,
  saveMemory,
  reviseMemory,
  memoryFit,
  useMemory,
  startExploration,
  pauseBranch,
  resumeBranch,
  stopExploration,
  closeBranch,
  tick,
  escapeHTML as esc,
  safeURL,
  readResultJSON,
} from "./research-state.mjs";

const $ = (s) => document.querySelector(s);
const icons = {
  compass:
    '<circle cx="12" cy="12" r="9"/><path d="m16 8-2.5 5.5L8 16l2.5-5.5Z"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  book: '<path d="M12 5c-3-2-7-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-2-1-6-1-9 1v15"/>',
  branch:
    '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10m0-5h5a7 7 0 0 0 7-4"/>',
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  down: '<path d="m8 5 7 7-7 7"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  palette:
    '<path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-4h3a4 4 0 0 0 4-4 10 10 0 0 0-9-10Z"/><circle cx="8" cy="8" r=".6"/><circle cx="14" cy="7" r=".6"/><circle cx="6" cy="13" r=".6"/>',
  play: '<path d="m8 4 12 8-12 8Z"/>',
  chart: '<path d="M4 3v17h17M8 16v-4m5 4V7m5 9V4"/>',
  note: '<path d="M5 3h10l4 4v14H5Zm9 0v5h5M8 12h8M8 16h6"/>',
  spark:
    '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
  link: '<path d="m10 13 4-4m-7 7-1 1a4 4 0 0 1-5-5l4-4a4 4 0 0 1 6 0m2 8a4 4 0 0 0 6 0l4-4a4 4 0 0 0-5-5l-1 1" transform="translate(1 0) scale(.9)"/>',
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.note}</svg>`;
const drawIcons = () =>
  document.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
    el.removeAttribute("data-icon");
  });
const labels = {
  done: "已完成",
  running: "演示中",
  queued: "排队中",
  paused: "已暂停",
  interrupted: "已中断",
  cancelled: "已取消",
  active: "进行中",
  closed: "已收束",
  review: "待讨论",
  measured: "已有结果",
  candidate: "经验草稿",
  supported: "已复验 · 用户判定",
  contested: "存在反例",
};
const statusTag = (status) =>
  `<span class="tag ${["done", "supported", "closed"].includes(status) ? "positive" : ["review", "contested", "interrupted"].includes(status) ? "warning" : ""}">${esc(labels[status] || status)}</span>`;
const originTag = (origin) =>
  `<span class="tag">${origin === "demo" ? "示例数据" : origin === "mixed" ? "混合来源" : "用户录入 · 未核验"}</span>`;
const number = (v) =>
  Number.isFinite(v)
    ? v.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : "—";
const when = (v) => {
  const d = new Date(v);
  return Number.isFinite(d.getTime())
    ? d.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";
};
const rootBranch = (studyId) =>
  state.branches.find((b) => b.studyId === studyId && !b.parentId);
const completed = (studyId) =>
  state.runs.filter(
    (r) =>
      r.status === "done" && branchOf(state, r.branchId)?.studyId === studyId,
  );
let state,
  lastSaved = null,
  storageBlocked = false,
  unreadable = null,
  flowHandler = null,
  noticeTimer,
  detailRun = null,
  compareLeft = null,
  compareRight = null;
try {
  lastSaved = localStorage.getItem(STORAGE_KEY);
  state = lastSaved ? decodeState(lastSaved) : createDemo();
} catch (error) {
  state = createDemo();
  unreadable = lastSaved;
  storageBlocked = true;
  storageWarning(
    `已有记录未能读取，已保留原始内容。当前展示临时样例。请先在「原型说明与备份」中导出原始记录。${error.message}`,
  );
}
function storageWarning(text) {
  $("#storage-warning").hidden = false;
  $("#storage-warning").textContent = text;
}
function save() {
  if (storageBlocked) return false;
  try {
    if (localStorage.getItem(STORAGE_KEY) !== lastSaved) {
      storageBlocked = true;
      storageWarning(
        "另一个页面更新了研究记录。本页已暂停保存，请先导出本页备份，再刷新加载最新记录。",
      );
      return false;
    }
    const data = JSON.stringify(state);
    localStorage.setItem(STORAGE_KEY, data);
    lastSaved = data;
    return true;
  } catch {
    storageBlocked = true;
    storageWarning(
      "浏览器无法保存更多研究记录。当前改动仅在此页面内，请立即导出备份。",
    );
    return false;
  }
}
function toast(text) {
  clearTimeout(noticeTimer);
  $("#notice").textContent = text;
  noticeTimer = setTimeout(() => {
    $("#notice").textContent = "";
  }, 4200);
}
function act(fn, success) {
  if (storageBlocked) {
    toast("本页保存已暂停，请先导出备份并处理顶部提示。");
    return false;
  }
  const before = structuredClone(state);
  try {
    fn();
    save();
    render();
    if (success) toast(success);
    return true;
  } catch (error) {
    state = before;
    toast(error.message);
    return false;
  }
}
function goBranch(branchId, tab = "research") {
  const b = branchOf(state, branchId);
  if (!b) return;
  state.branch = b.id;
  state.study = b.studyId;
  state.page = "study";
  state.tab = tab;
  const ancestors = [];
  let cursor = b;
  while (cursor.parentId) {
    ancestors.push(cursor.parentId);
    cursor = branchOf(state, cursor.parentId);
  }
  state.collapsed = state.collapsed.filter((x) => !ancestors.includes(x));
  $("#sidebar").classList.remove("open");
  $("[data-action=sidebar]").setAttribute("aria-expanded", "false");
  compareLeft = null;
  compareRight = null;
  save();
  render();
  window.scrollTo({ top: 0 });
}
function branchStatus(b) {
  if (b.status !== "active") return b.status;
  if (b.exploration?.state === "review") return "review";
  const run = runsOf(state, b.id).find((r) =>
    ["running", "queued"].includes(r.status),
  );
  if (run) return run.status;
  const latest = latestRun(state, b.id);
  if (!latest) return "active";
  return latest.snapshot.protocol.guard &&
    latest.result.guard > latest.snapshot.protocol.guard.limit
    ? "review"
    : "measured";
}
function tree(b, depth = 0) {
  const children = state.branches.filter((x) => x.parentId === b.id),
    folded = state.collapsed.includes(b.id);
  return `<li><div class="tree-row ${state.page === "study" && state.branch === b.id ? "selected" : ""}">${children.length ? `<button class="tree-fold" data-action="fold" data-id="${esc(b.id)}" aria-label="${folded ? "展开" : "折叠"}${esc(b.title)}" aria-expanded="${!folded}">${icon("down")}</button>` : '<span class="tree-fold"></span>'}<button class="tree-link" data-action="branch" data-id="${esc(b.id)}" ${state.branch === b.id && state.page === "study" ? 'aria-current="page"' : ""} title="${esc(b.title)} · ${esc(labels[branchStatus(b)])}"><span class="state-dot ${branchStatus(b)}"></span><span class="tree-title">${esc(b.title)}</span></button></div>${children.length && !folded ? `<ul class="${depth >= 3 ? "deep-tree" : ""}">${children.map((x) => tree(x, depth + 1)).join("")}</ul>` : ""}</li>`;
}
function renderSidebar() {
  $("#side-content").innerHTML =
    `<div class="side-label"><span>我的研究</span><span>${state.studies.length}</span></div>${state.studies.map((s) => `<button class="study-link ${s.id === state.study && state.page === "study" ? "selected" : ""}" data-action="study" data-id="${esc(s.id)}">${icon("compass")}<span>${esc(s.title)}</span></button>${s.id === state.study && state.page === "study" ? `<div class="side-label"><span>探索分支</span><span>${state.branches.filter((b) => b.studyId === s.id).length}</span></div><ul class="branch-tree">${tree(rootBranch(s.id))}</ul>` : ""}`).join("")}`;
  document
    .querySelectorAll(".rail-button")
    .forEach((el) =>
      el.classList.toggle(
        "active",
        el.dataset.action === (state.page === "memories" ? "memories" : "home"),
      ),
    );
}
function home() {
  return `<div class="page home"><div class="eyebrow">${icon("compass")} EUBOULIA / RESEARCH</div><h1>从一个值得验证的想法开始。</h1><p class="intro">提出方向，围绕数据讨论。每一次尝试，都能成为下一步的起点。</p><form id="direction-form" class="direction-box"><label for="direction" class="sr-only">你想研究什么</label><textarea id="direction" name="direction" maxlength="2000" placeholder="你想研究什么？可以是一处瓶颈、一个猜想，或一组想解释的数据。" required>${esc(state.homeDraft)}</textarea><div class="direction-tools"><span>先说方向，评测条件可以逐步补齐</span><button class="button primary" type="submit">开始研究 ${icon("arrow")}</button></div></form><div class="prompt-examples"><button data-action="prompt" data-text="提高推理吞吐，先确认瓶颈，守住尾延迟">提高吞吐，守住尾延迟 ↗</button><button data-action="prompt" data-text="解释两次实验的差异，先检查评测条件是否一致">解释两次实验的差异 ↗</button><button data-action="prompt" data-text="验证一篇论文中的方法，先建立最小对照实验">验证论文里的一个方法 ↗</button></div><div class="section-heading"><h2>接着上次往下做</h2><span>${state.studies.length} 个研究方向</span></div>${[
    ...state.studies,
  ]
    .reverse()
    .map((s) => {
      const branches = state.branches.filter((b) => b.studyId === s.id),
        candidate =
          branches.find((b) => b.id === state.branch) ||
          branches.find((b) => b.parentId) ||
          branches[0];
      return `<article class="recent-study"><div class="recent-main"><div class="card-meta">${s.demo ? originTag("demo") : '<span class="tag accent">你的研究</span>'}<span>${branches.length} 条分支 · ${completed(s.id).length} 份结果</span></div><h3>${esc(s.title)}</h3><p>${s.demo ? "批处理有提升，也有代价。下一步：拆开验证批大小与调度策略。" : esc(candidate.hypothesis)}</p><div class="recent-meta"><span class="state-dot ${branchStatus(candidate)}"></span><span>${esc(candidate.title)}</span><button class="text-button" data-action="branch" data-id="${candidate.id}">继续研究 ${icon("arrow")}</button></div></div><div class="recent-visual" aria-hidden="true"><div class="mini-tree"><div><span class="state-dot done"></span>建立基线</div><div class="child"><span class="state-dot review"></span>${s.demo ? "调整批处理" : "提出假设"}</div><div class="active-leaf">${s.demo ? "↳ 分开验证两种思路" : "↳ 从结果继续探索"}</div></div></div></article>`;
    })
    .join(
      "",
    )}<div class="home-footer"><span>实验执行为演示；也可录入自己的结果。</span><button class="text-button" data-action="import-backup">导入研究备份 ↗</button></div></div>`;
}
function currentReference(b, result) {
  return (
    state.runs.find(
      (r) => r.id === b.snapshot.referenceRunId && r.id !== result?.id,
    ) ||
    completed(b.studyId).find(
      (r) => r.branchId === rootBranch(b.studyId)?.id && r.id !== result?.id,
    ) ||
    null
  );
}
function runButton(r) {
  return `<button class="reference-button" data-action="run-detail" data-id="${esc(r.id)}">${icon("chart")}${esc(r.label)} · ${esc(branchOf(state, r.branchId).title)}</button>`;
}
function metricBlock(r, ref) {
  const p = r.snapshot.protocol,
    comparison = comparable(ref, r),
    guardBad = p.guard && r.result.guard > p.guard.limit;
  return `<div class="metric-row"><div><div class="metric-name">${esc(p.metric.name)}</div><div class="metric-value">${number(r.result.metric)}<small>${esc(p.metric.unit)}</small>${comparison.ok && comparison.percent !== null ? `<span class="metric-change ${comparison.improved ? "" : "bad"}">${comparison.percent >= 0 ? "+" : ""}${number(comparison.percent)}%</span>` : ""}</div><div class="metric-note">${comparison.ok ? `对照 ${esc(ref.label)} · ${number(ref.result.metric)} ${esc(p.metric.unit)}` : "单次记录 · 暂无可比对照"}</div></div>${p.guard ? `<div><div class="metric-name">${esc(p.guard.name)}</div><div class="metric-value">${number(r.result.guard)}<small>${esc(p.guard.unit)}</small><span class="metric-change ${guardBad ? "bad" : ""}">${guardBad ? "超出约束" : "约束范围内"}</span></div><div class="metric-note">约定上限 ${number(p.guard.limit)} ${esc(p.guard.unit)}</div></div>` : `<div><div class="metric-name">记录来源</div>${originTag(r.origin)}<div class="metric-note">${esc(when(r.endedAt))}</div></div>`}</div>`;
}
function charts(left, right) {
  if (!left || !right) return "";
  if ([left, right].some((r) => r.result.metric < 0 || r.result.guard < 0))
    return '<div class="inline-notice">这组记录包含负值，请在完整对照表中查看带符号的原始数值。</div>';
  const columns = [
    { key: "metric", ...right.snapshot.protocol.metric },
    ...(right.snapshot.protocol.guard
      ? [{ key: "guard", ...right.snapshot.protocol.guard }]
      : []),
  ];
  return `<div class="chart-pair">${columns
    .map((c) => {
      const max = Math.max(
        Math.abs(left.result[c.key]),
        Math.abs(right.result[c.key]),
        1,
      );
      return `<div class="bar-chart"><div class="chart-title"><span>${esc(c.name)} · ${esc(c.unit)}</span><span>${c.key === "metric" ? (c.direction === "higher" ? "越高越好" : "越低越好") : `上限 ${number(c.limit)}`}</span></div>${[left, right].map((r, i) => `<div class="bar-line"><button data-action="cite-run" data-id="${r.id}" aria-label="讨论 ${esc(r.label)} 的${esc(c.name)}" data-metric="${esc(c.name)}">${esc(r.label)}</button><div class="bar-track"><button style="width:${Math.max(2, (Math.abs(r.result[c.key]) / max) * 100)}%" class="${i ? (c.key === "guard" && r.result.guard > c.limit ? "exceeded" : "candidate") : ""}" data-action="cite-run" data-id="${r.id}" data-metric="${esc(c.name)}" aria-label="讨论 ${esc(r.label)} 的${esc(c.name)} ${number(r.result[c.key])} ${esc(c.unit)}"></button></div><span class="bar-value">${number(r.result[c.key])}</span></div>`).join("")}<div class="chart-note">点选记录或柱形，将证据带入讨论。</div></div>`;
    })
    .join("")}</div>`;
}
function statusStrip(b) {
  const e = b.exploration;
  if (!e) return "";
  const run = runsOf(state, b.id).find((r) =>
    ["running", "queued", "paused"].includes(r.status),
  );
  return `<div class="run-status" id="run-status"><div class="run-status-head"><h3>${run ? `${labels[run.status]} · ${esc(run.label)}` : e.state === "review" ? "这一轮有结果了，回来一起看" : "本轮验证已结束"}</h3><span class="tag">${e.used} / ${e.maxRuns} 轮</span></div><p>${run ? `${esc(e.instruction)} · 最多 ${e.minutes} 分钟 · 本地演示单执行位` : esc(e.reason)}</p>${run ? `<div class="progress" role="progressbar" aria-label="演示进度" aria-valuenow="${run.phase}" aria-valuemin="0" aria-valuemax="3"><span style="width:${(run.phase / 3) * 100}%"></span></div><div class="card-actions"><button class="text-button" data-action="${b.status === "paused" ? "resume" : "pause"}">${b.status === "paused" ? "继续验证" : "暂停分支"}</button><button class="text-button" data-action="stop">结束这一轮</button><button class="text-button" data-action="run-detail" data-id="${run.id}">查看演示日志</button></div>` : ""}</div>`;
}
function refLabel(ref) {
  return ref.kind === "run"
    ? state.runs.find((r) => r.id === ref.id)?.label
    : ref.kind === "source"
      ? state.sources.find((r) => r.id === ref.id)?.title
      : state.memories.find((m) => m.id === ref.id)?.title;
}
function discussion(b) {
  const messages = state.messages.filter((m) => m.branchId === b.id),
    draft = state.drafts[b.id] || { text: "", refs: [] };
  return `<section class="discussion" id="discussion"><div class="subhead"><h3>围绕证据，一起往下想</h3><span>${messages.length} 条记录</span></div>${messages.map((m) => `<article class="message ${esc(m.author)}" id="${esc(m.id)}"><div class="message-avatar">${m.author === "you" ? "J" : m.author === "demo-agent" ? "✧" : m.author === "conclusion" ? "✓" : "·"}</div><div><div class="message-byline"><span>${{ you: "你", system: "研究记录", "demo-agent": "Agent · 演示回复", conclusion: "阶段结论" }[m.author]}</span><time>${when(m.createdAt)}</time></div><p>${esc(m.text)}</p>${m.refs.length ? `<div class="message-links">${m.refs.map((r) => `<button class="reference-button" data-action="${r.kind === "run" ? "run-detail" : r.kind === "source" ? "source-detail" : "memory-detail"}" data-id="${esc(r.id)}">${icon(r.kind === "run" ? "chart" : "note")}${esc(refLabel(r) || "原始引用")}</button>`).join("")}</div>` : ""}${m.author === "you" ? `<div class="message-actions"><button class="text-button" data-action="message-fork" data-id="${m.id}">从这个想法创建分支 ↗</button></div>` : ""}</div></article>`).join("")}<form id="discussion-form" class="composer"><div class="composer-context">${(draft.refs || []).map((r, i) => `<button type="button" data-action="remove-ref" data-index="${i}" aria-label="移除引用 ${esc(refLabel(r))}">${esc(refLabel(r))} ×</button>`).join("")}</div><label class="sr-only" for="message">讨论当前分支</label><textarea id="message" name="text" rows="2" maxlength="4000" placeholder="这次结果说明了什么？也可以选中上面的数据，带着证据讨论…" ${b.status === "closed" ? "disabled" : ""} required>${esc(draft.text)}</textarea><div class="tools"><span>本地记录 · 自由讨论暂不调用模型</span><button class="button primary" type="submit" ${b.status === "closed" ? "disabled" : ""}>记录讨论 ${icon("arrow")}</button></div></form></section>`;
}
function research(b) {
  const run = latestRun(state, b.id),
    ref = currentReference(b, run),
    comparison = comparable(ref, run),
    bad =
      run?.snapshot.protocol.guard &&
      run.result.guard > run.snapshot.protocol.guard.limit;
  return `${statusStrip(b)}${b.conclusion ? `<div class="inline-notice"><strong>阶段结论</strong><br>${esc(b.conclusion)}</div><br>` : ""}<section class="finding ${bad ? "attention" : "neutral"}"><div class="finding-header">${icon(run ? "spark" : "compass")}<span>${run ? "当前发现" : "这一条分支想弄清楚什么"}</span>${run ? originTag(run.origin) : '<span class="tag">待验证</span>'}</div><h2>${run ? (bad ? "约束超出了，先看清这次的代价。" : !b.parentId ? "先把研究的起点记录清楚。" : comparison.ok && !comparison.improved ? "主指标没有改善，换个角度再看。" : "新结果已记录，看看它说明了什么。") : esc(b.title)}</h2><p>${esc(run ? run.result.summary : b.hypothesis)}</p><div class="finding-actions">${run ? `<button class="text-button" data-action="cite-run" data-id="${run.id}">围绕这份结果讨论 ${icon("arrow")}</button><button class="text-button" data-action="run-detail" data-id="${run.id}">查看原始记录</button>` : `<button class="text-button" data-action="conditions">完善评测条件 ${icon("arrow")}</button><button class="text-button" data-action="import-result">录入第一份结果</button>`}</div>${run ? metricBlock(run, ref) : ""}</section>${comparison.ok ? `<section class="evidence-section"><div class="subhead"><h3>与起点对照</h3><button class="text-button" data-action="tab" data-tab="compare">展开完整对比 ↗</button></div>${charts(ref, run)}</section>` : run ? `<div class="evidence-section inline-notice">${esc(comparison.reason)}</div>` : ""}${
    b.memoryIds.length
      ? `<div class="evidence-section"><div class="subhead"><h3>这一步带着的经验</h3><span>使用前核对条件</span></div>${b.memoryIds
          .map((mid) => state.memories.find((m) => m.id === mid))
          .filter(Boolean)
          .map(
            (m) =>
              `<button class="reference-button" data-action="memory-detail" data-id="${m.id}">${icon("book")}${esc(m.title)} · ${esc(memoryFit(m, b))}</button>`,
          )
          .join(" ")}</div>`
      : ""
  }${discussion(b)}`;
}
function runsView(b) {
  const runs = runsOf(state, b.id);
  return `${statusStrip(b)}<div class="toolbar"><p>每次记录都保留评测条件、代码版本和来源。</p><button class="button" data-action="import-result">${icon("plus")}录入实验结果</button></div>${
    runs.length
      ? `<div class="table-wrap"><table><thead><tr><th>实验</th><th>状态 / 来源</th><th>${esc(b.snapshot.protocol.metric.name)}</th><th>${esc(b.snapshot.protocol.guard?.name || "约束")}</th><th>代码版本</th><th>记录时间</th></tr></thead><tbody>${[
          ...runs,
        ]
          .reverse()
          .map(
            (r) =>
              `<tr><td><button class="run-row-title" data-action="run-detail" data-id="${r.id}">${esc(r.label)} ↗</button></td><td>${statusTag(r.status)} ${originTag(r.origin)}</td><td>${number(r.result?.metric)} ${esc(r.snapshot.protocol.metric.unit)}</td><td>${number(r.result?.guard)} ${esc(r.snapshot.protocol.guard?.unit || "")}</td><td>${esc(r.snapshot.codeRef)}</td><td>${when(r.createdAt)}</td></tr>`,
          )
          .join("")}</tbody></table></div>`
      : `<div class="empty-state"><h3>先留下一份可回看的结果</h3><p>用演示验证体验完整流程，或录入你已经完成的实验。记录以后就能成为新的分支起点。</p><button class="button primary" data-action="explore">${icon("play")}演示一轮验证</button></div>`
  }`;
}
function compareView(b) {
  const runs = completed(b.studyId),
    latest = latestRun(state, b.id),
    ref = currentReference(b, latest);
  const left = runs.find((r) => r.id === compareLeft) || ref || runs[0];
  const right =
    runs.find((r) => r.id === compareRight) || latest || runs.at(-1);
  compareLeft = left?.id;
  compareRight = right?.id;
  if (runs.length < 2)
    return `<div class="empty-state"><h3>两份记录，才能开始对照</h3><p>可以从基线创建一个方案分支，完成验证后再回来。示例数据和用户结果会分开比较。</p><button class="button" data-action="import-result">录入实验结果</button></div>`;
  const options = (selected) =>
    runs
      .map(
        (r) =>
          `<option value="${r.id}" ${r.id === selected ? "selected" : ""}>${esc(r.label)} · ${esc(branchOf(state, r.branchId).title)} · ${r.origin === "demo" ? "示例" : "录入"}</option>`,
      )
      .join("");
  const c = comparable(left, right);
  return `<div class="compare-select"><label>对照起点<select id="compare-left">${options(left.id)}</select></label><span>→</span><label>候选结果<select id="compare-right">${options(right.id)}</select></label></div>${c.ok ? `<div class="compare-callout"><h3>${esc(right.snapshot.protocol.metric.name)}${c.percent === null ? `变化 ${number(c.delta)}` : `${c.percent >= 0 ? "增加" : "减少"} ${number(Math.abs(c.percent))}%`}</h3><p>评测条件一致；差异来自记录值，仍需重复实验与正确性检查。</p></div>${charts(left, right)}` : `<div class="inline-notice warning">${esc(c.reason)}下面保留原始数值，暂不计算改进幅度。</div>`}<div class="section-heading"><h2>把数值和条件放在一起看</h2></div><div class="table-wrap"><table><thead><tr><th>对照内容</th><th>${esc(left.label)}</th><th>${esc(right.label)}</th></tr></thead><tbody>${[
    [
      "来源",
      left.origin === "demo" ? "示例数据" : "用户录入",
      right.origin === "demo" ? "示例数据" : "用户录入",
    ],
    [
      "主指标",
      `${left.snapshot.protocol.metric.name} · ${number(left.result.metric)} ${left.snapshot.protocol.metric.unit}`,
      `${right.snapshot.protocol.metric.name} · ${number(right.result.metric)} ${right.snapshot.protocol.metric.unit}`,
    ],
    [
      "约束指标",
      left.snapshot.protocol.guard
        ? `${left.snapshot.protocol.guard.name} · ${number(left.result.guard)} ${left.snapshot.protocol.guard.unit}`
        : "未设置",
      right.snapshot.protocol.guard
        ? `${right.snapshot.protocol.guard.name} · ${number(right.result.guard)} ${right.snapshot.protocol.guard.unit}`
        : "未设置",
    ],
    ["数据", left.snapshot.protocol.dataset, right.snapshot.protocol.dataset],
    [
      "运行环境",
      left.snapshot.protocol.environment,
      right.snapshot.protocol.environment,
    ],
    [
      "评测方法",
      left.snapshot.protocol.evaluation,
      right.snapshot.protocol.evaluation,
    ],
    ["代码版本", left.snapshot.codeRef, right.snapshot.codeRef],
  ]
    .map(
      ([name, a, z]) =>
        `<tr><th>${esc(name)}</th><td>${esc(a)}</td><td>${esc(z)}</td></tr>`,
    )
    .join(
      "",
    )}</tbody></table></div><div class="card-actions"><button class="button" data-action="compare-discuss">带着这两份结果讨论</button><button class="button" data-action="save-memory">${icon("book")}整理为经验草稿</button></div>`;
}
function sourcesView(b) {
  const sources = state.sources.filter((r) => b.sourceIds.includes(r.id));
  return `<div class="toolbar"><p>保存来源、时间与适用条件，再把线索转成可验证的想法。</p><button class="button" data-action="add-source">${icon("plus")}添加参考资料</button></div><div class="inline-notice">资料链接由你添加；当前原型不会自动检索网页或判断内容。外部线索保持「待本地验证」。</div>${sources.length ? sources.map((r) => `<article class="source-card"><div class="card-meta"><span class="tag warning">待本地验证</span><span>${esc(r.published)}</span></div><h3><a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">${esc(r.title)} ↗</a></h3><p>${esc(r.relevance)}</p><p class="condition-note">适用条件：${esc(r.conditions)}</p><div class="card-actions"><button class="text-button" data-action="source-fork" data-id="${r.id}">由这条线索创建验证分支 ↗</button><button class="text-button" data-action="source-detail" data-id="${r.id}">查看来源记录</button></div></article>`).join("") : `<div class="empty-state" style="margin-top:20px"><h3>给这个想法留一个出处</h3><p>论文、官方文档、GitHub 讨论都可以。记录它为什么可能适用，再用本地实验判断。</p><button class="button" data-action="add-source">添加第一条资料</button></div>`}`;
}
function contextView(b) {
  const p = b.snapshot.protocol,
    study = studyOf(state, b.studyId);
  return `<div class="toolbar"><p>子分支继承创建时的快照，已有实验结果不会被改写。</p><button class="button" data-action="conditions">修改后续评测条件</button></div><dl class="context-grid"><dt>研究方向</dt><dd>${esc(study.title)}</dd><dt>当前假设</dt><dd>${esc(b.hypothesis)}</dd><dt>分支起点</dt><dd>${b.parentId ? `<button class="text-button" data-action="branch" data-id="${b.parentId}">${esc(branchOf(state, b.parentId).title)} ↗</button>` : "根基线"}</dd><dt>代码版本</dt><dd>${esc(b.snapshot.codeRef)}</dd><dt>数据与工作负载</dt><dd>${esc(p.dataset)}</dd><dt>运行环境</dt><dd>${esc(p.environment)}</dd><dt>评测方法</dt><dd>${esc(p.evaluation)}</dd><dt>主指标</dt><dd>${esc(p.metric.name)} · ${esc(p.metric.unit)} · ${p.metric.direction === "higher" ? "越高越好" : "越低越好"}</dd><dt>约束</dt><dd>${p.guard ? `${esc(p.guard.name)} ≤ ${number(p.guard.limit)} ${esc(p.guard.unit)}` : "尚未设置"}</dd><dt>源码与资料</dt><dd>${esc(study.resources || "暂未提供；可从参考资料页补充链接。")}</dd></dl><div class="inline-notice">源码路径作为研究上下文保存；当前原型不会读取这些目录，也不会修改文件。</div>${b.history.length ? `<details class="memory-history"><summary>查看 ${b.history.length} 条变更记录</summary><ul>${b.history.map((h) => `<li>${when(h.at)} · ${esc(h.type === "conditions" ? "更新后续评测条件，原快照已保留" : h.type === "resume" ? "重新开放分支" : h.text)}</li>`).join("")}</ul></details>` : ""}`;
}
function studyPage() {
  const b = branchOf(state);
  if (!b) {
    state.page = "home";
    return home();
  }
  const pendingRun = runsOf(state, b.id).find((r) =>
    ["running", "queued"].includes(r.status),
  );
  const tabs = [
    ["research", "研究记录"],
    ["runs", "实验", runsOf(state, b.id).length],
    ["compare", "对照"],
    ["sources", "参考资料", b.sourceIds.length],
    ["context", "条件"],
  ];
  return `<div class="page"><div class="heading-row"><div><div class="eyebrow">${b.parentId ? "EXPLORATION / 方案分支" : "STARTING POINT / 研究基线"}</div><h1>${esc(b.title)}</h1><div class="branch-subtitle">${statusTag(branchStatus(b))}<span>${b.parentId ? `从「${esc(branchOf(state, b.parentId).title)}」继续` : "先记录起点，再探索不同的方案"}</span></div></div><div class="heading-actions">${b.status !== "active" ? `<button class="button" data-action="resume">${b.status === "closed" ? "重新开放" : "恢复分支"}</button>` : pendingRun ? `<button class="button primary" data-action="run-detail" data-id="${pendingRun.id}">${icon("play")}查看验证</button>` : `<button class="button primary" data-action="explore">${icon("play")}演示验证</button>`}<button class="button" data-action="fork">${icon("branch")}新分支</button><button class="icon-button" data-action="branch-menu" aria-label="分支操作" title="分支操作">···</button></div></div><div class="tabs" role="tablist" aria-label="分支视图">${tabs.map(([key, label, count]) => `<button id="tab-${key}" role="tab" aria-selected="${state.tab === key}" aria-controls="branch-panel" tabindex="${state.tab === key ? 0 : -1}" data-action="tab" data-tab="${key}">${label}${count !== undefined ? `<span class="count">${count}</span>` : ""}</button>`).join("")}</div><section class="branch-body" id="branch-panel" role="tabpanel" aria-labelledby="tab-${esc(state.tab)}">${({ research: () => research(b), runs: () => runsView(b), compare: () => compareView(b), sources: () => sourcesView(b), context: () => contextView(b) }[state.tab] || (() => research(b)))()}</section></div>`;
}
function memoryCard(m, detailed = false) {
  const b = branchOf(state),
    runs = m.runIds
      .map((rid) => state.runs.find((r) => r.id === rid))
      .filter(Boolean);
  return `<article class="memory-card"><div class="card-meta">${statusTag(m.status)}${originTag(m.origin)}<span>${esc(studyOf(state, m.studyId)?.title)}</span></div><h3>${esc(m.title)}</h3><p>${esc(m.claim)}</p><p class="condition-note">适用条件：${esc(m.conditions)}</p>${b ? `<p class="condition-note">当前分支：${esc(memoryFit(m, b))}</p>` : ""}<div class="message-links">${runs.map(runButton).join("")}</div><div class="card-actions"><button class="text-button" data-action="memory-branch" data-id="${m.id}">回到产生经验的分支 ↗</button>${b ? `<button class="text-button" data-action="use-memory" data-id="${m.id}">带到当前分支</button>` : ""}<button class="text-button" data-action="memory-status" data-id="${m.id}">复验 / 记录反例</button>${!detailed ? `<button class="text-button" data-action="memory-detail" data-id="${m.id}">查看判定历史</button>` : ""}</div>${detailed ? `<details class="memory-history" open><summary>判定历史 · ${m.history.length} 次</summary><ul>${m.history.map((h) => `<li>${esc(when(h.at))} · ${esc(labels[h.from])} → ${esc(labels[h.to])}<br>${esc(h.reason)}</li>`).join("") || "<li>尚未复验，当前为经验草稿。</li>"}</ul></details>` : ""}</article>`;
}
function memoriesPage() {
  return `<div class="page"><div class="eyebrow">RESEARCH MEMORY / 经验库</div><h1>让下一次研究，有据可循。</h1><p class="intro">保留结论，也保留它成立的条件和出现过的反例。</p><div class="section-heading"><h2>${state.memories.length} 条经验</h2><span>每一条都能回到实验</span></div>${
    state.memories.length
      ? [...state.memories]
          .reverse()
          .map((m) => memoryCard(m))
          .join("")
      : `<div class="empty-state"><h3>经验从实验结果中长出来</h3><p>在结果对照页选择「整理为经验草稿」，保留结论、适用条件与证据。</p><button class="button" data-action="home">回到研究方向</button></div>`
  }</div>`;
}
function render() {
  const focused = document.activeElement,
    fid = focused?.id,
    pos = focused?.selectionStart,
    end = focused?.selectionEnd;
  renderSidebar();
  $("#breadcrumb").innerHTML =
    `<button data-action="home">本地研究</button>${state.page === "study" ? `<span>/</span><span>${esc(studyOf(state)?.title)}</span>` : state.page === "memories" ? "<span>/</span><span>经验库</span>" : ""}`;
  $("#content").innerHTML =
    state.page === "study"
      ? studyPage()
      : state.page === "memories"
        ? memoriesPage()
        : home();
  drawIcons();
  if (fid && !focused.isConnected) {
    const replacement = document.getElementById(fid);
    replacement?.focus({ preventScroll: true });
    if (replacement?.setSelectionRange && pos !== null)
      replacement.setSelectionRange(pos, end);
  }
  if (detailRun && $("#detail-dialog").open) renderRunDetail(detailRun);
}

let flowKey = "";
const inputField = (
  name,
  label,
  value = "",
  hint = "",
  type = "text",
  required = true,
  max = 2000,
) =>
  `<div class="field"><label>${label}<input name="${name}" type="${type}" value="${esc(value)}" ${required ? "required" : ""} ${type === "number" ? 'step="any"' : `maxlength="${max}"`} /></label>${hint ? `<p class="field-hint">${hint}</p>` : ""}</div>`;
const textField = (
  name,
  label,
  value = "",
  hint = "",
  required = true,
  max = 4000,
) =>
  `<div class="field"><label>${label}<textarea name="${name}" rows="3" maxlength="${max}" ${required ? "required" : ""}>${esc(value)}</textarea></label>${hint ? `<p class="field-hint">${hint}</p>` : ""}</div>`;
function openFlow(title, fields, handler, submit = "保存", key = "") {
  $("#detail-dialog").close();
  detailRun = null;
  flowKey = key;
  $("#flow-title").textContent = title;
  $("#flow-fields").innerHTML = fields;
  $("#flow-error").textContent = "";
  $("#flow-submit").textContent = submit;
  $("#flow-hint").textContent = "仅保存到本地";
  flowHandler = handler;
  const draft = state.drafts[`form:${key}`];
  if (key && draft) {
    for (const el of $("#flow-form").elements)
      if (el.name && Object.hasOwn(draft, el.name)) {
        if (el.type === "checkbox")
          el.checked = draft[el.name].includes(el.value);
        else el.value = draft[el.name][0] ?? "";
      }
  }
  $("#flow-dialog").showModal();
  $("#flow-fields input, #flow-fields textarea, #flow-fields select")?.focus();
}
function details(title, content) {
  detailRun = null;
  $("#detail-title").textContent = title;
  $("#detail-content").innerHTML = content;
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
}
function protocolFields(
  p = {
    dataset: "",
    environment: "",
    evaluation: "",
    metric: { name: "主指标", unit: "", direction: "higher" },
    guard: null,
  },
) {
  return `${textField("dataset", "数据 / 工作负载", p.dataset, "固定数据版本与请求分布。", false, 2000)}${inputField("environment", "本地运行环境", p.environment, "例如硬件、模型、运行时版本。", "text", false)}${textField("evaluation", "评测方法", p.evaluation, "说明预热、重复次数、并发与统计口径。", false, 2000)}<div class="field-row three">${inputField("metricName", "主指标名称", p.metric.name, "", "text", true, 80)}${inputField("metricUnit", "单位", p.metric.unit, "", "text", false, 40)}<div class="field"><label>改善方向<select name="metricDirection"><option value="higher" ${p.metric.direction === "higher" ? "selected" : ""}>越高越好</option><option value="lower" ${p.metric.direction === "lower" ? "selected" : ""}>越低越好</option></select></label></div></div><div class="field-row three">${inputField("guardName", "约束指标 · 可选", p.guard?.name || "", "", "text", false, 80)}${inputField("guardUnit", "单位", p.guard?.unit || "", "", "text", false, 40)}${inputField("guardLimit", "约束上限", p.guard?.limit ?? "", "", "number", false)}</div>`;
}
function protocolFrom(data) {
  const guardName = String(data.guardName || "").trim();
  if (guardName && String(data.guardLimit).trim() === "")
    throw Error("设置约束指标后，请填写约束上限。");
  return {
    dataset: String(data.dataset || "").trim() || "待确认的数据集",
    environment: String(data.environment || "").trim() || "待确认的本地环境",
    evaluation: String(data.evaluation || "").trim() || "待确认的评测方法",
    metric: {
      name: String(data.metricName || "主指标").trim(),
      unit: String(data.metricUnit || "").trim(),
      direction: data.metricDirection || "higher",
    },
    guard: guardName
      ? {
          name: guardName,
          unit: String(data.guardUnit || "").trim(),
          limit: Number(data.guardLimit),
        }
      : null,
  };
}
function newDirection(direction = "") {
  if (direction && state.drafts["form:new"]?.direction?.[0] !== direction)
    delete state.drafts["form:new"];
  openFlow(
    "开始一个研究方向",
    `<p class="dialog-intro">先把要弄清楚的问题留下来。评测细节可以在建立基线时继续补充。</p>${textField("direction", "你想弄清楚什么", direction || state.homeDraft, "", true, 2000)}<details class="optional-fields"><summary>添加源码、资料与评测约定 · 可选</summary>${textField("resources", "源码目录 / 数据 / 文档链接", "", "可填写多个路径或链接，一行一个；这里记录上下文，不读取文件。", false, 8000)}${protocolFields()}</details>`,
    (data) =>
      createStudy(state, {
        direction: data.direction,
        resources: data.resources,
        protocol: protocolFrom(data),
      }),
    "进入研究",
    "new",
  );
}
function showFork(parentId = state.branch, runId = null, preset = {}) {
  const parent = branchOf(state, parentId),
    runs = runsOf(state, parentId).filter((r) => r.status === "done");
  const chosen = runId || latestRun(state, parentId)?.id || "";
  openFlow(
    "从这里，试一个新想法",
    `<p class="dialog-intro">起点来自「${esc(parent.title)}」。代码版本、评测条件和参考经验会以快照继承。</p>${inputField("title", "方案名称", preset.title || "", "例如：按请求长度调整批大小。", "text", true, 160)}${textField("hypothesis", "这次想验证什么", preset.hypothesis || "", "尽量说清改变什么、预期看到什么，以及什么结果会推翻这个想法。")}<div class="field"><label>从哪一步继续<select name="runId"><option value="">当前分支条件 · 尚无新实验快照</option>${runs.map((r) => `<option value="${r.id}" ${r.id === chosen ? "selected" : ""}>${esc(r.label)} · ${esc(r.snapshot.codeRef)} · ${r.origin === "demo" ? "示例" : "用户录入"}</option>`).join("")}</select></label></div>`,
    (data) =>
      forkBranch(state, parentId, {
        title: data.title,
        hypothesis: data.hypothesis,
        runId: data.runId || null,
        sourceIds: preset.sourceIds || [],
        memoryIds: preset.memoryIds || [],
      }),
    "创建分支",
    `fork:${parentId}:${preset.key || ""}`,
  );
}
function showExplore() {
  const b = branchOf(state),
    p = b.snapshot.protocol;
  openFlow(
    "让这个想法跑一轮",
    `<p class="dialog-intro">体验验证、返回发现和继续分支的过程。使用固定演示规则生成数据，不调用模型、不执行你的源码。</p>${textField("instruction", "这一轮先探索什么", b.hypothesis)}<div class="field-row"><div class="field"><label>最多验证几轮<input name="maxRuns" type="number" min="1" max="8" step="1" value="2" required /></label></div><div class="field"><label>最多几分钟<input name="minutes" type="number" min="1" max="60" step="1" value="5" required /></label></div></div><div class="inline-notice">每轮演示约 5 秒。${p.guard ? `超过 ${esc(p.guard.name)} ${number(p.guard.limit)} ${esc(p.guard.unit)} 时，会提前回来讨论。` : "达到约定轮次后，会回来讨论结果。"}多个分支依次占用同一个演示执行位。</div>`,
    (data) =>
      startExploration(state, b.id, {
        maxRuns: Number(data.maxRuns),
        minutes: Number(data.minutes),
        instruction: data.instruction,
      }),
    "开始演示验证",
    `explore:${b.id}`,
  );
  $("#flow-hint").textContent = "演示数据 · 无真实执行";
}
function showImportResult() {
  const b = branchOf(state),
    p = b.snapshot.protocol;
  openFlow(
    "录入一份实验结果",
    `<p class="dialog-intro">保存你在本地已完成的测量。数值会标注为「用户录入 · 未核验」，原始记录可随时回看。</p><details class="optional-fields"><summary>从 JSON 文件填入测量数据</summary><label>选择实验 JSON<input id="result-json" type="file" accept="application/json,.json" /></label><p class="field-hint">字段：metric、guard（如有约束）、codeRef、summary、artifact。可附 protocol 核对评测条件；最多 256 KB，只在浏览器内读取。</p></details><div class="field-row">${inputField("metric", `${esc(p.metric.name)} ${esc(p.metric.unit)}`, "", "", "number")}${p.guard ? inputField("guard", `${esc(p.guard.name)} ${esc(p.guard.unit)}`, "", "", "number") : ""}</div>${inputField("codeRef", "本次代码版本", b.snapshot.codeRef === "待记录" ? "" : b.snapshot.codeRef, "建议填写 commit 或能定位改动的版本。", "text", true, 500)}${textField("summary", "结果说明", "", "描述观察到的结果，保留不确定性。")}${inputField("artifact", "原始记录路径或链接", "", "填写日志、报告或数据文件的位置；本页不会读取其内容。")}<details class="optional-fields"><summary>当前评测条件</summary><p class="field-hint">${esc(p.dataset)}<br>${esc(p.environment)}<br>${esc(p.evaluation)}</p></details>`,
    (data) =>
      importResult(state, b.id, {
        ...data,
        metric: Number(data.metric),
        guard: p.guard ? Number(data.guard) : null,
      }),
    "保存结果",
    `result:${b.id}`,
  );
}
function showConditions() {
  const b = branchOf(state);
  openFlow(
    "后续实验的评测条件",
    `<p class="dialog-intro">已有实验和子分支保留原快照。条件改变后，旧结果和经验需要重新判断可比性。</p>${inputField("codeRef", "起始代码版本", b.snapshot.codeRef, "", "text", true, 500)}${protocolFields(b.snapshot.protocol)}`,
    (data) => reviseProtocol(state, b.id, protocolFrom(data), data.codeRef),
    "保存条件",
    `conditions:${b.id}`,
  );
}
function showSource() {
  const b = branchOf(state);
  openFlow(
    "把一条外部经验带进来",
    `${inputField("title", "资料标题", "", "", "text", true, 200)}${inputField("url", "原文链接", "", "论文、官方文档、GitHub 讨论等。", "url")}${inputField("published", "发表日期 / 版本", "", "尚未确认时可以明确写「待确认」。", "text", true, 100)}${textField("relevance", "为什么与当前问题有关")}${textField("conditions", "原经验成立的条件", "", "例如硬件、数据规模、软件版本，或尚未核对的差异。", true, 2000)}`,
    (data) => addSource(state, b.id, data),
    "保存线索",
    `source:${b.id}`,
  );
}
function showMemory() {
  const b = branchOf(state),
    runs = completed(b.studyId),
    latest = latestRun(state, b.id);
  if (!runs.length) {
    toast("先留下一份实验结果，再整理经验。");
    return;
  }
  const selected = [compareLeft, compareRight, latest?.id].filter(Boolean);
  openFlow(
    "把这次研究，留给下一次",
    `${inputField("title", "经验标题", "", "用一句话说明值得记住的发现。", "text", true, 160)}${textField("claim", "目前能支持的结论", "")}${textField("conditions", "适用条件与局限", `${b.snapshot.protocol.dataset}\n${b.snapshot.protocol.environment}`, "也记录失败的尝试、反例和仍需复验的条件。", true, 2000)}<div class="field"><label>支持这条经验的实验记录</label>${runs.map((r) => `<label class="checkbox"><input type="checkbox" name="runIds" value="${r.id}" ${selected.includes(r.id) ? "checked" : ""} /><span>${esc(r.label)} · ${esc(branchOf(state, r.branchId).title)} · ${r.origin === "demo" ? "示例" : "用户录入"}</span></label>`).join("")}</div><div class="inline-notice">先保存为草稿。之后可以补充判定依据，记录复验或反例；示例经验始终保留来源标识。</div>`,
    (data, form) =>
      saveMemory(state, b.id, { ...data, runIds: form.getAll("runIds") }),
    "保存经验草稿",
    `memory:${b.id}`,
  );
}
function showMemoryStatus(mid) {
  const m = state.memories.find((x) => x.id === mid);
  openFlow(
    "更新这条经验的判定",
    `<p class="dialog-intro">${esc(m.title)}</p><div class="field"><label>新的判定<select name="status"><option value="candidate" ${m.status === "candidate" ? "selected" : ""}>继续保留为草稿</option><option value="supported" ${m.status === "supported" ? "selected" : ""}>已复验 · 用户判定</option><option value="contested" ${m.status === "contested" ? "selected" : ""}>发现反例 / 存在争议</option></select></label></div>${textField("reason", "判定依据", "", "写明哪些实验支持、哪里出现反例。旧判定会保留在历史中。")}<div class="inline-notice">标记复验至少需要两份同条件、同来源类型的记录；系统不替你判断结论是否成立。</div>`,
    (data) => reviseMemory(state, mid, data.status, data.reason),
    "保存判定",
    `verdict:${mid}`,
  );
}
function renderRunDetail(rid) {
  const r = state.runs.find((x) => x.id === rid);
  if (!r) return;
  $("#detail-title").textContent = `${r.label} · 实验记录`;
  const p = r.snapshot.protocol,
    url = safeURL(r.result?.artifact);
  $("#detail-content").innerHTML =
    `<div class="card-meta">${statusTag(r.status)}${originTag(r.origin)}<span>${when(r.createdAt)}</span></div>${
      r.result
        ? `<p class="dialog-intro">${esc(r.result.summary)}</p>${metricBlock(
            r,
            state.runs.find((x) => x.id === r.snapshot.referenceRunId),
          )}`
        : ""
    }<dl class="context-grid"><dt>来源分支</dt><dd><button class="text-button" data-action="detail-branch" data-id="${r.branchId}">${esc(branchOf(state, r.branchId).title)} ↗</button></dd><dt>代码版本</dt><dd>${esc(r.snapshot.codeRef)}</dd><dt>数据</dt><dd>${esc(p.dataset)}</dd><dt>运行环境</dt><dd>${esc(p.environment)}</dd><dt>评测方法</dt><dd>${esc(p.evaluation)}</dd><dt>原始记录</dt><dd>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(r.result.artifact)} ↗</a>` : esc(r.result?.artifact || "尚未产生结果")}</dd></dl><details class="memory-history"><summary>查看${r.origin === "demo" ? "演示" : "记录"}日志</summary><pre class="log">${esc(r.logs.join("\n"))}</pre></details>${r.status === "done" ? `<div class="card-actions"><button class="button primary" data-action="run-fork" data-id="${r.id}">${icon("branch")}从这一步创建分支</button><button class="button" data-action="cite-run" data-id="${r.id}">引用到当前讨论</button></div>` : ""}`;
}
function showRun(rid) {
  const r = state.runs.find((x) => x.id === rid);
  if (!r) return;
  details("实验记录", "");
  detailRun = rid;
  renderRunDetail(rid);
}
function citeRun(rid, metric) {
  const r = state.runs.find((x) => x.id === rid);
  if (!r) return;
  $("#detail-dialog").close();
  detailRun = null;
  if (state.study !== branchOf(state, r.branchId).studyId) goBranch(r.branchId);
  if (state.page !== "study") goBranch(state.branch || r.branchId);
  const draft = (state.drafts[state.branch] ||= { text: "", refs: [] });
  if (!draft.refs.some((x) => x.kind === "run" && x.id === rid))
    draft.refs.push({ kind: "run", id: rid });
  if (!draft.text)
    draft.text = metric
      ? `这份结果的${metric}为什么会这样变化？下一步怎么验证？`
      : "这次结果说明了什么？还有哪些解释需要验证？";
  state.tab = "research";
  save();
  render();
  $("#message")?.focus();
  $("#message")?.scrollIntoView({ block: "center", behavior: "smooth" });
}
let backupObjectURL = null;
function exportBackup(raw = false) {
  const text = (raw ? unreadable : JSON.stringify(state, null, 2)) || "";
  if (backupObjectURL) URL.revokeObjectURL(backupObjectURL);
  backupObjectURL = URL.createObjectURL(
    new Blob([text], { type: "application/json" }),
  );
  const filename = `euboulia-${raw ? "original" : "research"}-${new Date().toISOString().slice(0, 10)}.json`;
  details(
    "保存一份研究备份",
    `<p class="dialog-intro">${raw ? "保留无法读取的原始记录，方便恢复。" : "包含研究方向、分支、实验、讨论、资料与经验。"}可以下载 JSON；如果浏览器未提供下载，也可以复制完整内容保存。</p><label>备份 JSON<textarea id="backup-json" rows="8" readonly spellcheck="false">${esc(text)}</textarea></label><div class="card-actions"><a class="button primary" href="${backupObjectURL}" download="${filename}">下载 JSON</a><button class="button" data-action="copy-backup">复制完整备份</button></div>`,
  );
}
async function copyBackup() {
  const field = $("#backup-json");
  try {
    await navigator.clipboard.writeText(field.value);
    toast("完整备份已复制，可以保存为 JSON 文件。");
  } catch {
    field.focus();
    field.select();
    toast("备份已选中，请按 ⌘/Ctrl C 复制并保存。");
  }
}
function showAbout() {
  details(
    "关于这版研究工作台",
    `<p class="dialog-intro">本地交互原型 · v0.4</p><p class="dialog-intro">可以创建研究方向、讨论、逐层分支、演示验证、对照结果，并把经验带到下一次研究。示例指标与回复由固定规则生成，未连接模型或真实实验执行器。</p><p class="dialog-intro">研究记录保存在当前浏览器与地址。请定期导出备份；刷新不会自动重放未完成的演示。源码路径与网页链接仅作为参考记录，不会自动读取。</p><div class="card-actions"><button class="button primary" data-action="export">导出全部研究备份</button>${unreadable ? '<button class="button" data-action="export-raw">导出无法读取的原始记录</button>' : ""}<button class="button" data-action="import-backup">导入备份</button></div><details class="memory-history"><summary>历史原型与演示管理</summary><p class="condition-note">旧研究室及 Agent 连接保留在历史页面，使用独立数据。默认工作台无需登录。</p><div class="card-actions"><a href="team.html" class="text-button">旧研究室 / Agent 连接 ↗</a><a href="network.html" class="text-button">旧任务图 ↗</a><button class="text-button" data-action="reset">重置工作台样例</button></div></details>`,
  );
}
function search() {
  const q = $("#search-input").value.trim().toLowerCase();
  const matches = [];
  for (const b of state.branches)
    if (
      `${b.title} ${b.hypothesis} ${studyOf(state, b.studyId).title}`
        .toLowerCase()
        .includes(q)
    )
      matches.push({
        title: b.title,
        note: studyOf(state, b.studyId).title,
        branchId: b.id,
      });
  for (const m of state.messages)
    if (q && m.text.toLowerCase().includes(q))
      matches.push({
        title: m.text.slice(0, 90),
        note: `讨论 · ${branchOf(state, m.branchId).title}`,
        branchId: m.branchId,
        messageId: m.id,
      });
  for (const m of state.memories)
    if (`${m.title} ${m.claim}`.toLowerCase().includes(q))
      matches.push({ title: m.title, note: "经验库", memoryId: m.id });
  $("#search-results").innerHTML =
    `<p class="results-count">${matches.length} 条匹配记录</p>${matches
      .slice(0, 30)
      .map(
        (m) =>
          `<button class="search-result" data-action="search-go" ${m.memoryId ? `data-memory="${m.memoryId}"` : `data-id="${m.branchId}"`} ${m.messageId ? `data-message="${m.messageId}"` : ""}><strong>${esc(m.title)}</strong><small>${esc(m.note)}</small></button>`,
      )
      .join("")}`;
}
document.addEventListener("click", (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action,
    rid = el.dataset.id;
  if (action === "home" || action === "new") {
    $("#sidebar").classList.remove("open");
    $("[data-action=sidebar]").setAttribute("aria-expanded", "false");
    state.page = "home";
    save();
    render();
    if (action === "new") $("#direction").focus();
    window.scrollTo({ top: 0 });
  } else if (action === "study")
    goBranch(
      state.branches.find((b) => b.studyId === rid && b.id === state.branch)
        ?.id || rootBranch(rid)?.id,
    );
  else if (action === "branch") goBranch(rid);
  else if (action === "memories") {
    $("#sidebar").classList.remove("open");
    $("[data-action=sidebar]").setAttribute("aria-expanded", "false");
    state.page = "memories";
    save();
    render();
  } else if (action === "fold") {
    act(() => {
      state.collapsed = state.collapsed.includes(rid)
        ? state.collapsed.filter((x) => x !== rid)
        : [...state.collapsed, rid];
    });
  } else if (action === "sidebar") {
    const open = $("#sidebar").classList.toggle("open");
    el.setAttribute("aria-expanded", String(open));
  } else if (action === "prompt") {
    state.homeDraft = el.dataset.text;
    save();
    $("#direction").value = state.homeDraft;
    $("#direction").focus();
  } else if (action === "tab") {
    state.tab = el.dataset.tab;
    save();
    render();
  } else if (action === "fork") showFork();
  else if (action === "message-fork") {
    const m = state.messages.find((x) => x.id === rid);
    showFork(m.branchId, null, { hypothesis: m.text, key: m.id });
  } else if (action === "run-fork") {
    const r = state.runs.find((x) => x.id === rid);
    showFork(r.branchId, r.id);
  } else if (action === "explore") showExplore();
  else if (action === "pause") {
    $("#detail-dialog").close();
    act(() => pauseBranch(state, state.branch), "分支已暂停，记录保留。");
  } else if (action === "resume")
    act(() => resumeBranch(state, state.branch), "分支已恢复。");
  else if (action === "stop")
    act(
      () => stopExploration(state, state.branch),
      "本轮验证已结束，已有结果保留。",
    );
  else if (action === "import-result") showImportResult();
  else if (action === "conditions") showConditions();
  else if (action === "add-source") showSource();
  else if (action === "source-fork") {
    const r = state.sources.find((x) => x.id === rid);
    showFork(state.branch, null, {
      hypothesis: r.relevance,
      sourceIds: [r.id],
      key: r.id,
    });
  } else if (action === "source-detail") {
    const r = state.sources.find((x) => x.id === rid);
    if (r)
      details(
        r.title,
        `<div class="card-meta"><span class="tag warning">待本地验证</span><span>${esc(r.published)}</span></div><p class="dialog-intro">${esc(r.relevance)}</p><p class="dialog-intro">适用条件：${esc(r.conditions)}</p><p class="dialog-intro">记录时间：${when(r.capturedAt)}</p><a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>`,
      );
  } else if (action === "save-memory") showMemory();
  else if (action === "memory-detail") {
    const m = state.memories.find((x) => x.id === rid);
    if (m) details("经验与证据", memoryCard(m, true));
  } else if (action === "memory-status") showMemoryStatus(rid);
  else if (action === "memory-branch") {
    $("#detail-dialog").close();
    goBranch(state.memories.find((x) => x.id === rid).branchId);
  } else if (action === "use-memory") {
    $("#detail-dialog").close();
    act(
      () => useMemory(state, state.branch, rid),
      "已引用到当前分支，请核对适用条件。",
    );
  } else if (action === "run-detail") showRun(rid);
  else if (action === "detail-branch") {
    $("#detail-dialog").close();
    detailRun = null;
    goBranch(rid, "runs");
  } else if (action === "cite-run") citeRun(rid, el.dataset.metric);
  else if (action === "compare-discuss") {
    const draft = (state.drafts[state.branch] ||= { text: "", refs: [] });
    for (const runId of [compareLeft, compareRight])
      if (runId && !draft.refs.some((r) => r.kind === "run" && r.id === runId))
        draft.refs.push({ kind: "run", id: runId });
    draft.text ||= "这两份结果的差异说明了什么？下一步需要排除哪些影响因素？";
    state.tab = "research";
    save();
    render();
    $("#message").focus();
  } else if (action === "remove-ref") {
    act(() => {
      state.drafts[state.branch].refs.splice(Number(el.dataset.index), 1);
    });
  } else if (action === "branch-menu") {
    const b = branchOf(state);
    details(
      "这条分支接下来怎么走",
      `<p class="dialog-intro">${esc(b.title)}</p><div class="card-actions">${b.status === "active" ? '<button class="button" data-action="pause">暂停分支</button>' : ""}<button class="button" data-action="save-memory">整理经验草稿</button><button class="button" data-action="conclude">记录阶段结论</button><button class="button" data-action="export">导出研究备份</button></div>`,
    );
  } else if (action === "conclude") {
    const b = branchOf(state);
    openFlow(
      "给这一段研究留下结论",
      `${textField("conclusion", "目前得到的结论", b.conclusion, "可以明确写「没有得到确定结果」，并记录下一次从哪里继续。")}<div class="inline-notice">这条分支将收束，未完成的演示会取消。子分支仍可独立继续；之后可重新开放。</div>`,
      (data) => closeBranch(state, b.id, data.conclusion),
      "记录并收束",
      `conclusion:${b.id}`,
    );
  } else if (action === "close-dialog") $("#flow-dialog").close();
  else if (action === "close-detail") {
    $("#detail-dialog").close();
    detailRun = null;
  } else if (action === "close-search") $("#search-dialog").close();
  else if (action === "about") showAbout();
  else if (action === "export") exportBackup();
  else if (action === "export-raw") exportBackup(true);
  else if (action === "copy-backup") void copyBackup();
  else if (action === "import-backup") {
    $("#backup-input").value = "";
    $("#backup-input").click();
  } else if (action === "reset")
    openFlow(
      "重置当前工作台",
      '<p class="dialog-intro">这会清除当前工作台中的研究、讨论与经验，并恢复内置样例。旧研究室的数据和外观偏好不受影响。建议先导出备份。</p><label class="checkbox"><input type="checkbox" name="confirm" value="yes" required /><span>我已备份需要保留的研究，确认重置。</span></label>',
      () => {
        state = createDemo();
      },
      "确认重置",
      "recovery-reset",
    );
  else if (action === "search") {
    $("#search-dialog").showModal();
    $("#search-input").focus();
    search();
  } else if (action === "search-go") {
    $("#search-dialog").close();
    if (el.dataset.memory) {
      state.page = "memories";
      save();
      render();
      details(
        "经验与证据",
        memoryCard(
          state.memories.find((m) => m.id === el.dataset.memory),
          true,
        ),
      );
    } else {
      goBranch(rid);
      if (el.dataset.message)
        document
          .getElementById(el.dataset.message)
          ?.scrollIntoView({ block: "center" });
    }
  }
});
document.addEventListener("submit", (event) => {
  if (event.target.id === "direction-form") {
    event.preventDefault();
    newDirection($("#direction").value);
  }
  if (event.target.id === "discussion-form") {
    event.preventDefault();
    act(
      () =>
        addMessage(
          state,
          state.branch,
          $("#message").value,
          state.drafts[state.branch]?.refs || [],
        ),
      "讨论已记录，可以从这个想法创建验证分支。",
    );
    $("#message")?.focus();
  }
  if (event.target.id === "flow-form") {
    event.preventDefault();
    const recovery = ["recovery-reset", "recovery-import"].includes(flowKey);
    if (storageBlocked && !recovery) {
      $("#flow-error").textContent =
        "本页保存已暂停，请先导出备份并处理顶部提示。";
      return;
    }
    const form = new FormData(event.target),
      before = structuredClone(state);
    try {
      flowHandler(Object.fromEntries(form), form);
      if (flowKey) delete state.drafts[`form:${flowKey}`];
      if (recovery) {
        lastSaved = localStorage.getItem(STORAGE_KEY);
        storageBlocked = false;
      }
      const saved = save();
      if (recovery && saved) {
        unreadable = null;
        $("#storage-warning").hidden = true;
      }
      $("#flow-dialog").close();
      render();
      toast(
        saved ? "已保存到本地研究记录。" : "当前改动仅在本页，请导出备份。",
      );
    } catch (error) {
      state = before;
      $("#flow-error").textContent = error.message;
    }
  }
});
function saveFormDraft() {
  if (!flowKey) return;
  const data = {},
    form = new FormData($("#flow-form"));
  for (const el of $("#flow-form").elements)
    if (el.name) data[el.name] = form.getAll(el.name);
  state.drafts[`form:${flowKey}`] = data;
  save();
}
document.addEventListener("input", (event) => {
  if (event.target.id === "direction") {
    state.homeDraft = event.target.value;
    save();
  } else if (event.target.id === "message") {
    const draft = (state.drafts[state.branch] ||= { text: "", refs: [] });
    draft.text = event.target.value;
    save();
  } else if (event.target.id === "search-input") search();
  else if (event.target.closest("#flow-form")) saveFormDraft();
});
document.addEventListener("change", async (event) => {
  if (event.target.id === "compare-left") {
    compareLeft = event.target.value;
    render();
  } else if (event.target.id === "compare-right") {
    compareRight = event.target.value;
    render();
  } else if (event.target.id === "result-json") {
    const file = event.target.files[0],
      targetBranch = state.branch,
      targetKey = flowKey;
    if (!file) return;
    try {
      if (file.size > 262144) throw Error("实验 JSON 不能超过 256 KB。");
      const data = readResultJSON(
        await file.text(),
        branchOf(state, targetBranch).snapshot.protocol,
      );
      if (flowKey !== targetKey || !$("#flow-dialog").open) return;
      for (const key of ["metric", "guard", "codeRef", "summary", "artifact"]) {
        const field = $("#flow-form").elements.namedItem(key);
        if (field && data[key] !== undefined) field.value = String(data[key]);
      }
      saveFormDraft();
      $("#flow-error").textContent = "";
      toast("测量数据已填入，请核对后保存。");
    } catch (error) {
      $("#flow-error").textContent = error.message;
    }
  } else if (event.target.closest("#flow-form")) saveFormDraft();
});
$("#backup-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 5000000) {
    toast("备份不能超过 5 MB。");
    return;
  }
  try {
    const imported = decodeState(await file.text());
    openFlow(
      "恢复研究备份",
      `<p class="dialog-intro">备份包含 ${imported.studies.length} 项研究、${imported.branches.length} 条分支、${imported.runs.length} 份实验与 ${imported.memories.length} 条经验。</p><p class="dialog-intro">恢复将替换当前工作台内容。建议先导出当前备份，已中断的演示不会自动执行。</p><label class="checkbox"><input name="confirm" value="yes" type="checkbox" required /><span>确认用这份备份替换当前工作台。</span></label>`,
      () => {
        state = imported;
        state.page = "home";
      },
      "恢复备份",
      "recovery-import",
    );
  } catch (error) {
    toast(`备份未导入：${error.message}`);
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#search-dialog").showModal();
    $("#search-input").focus();
    search();
  }
  if (event.key === "Escape") {
    $("#sidebar").classList.remove("open");
    $("[data-action=sidebar]").setAttribute("aria-expanded", "false");
  }
  const tab = event.target.closest('[role="tab"]');
  if (tab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const pendingRun = runsOf(state, b.id).find((r) =>
      ["running", "queued"].includes(r.status),
    );
    const tabs = [...document.querySelectorAll('[role="tab"]')],
      index = tabs.indexOf(tab),
      next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) %
              tabs.length;
    state.tab = tabs[next].dataset.tab;
    save();
    render();
    document.getElementById(`tab-${state.tab}`).focus();
  }
});
window.addEventListener("storage", (event) => {
  if (event.key === STORAGE_KEY && event.newValue !== lastSaved) {
    storageBlocked = true;
    storageWarning(
      "另一个页面更新了研究记录。本页已暂停保存，请先导出本页备份，再刷新加载最新记录。",
    );
  }
});
$("#detail-dialog").addEventListener("close", () => {
  detailRun = null;
  if (backupObjectURL) {
    URL.revokeObjectURL(backupObjectURL);
    backupObjectURL = null;
  }
});
save();
render();
setInterval(() => {
  if (!storageBlocked && tick(state)) {
    save();
    render();
  }
}, 400);
