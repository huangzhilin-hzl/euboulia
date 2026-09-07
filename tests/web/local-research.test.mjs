import test from "node:test";
import assert from "node:assert/strict";
import {
  createDemo,
  createEmpty,
  createStudy,
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
  closeBranch,
  stopExploration,
  tick,
  decodeState,
  validateState,
  escapeHTML,
  safeURL,
  readResultJSON,
} from "../../docs/prototypes/inference-world/research-state.mjs";

const protocol = () => ({
  dataset: "fixture-v1",
  environment: "CPU fixture, runtime v1",
  evaluation: "three warm measurements",
  metric: { name: "throughput", unit: "items/s", direction: "higher" },
  guard: { name: "latency", unit: "ms", limit: 50 },
});
function fresh() {
  const s = createEmpty();
  createStudy(
    s,
    {
      direction: "Investigate a measurable bottleneck",
      resources: "/repo/one\n/repo/two",
      protocol: protocol(),
    },
    0,
  );
  return s;
}
const result = (overrides = {}) => ({
  metric: 100,
  guard: 40,
  summary: "Measured fixture",
  artifact: "/results/one.json",
  codeRef: "fixture:abc",
  ...overrides,
});
function completeOne(s, now = 0) {
  tick(s, now);
  tick(s, now + 1600);
  tick(s, now + 3200);
  tick(s, now + 4800);
}

test("sample opens without identities and every built-in measurement is labelled", () => {
  const s = createDemo();
  assert.equal(s.page, "home");
  assert.equal(s.branches.length, 5);
  assert.ok(s.runs.every((r) => r.origin === "demo"));
  assert.equal(s.memories[0].origin, "demo");
  assert.equal(s.memories[0].status, "supported");
  assert.deepEqual(decodeState(JSON.stringify(s)), s);
});
test("new direction is an empty real research record, not fabricated findings", () => {
  const s = fresh();
  assert.equal(s.runs.length, 0);
  assert.equal(s.studies[0].demo, false);
  assert.equal(branchOf(s).parentId, null);
  assert.equal(s.messages.filter((m) => m.author === "demo-agent").length, 0);
  assert.equal(s.studies[0].resources, "/repo/one\n/repo/two");
});
test("nested forks retain the selected run snapshot despite later parent edits", () => {
  const s = fresh(),
    base = branchOf(s),
    r = importResult(s, base.id, result(), 0);
  const child = forkBranch(
    s,
    base.id,
    { title: "A", hypothesis: "One change", runId: r.id },
    1,
  );
  const grandchild = forkBranch(
    s,
    child.id,
    { title: "A1", hypothesis: "A refinement" },
    2,
  );
  const replacement = protocol();
  replacement.dataset = "fixture-v2";
  reviseProtocol(s, base.id, replacement, "fixture:def", 3);
  assert.equal(base.snapshot.protocol.dataset, "fixture-v2");
  assert.equal(child.snapshot.protocol.dataset, "fixture-v1");
  assert.equal(grandchild.snapshot.protocol.dataset, "fixture-v1");
  assert.equal(grandchild.snapshot.referenceRunId, r.id);
  assert.equal(r.snapshot.protocol.dataset, "fixture-v1");
  assert.notEqual(child.snapshot, grandchild.snapshot);
});
test("fork cannot silently take a run from another branch or another study", () => {
  const s = fresh(),
    first = branchOf(s),
    r = importResult(s, first.id, result(), 0);
  createStudy(
    s,
    { direction: "An unrelated question", protocol: protocol() },
    1,
  );
  assert.throws(
    () =>
      forkBranch(s, s.branch, { title: "bad", hypothesis: "bad", runId: r.id }),
    /当前分支/,
  );
});
test("discussion stays in the selected branch and keeps only valid study references", () => {
  const s = fresh(),
    b = branchOf(s),
    r = importResult(s, b.id, result(), 0);
  const a = forkBranch(s, b.id, { title: "A", hypothesis: "A", runId: r.id });
  addMessage(
    s,
    a.id,
    "Why did this change?",
    [
      { kind: "run", id: r.id },
      { kind: "run", id: "missing" },
    ],
    1,
  );
  assert.equal(s.messages.at(-1).branchId, a.id);
  assert.equal(s.messages.at(-1).refs.length, 1);
  assert.equal(
    s.messages.filter(
      (m) => m.branchId === b.id && m.text === "Why did this change?",
    ).length,
    0,
  );
});
test("imported results keep numeric zeros and reject nonfinite or missing evidence", () => {
  const s = fresh();
  const r = importResult(s, s.branch, result({ metric: 0, guard: 0 }));
  assert.equal(r.result.metric, 0);
  assert.equal(r.origin, "imported");
  assert.throws(
    () => importResult(s, s.branch, result({ metric: Infinity })),
    /数值/,
  );
  assert.throws(
    () => importResult(s, s.branch, result({ guard: NaN })),
    /数值/,
  );
  assert.throws(
    () => importResult(s, s.branch, result({ artifact: "" })),
    /原始记录/,
  );
});
test("comparison rejects mixed provenance and changed or incomplete conditions", () => {
  const s = fresh(),
    a = importResult(s, s.branch, result()),
    b = importResult(s, s.branch, result({ metric: 120 }));
  assert.deepEqual(comparable(a, b), {
    ok: true,
    delta: 20,
    percent: 20,
    improved: true,
  });
  b.origin = "demo";
  assert.equal(comparable(a, b).ok, false);
  b.origin = "imported";
  b.snapshot.protocol.environment = "different machine";
  assert.equal(comparable(a, b).ok, false);
  b.snapshot.protocol = structuredClone(a.snapshot.protocol);
  b.snapshot.protocol.dataset = "待确认的数据集";
  a.snapshot.protocol.dataset = "待确认的数据集";
  assert.match(comparable(a, b).reason, /尚未补齐/);
});
test("comparison handles a zero baseline and lower-is-better metrics", () => {
  const s = fresh(),
    a = importResult(s, s.branch, result({ metric: 0 })),
    b = importResult(s, s.branch, result({ metric: 20 }));
  assert.equal(comparable(a, b).percent, null);
  a.result.metric = 100;
  b.result.metric = 80;
  a.snapshot.protocol.metric.direction = "lower";
  b.snapshot.protocol.metric.direction = "lower";
  assert.equal(comparable(a, b).improved, true);
});
test("sources are traceable unverified clues that can seed a child branch", () => {
  const s = fresh(),
    parent = branchOf(s);
  const r = addSource(
    s,
    parent.id,
    {
      title: "Official technique",
      url: "https://example.com/docs",
      published: "v2",
      relevance: "Compare the technique",
      conditions: "Check software version",
    },
    0,
  );
  const b = forkBranch(s, parent.id, {
    title: "Try the technique",
    hypothesis: r.relevance,
    sourceIds: [r.id],
  });
  assert.equal(r.status, "unverified");
  assert.ok(b.sourceIds.includes(r.id));
  assert.throws(
    () =>
      addSource(s, b.id, {
        title: "bad",
        url: "javascript:alert(1)",
        published: "v1",
        relevance: "x",
        conditions: "x",
      }),
    /http/,
  );
});
test("one execution slot is shared by separate branch budgets", () => {
  const s = fresh(),
    base = branchOf(s);
  const a = forkBranch(s, base.id, { title: "A", hypothesis: "A" });
  const b = forkBranch(s, base.id, { title: "B", hypothesis: "B" });
  startExploration(s, a.id, { maxRuns: 1, minutes: 1, instruction: "A" }, 0);
  startExploration(s, b.id, { maxRuns: 1, minutes: 1, instruction: "B" }, 0);
  tick(s, 0);
  assert.equal(s.runs.filter((r) => r.status === "running").length, 1);
  assert.equal(s.runs.filter((r) => r.status === "queued").length, 1);
  completeOne(s, 0);
  assert.equal(runsOf(s, a.id)[0].status, "done");
  assert.equal(runsOf(s, b.id)[0].status, "running");
  assert.equal(a.exploration.state, "review");
});
test("guardrail stops autonomous demonstration before the requested maximum", () => {
  const s = fresh();
  startExploration(
    s,
    s.branch,
    { maxRuns: 5, minutes: 1, instruction: "Explore" },
    0,
  );
  completeOne(s, 0);
  completeOne(s, 4800);
  assert.equal(branchOf(s).exploration.used, 2);
  assert.equal(branchOf(s).exploration.state, "review");
  assert.match(branchOf(s).exploration.reason, /约束/);
  assert.equal(s.runs.length, 2);
  assert.ok(s.runs.every((r) => r.origin === "demo"));
  assert.ok(
    s.messages
      .filter((m) => m.author === "demo-agent")
      .every((m) => m.text.startsWith("示例结果")),
  );
});
test("time budgets stop running or waiting work without producing results", () => {
  const s = fresh();
  startExploration(
    s,
    s.branch,
    { maxRuns: 8, minutes: 1, instruction: "Explore" },
    0,
  );
  tick(s, 0);
  tick(s, 60000);
  assert.equal(s.runs[0].status, "cancelled");
  assert.equal(s.runs[0].result, null);
  assert.match(branchOf(s).exploration.reason, /时间预算/);
});
test("pause releases the slot and resume honors the remaining budget", () => {
  const s = fresh(),
    b = branchOf(s);
  startExploration(
    s,
    b.id,
    { maxRuns: 1, minutes: 1, instruction: "Explore" },
    0,
  );
  tick(s, 0);
  pauseBranch(s, b.id, 1000);
  tick(s, 5000);
  assert.equal(s.runs[0].status, "paused");
  resumeBranch(s, b.id, 100000);
  assert.equal(b.exploration.deadline, 159000);
  assert.equal(s.runs[0].status, "queued");
  completeOne(s, 100000);
  assert.equal(s.runs[0].status, "done");
});
test("reload interrupts in-flight and queued demos and never silently replays", () => {
  const s = fresh();
  startExploration(
    s,
    s.branch,
    { maxRuns: 1, minutes: 1, instruction: "Explore" },
    0,
  );
  tick(s, 0);
  const restored = decodeState(JSON.stringify(s));
  assert.equal(restored.runs[0].status, "interrupted");
  assert.equal(branchOf(restored).exploration.state, "stopped");
  assert.equal(tick(restored, 5000), false);
  assert.equal(restored.runs[0].result, null);
  startExploration(
    restored,
    restored.branch,
    { maxRuns: 1, minutes: 1, instruction: "Retry explicitly" },
    6000,
  );
  assert.equal(restored.runs.length, 2);
});
test("cancellation and closure retain evidence and do not close child branches", () => {
  const s = fresh(),
    parent = branchOf(s),
    r = importResult(s, parent.id, result(), 0);
  const child = forkBranch(s, parent.id, {
    title: "A",
    hypothesis: "A",
    runId: r.id,
  });
  startExploration(
    s,
    parent.id,
    { maxRuns: 1, minutes: 1, instruction: "Explore" },
    0,
  );
  closeBranch(s, parent.id, "No firm conclusion yet", 100);
  assert.equal(parent.status, "closed");
  assert.equal(child.status, "active");
  assert.equal(r.status, "done");
  assert.equal(runsOf(s, parent.id).at(-1).status, "cancelled");
  assert.throws(() => addMessage(s, parent.id, "more"), /重新开放/);
  resumeBranch(s, parent.id, 200);
  assert.equal(parent.conclusion, "No firm conclusion yet");
  assert.ok(parent.history.some((h) => h.type === "conclusion"));
});
test("conditions cannot change in the middle of a pending exploration", () => {
  const s = fresh();
  startExploration(
    s,
    s.branch,
    { maxRuns: 1, minutes: 1, instruction: "Explore" },
    0,
  );
  assert.throws(
    () => reviseProtocol(s, s.branch, protocol(), "new"),
    /结束当前验证/,
  );
  stopExploration(s, s.branch);
  reviseProtocol(s, s.branch, protocol(), "new");
  assert.notEqual(s.runs[0].snapshot.codeRef, branchOf(s).snapshot.codeRef);
});
test("memory requires evidence and keeps reversals in an append-only history", () => {
  const s = fresh(),
    b = branchOf(s),
    r = importResult(s, b.id, result());
  assert.throws(
    () =>
      saveMemory(s, b.id, {
        title: "x",
        claim: "x",
        conditions: "x",
        runIds: [],
      }),
    /至少选择/,
  );
  const m = saveMemory(s, b.id, {
    title: "A scoped finding",
    claim: "A claim",
    conditions: "Only this workload",
    runIds: [r.id],
  });
  assert.equal(m.status, "candidate");
  assert.throws(
    () => reviseMemory(s, m.id, "supported", "one run"),
    /至少需要两份/,
  );
  const r2 = importResult(s, b.id, result({ metric: 101 }));
  m.runIds.push(r2.id);
  reviseMemory(s, m.id, "supported", "Repeated measurement matches");
  reviseMemory(s, m.id, "contested", "A counterexample was observed");
  assert.equal(m.status, "contested");
  assert.equal(m.history.length, 2);
  assert.equal(m.history[0].to, "supported");
});
test("experience retrieval carries evidence but flags changed conditions", () => {
  const s = createDemo(),
    m = s.memories[0],
    b = branchOf(s);
  useMemory(s, b.id, m.id);
  useMemory(s, b.id, m.id);
  assert.equal(b.memoryIds.filter((x) => x === m.id).length, 1);
  const p = structuredClone(b.snapshot.protocol);
  p.dataset = "new input distribution";
  reviseProtocol(s, b.id, p, "new");
  assert.match(memoryFit(m, b), /条件已变化/);
  const child = forkBranch(s, b.id, {
    title: "Another path",
    hypothesis: "Retest the finding",
  });
  assert.ok(child.snapshot.inheritedMemoryIds.includes(m.id));
});
test("roundtrip keeps per-branch and form drafts, sources, revisions and evidence", () => {
  const s = createDemo();
  s.drafts[s.branch] = {
    text: "Keep this thought",
    refs: [{ kind: "run", id: s.runs[0].id }],
  };
  s.drafts["form:fork"] = { title: ["Next idea"], runIds: [s.runs[0].id] };
  const restored = decodeState(JSON.stringify(s));
  assert.deepEqual(restored, s);
});
test("malformed backups cannot replace usable records", () => {
  const cases = [
    (s) => {
      s.version = 999;
    },
    (s) => {
      s.branches[0].parentId = s.branches[1].id;
    },
    (s) => {
      s.runs[0].result.metric = "100";
    },
    (s) => {
      s.sources[0].url = "javascript:alert(1)";
    },
    (s) => {
      s.drafts[s.branch] = { text: "a", refs: null };
    },
    (s) => {
      s.memories[0].runIds = ["run-99999"];
    },
    (s) => {
      s.branches[0].snapshot.referenceRunId = "run-99999";
    },
    (s) => {
      s.branches[0].sourceIds = ["source-99999"];
    },
    (s) => {
      s.branches[0].id = s.branches[1].id;
    },
  ];
  for (const mutate of cases) {
    const s = createDemo();
    mutate(s);
    assert.throws(() => decodeState(JSON.stringify(s)));
  }
  assert.throws(() => decodeState("x"));
  assert.throws(() => decodeState(" ".repeat(5000001)));
  assert.doesNotThrow(() => validateState(createDemo()));
});
test("untrusted titles and URLs cannot become active markup", () => {
  assert.equal(
    escapeHTML('<img onerror="alert(1)">&'),
    "&lt;img onerror=&quot;alert(1)&quot;&gt;&amp;",
  );
  assert.equal(safeURL("javascript:alert(1)"), "");
  assert.equal(safeURL("data:text/html,hi"), "");
  assert.equal(safeURL("https://example.com/a"), "https://example.com/a");
});
test("result JSON accepts measured fields and rejects mismatched protocols", () => {
  const data = result({ protocol: protocol() });
  assert.deepEqual(readResultJSON(JSON.stringify(data), protocol()), data);
  data.protocol.environment = "another environment";
  assert.throws(
    () => readResultJSON(JSON.stringify(data), protocol()),
    /评测条件/,
  );
  assert.throws(
    () => readResultJSON(JSON.stringify(result({ metric: "100" })), protocol()),
    /数值/,
  );
  assert.throws(() => readResultJSON("x".repeat(262145), protocol()), /256 KB/);
});
test("run labels remain unambiguous across branches in one study", () => {
  const s = fresh(),
    root = branchOf(s);
  const a = importResult(s, root.id, result());
  const b = forkBranch(s, root.id, {
    title: "A",
    hypothesis: "Compare",
    runId: a.id,
  });
  const candidate = importResult(s, b.id, result({ metric: 120 }));
  startExploration(
    s,
    root.id,
    { maxRuns: 1, minutes: 1, instruction: "Demo" },
    0,
  );
  assert.equal(a.label, "R01");
  assert.equal(candidate.label, "R02");
  assert.equal(s.runs.at(-1).label, "R03");
});
test("backup validation rejects malformed histories before rendering", () => {
  for (const key of ["branches", "memories"]) {
    const s = createDemo();
    s[key][0].history = [null];
    assert.throws(() => decodeState(JSON.stringify(s)), /历史/);
  }
});
