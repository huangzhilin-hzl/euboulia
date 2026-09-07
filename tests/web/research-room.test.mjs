import test from "node:test";
import assert from "node:assert/strict";
import {
  createRoom,
  sendMessage,
  chooseDirection,
  updateTask,
  setGpuOnline,
  restoreRoom,
} from "../../docs/prototypes/inference-world/room-state.mjs";

test("a message becomes work with the same origin, room, assignee and evidence", () => {
  const s = createRoom();
  s.room = "kernels";
  const message = sendMessage(s, {
    text: "检查等待原因",
    asTask: true,
    owner: "prism",
    refs: ["trace", "trace", "missing"],
  });
  const task = s.tasks.find((t) => t.source === message.id);
  assert.equal(task.room, "kernels");
  assert.equal(task.owner, "prism");
  assert.equal(task.id, message.task);
  assert.deepEqual(task.refs, ["trace"]);
  assert.equal(task.state, "queued");
  assert.match(s.messages.at(-1).text, /接入真实 runtime/);
});

test("selecting a direction is idempotent and keeps the old evidence", () => {
  const s = createRoom(),
    before = structuredClone(s.messages);
  chooseDirection(s, "ranks");
  chooseDirection(s, "mhc");
  assert.equal(s.tasks.length, 4);
  assert.equal(s.decision, "ranks");
  assert.deepEqual(s.messages.slice(0, before.length), before);
  assert.deepEqual(s.tasks.at(-1).refs, ["trace"]);
});

test("GPU disconnect blocks GPU work while computer analysis stays available", () => {
  const s = createRoom();
  setGpuOnline(s, false);
  chooseDirection(s, "ranks");
  assert.equal(s.tasks.find((t) => t.id === "profile").state, "running");
  assert.equal(s.tasks.at(-1).state, "blocked");
  updateTask(s, s.tasks.at(-1).id, "pause");
  setGpuOnline(s, true);
  assert.equal(s.tasks.at(-1).state, "paused");
  updateTask(s, s.tasks.at(-1).id, "resume");
  assert.equal(s.tasks.at(-1).state, "queued");
});

test("resumed GPU work must queue again and cannot overlap an active measurement", () => {
  const s = createRoom();
  chooseDirection(s, "ranks");
  const rankTask = s.tasks.at(-1).id;
  updateTask(s, "correctness", "start");
  updateTask(s, "correctness", "pause");
  updateTask(s, rankTask, "start");
  updateTask(s, "correctness", "resume");
  assert.equal(s.tasks.find((t) => t.id === "correctness").state, "queued");
  assert.throws(() => updateTask(s, "correctness", "start"), /独占/);
  assert.equal(s.tasks.filter((t) => t.gpu && t.state === "running").length, 1);
});

test("empty submissions create no work; local drafts and history survive reload", () => {
  const s = createRoom(),
    count = s.messages.length;
  assert.equal(sendMessage(s, { text: "  ", asTask: true }), null);
  assert.equal(s.messages.length, count);
  s.drafts.dsv4 = { text: "未发出的方向", refs: ["trace"], asTask: true };
  sendMessage(s, { text: "<script>literal text</script>" });
  const restored = restoreRoom(JSON.stringify(s));
  assert.deepEqual(restored, s);
  assert.equal(restored.messages.at(-2).text, "<script>literal text</script>");
  assert.equal(restoreRoom("{broken").schema, 2);
  assert.equal(restoreRoom('{"schema":1}').tasks.length, 3);
});
