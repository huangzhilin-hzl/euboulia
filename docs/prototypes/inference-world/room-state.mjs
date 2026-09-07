// Local interaction fixtures. This module never runs agents or submits GPU work.
export const STORAGE_KEY = "euboulia.research-room.v2";
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
    schema: 2,
    next: 20,
    room: "dsv4",
    view: "conversation",
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
        time: "10:24",
        text: "先搞清楚 DSV4 在 H20 上为什么慢。复用已有基线和 profile，优先看 decode；有依据再改代码。",
        refs: [],
      },
      {
        id: "m2",
        author: "prism",
        room: "dsv4",
        time: "10:26",
        text: "我在 decode 窗口里看到一段等待。但目前只有 rank 0，不能判断是通信本身慢，还是其他 rank 的计算拖住了它。建议补采多 rank，再决定是否改通信 kernel。",
        refs: ["trace"],
        kind: "finding",
      },
      {
        id: "m3",
        author: "atlas",
        room: "dsv4",
        time: "10:28",
        text: "另一条 MHC 线索已经有初步对比。TPOT 从 41.8 ms 降到 38.6 ms；正确性尚未齐全，先保留为候选。",
        refs: ["compare", "patch"],
        kind: "result",
      },
      {
        id: "m4",
        author: "lin",
        room: "dsv4",
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
function note(s, text, refs = []) {
  s.messages.push({
    id: newId(s, "m"),
    author: "system",
    room: s.room,
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
  const cleanRefs = [...new Set(refs)].filter((r) => ARTIFACTS[r]);
  const message = {
    id: newId(s, "m"),
    author: "julian",
    room: s.room,
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
      "已保存到本地研究室。这是交互原型，未调用模型；可用下方示例动作体验证据引用与任务接力。",
    );
  return message;
}
export function chooseDirection(s, direction) {
  if (s.decision) return;
  if (!["ranks", "mhc"].includes(direction)) throw Error("未知研究方向");
  s.decision = direction;
  const ranks = direction === "ranks";
  s.tasks.push({
    id: newId(s, "t"),
    title: ranks ? "补采多 rank，定位等待来源" : "验证 MHC 在 C1 / C16 的收益",
    owner: ranks ? "prism" : "atlas",
    room: "dsv4",
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
  );
}
export function updateTask(s, id, action) {
  const t = s.tasks.find((t) => t.id === id);
  if (!t) throw Error("任务不存在");
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
export function restoreRoom(raw) {
  try {
    const s = JSON.parse(raw);
    if (
      s.schema !== 2 ||
      !Number.isSafeInteger(s.next) ||
      s.next < 20 ||
      !Array.isArray(s.tasks) ||
      !Array.isArray(s.messages)
    )
      return createRoom();
    if (
      !s.tasks.every(
        (t) =>
          t &&
          typeof t.title === "string" &&
          typeof t.id === "string" &&
          ["queued", "running", "blocked", "paused", "done"].includes(
            t.state,
          ) &&
          Array.isArray(t.refs),
      )
    )
      return createRoom();
    if (
      !s.messages.every(
        (m) =>
          m &&
          typeof m.text === "string" &&
          typeof m.id === "string" &&
          Array.isArray(m.refs),
      )
    )
      return createRoom();
    return {
      ...createRoom(),
      ...s,
      room: ["dsv4", "kernels"].includes(s.room) ? s.room : "dsv4",
      view: ["conversation", "tasks", "artifacts", "map"].includes(s.view)
        ? s.view
        : "conversation",
      drafts: s.drafts && typeof s.drafts === "object" ? s.drafts : {},
    };
  } catch {
    return createRoom();
  }
}
