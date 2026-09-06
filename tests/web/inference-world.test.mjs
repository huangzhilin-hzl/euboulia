import test from "node:test";
import assert from "node:assert/strict";
import {
  createWorld,
  byId,
  eligibility,
  startTask,
  finishTask,
  addHypothesis,
  addDependency,
  levels,
  setBudget,
  evaluatePractice,
  trialPractice,
  promotePractice,
  rollbackPractice,
} from "../../docs/prototypes/inference-world/model.mjs";

test("artifact dependencies and human-only admission both govern readiness", () => {
  const world = createWorld();
  assert.match(eligibility(world, "V1", "julian").join(" "), /等待 T1/);
  byId(world, "S1").status = "pending";
  assert.match(eligibility(world, "S1", "atlas").join(" "), /限真人/);
  startTask(world, "S1", "julian");
  assert.equal(byId(world, "S1").owner, "julian");
});

test("parallel profile uses another exclusive pool; offline does not free a running lease", () => {
  const world = createWorld();
  startTask(world, "P1", "prism");
  assert.equal(byId(world, "T1").resource, "xx1");
  assert.equal(byId(world, "P1").resource, "xx2");
  world.resources[1].online = false;
  addHypothesis(world, "第三个测量");
  for (const id of ["H3", "W3"]) {
    startTask(world, id, "prism");
    finishTask(world, id);
  }
  assert.match(eligibility(world, "T3", "atlas").join(" "), /等待空闲/);
  finishTask(world, "P1", "failure");
  assert.equal(world.resources[1].lease, null);
  assert.match(eligibility(world, "T3", "atlas").join(" "), /等待空闲/);
});

test("budget admission includes reservations and retry settles without deleting history", () => {
  const world = createWorld();
  setBudget(world, "atlas", 27000);
  assert.match(eligibility(world, "P1", "atlas").join(" "), /预算不足/);
  finishTask(world, "T1", "failure");
  assert.equal(world.members.find((m) => m.id === "atlas").reserved, 0);
  assert.equal(world.resources[0].lease, null);
  setBudget(world, "atlas", 30000);
  startTask(world, "T1", "atlas");
  finishTask(world, "T1");
  assert.deepEqual(
    byId(world, "T1").history.map((h) => h.status),
    ["failed", "completed"],
  );
  assert.equal(
    byId(world, "T1").history[0].result.error,
    "演示构建或运行失败，未形成有效比较",
  );
  assert.equal(byId(world, "T1").attempt, 2);
});

test("invalid edits are atomic; completed and running inputs cannot drift", () => {
  const world = createWorld(),
    before = structuredClone(world);
  assert.throws(() => addDependency(world, "V1", "H1"), /形成环/);
  assert.deepEqual(world, before);
  assert.throws(() => addDependency(world, "H2", "T1"), /不能改写/);
  assert.throws(() => addDependency(world, "H2", "H2"), /形成环/);
  addDependency(world, "P1", "H2");
  assert.equal(world.revision, 2);
  assert.ok(levels(world).get("H2") > levels(world).get("P1"));
});

test("valid measurement with regression completes work but cannot promote a candidate", () => {
  const world = createWorld();
  finishTask(world, "T1", "regression");
  assert.equal(byId(world, "T1").status, "completed");
  startTask(world, "V1", "julian");
  finishTask(world, "V1");
  assert.equal(byId(world, "V1").result.accepted, false);
  assert.equal(world.champion, "B0");
  assert.match(eligibility(world, "E1", "julian").join(" "), /等待 P1/);
});

test("a winning round continues from its champion while retaining B0 and fresh profile", () => {
  const world = createWorld();
  finishTask(world, "T1");
  startTask(world, "V1", "julian");
  finishTask(world, "V1");
  assert.equal(world.champion, "T1");
  assert.throws(() => addHypothesis(world, "下一轮", "joint", "E1"), /先完成/);
  startTask(world, "P1", "prism");
  finishTask(world, "P1");
  startTask(world, "E1", "julian");
  finishTask(world, "E1");
  assert.equal(world.experiences[0].status, "candidate");
  addHypothesis(world, "下一轮", "joint", "E1");
  assert.deepEqual(byId(world, "H3").deps, ["E1"]);
  assert.deepEqual(byId(world, "T3").deps, ["W3", "T1", "B0"]);
  assert.equal(byId(world, "T3").parentBenchmark, "T1");
  assert.equal(byId(world, "P3").status, "pending");
  assert.ok(!byId(world, "V3").deps.includes("P3"));
  for (const id of ["H3", "W3", "T3", "V3"]) {
    startTask(world, id, ["H3", "V3"].includes(id) ? "julian" : "prism");
    finishTask(world, id, id === "T3" ? "regression" : "success");
  }
  assert.equal(byId(world, "V3").result.accepted, false);
  assert.equal(world.champion, "T1");
});

test("practice requires independent evaluation and trial; promotion pins new tasks only", () => {
  const world = createWorld();
  assert.throws(() => promotePractice(world), /独立评测/);
  assert.throws(() => trialPractice(world), /独立评测/);
  evaluatePractice(world, false);
  assert.throws(() => promotePractice(world), /独立评测/);
  evaluatePractice(world, true);
  assert.throws(() => promotePractice(world), /有限试用/);
  trialPractice(world);
  promotePractice(world);
  addHypothesis(world, "采用新方法");
  assert.equal(byId(world, "T1").practiceVersion, "v1");
  assert.equal(byId(world, "T3").practiceVersion, "v2");
  rollbackPractice(world);
  addHypothesis(world, "回滚后的方法");
  assert.equal(byId(world, "T4").practiceVersion, "v1");
  assert.equal(byId(world, "T3").practiceVersion, "v2");
  assert.deepEqual(
    world.practice.history.map((h) => h.to),
    ["v2", "v1"],
  );
  evaluatePractice(world, false);
  assert.equal(world.practice.trial, null);
});

test("a branch using an older parent cannot overwrite a newer champion without comparison", () => {
  const world = createWorld();
  addHypothesis(world, "并行方案");
  finishTask(world, "T1");
  startTask(world, "V1", "julian");
  finishTask(world, "V1");
  for (const id of ["H3", "W3", "T3", "V3"]) {
    startTask(world, id, ["H3", "V3"].includes(id) ? "julian" : "prism");
    finishTask(world, id);
  }
  assert.equal(byId(world, "V3").result.accepted, true);
  assert.equal(byId(world, "V3").result.needsChampionComparison, true);
  assert.equal(world.champion, "T1");
});
