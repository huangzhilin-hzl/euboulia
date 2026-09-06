// Deterministic, in-memory demonstration. No agent, network or GPU execution.
export const STATUS = {
  completed: "已交付",
  pending: "待接力",
  running: "执行中",
  failed: "执行失败",
};
const task = (id, title, type, deps, status = "pending", extra = {}) => ({
  id,
  title,
  type,
  deps,
  status,
  admission:
    type === "strategy"
      ? "human"
      : ["benchmark", "profile", "build"].includes(type)
        ? "agent"
        : "either",
  gpu: ["benchmark", "profile"].includes(type),
  cost: 2000,
  attempt: status === "pending" ? 0 : 1,
  history: [],
  practiceVersion: "v1",
  ...extra,
});
export function createWorld() {
  return {
    revision: 1,
    selected: "T1",
    nextBranch: 3,
    champion: "B0",
    members: [
      {
        id: "julian",
        name: "Julian",
        kind: "human",
        runtime: "管理员",
        online: true,
      },
      {
        id: "lin",
        name: "Lin",
        kind: "human",
        runtime: "性能工程师",
        online: true,
      },
      {
        id: "atlas",
        name: "Atlas",
        kind: "agent",
        runtime: "Codex",
        computer: "Local Mac",
        online: true,
        limit: 100000,
        used: 24000,
        reserved: 2000,
      },
      {
        id: "prism",
        name: "Prism",
        kind: "agent",
        runtime: "Claude",
        computer: "Team connector",
        online: true,
        limit: 80000,
        used: 16000,
        reserved: 0,
      },
    ],
    resources: [
      { id: "xx1", model: "H20", gpus: 8, online: true, lease: "T1" },
      { id: "xx2", model: "H20", gpus: 8, online: true, lease: null },
      { id: "xx3", model: "A100", gpus: 8, online: true, lease: null },
      { id: "xx4", model: "H20", gpus: 8, online: false, lease: null },
    ],
    tasks: [
      task("S1", "锁定 DSV4 策略", "strategy", [], "completed", {
        owner: "julian",
        note: "真人定稿模型、矩阵、正确性与门槛，交付 recipe lock。",
      }),
      task("B0", "无 profile 基线", "benchmark", ["S1"], "completed", {
        owner: "atlas",
        note: "原始参照 B0；输入、硬件和测量协议已锁定。",
      }),
      task("P0", "诊断采样", "profile", ["S1"], "completed", {
        owner: "prism",
        note: "诊断通道与性能通道分开。此原型不包含真实 trace。",
      }),
      task(
        "A0",
        "热点分析与讨论",
        "analysis",
        ["S1", "B0", "P0"],
        "completed",
        {
          owner: "lin",
          note: "把观察、可能原因与缺失证据分开，选择值得验证的假设。",
        },
      ),
      task("H1", "A · 减少 MHC 中间开销", "proposal", ["A0"], "completed", {
        owner: "julian",
        note: "演示假设，尚不能由原有 profile 摘要证明收益。",
      }),
      task("H2", "B · 检查通信等待", "proposal", ["A0"], "pending", {
        note: "先确认跨 rank 等待来源，再选择是否改变通信算法。",
      }),
      task("W1", "候选 A 物化与构建", "build", ["H1"], "completed", {
        owner: "atlas",
        note: "固定父版本与改动；后续测量引用同一候选快照。",
      }),
      task("T1", "候选 A · A/B 验证", "benchmark", ["W1", "B0"], "running", {
        owner: "atlas",
        resource: "xx1",
        parentBenchmark: "B0",
        note: "GPU 任务在独占 xx1 的 Pod 中运行。演示初始状态。",
      }),
      task("P1", "候选 A · Profile", "profile", ["W1"], "pending", {
        note: "可与 A/B 并行，但必须使用隔离的 GPU 资源。",
      }),
      task("V1", "逐点门槛与晋升", "verdict", ["B0", "T1"], "pending", {
        note: "读取无 profile 的比较；profile 不作为性能晋升输入。",
      }),
      task("E1", "提炼经验与下一轮", "experience", ["V1", "P1"], "pending", {
        note: "判定结果与机制解释汇合，形成有边界的经验草案。",
      }),
    ],
    messages: {},
    experiences: [],
    practice: {
      active: "v1",
      candidate: "v2",
      evaluation: null,
      trial: null,
      history: [],
    },
    events: [
      {
        text: "Atlas 已认领 T1 · xx1 / H20 × 8 · Pod 独占（演示）",
        task: "T1",
      },
    ],
  };
}
export function byId(world, id) {
  const t = world.tasks.find((t) => t.id === id);
  if (!t) throw Error("任务不存在");
  return t;
}
export function compatibleResources(world) {
  return world.resources.filter(
    (r) => r.online && r.model === "H20" && r.gpus >= 8 && !r.lease,
  );
}
export function inputBlockers(world, t) {
  return t.deps
    .filter((id) => byId(world, id).status !== "completed")
    .map((id) => `等待 ${id} · ${byId(world, id).title}`);
}
export function eligibility(world, id, memberId) {
  const t = byId(world, id),
    m = world.members.find((m) => m.id === memberId),
    why = inputBlockers(world, t);
  if (t.status === "completed" || t.status === "running")
    why.push(
      t.status === "running" ? "已有执行中的尝试" : "已交付，历史保持不变",
    );
  if (!m) return [...why, "请选择负责人"];
  if (!m.online) why.push(`${m.name} 离线`);
  if (t.admission !== "either" && m.kind !== t.admission)
    why.push(
      t.admission === "human" ? "此任务限真人交付" : "此任务需要执行 Agent",
    );
  if (m.kind === "agent" && m.used + m.reserved + t.cost > m.limit)
    why.push(`${m.name} 日 token 预算不足`);
  if (t.gpu && !compatibleResources(world).length)
    why.push("等待空闲、在线的 H20 × 8 独占资源");
  return why;
}
export function addEvent(world, text, id) {
  world.events.unshift({ text, task: id });
}
export function startTask(world, id, memberId) {
  const blockers = eligibility(world, id, memberId);
  if (blockers.length) throw Error(blockers.join("；"));
  const t = byId(world, id),
    m = world.members.find((m) => m.id === memberId);
  t.attempt++;
  t.status = "running";
  t.owner = m.id;
  t.result = null;
  if (m.kind === "agent") m.reserved += t.cost;
  if (t.gpu) {
    const r = compatibleResources(world)[0];
    r.lease = id;
    t.resource = r.id;
  }
  addEvent(
    world,
    `${m.name} 认领 ${id} · attempt ${t.attempt}${t.resource ? " · " + t.resource + " 独占" : ""}（演示）`,
    id,
  );
  return t;
}
export function finishTask(world, id, outcome = "success") {
  const t = byId(world, id);
  if (t.status !== "running") throw Error("只有执行中的尝试可以提交结果");
  const m = world.members.find((m) => m.id === t.owner);
  if (m.kind === "agent") {
    m.reserved = Math.max(0, m.reserved - t.cost);
    m.used += t.cost;
  }
  world.resources
    .filter((r) => r.lease === id)
    .forEach((r) => {
      r.lease = null;
    });
  t.status = outcome === "failure" ? "failed" : "completed";
  t.result =
    outcome === "failure"
      ? { error: "演示构建或运行失败，未形成有效比较" }
      : {
          artifact: `demo://${id}/attempt-${t.attempt}`,
          regressed: outcome === "regression",
        };
  if (t.type === "verdict" && outcome !== "failure") {
    const measured = t.deps
      .map((d) => byId(world, d))
      .findLast((d) => d.type === "benchmark" && d.id !== "B0");
    t.result.accepted = !!measured?.result && !measured.result.regressed;
    const canPromote =
      t.result.accepted &&
      world.champion === (measured.parentBenchmark || "B0");
    if (canPromote) world.champion = measured.id;
    t.result.needsChampionComparison = t.result.accepted && !canPromote;
    t.result.explanation = t.result.needsChampionComparison
      ? "演示：父版本门槛通过，但 champion 已变化，需要补充比较"
      : t.result.accepted
        ? "演示：同协议门槛通过"
        : "演示：性能回退，不更新 champion";
  }
  t.history.push({
    attempt: t.attempt,
    status: t.status,
    owner: t.owner,
    resource: t.resource,
    result: structuredClone(t.result),
  });
  if (t.type === "experience" && t.status === "completed")
    world.experiences.push({
      id: `EXP-${world.experiences.length + 8}`,
      title: "本轮结论需要界定适用条件与反例",
      status: "candidate",
      source: id,
    });
  addEvent(
    world,
    `${id} · ${t.status === "failed" ? "尝试失败" : "产物已验收"}${outcome === "regression" ? " · 记录性能回退" : ""}（演示）`,
    id,
  );
  return t;
}
export function addHypothesis(world, title, source = "human", origin = "A0") {
  const parent = world.champion;
  const input = byId(world, origin);
  if (input.status !== "completed") throw Error("先完成来源任务，再派生下一轮");
  title = title.trim();
  if (!title || title.length > 90) throw Error("假设名称需要 1–90 个字符");
  const n = world.nextBranch++,
    h = `H${n}`,
    w = `W${n}`,
    t = `T${n}`,
    p = `P${n}`,
    v = `V${n}`,
    e = `E${n}`;
  world.tasks.push(
    task(h, title, "proposal", [origin], "pending", {
      source,
      note: "新假设草稿；预期收益、证据与否定条件需要共同定稿。",
    }),
    task(w, "隔离物化与构建", "build", [h]),
    task(
      t,
      "无 profile A/B",
      "benchmark",
      [...new Set([w, parent, "B0"])],
      "pending",
      { parentBenchmark: parent },
    ),
    task(p, "候选诊断采样", "profile", [w]),
    task(v, "逐点门槛判定", "verdict", [...new Set(["B0", parent, t])]),
    task(e, "经验与下一轮", "experience", [v, p]),
  );
  world.tasks.slice(-6).forEach((t) => {
    t.practiceVersion = world.practice.active;
  });
  world.revision++;
  addEvent(
    world,
    `图 v${world.revision} · 新增假设 ${h} 与验证子图（演示）`,
    h,
  );
  return h;
}
export function downstream(world, id) {
  const seen = new Set(),
    queue = [id];
  while (queue.length) {
    const q = queue.shift();
    for (const t of world.tasks) {
      if (t.deps.includes(q) && !seen.has(t.id)) {
        seen.add(t.id);
        queue.push(t.id);
      }
    }
  }
  return seen;
}
export function upstream(world, id) {
  const seen = new Set();
  const visit = (id) => {
    for (const dep of byId(world, id).deps) {
      if (!seen.has(dep)) {
        seen.add(dep);
        visit(dep);
      }
    }
  };
  visit(id);
  return seen;
}
export function addDependency(world, source, target) {
  const from = byId(world, source),
    to = byId(world, target);
  if (from.id === to.id || downstream(world, target).has(source))
    throw Error("这条依赖会形成环；请创建下一轮任务");
  if (to.status !== "pending") throw Error("运行中或已发生的任务输入不能改写");
  if (to.deps.includes(source)) throw Error("这条依赖已存在");
  to.deps.push(source);
  world.revision++;
  addEvent(
    world,
    `图 v${world.revision} · ${source} → ${target}（演示）`,
    target,
  );
}
export function levels(world) {
  const memo = new Map(),
    visiting = new Set();
  const level = (id) => {
    if (memo.has(id)) return memo.get(id);
    if (visiting.has(id)) throw Error("任务图存在环");
    visiting.add(id);
    const t = byId(world, id),
      n = t.deps.length ? Math.max(...t.deps.map(level)) + 1 : 0;
    visiting.delete(id);
    memo.set(id, n);
    return n;
  };
  world.tasks.forEach((t) => level(t.id));
  return memo;
}
export function setBudget(world, memberId, limit) {
  const m = world.members.find((m) => m.id === memberId);
  if (!m || m.kind !== "agent" || !Number.isFinite(limit) || limit < 0)
    throw Error("请输入有效的非负 token 预算");
  m.limit = limit;
}
export function evaluatePractice(world, passed = true) {
  world.practice.trial = null;
  world.practice.evaluation = {
    passed,
    holdout: true,
    cases: 12,
    old: 8,
    new: passed ? 11 : 7,
    budget: "相同模型 / 工具 / token 与 GPU 预算",
    label: "演示评测；未运行真实案例",
  };
  addEvent(
    world,
    `Practice v2 · 演示留出评测${passed ? "通过" : "回退"}`,
    "A0",
  );
}
export function promotePractice(world) {
  if (!world.practice.evaluation?.passed || !world.practice.evaluation.holdout)
    throw Error("先通过独立评测，再晋升能力包");
  if (world.practice.trial !== "passed")
    throw Error("先完成有限试用，再晋升能力包");
  if (world.practice.active === "v2") throw Error("v2 已经是当前版本");
  world.practice.history.push({
    from: world.practice.active,
    to: "v2",
    evaluation: structuredClone(world.practice.evaluation),
  });
  world.practice.active = "v2";
  addEvent(world, "Practice v2 已晋升（演示）；既有任务的版本不变", "A0");
}
export function rollbackPractice(world) {
  if (world.practice.active === "v1") throw Error("当前已经是 v1");
  world.practice.history.push({ from: "v2", to: "v1", reason: "人工回滚演示" });
  world.practice.active = "v1";
  addEvent(world, "Practice 默认版本已回滚到 v1（演示）", "A0");
}

export function trialPractice(world) {
  if (!world.practice.evaluation?.passed)
    throw Error("先通过独立评测，再进入有限试用");
  world.practice.trial = "passed";
}
