// Local research records. No model calls, shell commands, or remote dispatch.
export const STORAGE_KEY = "euboulia.local-research.v1";
export const VERSION = 1;
const copy = (value) => structuredClone(value);
const nowISO = (now = Date.now()) => new Date(now).toISOString();
const id = (s, kind) => `${kind}-${s.next++}`;
const need = (value, label, max = 4000) => {
  if (typeof value !== "string" || !value.trim() || value.length > max)
    throw Error(`请填写${label}（最多 ${max} 字）。`);
  return value.trim();
};
export const branchOf = (s, branchId = s.branch) =>
  s.branches.find((b) => b.id === branchId);
export const studyOf = (s, studyId = s.study) =>
  s.studies.find((r) => r.id === studyId);
export const runsOf = (s, branchId) =>
  s.runs.filter((r) => r.branchId === branchId);
export const latestRun = (s, branchId) =>
  runsOf(s, branchId)
    .filter((r) => r.status === "done")
    .at(-1);
const nextRunLabel = (s, studyId) =>
  `R${String(s.runs.filter((r) => branchOf(s, r.branchId)?.studyId === studyId).length + 1).padStart(2, "0")}`;
export const protocolKey = (p) =>
  JSON.stringify([p.dataset, p.environment, p.evaluation, p.metric, p.guard]);
export const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export function safeURL(value) {
  try {
    const u = new URL(value);
    return ["https:", "http:"].includes(u.protocol) ? u.href : "";
  } catch {
    return "";
  }
}
export function validateProtocol(p) {
  if (!p || typeof p !== "object") throw Error("缺少评测条件。");
  for (const key of ["dataset", "environment", "evaluation"])
    need(p[key], "评测条件", 2000);
  need(p.metric?.name, "主指标", 80);
  if (
    typeof p.metric.unit !== "string" ||
    p.metric.unit.length > 40 ||
    !["higher", "lower"].includes(p.metric.direction)
  )
    throw Error("主指标格式不正确。");
  if (p.guard !== null) {
    need(p.guard?.name, "约束指标", 80);
    if (
      typeof p.guard.unit !== "string" ||
      p.guard.unit.length > 40 ||
      !Number.isFinite(p.guard.limit)
    )
      throw Error("约束指标格式不正确。");
  }
  return copy(p);
}
function message(s, b, text, author = "system", refs = [], now) {
  const m = {
    id: id(s, "message"),
    branchId: b.id,
    text,
    author,
    refs: copy(refs),
    createdAt: nowISO(now),
  };
  s.messages.push(m);
  return m;
}
function newBranch(s, studyId, parentId, title, hypothesis, snapshot, now) {
  const b = {
    id: id(s, "branch"),
    studyId,
    parentId,
    title,
    hypothesis,
    status: "active",
    snapshot: copy(snapshot),
    sourceIds: [],
    memoryIds: [],
    exploration: null,
    conclusion: "",
    history: [],
    createdAt: nowISO(now),
  };
  s.branches.push(b);
  return b;
}
export function createEmpty() {
  return {
    version: VERSION,
    next: 1,
    page: "home",
    study: null,
    branch: null,
    tab: "research",
    studies: [],
    branches: [],
    runs: [],
    messages: [],
    sources: [],
    memories: [],
    drafts: {},
    collapsed: [],
    homeDraft: "",
  };
}
export function createStudy(s, { direction, resources = "", protocol }, now) {
  const title = need(direction, "研究方向", 2000);
  const conditions = validateProtocol(protocol);
  if (typeof resources !== "string" || resources.length > 8000)
    throw Error("资料最多 8000 字。");
  const study = {
    id: id(s, "study"),
    title,
    resources,
    demo: false,
    createdAt: nowISO(now),
  };
  s.studies.push(study);
  const b = newBranch(
    s,
    study.id,
    null,
    "建立基线",
    "先固定评测条件，记录当前表现，作为后续方案的参照。",
    {
      protocol: conditions,
      codeRef: "待记录",
      referenceRunId: null,
      inheritedMemoryIds: [],
    },
    now,
  );
  message(
    s,
    b,
    "研究方向已记录。先补齐评测条件、建立基线，再从结果创建方案分支。这里是本地交互原型，尚未调用模型或启动真实实验。",
    "system",
    [],
    now,
  );
  s.study = study.id;
  s.branch = b.id;
  s.page = "study";
  s.tab = "research";
  s.homeDraft = "";
  return study;
}
export function forkBranch(
  s,
  parentId,
  { title, hypothesis, runId = null, sourceIds = [], memoryIds = [] },
  now,
) {
  const parent = branchOf(s, parentId);
  if (!parent) throw Error("研究起点不存在。");
  const name = need(title, "方案名称", 160),
    reason = need(hypothesis, "要验证的想法");
  const run = runId
    ? s.runs.find(
        (r) => r.id === runId && r.branchId === parentId && r.status === "done",
      )
    : null;
  if (runId && !run) throw Error("请选择当前分支中已完成的实验。");
  if (
    sourceIds.some(
      (sid) =>
        !s.sources.some((r) => r.id === sid && r.studyId === parent.studyId),
    )
  )
    throw Error("参考资料不属于这项研究。");
  if (memoryIds.some((mid) => !s.memories.some((m) => m.id === mid)))
    throw Error("经验记录不存在。");
  const snapshot = run
    ? {
        protocol: run.snapshot.protocol,
        codeRef: run.snapshot.codeRef,
        referenceRunId: run.id,
        inheritedMemoryIds: [...parent.memoryIds],
      }
    : copy(parent.snapshot);
  snapshot.inheritedMemoryIds = [
    ...new Set([
      ...snapshot.inheritedMemoryIds,
      ...parent.memoryIds,
      ...memoryIds,
    ]),
  ];
  const b = newBranch(s, parent.studyId, parentId, name, reason, snapshot, now);
  b.sourceIds = [...new Set([...parent.sourceIds, ...sourceIds])];
  b.memoryIds = [...snapshot.inheritedMemoryIds];
  message(
    s,
    b,
    `从「${parent.title}」${run ? `的实验 ${run.label} ` : "的当前条件"}分叉。起点快照已保留。`,
    "system",
    run ? [{ kind: "run", id: run.id }] : [],
    now,
  );
  s.branch = b.id;
  s.study = b.studyId;
  s.page = "study";
  s.tab = "research";
  return b;
}
export function addMessage(s, branchId, text, refs = [], now) {
  const b = branchOf(s, branchId);
  if (!b) throw Error("分支不存在。");
  if (b.status === "closed") throw Error("请先重新开放这个分支。");
  const value = need(text, "讨论内容");
  const validRefs = refs.filter(
    (r) =>
      (r.kind === "run" &&
        s.runs.some(
          (x) => x.id === r.id && branchOf(s, x.branchId).studyId === b.studyId,
        )) ||
      (r.kind === "source" &&
        s.sources.some((x) => x.id === r.id && x.studyId === b.studyId)) ||
      (r.kind === "memory" && s.memories.some((x) => x.id === r.id)),
  );
  const m = message(s, b, value, "you", validRefs, now);
  delete s.drafts[b.id];
  return m;
}
export function reviseProtocol(s, branchId, protocol, codeRef, now) {
  const b = branchOf(s, branchId),
    p = validateProtocol(protocol);
  if (!b || b.status !== "active") throw Error("请先恢复这个分支。");
  if (
    runsOf(s, branchId).some((r) =>
      ["running", "queued", "paused"].includes(r.status),
    )
  )
    throw Error("请先结束当前验证，再修改评测条件。");
  const code = need(codeRef, "代码版本", 500);
  b.history.push({
    type: "conditions",
    snapshot: copy(b.snapshot),
    at: nowISO(now),
  });
  b.snapshot = { ...copy(b.snapshot), protocol: p, codeRef: code };
  message(
    s,
    b,
    "后续实验将使用新的评测条件；已有结果与子分支保留原快照。",
    "system",
    [],
    now,
  );
}
function validateResult(data, protocol) {
  if (
    !data ||
    typeof data !== "object" ||
    !Number.isFinite(data.metric) ||
    (protocol.guard && !Number.isFinite(data.guard))
  )
    throw Error("请填写有效的测量数值。");
  need(data.summary, "结果说明");
  need(data.artifact, "原始记录路径或链接", 2000);
  need(data.codeRef, "代码版本", 500);
  return {
    metric: data.metric,
    guard: protocol.guard ? data.guard : null,
    summary: data.summary.trim(),
    artifact: data.artifact.trim(),
  };
}
export function readResultJSON(text, protocol) {
  if (typeof text !== "string" || text.length > 262144)
    throw Error("实验 JSON 不能超过 256 KB。");
  const data = JSON.parse(text);
  if (
    data?.protocol &&
    protocolKey(validateProtocol(data.protocol)) !== protocolKey(protocol)
  )
    throw Error("文件中的评测条件与当前分支不同，请先更新分支条件再导入。");
  validateResult(data, protocol);
  return data;
}
export function importResult(s, branchId, data, now) {
  const b = branchOf(s, branchId);
  if (!b || b.status !== "active") throw Error("请先恢复这个分支。");
  const result = validateResult(data, b.snapshot.protocol);
  const rid = id(s, "run");
  const run = {
    id: rid,
    branchId,
    label: nextRunLabel(s, b.studyId),
    status: "done",
    origin: "imported",
    snapshot: { ...copy(b.snapshot), codeRef: data.codeRef.trim() },
    result,
    createdAt: nowISO(now),
    endedAt: nowISO(now),
    phase: 3,
    logs: ["用户录入结果；原始记录尚未由本产品读取或核验。"],
  };
  s.runs.push(run);
  message(
    s,
    b,
    "实验结果已记录。可以与相同条件下的记录对照，或从这一步创建新方案。",
    "system",
    [{ kind: "run", id: rid }],
    now,
  );
  return run;
}
export function comparable(left, right) {
  if (!left || !right)
    return { ok: false, reason: "选择两份已完成的实验记录。" };
  if (left.id === right.id)
    return { ok: false, reason: "请选择两份不同的实验记录。" };
  if (left.origin !== right.origin)
    return { ok: false, reason: "演示数据与用户录入的结果不能计算改进幅度。" };
  if (
    [left, right].some((r) =>
      ["dataset", "environment", "evaluation"].some((key) =>
        r.snapshot.protocol[key].startsWith("待确认"),
      ),
    )
  )
    return {
      ok: false,
      reason: "评测条件尚未补齐，请先确认数据、环境和评测方法。",
    };
  if (
    protocolKey(left.snapshot.protocol) !== protocolKey(right.snapshot.protocol)
  )
    return {
      ok: false,
      reason:
        "评测条件不同：请使用相同数据、环境、评测方法和指标重新建立对照。",
    };
  const delta = right.result.metric - left.result.metric;
  return {
    ok: true,
    delta,
    percent:
      left.result.metric === 0
        ? null
        : (delta / Math.abs(left.result.metric)) * 100,
    improved:
      right.snapshot.protocol.metric.direction === "higher"
        ? delta > 0
        : delta < 0,
  };
}
export function addSource(
  s,
  branchId,
  { title, url, published, relevance, conditions },
  now,
) {
  const b = branchOf(s, branchId);
  if (!b) throw Error("分支不存在。");
  const source = {
    id: id(s, "source"),
    studyId: b.studyId,
    branchId,
    title: need(title, "资料标题", 200),
    url: safeURL(url),
    published: need(published, "发表日期或版本", 100),
    relevance: need(relevance, "与当前问题的关系"),
    conditions: need(conditions, "适用条件", 2000),
    capturedAt: nowISO(now),
    status: "unverified",
  };
  if (!source.url) throw Error("请输入 http 或 https 资料链接。");
  s.sources.push(source);
  b.sourceIds.push(source.id);
  return source;
}
export function saveMemory(
  s,
  branchId,
  { title, claim, conditions, runIds },
  now,
) {
  const b = branchOf(s, branchId);
  if (!b) throw Error("分支不存在。");
  const selected = [...new Set(runIds || [])].map((rid) =>
    s.runs.find(
      (r) =>
        r.id === rid &&
        r.status === "done" &&
        branchOf(s, r.branchId).studyId === b.studyId,
    ),
  );
  if (!selected.length || selected.some((r) => !r))
    throw Error("至少选择一份本研究的实验记录作为证据。");
  const m = {
    id: id(s, "memory"),
    branchId,
    studyId: b.studyId,
    title: need(title, "经验标题", 160),
    claim: need(claim, "结论"),
    conditions: need(conditions, "适用条件", 2000),
    runIds: selected.map((r) => r.id),
    protocol: copy(b.snapshot.protocol),
    status: "candidate",
    origin: selected.every((r) => r.origin === "demo")
      ? "demo"
      : selected.every((r) => r.origin === "imported")
        ? "imported"
        : "mixed",
    history: [],
    createdAt: nowISO(now),
  };
  s.memories.push(m);
  return m;
}
export function reviseMemory(s, memoryId, status, reason, now) {
  const m = s.memories.find((x) => x.id === memoryId);
  if (!m || !["candidate", "supported", "contested"].includes(status))
    throw Error("经验状态不正确。");
  const why = need(reason, "判定依据");
  if (status === "supported") {
    const runs = m.runIds.map((rid) => s.runs.find((r) => r.id === rid));
    if (
      runs.length < 2 ||
      m.origin === "mixed" ||
      runs.some(
        (r) => protocolKey(r.snapshot.protocol) !== protocolKey(m.protocol),
      )
    )
      throw Error("标记复验至少需要两份同条件、同来源类型的实验记录。");
  }
  m.history.push({ from: m.status, to: status, reason: why, at: nowISO(now) });
  m.status = status;
}
export function memoryFit(memory, branch) {
  return protocolKey(memory.protocol) === protocolKey(branch.snapshot.protocol)
    ? "条件一致 · 仍需判断适用性"
    : "条件已变化 · 使用前需复验";
}
export function useMemory(s, branchId, memoryId, now) {
  const b = branchOf(s, branchId),
    m = s.memories.find((x) => x.id === memoryId);
  if (!b || !m) throw Error("记录不存在。");
  if (!b.memoryIds.includes(memoryId)) {
    b.memoryIds.push(memoryId);
    message(
      s,
      b,
      `参考经验「${m.title}」。${memoryFit(m, b)}。`,
      "system",
      [{ kind: "memory", id: memoryId }],
      now,
    );
  }
}
function enqueue(s, b, now) {
  const e = b.exploration;
  const run = {
    id: id(s, "run"),
    branchId: b.id,
    label: nextRunLabel(s, b.studyId),
    status: "queued",
    origin: "demo",
    snapshot: copy(b.snapshot),
    result: null,
    createdAt: nowISO(now),
    phase: 0,
    logs: ["已加入本地演示队列；没有启动进程或调用模型。"],
    explorationId: e.id,
    ordinal: e.used + 1,
  };
  s.runs.push(run);
  return run;
}
export function startExploration(
  s,
  branchId,
  { maxRuns = 2, minutes = 5, instruction = "先验证一个最小改动" },
  now = Date.now(),
) {
  const b = branchOf(s, branchId);
  if (!b || b.status !== "active") throw Error("请先恢复这个分支。");
  if (
    !Number.isInteger(maxRuns) ||
    maxRuns < 1 ||
    maxRuns > 8 ||
    !Number.isFinite(minutes) ||
    minutes < 1 ||
    minutes > 60
  )
    throw Error("演示预算为 1–8 轮、1–60 分钟。");
  if (
    runsOf(s, branchId).some((r) =>
      ["running", "queued", "paused"].includes(r.status),
    )
  )
    throw Error("这个分支已有验证，请先继续或结束它。");
  b.exploration = {
    id: id(s, "exploration"),
    state: "running",
    maxRuns,
    minutes,
    used: 0,
    deadline: now + minutes * 60000,
    instruction: need(instruction, "探索方向"),
    reason: "",
  };
  message(
    s,
    b,
    `开始演示验证：最多 ${maxRuns} 轮 / ${minutes} 分钟。${instruction}`,
    "system",
    [],
    now,
  );
  return enqueue(s, b, now);
}
export function pauseBranch(s, branchId, now = Date.now()) {
  const b = branchOf(s, branchId);
  if (!b || b.status !== "active") throw Error("只有活跃分支可以暂停。");
  b.status = "paused";
  if (b.exploration?.state === "running") {
    b.exploration.state = "paused";
    b.exploration.remaining = Math.max(0, b.exploration.deadline - now);
  }
  runsOf(s, branchId)
    .filter((r) => ["running", "queued"].includes(r.status))
    .forEach((r) => {
      r.status = "paused";
      r.logs.push("用户暂停了这条分支。");
    });
}
export function resumeBranch(s, branchId, now = Date.now()) {
  const b = branchOf(s, branchId);
  if (!b || !["paused", "closed"].includes(b.status))
    throw Error("这个分支不需要恢复。");
  b.history.push({ type: "resume", conclusion: b.conclusion, at: nowISO(now) });
  b.status = "active";
  if (
    b.exploration?.state === "paused" &&
    runsOf(s, branchId).some((r) => r.status === "paused")
  ) {
    b.exploration.state = "running";
    b.exploration.deadline =
      now + (b.exploration.remaining ?? b.exploration.minutes * 60000);
    runsOf(s, branchId)
      .filter((r) => r.status === "paused")
      .forEach((r) => {
        r.status = "queued";
        r.phase = 0;
        r.logs.push("恢复后重新进入演示队列。");
      });
  }
}
export function stopExploration(s, branchId, reason = "用户结束本轮验证", now) {
  const b = branchOf(s, branchId);
  if (!b) throw Error("分支不存在。");
  if (b.exploration) {
    b.exploration.state = "stopped";
    b.exploration.reason = reason;
  }
  runsOf(s, branchId)
    .filter((r) => ["running", "queued", "paused"].includes(r.status))
    .forEach((r) => {
      r.status = "cancelled";
      r.endedAt = nowISO(now);
      r.logs.push(reason);
    });
}
export function closeBranch(s, branchId, conclusion, now) {
  const b = branchOf(s, branchId),
    value = need(conclusion, "本阶段结论");
  if (!b) throw Error("分支不存在。");
  stopExploration(s, branchId, "分支收束，剩余演示验证已取消。", now);
  b.status = "closed";
  b.conclusion = value;
  b.history.push({ type: "conclusion", text: value, at: nowISO(now) });
  message(s, b, value, "conclusion", [], now);
}
function fixtureResult(s, run) {
  const p = run.snapshot.protocol;
  const ref = s.runs.find(
    (r) => r.id === run.snapshot.referenceRunId && r.origin === "demo",
  );
  const base =
    ref?.result.metric ??
    (studyOf(s, branchOf(s, run.branchId).studyId).demo ? 1000 : 100);
  const metric = Number(
    (
      base *
      (p.metric.direction === "higher"
        ? 1 + 0.06 * run.ordinal
        : 1 - 0.04 * run.ordinal)
    ).toFixed(2),
  );
  const guard = p.guard
    ? Number(
        (
          p.guard.limit -
          Math.max(1, Math.abs(p.guard.limit) * 0.1) +
          (run.ordinal > 1 ? Math.max(2, Math.abs(p.guard.limit) * 0.2) : 0)
        ).toFixed(2),
      )
    : null;
  return {
    metric,
    guard,
    summary:
      run.ordinal > 1 && p.guard
        ? "示例结果：主指标改善，但约束指标超出约定范围。需要讨论取舍或继续拆分方案。"
        : "示例结果：这次改动显示出改善线索，仍需重复测量和检查适用范围。",
    artifact: "内置演示数据 · 无真实原始文件",
  };
}
// One simulated machine, one running record. Ticks never invoke external work.
export function tick(s, now = Date.now()) {
  let changed = false;
  for (const b of s.branches) {
    if (b.exploration?.state === "running" && now >= b.exploration.deadline) {
      stopExploration(s, b.id, "达到时间预算，等待讨论。", now);
      changed = true;
    }
  }
  const running = s.runs.find((r) => r.status === "running");
  if (running && now >= running.nextPhaseAt) {
    const b = branchOf(s, running.branchId),
      e = b.exploration;
    running.phase++;
    running.logs.push(
      [
        "",
        "演示：读取固定的代码与评测条件快照。",
        "演示：生成带来源标记的示例指标。",
        "演示：对照约束并整理下一步。",
      ][running.phase],
    );
    running.nextPhaseAt = now + 1600;
    if (running.phase === 3) {
      running.status = "done";
      running.endedAt = nowISO(now);
      running.result = fixtureResult(s, running);
      e.used++;
      const exceeded =
        running.snapshot.protocol.guard &&
        running.result.guard > running.snapshot.protocol.guard.limit;
      if (exceeded || e.used >= e.maxRuns) {
        e.state = "review";
        e.reason = exceeded
          ? "约束指标超出范围，回来讨论下一步。"
          : "本轮预算已完成，回来讨论结果。";
      } else enqueue(s, b, now);
      message(
        s,
        b,
        running.result.summary,
        "demo-agent",
        [{ kind: "run", id: running.id }],
        now,
      );
    }
    changed = true;
  }
  if (!s.runs.some((r) => r.status === "running")) {
    const queued = s.runs.find(
      (r) =>
        r.status === "queued" &&
        branchOf(s, r.branchId)?.status === "active" &&
        branchOf(s, r.branchId).exploration?.state === "running",
    );
    if (queued) {
      queued.status = "running";
      queued.nextPhaseAt = now + 1600;
      queued.logs.push("获得演示执行位；其他分支排队等待。");
      changed = true;
    }
  }
  return changed;
}
export function recover(s) {
  for (const b of s.branches) {
    if (b.exploration?.state === "running") {
      b.exploration.state = "stopped";
      b.exploration.reason = "页面已重新打开，演示验证未自动重放。";
    }
  }
  for (const r of s.runs) {
    if (["running", "queued"].includes(r.status)) {
      r.status = "interrupted";
      r.logs.push("页面关闭或刷新；保留记录，未自动重放。");
    }
  }
  return s;
}
export function validateState(value) {
  if (
    !value ||
    value.version !== VERSION ||
    !Number.isSafeInteger(value.next) ||
    value.next < 1
  )
    throw Error("不是有效的研究工作台备份。");
  for (const key of [
    "studies",
    "branches",
    "runs",
    "messages",
    "sources",
    "memories",
  ]) {
    if (!Array.isArray(value[key]) || value[key].length > 10000)
      throw Error("研究记录结构不正确。");
    const ids = value[key].map((x) => x?.id);
    if (
      ids.some((x) => typeof x !== "string" || !/^[a-z]+-[0-9]+$/.test(x)) ||
      new Set(ids).size !== ids.length
    )
      throw Error("记录标识不正确。");
  }
  const all = [
    "studies",
    "branches",
    "runs",
    "messages",
    "sources",
    "memories",
  ].flatMap((key) => value[key]);
  if (all.some((x) => Number(x.id.split("-").at(-1)) >= value.next))
    throw Error("记录序号不正确。");
  for (const study of value.studies) {
    need(study.title, "研究方向", 2000);
    if (
      typeof study.demo !== "boolean" ||
      typeof study.resources !== "string" ||
      value.branches.filter((b) => b.studyId === study.id && !b.parentId)
        .length !== 1
    )
      throw Error("研究资料或基线结构错误。");
  }
  for (const b of value.branches) {
    need(b.title, "方案名称", 160);
    need(b.hypothesis, "研究假设");
    if (
      !studyOf(value, b.studyId) ||
      !["active", "paused", "closed"].includes(b.status) ||
      typeof b.conclusion !== "string" ||
      !Array.isArray(b.history) ||
      !Array.isArray(b.sourceIds) ||
      !Array.isArray(b.memoryIds) ||
      !Array.isArray(b.snapshot?.inheritedMemoryIds) ||
      typeof b.snapshot.codeRef !== "string"
    )
      throw Error("分支结构不正确。");
    validateProtocol(b.snapshot.protocol);
    if (
      b.history.some(
        (h) =>
          !h ||
          !["conditions", "resume", "conclusion"].includes(h.type) ||
          typeof h.at !== "string",
      )
    )
      throw Error("分支历史不正确。");
    let cursor = b,
      seen = new Set([b.id]);
    while (cursor.parentId) {
      cursor = branchOf(value, cursor.parentId);
      if (!cursor || cursor.studyId !== b.studyId || seen.has(cursor.id))
        throw Error("分支关系包含循环或无效引用。");
      seen.add(cursor.id);
    }
    if (
      b.exploration &&
      (!Number.isInteger(b.exploration.used) ||
        !Number.isInteger(b.exploration.maxRuns) ||
        !["running", "paused", "stopped", "review"].includes(
          b.exploration.state,
        ))
    )
      throw Error("验证预算不正确。");
  }
  for (const r of value.runs) {
    if (
      !branchOf(value, r.branchId) ||
      !["demo", "imported"].includes(r.origin) ||
      ![
        "done",
        "running",
        "queued",
        "paused",
        "interrupted",
        "cancelled",
      ].includes(r.status) ||
      !Array.isArray(r.logs)
    )
      throw Error("实验记录不正确。");
    validateProtocol(r.snapshot?.protocol);
    need(r.label, "实验名称", 100);
    if (r.logs.some((line) => typeof line !== "string"))
      throw Error("实验日志不正确。");
    if (r.status === "done")
      validateResult(
        { ...r.result, codeRef: r.snapshot.codeRef },
        r.snapshot.protocol,
      );
  }
  for (const b of value.branches) {
    if (
      b.snapshot.referenceRunId &&
      !value.runs.some(
        (r) =>
          r.id === b.snapshot.referenceRunId &&
          r.status === "done" &&
          branchOf(value, r.branchId).studyId === b.studyId,
      )
    )
      throw Error("对照实验引用无效。");
  }
  const validRef = (r) =>
    r &&
    typeof r.id === "string" &&
    ["run", "source", "memory"].includes(r.kind);
  for (const m of value.messages) {
    need(m.text, "讨论内容", 10000);
    if (
      !branchOf(value, m.branchId) ||
      !Array.isArray(m.refs) ||
      m.refs.some((r) => !validRef(r)) ||
      !["you", "system", "demo-agent", "conclusion"].includes(m.author)
    )
      throw Error("讨论记录不正确。");
  }
  for (const r of value.sources) {
    if (
      branchOf(value, r.branchId)?.studyId !== r.studyId ||
      !studyOf(value, r.studyId) ||
      !safeURL(r.url)
    )
      throw Error("资料引用不正确。");
    need(r.title, "资料标题", 200);
    need(r.relevance, "资料说明");
    need(r.conditions, "适用条件", 2000);
    need(r.published, "发表日期或版本", 100);
  }
  for (const m of value.memories) {
    need(m.title, "经验标题", 160);
    need(m.claim, "结论");
    need(m.conditions, "适用条件", 2000);
    validateProtocol(m.protocol);
    if (
      branchOf(value, m.branchId)?.studyId !== m.studyId ||
      !["demo", "imported", "mixed"].includes(m.origin) ||
      !Array.isArray(m.history) ||
      !Array.isArray(m.runIds) ||
      !m.runIds.length ||
      m.runIds.some(
        (rid) =>
          !value.runs.some(
            (r) =>
              r.id === rid &&
              r.status === "done" &&
              branchOf(value, r.branchId).studyId === m.studyId,
          ),
      ) ||
      !["candidate", "supported", "contested"].includes(m.status)
    )
      throw Error("经验证据不正确。");
    if (
      m.history.some(
        (h) =>
          !h ||
          !["candidate", "supported", "contested"].includes(h.from) ||
          !["candidate", "supported", "contested"].includes(h.to) ||
          typeof h.reason !== "string" ||
          typeof h.at !== "string",
      )
    )
      throw Error("经验判定历史不正确。");
  }
  if (
    typeof value.drafts !== "object" ||
    !value.drafts ||
    Array.isArray(value.drafts) ||
    !Array.isArray(value.collapsed) ||
    typeof value.homeDraft !== "string"
  )
    throw Error("草稿格式不正确。");
  for (const [key, draft] of Object.entries(value.drafts)) {
    if (!draft || typeof draft !== "object" || Array.isArray(draft))
      throw Error("草稿格式不正确。");
    if (key.startsWith("form:")) {
      if (
        Object.values(draft).some(
          (v) => !Array.isArray(v) || v.some((x) => typeof x !== "string"),
        )
      )
        throw Error("表单草稿不正确。");
    } else if (
      !branchOf(value, key) ||
      typeof draft.text !== "string" ||
      !Array.isArray(draft.refs) ||
      draft.refs.some((r) => !validRef(r))
    )
      throw Error("讨论草稿不正确。");
  }
  for (const b of value.branches)
    if (
      b.sourceIds.some(
        (sid) =>
          !value.sources.some((r) => r.id === sid && r.studyId === b.studyId),
      ) ||
      [...b.memoryIds, ...b.snapshot.inheritedMemoryIds].some(
        (mid) => !value.memories.some((m) => m.id === mid),
      )
    )
      throw Error("分支资料或经验引用无效。");
  if (
    !branchOf(value, value.branch) ||
    !studyOf(value, value.study) ||
    branchOf(value, value.branch).studyId !== value.study
  ) {
    value = copy(value);
    value.study = value.studies[0]?.id || null;
    value.branch =
      value.branches.find((b) => b.studyId === value.study && !b.parentId)
        ?.id || null;
    value.page = "home";
  }
  if (
    !["home", "study", "memories"].includes(value.page) ||
    !["research", "runs", "compare", "sources", "context"].includes(value.tab)
  )
    throw Error("视图记录不正确。");
  return copy(value);
}
export function decodeState(text) {
  if (typeof text !== "string" || text.length > 5000000)
    throw Error("备份需要是 5 MB 以内的 JSON 文件。");
  return recover(validateState(JSON.parse(text)));
}
export function createDemo() {
  const s = createEmpty(),
    date = Date.parse("2026-09-07T02:00:00Z");
  const protocol = {
    dataset: "固定请求集 v1 · 短 / 中 / 长输入各 100 条",
    environment: "本机演示环境 · 同一模型与资源配置",
    evaluation: "预热后测量 3 次，固定并发 8；吞吐与 P95 同时记录",
    metric: { name: "吞吐", unit: "tok/s", direction: "higher" },
    guard: { name: "P95 延迟", unit: "ms", limit: 55 },
  };
  const study = createStudy(
    s,
    {
      direction: "提高推理吞吐，守住长请求延迟",
      resources:
        "示例源码 /research/inference-engine\n示例数据 /research/benchmarks",
      protocol,
    },
    date,
  );
  study.demo = true;
  const base = branchOf(s);
  base.snapshot.codeRef = "demo:baseline-v1";
  const r0 = importResult(
    s,
    base.id,
    {
      metric: 1000,
      guard: 40,
      summary:
        "示例基线：吞吐 1000 tok/s，P95 40 ms，作为所有同条件实验的起点。",
      artifact: "内置演示数据 · baseline",
      codeRef: "demo:baseline-v1",
    },
    date,
  );
  r0.origin = "demo";
  r0.label = "R01";
  const a = forkBranch(
    s,
    base.id,
    {
      title: "调整批处理策略",
      hypothesis: "提高批处理利用率可能增加吞吐，但需要观察长请求的等待时间。",
      runId: r0.id,
    },
    date + 1000,
  );
  const r1 = importResult(
    s,
    a.id,
    {
      metric: 1240,
      guard: 68,
      summary:
        "示例结果：吞吐提高 24%，P95 却超出 55 ms 的约定。需要拆开验证批大小与调度策略。",
      artifact: "内置演示数据 · batching",
      codeRef: "demo:batch-v1",
    },
    date + 2000,
  );
  r1.origin = "demo";
  r1.label = "R02";
  message(
    s,
    a,
    "先别把批大小和调度一起改。保留这个结果，分别验证固定批大小和自适应批处理。",
    "you",
    [{ kind: "run", id: r1.id }],
    date + 3000,
  );
  const a1 = forkBranch(
    s,
    a.id,
    {
      title: "A1 · 固定批大小",
      hypothesis: "只调整批大小，检查吞吐提升是否仍能满足延迟约束。",
      runId: r1.id,
    },
    date + 4000,
  );
  const r2 = importResult(
    s,
    a1.id,
    {
      metric: 1180,
      guard: 48,
      summary: "示例结果：相对根基线吞吐增加 18%，P95 回到约定范围。",
      artifact: "内置演示数据 · fixed-batch",
      codeRef: "demo:fixed-v1",
    },
    date + 5000,
  );
  r2.origin = "demo";
  r2.label = "R03";
  const r3 = importResult(
    s,
    a1.id,
    {
      metric: 1176,
      guard: 49,
      summary: "示例重复测量：结果接近上一轮，尚不说明其他请求分布也有效。",
      artifact: "内置演示数据 · fixed-batch-repeat",
      codeRef: "demo:fixed-v1",
    },
    date + 6000,
  );
  r3.origin = "demo";
  r3.label = "R04";
  const mem = saveMemory(
    s,
    a1.id,
    {
      title: "批处理优化需要同时看尾延迟",
      claim:
        "这组示例中，更大的批处理吞吐更高，但长请求等待也更明显。固定批大小提供了较平衡的候选。",
      conditions:
        "仅适用于当前固定请求集、并发 8 和相同模型配置。换并发或请求分布需要复验。",
      runIds: [r0.id, r1.id, r2.id, r3.id],
    },
    date + 7000,
  );
  reviseMemory(
    s,
    mem.id,
    "supported",
    "示例判定：两次固定批大小测量方向一致；保留基线与退化结果供回看。",
    date + 7000,
  );
  const a2 = forkBranch(
    s,
    a.id,
    {
      title: "A2 · 自适应批处理",
      hypothesis: "根据请求长度调整批处理，验证是否能兼顾吞吐与尾延迟。",
      runId: r1.id,
      memoryIds: [mem.id],
    },
    date + 8000,
  );
  addSource(
    s,
    a2.id,
    {
      title: "Weco：从已有结果继续分支探索",
      url: "https://docs.weco.ai/using-weco/steerability",
      published: "查阅于 2026-09-07",
      relevance:
        "借鉴从指定实验起点创建子分支、单独设置探索预算的研究组织方式。它不是批处理优化的性能证据。",
      conditions: "适用于组织实验过程；具体优化假设仍需本地验证。",
    },
    date + 8000,
  );
  const other = forkBranch(
    s,
    base.id,
    {
      title: "B · 检查算子开销",
      hypothesis: "先通过 profile 判断热点，避免在缺少证据时修改算子。",
      runId: r0.id,
    },
    date + 9000,
  );
  pauseBranch(s, other.id, date + 9000);
  s.study = study.id;
  s.branch = a.id;
  s.page = "home";
  s.messages = s.messages.filter((m) => !m.text.startsWith("实验结果已记录"));
  return s;
}
