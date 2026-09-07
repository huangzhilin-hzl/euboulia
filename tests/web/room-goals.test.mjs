import test from "node:test";
import assert from "node:assert/strict";
import {
  createRoom,
  createResearchRoom,
  createResearchGoal,
  enterRoom,
  sendMessage,
  scopeKey,
  inScope,
  updateTask,
  updateGoalStatus,
  restoreRoom,
  setGpuOnline,
  SEED_GOAL,
  reviseResearchGoal,
} from "../../docs/prototypes/inference-world/room-state.mjs";

test("a new room starts without another room's context; goal inherits a snapshot", () => {
  const s = createRoom();
  assert.throws(() => createResearchRoom(s, { name: " " }), /名字/);
  assert.throws(() => createResearchRoom(s, { name: "DSV4-ON-H20" }), /同名/);
  const room = createResearchRoom(s, {
    name: "新研究室",
    context: "模型 A / GPU B",
  });
  assert.equal(s.goal, null);
  assert.equal(s.view, "overview");
  const goal = createResearchGoal(s, {
    prompt: "弄清等待原因",
    firstTask: false,
  });
  room.context = "之后的上下文";
  assert.equal(goal.context, "模型 A / GPU B");
  assert.deepEqual(goal.refs, []);
  assert.equal(s.tasks.filter((t) => t.goal === goal.id).length, 0);
  assert.equal(goal.criterion, "");
  assert.throws(() => enterRoom(s, "dsv4", goal.id), /不属于/);
});

test("goals and free discussion keep separate drafts, messages, tasks and artifacts", () => {
  const s = createRoom();
  enterRoom(s, "dsv4");
  const free = sendMessage(s, { text: "一个想法", refs: ["trace"] });
  const generalKey = scopeKey(s);
  s.drafts[generalKey] = { text: "自由讨论草稿" };
  const a = createResearchGoal(s, { prompt: "分析等待", refs: ["trace"] });
  const aKey = scopeKey(s);
  s.drafts[aKey] = { text: "目标 A 草稿" };
  const aMessage = sendMessage(s, { text: "A 的工作", asTask: true });
  const b = createResearchGoal(s, { prompt: "研究访存" });
  assert.notEqual(scopeKey(s), aKey);
  assert.equal(inScope(s, free), false);
  assert.equal(inScope(s, aMessage), false);
  assert.equal(s.tasks.filter((t) => inScope(s, t)).length, 1);
  assert.deepEqual(b.refs, []);
  enterRoom(s, "dsv4", a.id);
  assert.equal(s.drafts[scopeKey(s)].text, "目标 A 草稿");
  enterRoom(s, "dsv4");
  assert.equal(inScope(s, free), true);
  assert.equal(s.drafts[scopeKey(s)].text, "自由讨论草稿");
});

test("converting discussion preserves evidence and origin without duplicating a goal", () => {
  const s = createRoom();
  enterRoom(s, "kernels");
  const message = sendMessage(s, { text: "值得验证的线索", refs: ["trace"] });
  const goal = createResearchGoal(s, {
    prompt: message.text,
    source: message.id,
    refs: ["memory", "bad"],
  });
  const count = s.tasks.length;
  assert.deepEqual(goal.refs, ["memory", "trace"]);
  assert.equal(goal.source, message.id);
  assert.equal(message.derivedGoal, goal.id);
  assert.equal(message.goal, null);
  assert.equal(
    createResearchGoal(s, { prompt: message.text, source: message.id }).id,
    goal.id,
  );
  assert.equal(s.tasks.length, count);
  enterRoom(s, "dsv4");
  assert.throws(
    () => createResearchGoal(s, { prompt: "不同研究室", source: message.id }),
    /不属于/,
  );
});

test("pausing a goal respects manual pauses, other goals and GPU exclusivity on resume", () => {
  const s = createRoom();
  enterRoom(s, "dsv4", SEED_GOAL);
  updateTask(s, "profile", "pause");
  updateTask(s, "correctness", "start");
  updateGoalStatus(s, SEED_GOAL, "pause");
  assert.equal(s.tasks.find((t) => t.id === "correctness").state, "paused");
  assert.throws(
    () => sendMessage(s, { text: "继续实验", asTask: true }),
    /恢复目标/,
  );
  assert.throws(() => updateTask(s, "correctness", "resume"), /恢复目标/);
  sendMessage(s, { text: "暂停时仍可以讨论" });
  const second = createResearchGoal(s, { prompt: "其他目标" });
  const otherTask = s.tasks.at(-1);
  otherTask.gpu = true;
  updateTask(s, otherTask.id, "start");
  updateGoalStatus(s, SEED_GOAL, "resume");
  assert.equal(s.tasks.find((t) => t.id === "profile").state, "paused");
  assert.equal(s.tasks.find((t) => t.id === "correctness").state, "queued");
  assert.throws(() => updateTask(s, "correctness", "start"), /独占/);
  assert.equal(s.messages.at(-1).goal, SEED_GOAL);
  assert.equal(s.goal, second.id);
  updateGoalStatus(s, SEED_GOAL, "pause");
  setGpuOnline(s, false);
  updateGoalStatus(s, SEED_GOAL, "resume");
  assert.equal(s.tasks.find((t) => t.id === "correctness").state, "blocked");
});

test("a goal needs finished work and a conclusion; delivery is recorded in its own thread", () => {
  const s = createRoom();
  const goal = createResearchGoal(s, { prompt: "检查测量协议" });
  const task = s.tasks.at(-1);
  assert.throws(
    () => updateGoalStatus(s, goal.id, "complete", "结束"),
    /交付或取消/,
  );
  updateTask(s, task.id, "start");
  assert.throws(() => updateTask(s, task.id, "complete", "  "), /交付说明/);
  enterRoom(s, "kernels");
  updateTask(s, task.id, "complete", "缺少可比数据，需要后续补测。");
  assert.equal(s.messages.at(-1).goal, goal.id);
  assert.equal(s.messages.at(-1).room, "dsv4");
  assert.throws(() => updateGoalStatus(s, goal.id, "complete", ""), /记录结论/);
  updateGoalStatus(s, goal.id, "complete", "尚未证明优化有效。");
  assert.equal(goal.status, "completed");
  assert.match(s.messages.at(-1).text, /不代表性能验收通过/);
  updateGoalStatus(s, goal.id, "reopen");
  assert.equal(goal.status, "active");
  assert.equal(goal.conclusion, "尚未证明优化有效。");
  assert.equal(task.state, "done");
});

test("cancelling paused work does not revive it when the goal resumes", () => {
  const s = createRoom();
  const goal = createResearchGoal(s, { prompt: "试一条线索" });
  const task = s.tasks.at(-1);
  updateGoalStatus(s, goal.id, "pause");
  updateTask(s, task.id, "cancel");
  updateGoalStatus(s, goal.id, "resume");
  assert.equal(task.state, "cancelled");
  updateGoalStatus(s, goal.id, "complete", "这条线索暂不继续。");
  assert.equal(goal.status, "completed");
});

test("legacy v2 migrates history and drafts; v3 round-trips custom rooms, goals and form drafts", () => {
  const legacy = createRoom();
  legacy.schema = 2;
  delete legacy.rooms;
  delete legacy.goals;
  delete legacy.goal;
  legacy.drafts = {
    dsv4: { text: "旧草稿", refs: ["trace"] },
    kernels: { text: "另一个草稿" },
  };
  for (const item of [...legacy.tasks, ...legacy.messages]) delete item.goal;
  const s = restoreRoom(JSON.stringify(legacy));
  assert.equal(s.schema, 3);
  assert.equal(s.tasks[0].goal, SEED_GOAL);
  assert.equal(s.drafts[`dsv4:${SEED_GOAL}`].text, "旧草稿");
  assert.equal(s.drafts["kernels:general"].text, "另一个草稿");
  createResearchRoom(s, { name: "自建研究室" });
  createResearchGoal(s, { prompt: "<script>只是文本</script>" });
  s.forms["room:new"] = { name: "尚未创建" };
  s.drafts[scopeKey(s)] = { text: "尚未发送" };
  assert.deepEqual(restoreRoom(JSON.stringify(s)), s);
  const corrupt = structuredClone(s);
  corrupt.tasks.at(-1).room = "kernels";
  assert.equal(restoreRoom(JSON.stringify(corrupt)).rooms.length, 2);
  assert.equal(restoreRoom("{").schema, 3);
});

test("revising a goal preserves old task contracts and records the new version for future work", () => {
  const s = createRoom();
  const seed = s.goals.find((g) => g.id === SEED_GOAL);
  const seedTitle = seed.title;
  reviseResearchGoal(s, seed.id, {
    prompt: seed.prompt,
    criterion: "补充原目标标准",
    budget: seed.budget,
  });
  assert.equal(seed.title, seedTitle);
  const goal = createResearchGoal(s, {
    prompt: "解释等待",
    criterion: "先看 C1",
    budget: "先分析",
  });
  const original = structuredClone(s.tasks.at(-1));
  reviseResearchGoal(s, goal.id, {
    prompt: "解释等待",
    criterion: "覆盖 C1 和 C16",
    budget: "两小时",
  });
  assert.equal(goal.revision, 2);
  assert.equal(goal.history[0].criterion, "先看 C1");
  assert.deepEqual(s.tasks.at(-1), original);
  sendMessage(s, { text: "按新边界补充实验", asTask: true });
  assert.equal(s.tasks.at(-1).goalRevision, 2);
  assert.equal(original.goalRevision, 1);
  reviseResearchGoal(s, goal.id, {
    prompt: goal.prompt,
    criterion: goal.criterion,
    budget: goal.budget,
  });
  assert.equal(goal.revision, 2);
  assert.deepEqual(restoreRoom(JSON.stringify(s)), s);
});
