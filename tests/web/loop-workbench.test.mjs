import test from "node:test";
import assert from "node:assert/strict";
import * as M from "../../docs/prototypes/inference-world/loop-state.mjs";
const copy = (x) => structuredClone(x);
function fixture(mode = "solo") {
  const s = M.empty(),
    l = M.createLoop(s, { ...copy(M.TEMPLATES.code), mode });
  const root = M.nodeOf(s);
  const n = M.fork(s, root.id, { title: "候选", plan: "缓存重复读取" });
  return { s, l, root, n };
}
function finish(s, start = 0) {
  for (let i = 1; i <= 4; i++) M.tick(s, start + i * 2000);
}
test("numeric-only demonstrations respect zero and negative targets without inventing failed checks", () => {
  for (const target of [0, -100]) {
    const s = M.empty();
    M.createLoop(s, {
      ...copy(M.TEMPLATES.code),
      criteria: [
        {
          id: "score",
          name: "误差",
          kind: "metric",
          unit: "",
          direction: "lower",
          target,
          required: true,
        },
      ],
    });
    const n = M.fork(s, s.nodeId, { title: "尝试", plan: "降低误差" });
    M.start(s, n.id, { maxRounds: 2 }, 0);
    finish(s);
    finish(s, 8000);
    for (const node of s.nodes.filter((x) => x.result)) {
      assert.ok(node.result.values.score <= target);
      assert.doesNotMatch(node.result.summary, /必要条件失败/);
    }
    assert.equal(s.jobs[0].used, 2);
    M.validate(s);
  }
});
test("generic templates support numeric, rubric and checklist loops without repositories", () => {
  for (const key of ["answer", "code", "writing"]) {
    const s = M.empty();
    const l = M.createLoop(s, copy(M.TEMPLATES[key]));
    assert.equal(s.nodes.length, 1);
    assert.equal(s.nodes[0].result, null);
    assert.ok(l.artifactKind);
    M.validate(s);
  }
  const { s } = fixture();
  s.loops[0].criteria = [
    {
      id: "done",
      name: "读者能完成任务",
      kind: "check",
      unit: "",
      direction: "higher",
      target: null,
      required: true,
    },
  ];
  M.validateCriteria(s.loops[0].criteria);
});
test("new work does not fabricate findings and demo provenance survives roundtrip", () => {
  const { s } = fixture();
  assert.equal(s.nodes.filter((n) => n.result).length, 0);
  const seed = M.demo();
  assert.ok(
    seed.nodes.filter((n) => n.result).every((n) => n.result.origin === "demo"),
  );
  assert.equal(M.decode(JSON.stringify(seed)).nodes.length, 7);
});
test("nested nodes and historical evaluations retain their contract after revisions", () => {
  const { s, l, n } = fixture();
  const snapshot = copy(n.snapshot);
  M.updateContract(s, l.id, {
    context: "新的条件",
    criteria: l.criteria,
    mode: l.mode,
    roles: { lead: "协调人" },
  });
  const child = M.fork(s, n.id, { title: "子分支", plan: "下一种方法" });
  assert.deepEqual(n.snapshot, snapshot);
  assert.equal(child.snapshot.context, "新的条件");
  assert.equal(child.parentId, n.id);
  assert.equal(child.depth, 2);
  M.validate(s);
});
test("adversarial plans require critique, rebuttal and selection in the correct roles", () => {
  const { s, n } = fixture("debate");
  assert.throws(() => M.start(s, n.id), /可验证/);
  assert.throws(() => M.debateStep(s, n.id, "select"), /质疑和答辩/);
  assert.throws(
    () => M.debateStep(s, n.id, "critique", "问题", "championA"),
    /角色/,
  );
  M.debateStep(s, n.id, "critique", "不能只看局部指标");
  assert.throws(() => M.debateStep(s, n.id, "select"), /质疑和答辩/);
  M.debateStep(s, n.id, "rebuttal", "将记录完整流水线");
  M.debateStep(s, n.id, "select");
  assert.equal(n.status, "ready");
  assert.equal(n.debate[0].role, "championB");
  M.start(s, n.id, {}, 0);
  assert.equal(n.status, "running");
});
test("revising a plan keeps prior debate but requires a fresh selection", () => {
  const { s, n } = fixture("debate");
  M.debateStep(s, n.id, "critique");
  M.debateStep(s, n.id, "rebuttal");
  M.debateStep(s, n.id, "select");
  M.revisePlan(s, n.id, "改变实现方案");
  assert.equal(n.status, "proposed");
  assert.equal(n.debate.length, 0);
  assert.equal(n.history[0].debate.length, 3);
  assert.throws(() => M.start(s, n.id), /可验证/);
});
test("high local score cannot outweigh a failed necessary condition", () => {
  const s = M.demo(),
    bad = s.nodes.find((n) => n.status === "rejected");
  assert.equal(bad.result.values.quality, 86);
  const current = M.loopOf(s).currentId;
  assert.throws(() => M.decide(s, bad.id, "keep", "高分"), /必要条件/);
  assert.equal(M.loopOf(s).currentId, current);
  assert.equal(M.assess(bad).pass, false);
  M.decide(s, bad.id, "reject", "保留失败证据");
  assert.ok(bad.result);
});
test("required numerical checks need a declared threshold and unknown checks cannot pass", () => {
  const { s, n } = fixture();
  const bad = copy(n.snapshot.criteria);
  bad[0].required = true;
  bad[0].target = null;
  assert.throws(() => M.validateCriteria(bad), /填写目标/);
  M.importEvaluation(s, n.id, {
    values: { latency: 0, correct: "unknown" },
    artifact: "记录",
    summary: "未检查",
  });
  assert.equal(n.result.values.latency, 0);
  assert.equal(M.assess(n).pass, false);
});
test("chat stays in its loop and node, labels simulation and leaves execution unchanged", () => {
  const { s, l, n } = fixture();
  M.start(s, n.id, { maxRounds: 2 }, 0);
  const execution = copy(n.execution);
  const reply = M.submitMessage(s, { text: "请解释评价", role: "auditor" });
  assert.equal(reply.nodeId, n.id);
  assert.equal(reply.loopId, l.id);
  assert.equal(reply.demo, true);
  assert.deepEqual(n.execution, execution);
  const other = M.createLoop(s, copy(M.TEMPLATES.writing));
  assert.throws(
    () =>
      M.submitMessage(s, { loopId: other.id, nodeId: n.id, text: "错误归属" }),
    /归属/,
  );
});
test("steering has a pending state and applies exactly once to the next iteration", () => {
  const { s, n } = fixture();
  const j = M.start(s, n.id, { maxRounds: 3, minutes: 1 }, 0);
  const old = copy(n.execution);
  const reply = M.submitMessage(s, {
    text: "下一轮增加长输入检查",
    intent: "steer",
  });
  const p = s.suggestions.find((p) => p.id === reply.suggestionId);
  assert.equal(p.status, "pending");
  M.applySuggestion(s, p.id, "next");
  assert.equal(p.status, "queued");
  assert.deepEqual(n.execution, old);
  finish(s);
  const next = M.nodeOf(s, j.nodeId);
  assert.notEqual(next.id, n.id);
  assert.equal(next.plan, p.text);
  assert.equal(p.status, "applied");
  assert.equal(p.appliedNodeId, next.id);
  assert.deepEqual(n.execution, old);
  assert.throws(() => M.applySuggestion(s, p.id, "next"), /已经处理/);
  M.validate(s);
});
test("steering can create a sibling branch while an iteration continues", () => {
  const { s, n } = fixture();
  const j = M.start(s, n.id, {}, 0);
  const reply = M.submitMessage(s, {
    text: "换一个不同的方法",
    intent: "plan",
  });
  const child = M.applySuggestion(s, reply.suggestionId, "branch");
  assert.equal(child.parentId, n.id);
  assert.equal(j.nodeId, n.id);
  assert.equal(j.status, "running");
  assert.equal(child.status, "ready");
});
test("pause releases progress and resumes with the original elapsed budget", () => {
  const { s, n } = fixture();
  const j = M.start(s, n.id, { maxRounds: 2, minutes: 1 }, 0);
  M.tick(s, 2000);
  M.pause(s, j.id, 2500);
  M.tick(s, 100000);
  assert.equal(j.phase, 1);
  assert.equal(n.result, null);
  M.resume(s, j.id, 100000);
  M.tick(s, 158000);
  assert.equal(j.status, "stopped");
  assert.equal(n.result, null);
});
test("round budgets and guardrails return control without auto-promotion", () => {
  const { s, l, n } = fixture();
  const current = l.currentId;
  const j = M.start(s, n.id, { maxRounds: 5, minutes: 1 }, 0);
  finish(s);
  finish(s, 8000);
  assert.equal(j.used, 2);
  assert.equal(j.status, "done");
  assert.match(j.reason, /必要条件/);
  assert.equal(l.currentId, current);
  assert.equal(M.nodesOf(s, l.id).filter((n) => n.result).length, 2);
});
test("new rounds in adversarial mode return to debate rather than inheriting selection", () => {
  const { s, n } = fixture("debate");
  M.debateStep(s, n.id, "critique");
  M.debateStep(s, n.id, "rebuttal");
  M.debateStep(s, n.id, "select");
  const j = M.start(s, n.id, { maxRounds: 3 }, 0);
  finish(s);
  const next = s.nodes.find((x) => x.parentId === n.id);
  assert.equal(next.status, "proposed");
  assert.equal(j.status, "done");
  assert.match(j.reason, /重新质疑/);
});
test("refresh retains in-flight work and pending steering but never replays it", () => {
  const { s, n } = fixture();
  const j = M.start(s, n.id, { maxRounds: 2 }, 0);
  const r = M.submitMessage(s, { text: "新的方向", intent: "steer" });
  M.applySuggestion(s, r.suggestionId, "next");
  const restored = M.decode(JSON.stringify(s));
  assert.equal(restored.jobs[0].status, "interrupted");
  assert.equal(M.nodeOf(restored, n.id).status, "interrupted");
  assert.equal(restored.jobs[0].pending.length, 1);
  M.tick(restored, 1000000);
  assert.equal(M.nodeOf(restored, n.id).result, null);
});
test("one active exploration per loop prevents conflicting ownership", () => {
  const { s, n } = fixture();
  M.start(s, n.id, {}, 0);
  const second = M.fork(s, n.parentId, {
    title: "并行想法",
    plan: "另一个计划",
  });
  assert.throws(() => M.start(s, second.id), /已有一轮/);
  assert.equal(second.status, "ready");
});
test("comparison rejects different contracts or provenance and handles lower-is-better", () => {
  const { s, root, n } = fixture();
  M.importEvaluation(s, root.id, {
    values: { latency: 100, correct: "pass" },
    artifact: "a",
    summary: "a",
  });
  M.importEvaluation(s, n.id, {
    values: { latency: 0, correct: "pass" },
    artifact: "b",
    summary: "b",
  });
  assert.equal(M.comparison(root, n).values[0].delta, -100);
  n.result.origin = "demo";
  assert.equal(M.comparison(root, n).ok, false);
  n.result.origin = "imported";
  n.snapshot.context = "不同条件";
  assert.equal(M.comparison(root, n).ok, false);
});
test("source and memory references survive forks with their evidence and counterexamples", () => {
  const { s, n } = fixture();
  assert.throws(
    () =>
      M.addSource(s, {
        title: "bad",
        url: "javascript:alert(1)",
        note: "x",
        version: "v1",
      }),
    /http/,
  );
  const r = M.addSource(s, {
    title: "资料",
    url: "https://example.org/paper",
    note: "尚未验证",
    version: "v1",
  });
  M.importEvaluation(s, n.id, {
    values: { latency: 90, correct: "pass" },
    artifact: "原始记录",
    summary: "结果",
  });
  const m = M.saveMemory(s, n.id, {
    title: "经验",
    claim: "缓存有收益",
    conditions: "当前数据",
  });
  const child = M.fork(s, n.id, {
    title: "子节点",
    plan: "复验",
    sourceId: r.id,
  });
  M.useMemory(s, child.id, m.id);
  M.verdictMemory(s, m.id, "contested", "另一种数据没有收益");
  assert.equal(m.history.length, 1);
  assert.equal(child.sourceIds[0], r.id);
  assert.equal(child.memoryIds[0], m.id);
  M.validate(s);
});
test("tree layout retains nested lineage and separate leaves", () => {
  const { s, n } = fixture();
  const a = M.fork(s, n.id, { title: "a", plan: "a" });
  const b = M.fork(s, n.id, { title: "b", plan: "b" });
  const c = M.fork(s, a.id, { title: "c", plan: "c" });
  const graph = M.layout(M.nodesOf(s));
  assert.equal(graph.positions.length, s.nodes.length);
  assert.ok(
    graph.positions.find((p) => p.id === c.id).x >
      graph.positions.find((p) => p.id === a.id).x,
  );
  assert.notEqual(
    graph.positions.find((p) => p.id === a.id).y,
    graph.positions.find((p) => p.id === b.id).y,
  );
});
test("malformed backups fail before replacing live records", () => {
  const seed = M.demo();
  const variants = [
    (s) => (s.nodes[0].parentId = s.nodes[1].id),
    (s) => (s.nodes[0].result.values.quality = null),
    (s) => (s.nodes[0].role = "intruder"),
    (s) => (s.sources[0].url = "data:text/html,bad"),
    (s) => (s.next = 1),
    (s) => (s.messages[0].nodeId = s.nodes.at(-1).id),
    (s) => (s.nodes[0].memoryIds = ["missing"]),
    (s) => (s.drafts = { x: { nested: true } }),
  ];
  for (const corrupt of variants) {
    const broken = copy(seed);
    corrupt(broken);
    assert.throws(() => M.decode(JSON.stringify(broken)));
  }
  M.validate(seed);
});
test("unsafe markup stays text and backups preserve scoped drafts", () => {
  assert.equal(
    M.escapeHTML("<img src=x onerror=alert(1)>"),
    "&lt;img src=x onerror=alert(1)&gt;",
  );
  const { s, l, n } = fixture();
  s.drafts[`${l.id}:${n.id}`] = "独立草稿";
  const restored = M.decode(JSON.stringify(s));
  assert.equal(restored.drafts[`${l.id}:${n.id}`], "独立草稿");
});
test("criterion identifiers cannot inject form attributes or shadow artifact fields", () => {
  for (const id of [
    'x" autofocus onfocus="alert(1)',
    "artifact",
    "summary",
    "constructor",
  ]) {
    const criteria = copy(M.TEMPLATES.code.criteria);
    criteria[0].id = id;
    assert.throws(() => M.validateCriteria(criteria), /评价标识/);
  }
});
test("corrupt pending directives cannot enter the execution queue through backups", () => {
  const { s, n } = fixture();
  const j = M.start(s, n.id, {}, 0);
  j.pending = [null];
  assert.throws(() => M.decode(JSON.stringify(s)));
  j.pending = [{ suggestionId: "missing", text: "无效引用" }];
  assert.throws(() => M.decode(JSON.stringify(s)), /引用/);
});
