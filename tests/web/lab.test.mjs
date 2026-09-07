import test from "node:test";
import assert from "node:assert/strict";
import {
  escapeHTML,
  inLabScope,
} from "../../docs/prototypes/inference-world/lab.mjs";

test("agent results and identities are escaped before HTML rendering", () => {
  assert.equal(
    escapeHTML('<img src=x onerror="alert(1)">&'),
    "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;",
  );
});
test("live messages stay in their originating room and goal", () => {
  const job = { context: { room_id: "a", goal_id: "one" } };
  assert.ok(inLabScope(job, { room_id: "a", goal_id: "one" }));
  assert.equal(inLabScope(job, { room_id: "a", goal_id: "two" }), false);
  assert.equal(inLabScope(job, { room_id: "b", goal_id: "one" }), false);
  assert.equal(inLabScope(job, { room_id: "a", goal_id: "" }), false);
});
