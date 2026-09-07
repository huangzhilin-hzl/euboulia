// Local interaction fixtures. This module never runs agents or submits GPU work.
export const STORAGE_KEY = "euboulia.research-room.v3";
export const LEGACY_STORAGE_KEY = "euboulia.research-room.v2";
export const SEED_GOAL = "g-decode";
export const MEMBERS = [
  {
    id: "julian",
    name: "Julian",
    initials: "J",
    role: "研究方向 · 人类",
    computer: "Local Mac",
    status: "在线",
    color: "sand",
  },
  {
    id: "atlas",
    name: "Atlas",
    initials: "A",
    role: "推理系统 · Agent",
    computer: "Local Mac",
    status: "可接力",
    color: "green",
  },
  {
    id: "prism",
    name: "Prism",
    initials: "P",
    role: "性能分析 · Agent",
    computer: "Lab connector",
    status: "分析中",
    color: "purple",
  },
  {
    id: "lin",
    name: "Lin",
    initials: "L",
    role: "Kernel 工程 · 人类",
    computer: "Lab connector",
    status: "离开",
    color: "blue",
  },
];
export const ARTIFACTS = {
  compare: {
    title: "MHC 候选与 B0 对比",
    kind: "实验对比",
    ref: "B0 ↔ MHC-01",
    summary: "TPOT 降低 7.7%，正确性证据仍待补齐。所有数值均为演示数据。",
  },
  trace: {
    title: "Decode 等待窗口",
    kind: "Profile",
    ref: "P0 · rank 0 · 12–16 ms",
    summary: "单 rank 的示意窗口只能定位等待，不能证明等待的跨 rank 原因。",
  },
  patch: {
    title: "减少 MHC 中间张量",
    kind: "改动提案",
    ref: "proposal/MHC-01",
    summary: "将相邻处理合并，减少中间张量读写。仍需正确性与独立端到端测量。",
  },
  memory: {
    title: "低并发收益不能外推",
    kind: "团队经验",
    ref: "DSV4 / H20 / C1",
    summary:
      "历史反例提示：C1 上有效的改动可能在 C16 回退。下一轮应同时覆盖两种并发。",
  },
};
export function createRoom() {
  return {
    schema: 3,
    page: "rooms",
    goal: null,
    rooms: [
      {
        id: "dsv4",
        name: "dsv4-on-h20",
        description: "一起研究 DSV4 的推理性能，让每次实验都有依据。",
        context:
          "DSV4 / H20 × 8 / SGLang；16K 输入 / 256 输出 / C1，待扩展 C16",
      },
      {
        id: "kernels",
        name: "kernel-playground",
        description: "从一个 kernel 问题开始，在小实验里探索。",
        context: "",
      },
    ],
    goals: [
      {
        id: SEED_GOAL,
        room: "dsv4",
        title: "把 DSV4 的解码再快一点",
        prompt: "复用已有基线和 profile，先解释 decode 等待，再验证优化。",
        criterion:
          "正确性通过，使用相同 workload 的无 profile A/B 判断收益，保留高并发反例。",
        budget: "2 GPU·h · 示例",
        owner: "atlas",
        context: "DSV4 / H20 × 8 / SGLang；16K / 256 · C1，待扩展 C16",
        status: "active",
        refs: ["trace", "compare", "memory"],
        createdAt: "2026-09-07T02:24:00.000Z",
      },
    ],
    forms: {},
    next: 20,
    room: "dsv4",
    view: "overview",
    gpuOnline: true,
    decision: null,
    filter: "all",
    savedMemory: false,
    drafts: {},
    tasks: [
      {
        id: "baseline",
        title: "建立可比基线",
        owner: "atlas",
        state: "done",
        room: "dsv4",
        goal: SEED_GOAL,
        gpu: true,
        description: "相同 workload；无 profiling 的测量通道。",
        refs: ["compare"],
      },
      {
        id: "profile",
        title: "解释 Decode 等待",
        owner: "prism",
        state: "running",
        room: "dsv4",
        goal: SEED_GOAL,
        description: "读取已有 trace，核对 rank 覆盖。",
        refs: ["trace"],
        gpu: false,
      },
      {
        id: "correctness",
        title: "补齐 MHC 正确性证据",
        owner: "atlas",
        state: "queued",
        room: "dsv4",
        goal: SEED_GOAL,
        description: "数值正确性尚未交付，候选不能晋升。",
        refs: ["patch", "compare"],
        gpu: true,
      },
    ],
    messages: [
      {
        id: "m1",
        author: "julian",
        room: "dsv4",
        goal: SEED_GOAL,
        time: "10:24",
        text: "先搞清楚 DSV4 在 H20 上为什么慢。复用已有基线和 profile，优先看 decode；有依据再改代码。",
        refs: [],
      },
      {
        id: "m2",
        author: "prism",
        room: "dsv4",
        goal: SEED_GOAL,
        time: "10:26",
        text: "我在 decode 窗口里看到一段等待。但目前只有 rank 0，不能判断是通信本身慢，还是其他 rank 的计算拖住了它。建议补采多 rank，再决定是否改通信 kernel。",
        refs: ["trace"],
        kind: "finding",
      },
      {
        id: "m3",
        author: "atlas",
        room: "dsv4",
        goal: SEED_GOAL,
        time: "10:28",
        text: "另一条 MHC 线索已经有初步对比。TPOT 从 41.8 ms 降到 38.6 ms；正确性尚未齐全，先保留为候选。",
        refs: ["compare", "patch"],
        kind: "result",
      },
      {
        id: "m4",
        author: "lin",
        room: "dsv4",
        goal: SEED_GOAL,
        time: "10:29",
        text: "别漏掉高并发。上次只看 C1 时有效，C16 却回退了；这次把反例带进验证。",
        refs: ["memory"],
      },
    ],
  };
}
const newId = (s, prefix) => `${prefix}${s.next++}`;
const timestamp = () =>
  new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
function note(
  s,
  text,
  refs = [],
  scope = { room: s.room, goal: currentGoal(s)?.id || null },
) {
  s.messages.push({
    id: newId(s, "m"),
    author: "system",
    ...scope,
    time: timestamp(),
    text,
    refs,
  });
}
export function sendMessage(
  s,
  { text, asTask = false, refs = [], owner = "atlas" },
) {
  const value = String(text).trim().slice(0, 4000);
  if (!value) return null;
  const goal = currentGoal(s);
  if (asTask && goal && goal.status !== "active")
    throw Error("请先恢复目标，再交办新的工作。");
  if (!MEMBERS.some((m) => m.id === owner))
    throw Error("请选择研究室中的负责人");
  const cleanRefs = [...new Set(refs)].filter((r) => ARTIFACTS[r]);
  const message = {
    id: newId(s, "m"),
    author: "julian",
    room: s.room,
    goal: goal?.id || null,
    time: timestamp(),
    text: value,
    refs: cleanRefs,
  };
  s.messages.push(message);
  if (asTask) {
    const task = {
      id: newId(s, "t"),
      title: value.slice(0, 64),
      description: value,
      owner,
      room: s.room,
      goal: goal?.id || null,
      goalRevision: goal ? goal.revision || 1 : null,
      state: "queued",
      refs: cleanRefs,
      gpu: false,
      source: message.id,
    };
    s.tasks.push(task);
    message.task = task.id;
    note(
      s,
      `任务已记入本地演示队列，负责人 ${MEMBERS.find((m) => m.id === owner)?.name || owner}。接入真实 runtime 后，才会实际派发。`,
    );
  } else
    note(
      s,
      "已保存到本地研究室。这是交互原型，未调用模型；可以引用证据、交办工作，或把讨论转为目标。",
    );
  return message;
}
export function chooseDirection(s, direction) {
  if (s.decision) return;
  if (s.goals.find((g) => g.id === SEED_GOAL)?.status !== "active")
    throw Error("请先恢复目标，再选择方向。");
  if (!["ranks", "mhc"].includes(direction)) throw Error("未知研究方向");
  s.decision = direction;
  const ranks = direction === "ranks";
  s.tasks.push({
    id: newId(s, "t"),
    title: ranks ? "补采多 rank，定位等待来源" : "验证 MHC 在 C1 / C16 的收益",
    owner: ranks ? "prism" : "atlas",
    room: "dsv4",
    goal: SEED_GOAL,
    goalRevision: s.goals.find((g) => g.id === SEED_GOAL)?.revision || 1,
    state: s.gpuOnline ? "queued" : "blocked",
    gpu: true,
    refs: ranks ? ["trace"] : ["compare", "memory"],
    description: ranks
      ? "沿用锁定 workload，在独占 H20 Pod 采样。先解释等待，再决定代码改动。"
      : "保留 B0，补齐正确性，分别测量 C1 与 C16；不使用 profile 数值作性能晋升依据。",
  });
  note(
    s,
    ranks
      ? "研究方向已记录：先补齐多 rank 证据。采样任务已进入演示队列，已有 MHC 候选保留。"
      : "研究方向已记录：验证 MHC 在 C1 / C16 的收益，并补齐正确性证据。",
    ranks ? ["trace"] : ["compare", "memory"],
    { room: "dsv4", goal: SEED_GOAL },
  );
}
export function updateTask(s, id, action, result = "") {
  const t = s.tasks.find((t) => t.id === id);
  if (!t) throw Error("任务不存在");
  const goal = s.goals.find((g) => g.id === t.goal);
  if (["start", "resume"].includes(action) && goal && goal.status !== "active")
    throw Error("请先恢复目标，再推进任务。");
  if (action === "complete" && t.state === "running") {
    if (!String(result).trim()) throw Error("请填写交付说明，保留判断依据。");
    t.state = "done";
    t.result = String(result).trim().slice(0, 4000);
    note(
      s,
      `本地交付记录「${t.title}」：${t.result}（人工记录，未由真实执行验证。）`,
      t.refs,
      { room: t.room, goal: t.goal || null },
    );
    return;
  }
  if (action === "cancel" && !["done", "cancelled"].includes(t.state)) {
    t.state = "cancelled";
    note(s, `已取消演示任务「${t.title}」，保留已有上下文。`, t.refs, {
      room: t.room,
      goal: t.goal || null,
    });
    return;
  }
  if (
    action === "pause" &&
    ["running", "queued", "blocked"].includes(t.state)
  ) {
    t.previous = t.state;
    t.state = "paused";
  } else if (action === "resume" && t.state === "paused") {
    // A paused measurement must re-enter the queue and acquire exclusivity again.
    t.state =
      t.gpu && !s.gpuOnline
        ? "blocked"
        : t.gpu
          ? "queued"
          : t.previous === "running"
            ? "running"
            : "queued";
  } else if (action === "start" && t.state === "queued") {
    if (t.gpu && !s.gpuOnline) {
      t.state = "blocked";
      return;
    }
    if (
      t.gpu &&
      s.tasks.some((x) => x.id !== id && x.gpu && x.state === "running")
    )
      throw Error("H20 演示池正在被另一项测量独占，请先暂停该项测量。");
    t.state = "running";
  } else throw Error("当前任务状态不支持此操作");
  note(
    s,
    `演示任务「${t.title}」${{ paused: "已暂停", running: "正在执行", queued: "已回到队列", blocked: "等待 H20 连接恢复" }[t.state]}。`,
    [],
    { room: t.room, goal: t.goal || null },
  );
}
export function setGpuOnline(s, online) {
  s.gpuOnline = Boolean(online);
  for (const t of s.tasks.filter((t) => t.gpu)) {
    if (!online && ["queued", "running"].includes(t.state)) {
      t.state = "blocked";
    } else if (online && t.state === "blocked") t.state = "queued";
  }
}

export const currentGoal = (s) =>
  s.goals.find((g) => g.id === s.goal && g.room === s.room) || null;
export const scopeKey = (s) => `${s.room}:${currentGoal(s)?.id || "general"}`;
export const inScope = (s, item) =>
  item.room === s.room && (item.goal || null) === (currentGoal(s)?.id || null);

export function enterRoom(s, room, goal = null) {
  if (!s.rooms.some((r) => r.id === room)) throw Error("研究室不存在");
  if (goal && !s.goals.some((g) => g.id === goal && g.room === room))
    throw Error("目标不属于这个研究室");
  s.room = room;
  s.goal = goal;
  s.page = "room";
  s.view = goal ? "conversation" : "overview";
  s.filter = "all";
}

export function createResearchRoom(
  s,
  { name, description = "", context = "" },
) {
  const value = String(name || "")
    .trim()
    .slice(0, 48);
  if (!value) throw Error("给研究室起一个名字");
  if (s.rooms.some((r) => r.name.toLowerCase() === value.toLowerCase()))
    throw Error("已有同名研究室，请换一个名字");
  const room = {
    id: newId(s, "r"),
    name: value,
    description: String(description).trim().slice(0, 240),
    context: String(context).trim().slice(0, 1000),
  };
  s.rooms.push(room);
  enterRoom(s, room.id);
  return room;
}

export function createResearchGoal(
  s,
  {
    prompt,
    criterion = "",
    budget = "",
    owner = "atlas",
    refs = [],
    source = null,
    firstTask = true,
  },
) {
  const value = String(prompt || "")
    .trim()
    .slice(0, 4000);
  if (!value) throw Error("先说说你想研究什么");
  const room = s.rooms.find((r) => r.id === s.room);
  if (!room) throw Error("请先进入一个研究室");
  if (!MEMBERS.some((m) => m.id === owner))
    throw Error("请选择研究室中的负责人");
  const origin = source
    ? s.messages.find((m) => m.id === source && m.room === s.room)
    : null;
  if (source && !origin) throw Error("原讨论不属于这个研究室");
  if (origin?.derivedGoal)
    return s.goals.find((g) => g.id === origin.derivedGoal);
  const cleanRefs = [...new Set([...refs, ...(origin?.refs || [])])].filter(
    (id) => ARTIFACTS[id],
  );
  const goal = {
    id: newId(s, "g"),
    room: s.room,
    title: value.split("\n")[0].slice(0, 80),
    prompt: value,
    criterion: String(criterion).trim().slice(0, 2000),
    budget: String(budget).trim().slice(0, 240),
    owner,
    context: room.context,
    status: "active",
    revision: 1,
    refs: cleanRefs,
    source,
    createdAt: new Date().toISOString(),
  };
  s.goals.push(goal);
  if (origin) origin.derivedGoal = goal.id;
  enterRoom(s, s.room, goal.id);
  const message = {
    id: newId(s, "m"),
    author: "julian",
    room: s.room,
    goal: goal.id,
    time: timestamp(),
    text: value,
    refs: cleanRefs,
    kind: "goal",
  };
  s.messages.push(message);
  if (firstTask) {
    const task = {
      id: newId(s, "t"),
      room: s.room,
      goal: goal.id,
      goalRevision: goal.revision,
      title: "梳理已有证据，提出第一步研究建议",
      description: `围绕「${goal.title}」整理现有材料、缺失信息与可验证的下一步。`,
      owner,
      state: "queued",
      gpu: false,
      refs: cleanRefs,
      source: message.id,
    };
    s.tasks.push(task);
    message.task = task.id;
  }
  note(
    s,
    firstTask
      ? "目标已建立，第一项工作已进入本地队列。此原型未调用 Agent；可在工作详情中演示开始和交付。"
      : "目标已建立，可以继续讨论或交办工作。此原型未调用 Agent。",
    cleanRefs,
  );
  return goal;
}

export function reviseResearchGoal(
  s,
  id,
  { prompt, criterion = "", budget = "" },
) {
  const goal = s.goals.find((g) => g.id === id);
  if (!goal) throw Error("目标不存在");
  if (goal.status === "completed")
    throw Error("请先重新开放目标，再补充目标说明。");
  const value = String(prompt || "")
    .trim()
    .slice(0, 4000);
  if (!value) throw Error("研究问题不能为空");
  const next = {
    prompt: value,
    title:
      value === goal.prompt ? goal.title : value.split("\n")[0].slice(0, 80),
    criterion: String(criterion).trim().slice(0, 2000),
    budget: String(budget).trim().slice(0, 240),
  };
  if (Object.entries(next).every(([key, value]) => goal[key] === value))
    return goal;
  goal.history ||= [];
  goal.history.push({
    revision: goal.revision || 1,
    prompt: goal.prompt,
    title: goal.title,
    criterion: goal.criterion,
    budget: goal.budget,
    savedAt: new Date().toISOString(),
  });
  goal.revision = (goal.revision || 1) + 1;
  Object.assign(goal, next);
  note(
    s,
    `目标说明已更新到第 ${goal.revision} 版。已有工作保留原版本，未重新派发；新交办的工作会引用新版说明。`,
    [],
    { room: goal.room, goal: goal.id },
  );
  return goal;
}

export function updateGoalStatus(s, id, action, conclusion = "") {
  const goal = s.goals.find((g) => g.id === id);
  if (!goal) throw Error("目标不存在");
  const tasks = s.tasks.filter((t) => t.goal === id);
  if (action === "pause" && goal.status === "active") {
    goal.pausedTasks = tasks
      .filter((t) => ["queued", "running", "blocked"].includes(t.state))
      .map((t) => t.id);
    for (const t of tasks.filter((t) => goal.pausedTasks.includes(t.id)))
      t.state = "paused";
    goal.status = "paused";
  } else if (action === "resume" && goal.status === "paused") {
    for (const t of tasks.filter(
      (t) => goal.pausedTasks?.includes(t.id) && t.state === "paused",
    ))
      t.state = t.gpu && !s.gpuOnline ? "blocked" : "queued";
    goal.pausedTasks = [];
    goal.status = "active";
  } else if (
    action === "complete" &&
    ["active", "paused"].includes(goal.status)
  ) {
    if (tasks.some((t) => !["done", "cancelled"].includes(t.state)))
      throw Error("请先交付或取消未结束的工作，再记录研究结论。");
    if (!String(conclusion).trim())
      throw Error("请记录结论，也可以说明没有得到确定结果。");
    goal.conclusion = String(conclusion).trim().slice(0, 4000);
    goal.status = "completed";
    goal.completedAt = new Date().toISOString();
  } else if (action === "reopen" && goal.status === "completed") {
    goal.status = "active";
  } else throw Error("当前目标状态不支持此操作");
  note(
    s,
    action === "complete"
      ? `研究已结束。结论：${goal.conclusion}（本地人工记录，不代表性能验收通过。）`
      : `目标「${goal.title}」${{ pause: "已暂停，相关工作停止推进", resume: "已恢复，相关工作回到队列", reopen: "重新开放，历史结论保留" }[action]}。`,
    goal.refs,
    { room: goal.room, goal: goal.id },
  );
}

export function restoreRoom(raw) {
  try {
    const s = JSON.parse(raw);
    if (
      !s ||
      ![2, 3].includes(s.schema) ||
      !Number.isSafeInteger(s.next) ||
      s.next < 20 ||
      !Array.isArray(s.tasks) ||
      !Array.isArray(s.messages)
    )
      return createRoom();
    if (s.schema === 2) {
      const defaults = createRoom();
      s.rooms = defaults.rooms;
      s.goals = defaults.goals;
      s.page = "rooms";
      s.goal = s.room === "dsv4" ? SEED_GOAL : null;
      s.forms = {};
      for (const item of [...s.tasks, ...s.messages])
        item.goal = item.room === "dsv4" ? SEED_GOAL : null;
      s.drafts = Object.fromEntries(
        Object.entries(s.drafts || {}).map(([room, draft]) => [
          `${room}:${room === "dsv4" ? SEED_GOAL : "general"}`,
          draft,
        ]),
      );
      s.schema = 3;
    }
    if (!Array.isArray(s.rooms) || !s.rooms.length || !Array.isArray(s.goals))
      return createRoom();
    if (
      !s.rooms.every(
        (r) =>
          r &&
          typeof r.id === "string" &&
          typeof r.name === "string" &&
          typeof r.context === "string" &&
          typeof r.description === "string",
      )
    )
      return createRoom();
    const rooms = new Set(s.rooms.map((r) => r.id));
    if (rooms.size !== s.rooms.length) return createRoom();
    if (
      !s.goals.every(
        (g) =>
          g &&
          typeof g.id === "string" &&
          rooms.has(g.room) &&
          ["title", "prompt", "criterion", "budget", "context", "owner"].every(
            (k) => typeof g[k] === "string",
          ) &&
          ["active", "paused", "completed"].includes(g.status) &&
          Array.isArray(g.refs),
      )
    )
      return createRoom();
    const goals = new Map(s.goals.map((g) => [g.id, g]));
    if (goals.size !== s.goals.length) return createRoom();
    const validScope = (item) =>
      item &&
      rooms.has(item.room) &&
      (!item.goal || goals.get(item.goal)?.room === item.room);
    if (
      !s.tasks.every(
        (t) =>
          validScope(t) &&
          typeof t.title === "string" &&
          typeof t.id === "string" &&
          typeof t.owner === "string" &&
          [
            "queued",
            "running",
            "blocked",
            "paused",
            "done",
            "cancelled",
          ].includes(t.state) &&
          Array.isArray(t.refs),
      )
    )
      return createRoom();
    if (
      !s.messages.every(
        (m) =>
          validScope(m) &&
          typeof m.text === "string" &&
          typeof m.id === "string" &&
          typeof m.author === "string" &&
          Array.isArray(m.refs),
      )
    )
      return createRoom();
    const ids = [...s.rooms, ...s.goals, ...s.tasks, ...s.messages].map(
      (i) => i.id,
    );
    if (new Set(ids).size !== ids.length) return createRoom();
    for (const item of [...s.goals, ...s.tasks, ...s.messages])
      item.refs = item.refs.filter((id) => ARTIFACTS[id]);
    // Protect generated IDs when restoring an older counter with newer objects.
    for (const id of ids) {
      const n = Number(id.match(/^[rgmt](\d+)$/)?.[1]);
      if (Number.isSafeInteger(n)) s.next = Math.max(s.next, n + 1);
    }
    s.room = rooms.has(s.room) ? s.room : s.rooms[0].id;
    s.goal = goals.get(s.goal)?.room === s.room ? s.goal : null;
    s.view = ["overview", "conversation", "tasks", "artifacts", "map"].includes(
      s.view,
    )
      ? s.view
      : "overview";
    s.page = [
      "rooms",
      "room",
      "members",
      "computers",
      "memory",
      "activity",
    ].includes(s.page)
      ? s.page
      : "rooms";
    s.drafts =
      s.drafts && typeof s.drafts === "object" && !Array.isArray(s.drafts)
        ? s.drafts
        : {};
    s.forms =
      s.forms && typeof s.forms === "object" && !Array.isArray(s.forms)
        ? s.forms
        : {};
    return { ...createRoom(), ...s };
  } catch {
    return createRoom();
  }
}
