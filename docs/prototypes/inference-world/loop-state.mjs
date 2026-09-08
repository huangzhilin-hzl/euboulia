// Product simulation only. No model requests, external retrieval or process execution.
import { escapeHTML, safeURL } from "./research-state.mjs";
export { escapeHTML, safeURL };
export const KEY = "euboulia.loop-workbench.v1";
export const ROLES = {
  lead: "协调者",
  researcher: "研究者",
  championA: "方案 A",
  championB: "方案 B",
  executor: "执行者",
  monitor: "过程观察者",
  auditor: "审阅者",
  you: "你",
};
export const STATUS = {
  baseline: "起点",
  proposed: "待质疑",
  challenged: "待答辩",
  ready: "可验证",
  running: "验证中",
  review: "待判断",
  kept: "已采纳",
  rejected: "未采纳",
  paused: "已暂停",
  interrupted: "已中断",
};
export const PHASES = ["准备方案", "执行尝试", "评价结果", "记录证据"];
const clone = (x) => structuredClone(x);
const uid = (s, type) => `${type}-${s.next++}`;
const requireText = (v, label, max = 4000) => {
  if (typeof v !== "string" || !v.trim() || v.length > max)
    throw Error(`请填写${label}（最多 ${max} 字）。`);
  return v.trim();
};
export const loopOf = (s, id = s.loopId) => s.loops.find((l) => l.id === id);
export const nodeOf = (s, id = s.nodeId) => s.nodes.find((n) => n.id === id);
export const nodesOf = (s, id = s.loopId) =>
  s.nodes.filter((n) => n.loopId === id);
export const contractKey = (c) => JSON.stringify([c.context, c.criteria]);
export function empty() {
  return {
    version: 1,
    next: 1,
    loopId: null,
    nodeId: null,
    view: "evolution",
    loops: [],
    nodes: [],
    messages: [],
    sources: [],
    memories: [],
    drafts: {},
    jobs: [],
    suggestions: [],
  };
}
export function validateCriteria(criteria) {
  if (!Array.isArray(criteria) || !criteria.length || criteria.length > 8)
    throw Error("需要 1–8 项评价标准。");
  const ids = new Set();
  for (const c of criteria) {
    requireText(c.id, "指标标识", 40);
    if (
      !/^[a-z][a-z0-9-]*$/.test(c.id) ||
      ["artifact", "summary", "constructor"].includes(c.id)
    )
      throw Error("评价标识应为独立的字母数字名称。");
    requireText(c.name, "评价标准", 100);
    if (
      ids.has(c.id) ||
      !["metric", "rubric", "check"].includes(c.kind) ||
      !["higher", "lower"].includes(c.direction) ||
      typeof c.unit !== "string" ||
      c.unit.length > 40 ||
      typeof c.required !== "boolean"
    )
      throw Error("评价标准格式不正确。");
    if (c.target !== null && !Number.isFinite(c.target))
      throw Error("目标需要是数字或留空。");
    if (c.required && c.kind !== "check" && c.target === null)
      throw Error("必须满足的数值或评分标准需要填写目标。");
    if (
      c.kind === "rubric" &&
      c.target !== null &&
      (c.target < 0 || c.target > 100)
    )
      throw Error("评分目标应为 0–100。");
    ids.add(c.id);
  }
  return clone(criteria);
}
function record(s, l, nodeId, role, text, extra = {}) {
  const m = {
    id: uid(s, "message"),
    loopId: l.id,
    nodeId,
    role,
    text,
    at: Date.now(),
    ...clone(extra),
  };
  s.messages.push(m);
  return m;
}
export function createLoop(
  s,
  {
    title,
    objective,
    artifactKind = "任意产物",
    context = "",
    criteria,
    mode = "solo",
    maxRounds = 3,
    minutes = 10,
  },
) {
  const l = {
    id: uid(s, "loop"),
    title: requireText(title, "Loop 名称", 180),
    objective: requireText(objective, "目标"),
    artifactKind: requireText(artifactKind, "产物类型", 100),
    context: requireText(context || "条件待补充", "评价条件"),
    criteria: validateCriteria(criteria),
    mode,
    roles: clone(ROLES),
    maxRounds: Number(maxRounds),
    minutes: Number(minutes),
    version: 1,
    currentId: null,
    history: [],
    demo: false,
  };
  if (!["solo", "debate"].includes(mode)) throw Error("协作模式不正确。");
  checkBudget(l.maxRounds, l.minutes);
  s.loops.push(l);
  const n = makeNode(
    s,
    l,
    null,
    "记录当前起点",
    "先保留现状及评价方法，再开始改进。",
    "researcher",
  );
  n.status = "baseline";
  l.currentId = n.id;
  s.loopId = l.id;
  s.nodeId = n.id;
  s.view = "evolution";
  record(
    s,
    l,
    n.id,
    "lead",
    "方向已建立。可以录入当前产物及评价，或用明确标记的演示体验后续流程。",
    { demo: true },
  );
  return l;
}
function makeNode(s, l, parent, title, plan, role = "championA") {
  const n = {
    id: uid(s, "node"),
    loopId: l.id,
    parentId: parent?.id || null,
    title: requireText(title, "方案名称", 180),
    plan: requireText(plan, "方案"),
    role,
    depth: parent ? parent.depth + 1 : 0,
    status: l.mode === "debate" ? "proposed" : "ready",
    snapshot: {
      context: l.context,
      criteria: clone(l.criteria),
      version: l.version,
      artifactKind: l.artifactKind,
      objective: l.objective,
    },
    debate: [],
    result: null,
    decision: null,
    sourceIds: parent ? [...parent.sourceIds] : [],
    memoryIds: parent ? [...parent.memoryIds] : [],
    directives: [],
    history: [],
    createdAt: Date.now(),
  };
  s.nodes.push(n);
  return n;
}
export function fork(
  s,
  parentId,
  { title, plan, role = "championA", sourceId = null },
) {
  const p = nodeOf(s, parentId);
  if (!p) throw Error("起点不存在。");
  const l = loopOf(s, p.loopId);
  if (!["championA", "championB", "lead"].includes(role))
    throw Error("请选择提案角色。");
  if (
    sourceId &&
    !s.sources.some((x) => x.id === sourceId && x.loopId === l.id)
  )
    throw Error("资料不属于这个 Loop。");
  const n = makeNode(s, l, p, title, plan, role);
  if (sourceId) n.sourceIds = [...new Set([...n.sourceIds, sourceId])];
  s.nodeId = n.id;
  s.loopId = l.id;
  record(s, l, n.id, role, `从「${p.title}」继续：${plan}`, { demo: false });
  return n;
}
export function proposePair(s, parentId) {
  const p = nodeOf(s, parentId),
    l = loopOf(s, p?.loopId);
  if (!l) throw Error("请选择一个起点。");
  const a = fork(s, p.id, {
    title: "A · 聚焦一个可改进点",
    plan: `围绕「${l.objective}」，先只改变一个因素，以「${l.criteria[0].name}」观察变化；保留其余条件。`,
    role: "championA",
  });
  const b = fork(s, p.id, {
    title: "B · 尝试另一种路径",
    plan: `从「${p.title}」探索不同方法，先完成一个小样本，再按相同条件评价「${l.artifactKind}」。`,
    role: "championB",
  });
  for (const n of [a, b]) {
    n.debate.push({
      kind: "proposal",
      role: n.role,
      text: n.plan,
      at: Date.now(),
      demo: true,
    });
  }
  s.nodeId = a.id;
  record(
    s,
    l,
    p.id,
    "lead",
    "已建立两个演示提案。它们共享起点；经过质疑与答辩后，可以分别验证。",
    { demo: true },
  );
  return [a, b];
}
export function debateStep(s, nodeId, kind, text = "", role = null) {
  const n = nodeOf(s, nodeId),
    l = loopOf(s, n?.loopId);
  if (!n || n.result) throw Error("已完成的尝试不能重写提案。");
  const expected = {
    critique: "proposed",
    rebuttal: "challenged",
    select: "challenged",
  };
  if (kind === "critique" && n.status !== "proposed")
    throw Error("这份提案已经进入后续阶段。");
  if (kind === "rebuttal" && n.status !== "challenged")
    throw Error("先提出具体质疑。");
  if (
    kind === "select" &&
    (!["challenged", "ready"].includes(n.status) ||
      !n.debate.some((d) => d.kind === "rebuttal"))
  )
    throw Error("先保留质疑和答辩，再决定验证。");
  if (!Object.hasOwn(expected, kind)) throw Error("不支持的讨论步骤。");
  const actor =
    kind === "critique"
      ? n.role === "championB"
        ? "championA"
        : "championB"
      : kind === "rebuttal"
        ? n.role
        : "lead";
  if (role && role !== actor) throw Error("该角色不能完成这个步骤。");
  const generated = {
    critique: `「${n.title}」的局部改善是否能推进「${l.objective}」？请说明对照、失败条件，以及「${
      l.criteria
        .filter((c) => c.required)
        .map((c) => c.name)
        .join("、") || "验收标准"
    }」如何验证。`,
    rebuttal: `保留起点与评价条件。验证「${n.plan}」时先做最小尝试；若必要条件失败，记录原因，不把局部收益视为目标达成。`,
    select: "已保留双方观点。该计划进入可验证状态；选择提案不等于采纳结果。",
  };
  const entry = {
    kind,
    role: actor,
    text: requireText(text || generated[kind], "讨论内容", 8000),
    at: Date.now(),
    demo: !text,
  };
  n.debate.push(entry);
  n.status = kind === "select" ? "ready" : "challenged";
  record(s, l, n.id, actor, entry.text, { demo: entry.demo, debate: kind });
  return entry;
}
export function revisePlan(s, nodeId, plan) {
  const n = nodeOf(s, nodeId);
  if (!n || n.result || ["running", "paused"].includes(n.status))
    throw Error("执行快照已固定，请另开分支。");
  n.history.push({
    kind: "plan",
    plan: n.plan,
    debate: clone(n.debate),
    at: Date.now(),
  });
  n.plan = requireText(plan, "修改后的计划");
  n.status = loopOf(s, n.loopId).mode === "debate" ? "proposed" : "ready";
  n.debate = [];
}
function checkBudget(rounds, minutes) {
  if (
    !Number.isInteger(rounds) ||
    rounds < 1 ||
    rounds > 12 ||
    !Number.isFinite(minutes) ||
    minutes < 1 ||
    minutes > 60
  )
    throw Error("预算应为 1–12 轮、1–60 分钟。");
}
export function start(
  s,
  nodeId,
  { maxRounds = 1, minutes = 5 } = {},
  now = Date.now(),
) {
  const n = nodeOf(s, nodeId),
    l = loopOf(s, n?.loopId);
  if (!n || !["ready", "interrupted"].includes(n.status) || n.result)
    throw Error("请选择可验证的计划。");
  if (
    s.jobs.some(
      (j) => j.loopId === l.id && ["running", "paused"].includes(j.status),
    )
  )
    throw Error("这个 Loop 已有一轮探索，请先暂停后结束或等待它返回。");
  checkBudget(Number(maxRounds), Number(minutes));
  const j = {
    id: uid(s, "job"),
    loopId: l.id,
    nodeId: n.id,
    rootId: n.id,
    maxRounds: Number(maxRounds),
    minutes: Number(minutes),
    used: 0,
    startedAt: now,
    phaseAt: now,
    phase: 0,
    status: "running",
    pending: [],
    elapsed: 0,
  };
  s.jobs.push(j);
  n.status = "running";
  n.execution = {
    plan: n.plan,
    directives: clone(n.directives),
    snapshot: clone(n.snapshot),
  };
  record(
    s,
    l,
    n.id,
    "executor",
    `演示验证已开始：最多 ${maxRounds} 轮 / ${minutes} 分钟。可以继续讨论；新指令会在下一轮生效。`,
    { demo: true },
  );
  return j;
}
export function pause(s, jobId, now = Date.now()) {
  const j = s.jobs.find((j) => j.id === jobId);
  if (j?.status !== "running") throw Error("没有可暂停的验证。");
  j.elapsed += now - j.startedAt;
  j.status = "paused";
  nodeOf(s, j.nodeId).status = "paused";
}
export function resume(s, jobId, now = Date.now()) {
  const j = s.jobs.find((j) => j.id === jobId);
  if (j?.status !== "paused") throw Error("验证没有暂停。");
  j.startedAt = now;
  j.phaseAt = now;
  j.status = "running";
  nodeOf(s, j.nodeId).status = "running";
}
export function stop(s, jobId, reason = "你结束了这轮探索") {
  const j = s.jobs.find((j) => j.id === jobId);
  if (!j || !["running", "paused"].includes(j.status))
    throw Error("验证已结束。");
  j.status = "stopped";
  j.reason = reason;
  const n = nodeOf(s, j.nodeId);
  if (!n.result) n.status = "interrupted";
  record(
    s,
    loopOf(s, j.loopId),
    n.id,
    "lead",
    `${reason}。已有结果与未应用指令已保留。`,
    { demo: true },
  );
}
export function submitMessage(
  s,
  {
    loopId = s.loopId,
    nodeId = s.nodeId,
    text,
    role = "lead",
    intent = "discuss",
  },
) {
  const l = loopOf(s, loopId),
    n = nodeId ? nodeOf(s, nodeId) : null;
  if (!l || (n && n.loopId !== l.id)) throw Error("讨论归属不正确。");
  if (
    !Object.hasOwn(ROLES, role) ||
    role === "you" ||
    !["discuss", "steer", "challenge", "plan"].includes(intent)
  )
    throw Error("讨论方式不正确。");
  const value = requireText(text, "讨论内容");
  const m = record(s, l, n?.id || null, "you", value, { target: role, intent });
  const context = n ? `当前节点「${n.title}」` : `整个 Loop「${l.title}」`;
  const answer = {
    discuss: `围绕${context}，你提出了：“${value}”。当前${n?.result ? "已有一份评价，可核对原始证据与验收标准" : "尚无这一步的评价证据"}。可以把这个问题交给其他角色质疑，或转成下一轮明确的尝试。`,
    challenge: `针对${context}的质疑已记录：“${value}”。请用对照或反例核查「${l.criteria.map((c) => c.name).join("、")}」；质疑不会自动改写既有评价。`,
    steer: `可以将“${value}”加入下一轮计划。当前执行快照保持不变，请在下方选择应用时机。`,
    plan: `把“${value}”作为独立方案，从${context}继续；沿用评价标准，保留失败路径，验证后再判断是否采纳。`,
  };
  const reply = record(s, l, n?.id || null, role, answer[intent], {
    demo: true,
    replyTo: m.id,
  });
  if (["steer", "plan"].includes(intent)) {
    const p = {
      id: uid(s, "suggestion"),
      loopId: l.id,
      nodeId: n?.id || l.currentId,
      text: value,
      kind: intent,
      status: "pending",
      messageId: reply.id,
    };
    s.suggestions.push(p);
    reply.suggestionId = p.id;
  }
  delete s.drafts[`${l.id}:${n?.id || "loop"}`];
  return reply;
}
export function applySuggestion(s, id, mode = "branch") {
  const p = s.suggestions.find((p) => p.id === id);
  if (!p || p.status !== "pending") throw Error("这个建议已经处理。");
  const l = loopOf(s, p.loopId),
    n = nodeOf(s, p.nodeId);
  let next;
  if (mode === "next") {
    const j = s.jobs.find(
      (j) => j.loopId === l.id && ["running", "paused"].includes(j.status),
    );
    if (!j) throw Error("没有运行中的探索，可以另开分支。");
    j.pending.push({ suggestionId: p.id, text: p.text });
    p.status = "queued";
    record(s, l, j.nodeId, "lead", "指令已排入下一轮；当前轮次不变。", {
      demo: true,
    });
  } else if (mode === "branch") {
    next = fork(s, n.id, {
      title: p.text.slice(0, 100),
      plan: p.text,
      role: "lead",
    });
    p.status = "applied";
    p.appliedNodeId = next.id;
  } else throw Error("请选择应用时机。");
  return next;
}
export function validateValues(criteria, values) {
  if (!values || typeof values !== "object") throw Error("缺少评价结果。");
  for (const c of criteria) {
    const v = values[c.id];
    if (
      c.kind === "check"
        ? !["pass", "fail", "unknown"].includes(v)
        : !Number.isFinite(v)
    )
      throw Error(`请填写「${c.name}」的有效结果。`);
    if (c.kind === "rubric" && (v < 0 || v > 100))
      throw Error("评分必须在 0–100 之间。");
  }
  return clone(values);
}
export function assess(n) {
  if (!n.result) return { pass: false, reason: "尚无评价" };
  const failed = n.snapshot.criteria.filter(
    (c) =>
      c.required &&
      (c.kind === "check"
        ? n.result.values[c.id] !== "pass"
        : c.target !== null &&
          (c.direction === "higher"
            ? n.result.values[c.id] < c.target
            : n.result.values[c.id] > c.target)),
  );
  return {
    pass: failed.length === 0,
    reason: failed.length
      ? `未满足：${failed.map((c) => c.name).join("、")}`
      : "必要条件已满足 · 仍需判断目标收益",
  };
}
export function importEvaluation(
  s,
  nodeId,
  { values, artifact, summary, origin = "imported" },
) {
  const n = nodeOf(s, nodeId);
  if (!n || n.result || ["running", "paused"].includes(n.status))
    throw Error("请先结束执行；完成的评价请通过新分支保留。");
  const result = {
    values: validateValues(n.snapshot.criteria, values),
    artifact: requireText(artifact, "产物或证据", 12000),
    summary: requireText(summary, "评价说明"),
    origin,
    at: Date.now(),
  };
  if (origin !== "imported" && origin !== "demo")
    throw Error("结果来源不正确。");
  n.result = result;
  n.status = "review";
  return result;
}
export function comparison(a, b) {
  if (!a?.result || !b?.result || a.id === b.id)
    return { ok: false, reason: "请选择两份不同的评价结果。" };
  if (
    a.loopId !== b.loopId ||
    contractKey(a.snapshot) !== contractKey(b.snapshot)
  )
    return { ok: false, reason: "评价条件不一致；只展示原值。" };
  if (a.snapshot.context === "条件待补充")
    return { ok: false, reason: "评价条件尚未确认。" };
  if (a.result.origin !== b.result.origin)
    return { ok: false, reason: "演示与用户记录不混算。" };
  return {
    ok: true,
    reason: "同条件、同来源类型",
    values: a.snapshot.criteria.map((c) => ({
      id: c.id,
      name: c.name,
      kind: c.kind,
      left: a.result.values[c.id],
      right: b.result.values[c.id],
      delta:
        c.kind === "check"
          ? null
          : b.result.values[c.id] - a.result.values[c.id],
    })),
  };
}
export function decide(s, nodeId, verdict, reason) {
  const n = nodeOf(s, nodeId),
    l = loopOf(s, n?.loopId);
  if (!n?.result || !["keep", "reject"].includes(verdict))
    throw Error("先完成评价，再记录判断。");
  const why = requireText(reason, "判断依据");
  if (verdict === "keep" && !assess(n).pass)
    throw Error("仍有必要条件未满足，不能采纳。");
  n.decision = { verdict, reason: why, at: Date.now(), by: "you" };
  n.status = verdict === "keep" ? "kept" : "rejected";
  n.history.push(clone(n.decision));
  if (verdict === "keep") l.currentId = n.id;
  else if (l.currentId === n.id)
    l.currentId =
      nodesOf(s, l.id)
        .filter((x) => x.id !== n.id && x.status === "kept")
        .at(-1)?.id || nodesOf(s, l.id)[0].id;
  record(
    s,
    l,
    n.id,
    "you",
    `${verdict === "keep" ? "采纳" : "暂不采纳"}：${why}`,
  );
}
export function updateContract(s, loopId, { context, criteria, mode, roles }) {
  const l = loopOf(s, loopId);
  if (!l) throw Error("Loop 不存在。");
  const c = validateCriteria(criteria);
  if (!["solo", "debate"].includes(mode)) throw Error("模式不正确。");
  const newRoles = { ...l.roles };
  for (const [k, v] of Object.entries(roles || {})) {
    if (!Object.hasOwn(ROLES, k) || k === "you") throw Error("角色不正确。");
    newRoles[k] = requireText(v, "角色名称", 40);
  }
  l.history.push({
    version: l.version,
    context: l.context,
    criteria: clone(l.criteria),
    mode: l.mode,
    at: Date.now(),
  });
  l.context = requireText(context, "评价条件");
  l.criteria = c;
  l.mode = mode;
  l.roles = newRoles;
  l.version++;
}
export function addSource(s, { title, url, note, version }, nodeId = s.nodeId) {
  const n = nodeOf(s, nodeId);
  if (!n || !safeURL(url)) throw Error("请提供 http/https 来源链接。");
  const r = {
    id: uid(s, "source"),
    loopId: n.loopId,
    nodeId: n.id,
    title: requireText(title, "资料名称", 200),
    url: safeURL(url),
    note: requireText(note, "与当前任务的关系"),
    version: requireText(version, "日期或版本", 100),
  };
  s.sources.push(r);
  n.sourceIds.push(r.id);
  return r;
}
export function saveMemory(s, nodeId, { title, claim, conditions }) {
  const n = nodeOf(s, nodeId);
  if (!n?.result) throw Error("经验需要关联一份结果。");
  const m = {
    id: uid(s, "memory"),
    loopId: n.loopId,
    nodeId: n.id,
    title: requireText(title, "经验名称", 180),
    claim: requireText(claim, "经验"),
    conditions: requireText(conditions, "适用条件"),
    status: "candidate",
    origin: n.result.origin,
    snapshot: clone(n.snapshot),
    history: [],
  };
  s.memories.push(m);
  return m;
}
export function useMemory(s, nodeId, memoryId) {
  const n = nodeOf(s, nodeId),
    m = s.memories.find((m) => m.id === memoryId);
  if (!n || !m) throw Error("引用不存在。");
  if (!n.memoryIds.includes(m.id)) n.memoryIds.push(m.id);
  record(
    s,
    loopOf(s, n.loopId),
    n.id,
    "lead",
    `引用经验「${m.title}」：${m.claim}。适用条件：${m.conditions}。${contractKey(m.snapshot) === contractKey(n.snapshot) ? "仍需判断适用性。" : "条件已变化，需要复验。"}`,
    { demo: true },
  );
}
export function verdictMemory(s, id, status, reason) {
  const m = s.memories.find((x) => x.id === id);
  if (!m || !["candidate", "contested"].includes(status))
    throw Error("判定不正确。");
  m.history.push({
    from: m.status,
    to: status,
    reason: requireText(reason, "判定理由"),
    at: Date.now(),
  });
  m.status = status;
}
function demoResult(n, round) {
  const values = {};
  for (const c of n.snapshot.criteria) {
    values[c.id] =
      c.kind === "check"
        ? round === 2 && c.required
          ? "fail"
          : "pass"
        : c.kind === "rubric"
          ? Math.min(100, 76 + round * 4)
          : c.direction === "lower"
            ? (c.target ?? 100) -
              Math.abs(c.target ?? 100) * (0.07 + round * 0.02)
            : (c.target ?? 100) +
              Math.abs(c.target ?? 100) * (0.02 + round * 0.03);
  }
  return {
    values,
    artifact: `演示产物 · ${n.title}\n计划：${n.execution?.plan || n.plan}\n${n.directives.map((d) => `本轮指令：${d.text}`).join("\n")}\n这里展示产物结构，未处理真实源码、文档或数据。`,
    summary: n.snapshot.criteria.some(
      (c) => c.required && values[c.id] === "fail",
    )
      ? "第二轮演示包含必要条件失败，用于体验返回讨论与保留负面证据。"
      : "演示评价已返回，需结合目标与证据决定下一步。",
    origin: "demo",
    at: Date.now(),
  };
}
export function tick(s, now = Date.now()) {
  let changed = false;
  for (const j of s.jobs.filter((j) => j.status === "running")) {
    const n = nodeOf(s, j.nodeId),
      l = loopOf(s, j.loopId);
    if (j.elapsed + now - j.startedAt >= j.minutes * 60000) {
      stop(s, j.id, "时间预算已用完");
      changed = true;
      continue;
    }
    if (now - j.phaseAt < 1800) continue;
    j.phaseAt = now;
    j.phase++;
    changed = true;
    if (j.phase < PHASES.length) {
      record(
        s,
        l,
        n.id,
        j.phase === 2 ? "monitor" : "executor",
        `演示阶段：${PHASES[j.phase]}。${j.phase === 2 ? "核对目标与必要条件，当前对话可以继续。" : ""}`,
        { demo: true },
      );
      continue;
    }
    n.result = demoResult(n, j.used + 1);
    n.status = "review";
    j.used++;
    record(s, l, n.id, "auditor", `${n.result.summary} ${assess(n).reason}。`, {
      demo: true,
    });
    if (!assess(n).pass || j.used >= j.maxRounds) {
      j.status = "done";
      j.reason = !assess(n).pass ? "必要条件失败，返回讨论" : "轮次预算已用完";
      continue;
    }
    // A debated candidate may execute once, but later plans must be debated again.
    const next = makeNode(
      s,
      l,
      n,
      `第 ${j.used + 1} 轮 · 继续改进`,
      j.pending.length ? j.pending.map((p) => p.text).join("\n") : n.plan,
      n.role,
    );
    for (const p of j.pending) {
      next.directives.push(clone(p));
      const suggestion = s.suggestions.find((x) => x.id === p.suggestionId);
      if (suggestion) {
        suggestion.status = "applied";
        suggestion.appliedNodeId = next.id;
      }
    }
    j.pending = [];
    if (l.mode === "debate") {
      j.status = "done";
      j.reason = "下一轮方案需要重新质疑与答辩";
      record(s, l, next.id, "lead", j.reason, { demo: true });
      continue;
    }
    next.status = "running";
    next.execution = {
      plan: next.plan,
      directives: clone(next.directives),
      snapshot: clone(next.snapshot),
    };
    j.nodeId = next.id;
    j.phase = 0;
  }
  return changed;
}
export function recover(s) {
  for (const j of s.jobs) {
    if (["running", "paused"].includes(j.status)) {
      j.status = "interrupted";
      j.reason = "页面重新打开；未自动重放";
      const n = nodeOf(s, j.nodeId);
      if (!n.result) n.status = "interrupted";
    }
  }
  return s;
}
export function layout(nodes) {
  const children = (id) => nodes.filter((n) => n.parentId === id);
  let row = 0;
  const positions = [];
  function walk(n, depth = 0) {
    const kids = children(n.id);
    let y;
    if (!kids.length) y = row++ * 126;
    else {
      const ys = kids.map((k) => walk(k, depth + 1));
      y = (ys[0] + ys.at(-1)) / 2;
    }
    positions.push({ id: n.id, x: depth * 224 + 28, y: y + 54, depth });
    return y;
  }
  nodes.filter((n) => !n.parentId).forEach((n) => walk(n));
  return {
    positions,
    width: Math.max(700, ...positions.map((p) => p.x + 218)),
    height: Math.max(330, row * 126 + 90),
  };
}
export function validate(value) {
  const s = clone(value);
  if (s?.version !== 1 || !Number.isSafeInteger(s.next) || s.next < 1)
    throw Error("不是 Loop 工作台备份。");
  for (const key of [
    "loops",
    "nodes",
    "messages",
    "sources",
    "memories",
    "jobs",
    "suggestions",
  ])
    if (!Array.isArray(s[key]) || s[key].length > 5000)
      throw Error("记录结构不正确。");
  const ids = new Set();
  for (const item of [
    "loops",
    "nodes",
    "messages",
    "sources",
    "memories",
    "jobs",
    "suggestions",
  ].flatMap((k) => s[k])) {
    if (
      !item ||
      typeof item.id !== "string" ||
      !/^[a-z]+-\d+$/.test(item.id) ||
      ids.has(item.id) ||
      Number(item.id.split("-")[1]) >= s.next
    )
      throw Error("记录标识不正确。");
    ids.add(item.id);
  }
  for (const l of s.loops) {
    requireText(l.title, "Loop", 180);
    requireText(l.objective, "目标");
    requireText(l.context, "条件");
    validateCriteria(l.criteria);
    checkBudget(l.maxRounds, l.minutes);
    if (
      !["solo", "debate"].includes(l.mode) ||
      typeof l.artifactKind !== "string" ||
      !Number.isInteger(l.version) ||
      !Array.isArray(l.history) ||
      !l.roles
    )
      throw Error("Loop 结构不正确。");
    for (const k of Object.keys(ROLES)) requireText(l.roles[k], "角色", 40);
    if (
      nodesOf(s, l.id).filter((n) => !n.parentId).length !== 1 ||
      nodeOf(s, l.currentId)?.loopId !== l.id
    )
      throw Error("Loop 起点不正确。");
  }
  for (const n of s.nodes) {
    if (
      !loopOf(s, n.loopId) ||
      !Object.hasOwn(STATUS, n.status) ||
      !Object.hasOwn(ROLES, n.role) ||
      !Array.isArray(n.debate) ||
      !Array.isArray(n.directives) ||
      !Array.isArray(n.sourceIds) ||
      !Array.isArray(n.memoryIds) ||
      !Array.isArray(n.history)
    )
      throw Error("节点结构不正确。");
    requireText(n.title, "节点", 180);
    requireText(n.plan, "计划");
    requireText(n.snapshot?.context, "快照条件");
    validateCriteria(n.snapshot.criteria);
    if (!Number.isInteger(n.depth) || n.depth < 0)
      throw Error("节点深度错误。");
    let p = n,
      seen = new Set([n.id]);
    while (p.parentId) {
      p = nodeOf(s, p.parentId);
      if (!p || p.loopId !== n.loopId || seen.has(p.id))
        throw Error("演进关系存在循环。");
      seen.add(p.id);
    }
    if (n.depth !== seen.size - 1) throw Error("节点深度不一致。");
    if (n.result) {
      validateValues(n.snapshot.criteria, n.result.values);
      requireText(n.result.artifact, "产物", 12000);
      requireText(n.result.summary, "评价");
      if (!["demo", "imported"].includes(n.result.origin))
        throw Error("结果来源错误。");
    }
    if (n.status === "kept" && (!n.result || !assess(n).pass))
      throw Error("采纳记录缺少必要条件。");
    for (const d of n.debate) {
      if (
        !["proposal", "critique", "rebuttal", "select"].includes(d.kind) ||
        !Object.hasOwn(ROLES, d.role)
      )
        throw Error("辩论记录错误。");
      requireText(d.text, "辩论", 8000);
    }
    for (const d of n.directives) requireText(d.text, "指令");
  }
  for (const m of s.messages) {
    if (
      !loopOf(s, m.loopId) ||
      (m.nodeId && nodeOf(s, m.nodeId)?.loopId !== m.loopId) ||
      !Object.hasOwn(ROLES, m.role)
    )
      throw Error("讨论归属不正确。");
    requireText(m.text, "讨论", 12000);
  }
  for (const r of s.sources) {
    if (nodeOf(s, r.nodeId)?.loopId !== r.loopId || !safeURL(r.url))
      throw Error("资料引用不正确。");
    requireText(r.title, "资料", 200);
    requireText(r.note, "资料说明");
    requireText(r.version, "版本", 100);
  }
  for (const m of s.memories) {
    if (
      !nodeOf(s, m.nodeId)?.result ||
      nodeOf(s, m.nodeId)?.loopId !== m.loopId ||
      !["candidate", "contested"].includes(m.status) ||
      !Array.isArray(m.history)
    )
      throw Error("经验证据不正确。");
    requireText(m.title, "经验", 180);
    requireText(m.claim, "经验内容");
    requireText(m.conditions, "适用条件");
    validateCriteria(m.snapshot?.criteria);
  }
  for (const n of s.nodes)
    if (
      n.sourceIds.some(
        (id) => !s.sources.some((r) => r.id === id && r.loopId === n.loopId),
      ) ||
      n.memoryIds.some((id) => !s.memories.some((m) => m.id === id))
    )
      throw Error("节点引用不正确。");
  for (const j of s.jobs) {
    if (
      nodeOf(s, j.nodeId)?.loopId !== j.loopId ||
      nodeOf(s, j.rootId)?.loopId !== j.loopId ||
      !["running", "paused", "stopped", "done", "interrupted"].includes(
        j.status,
      ) ||
      !Array.isArray(j.pending) ||
      !Number.isInteger(j.used) ||
      j.used < 0 ||
      j.used > j.maxRounds ||
      !Number.isInteger(j.phase) ||
      j.phase < 0 ||
      j.phase > 4 ||
      ![j.startedAt, j.phaseAt, j.elapsed].every(Number.isFinite)
    )
      throw Error("执行记录不正确。");
    checkBudget(j.maxRounds, j.minutes);
    for (const p of j.pending) {
      requireText(p?.text, "待应用指令");
      if (
        !s.suggestions.some(
          (x) =>
            x.id === p.suggestionId &&
            x.loopId === j.loopId &&
            x.status === "queued",
        )
      )
        throw Error("待应用指令引用不正确。");
    }
  }
  for (const p of s.suggestions) {
    if (
      nodeOf(s, p.nodeId)?.loopId !== p.loopId ||
      !["pending", "queued", "applied"].includes(p.status) ||
      !["steer", "plan"].includes(p.kind) ||
      !s.messages.some((m) => m.id === p.messageId && m.loopId === p.loopId)
    )
      throw Error("介入记录不正确。");
    requireText(p.text, "介入内容");
    if (p.appliedNodeId && nodeOf(s, p.appliedNodeId)?.loopId !== p.loopId)
      throw Error("介入结果不正确。");
  }
  if (
    !s.drafts ||
    typeof s.drafts !== "object" ||
    Array.isArray(s.drafts) ||
    Object.values(s.drafts).some((v) => typeof v !== "string")
  )
    throw Error("草稿不正确。");
  if (
    !["evolution", "plans", "results", "knowledge", "contract"].includes(s.view)
  )
    throw Error("视图不正确。");
  if (s.loopId !== null && !loopOf(s)) throw Error("当前 Loop 不存在。");
  if (s.nodeId !== null && nodeOf(s)?.loopId !== s.loopId)
    throw Error("当前节点不正确。");
  return s;
}
export function decode(text) {
  if (typeof text !== "string" || text.length > 5000000)
    throw Error("备份上限为 5 MB。");
  return recover(validate(JSON.parse(text)));
}
export const TEMPLATES = {
  answer: {
    title: "让知识库回答更可靠",
    objective: "提高答案的可用性，同时保留准确、可追溯的引用。",
    artifactKind: "回答 / 提示词",
    context: "同一组 20 条问题；相同知识库；按准确、完整、清晰三项评分。",
    criteria: [
      {
        id: "quality",
        name: "答案质量",
        kind: "rubric",
        unit: "分",
        direction: "higher",
        target: 80,
        required: false,
      },
      {
        id: "citations",
        name: "引用可追溯",
        kind: "check",
        unit: "",
        direction: "higher",
        target: null,
        required: true,
      },
    ],
    mode: "debate",
  },
  code: {
    title: "缩短数据处理耗时",
    objective: "降低完整流水线耗时，保持输出一致。",
    artifactKind: "代码 / 数据",
    context: "同一份输入数据、同一运行环境；计时三次取中位数；输出逐项对照。",
    criteria: [
      {
        id: "latency",
        name: "总耗时",
        kind: "metric",
        unit: "秒",
        direction: "lower",
        target: 100,
        required: false,
      },
      {
        id: "correct",
        name: "输出一致",
        kind: "check",
        unit: "",
        direction: "higher",
        target: null,
        required: true,
      },
    ],
    mode: "solo",
  },
  writing: {
    title: "迭代一份产品说明",
    objective: "让读者能清楚理解功能并独立完成第一次使用。",
    artifactKind: "文档 / 设计方案",
    context: "按同一目标读者、术语表和评审清单检查每个版本。",
    criteria: [
      {
        id: "clarity",
        name: "表达清晰度",
        kind: "rubric",
        unit: "分",
        direction: "higher",
        target: 85,
        required: false,
      },
      {
        id: "steps",
        name: "关键步骤完整",
        kind: "check",
        unit: "",
        direction: "higher",
        target: null,
        required: true,
      },
    ],
    mode: "solo",
  },
  custom: {
    title: "",
    objective: "",
    artifactKind: "任意产物",
    context: "",
    criteria: [
      {
        id: "progress",
        name: "目标完成度",
        kind: "rubric",
        unit: "分",
        direction: "higher",
        target: null,
        required: false,
      },
    ],
    mode: "solo",
  },
};
export function demo() {
  const s = empty();
  const l = createLoop(s, TEMPLATES.answer);
  l.demo = true;
  const root = nodeOf(s);
  root.title = "原始回答流程";
  importEvaluation(s, root.id, {
    values: { quality: 68, citations: "pass" },
    artifact: "示例起点：直接检索后生成回答。\n已保存问题集与评分表的引用。",
    summary: "答案可读，但复杂问题经常缺少必要背景。",
    origin: "demo",
  });
  root.status = "kept";
  root.decision = {
    verdict: "keep",
    reason: "保留为对照起点",
    at: Date.now(),
    by: "you",
  };
  const [a, b] = proposePair(s, root.id);
  a.title = "分解问题后检索";
  a.plan = "先拆分子问题，分别检索，再带着来源合成回答。";
  b.title = "压缩检索上下文";
  b.plan = "减少无关片段，检查更短上下文能否提高答案质量。";
  for (const n of [a, b]) {
    debateStep(s, n.id, "critique");
    debateStep(s, n.id, "rebuttal");
    debateStep(s, n.id, "select");
  }
  importEvaluation(s, a.id, {
    values: { quality: 82, citations: "pass" },
    artifact: "演示产物 v2\n问题分解 → 分别检索 → 引用校验 → 回答合成。",
    summary: "在固定问题集上的演示评分提高；需要继续验证长问题。",
    origin: "demo",
  });
  decide(s, a.id, "keep", "演示：必要条件通过，保留为当前方案。");
  importEvaluation(s, b.id, {
    values: { quality: 86, citations: "fail" },
    artifact: "演示产物 v2b\n上下文缩短，但部分引用缺少支撑。",
    summary: "评分更高，却丢失可追溯引用，不能采纳。",
    origin: "demo",
  });
  decide(s, b.id, "reject", "演示：引用条件失败，保留失败原因。");
  const c = fork(s, a.id, {
    title: "增加反例问题",
    plan: "沿用分解检索方案，加入容易误答的反例，检验结论的适用范围。",
  });
  const d = fork(s, a.id, {
    title: "先给出证据，再组织回答",
    plan: "先列出能支撑结论的材料，再合成回答，观察是否减少无依据陈述。",
    role: "championB",
  });
  debateStep(s, c.id, "critique");
  debateStep(s, c.id, "rebuttal");
  debateStep(s, c.id, "select");
  addSource(
    s,
    {
      title: "AMMO · 分离提案、质疑与验收",
      url: "https://amazon-science.github.io/ammo/AMMO_arXiv_Paper.pdf",
      note: "参考对抗式提案与独立验收的组织方式；具体效果需要在本任务验证。",
      version: "论文快照 · 2026-06",
    },
    c.id,
  );
  saveMemory(s, b.id, {
    title: "评分提高，不代表必要条件通过",
    claim: "压缩上下文可能使引用失去支撑；保留逐项检查。",
    conditions: "当前问题集、知识库与引用评价方法。",
  });
  const code = createLoop(s, TEMPLATES.code);
  code.demo = true;
  const cr = nodeOf(s);
  importEvaluation(s, cr.id, {
    values: { latency: 120, correct: "pass" },
    artifact: "演示：流水线计时记录",
    summary: "固定输入下的基线。",
    origin: "demo",
  });
  cr.status = "kept";
  const cn = fork(s, cr.id, {
    title: "减少重复读取",
    plan: "缓存重复读取的中间数据，保持输出一致。",
  });
  s.loopId = l.id;
  s.nodeId = c.id;
  s.view = "evolution";
  return s;
}
