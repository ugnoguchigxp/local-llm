import assert from "node:assert/strict";
import test from "node:test";

import { EventMapper } from "../src/event_mapper.js";

test("maps agent message deltas and authoritative completion", () => {
  const mapper = new EventMapper();
  mapper.map("item/started", {
    sessionId: "s1",
    viewCursor: "c1",
    item: { itemId: "i1", turnId: "t1", kind: "agentMessage", status: "inProgress", text: "" },
  });
  const delta = mapper.map("item/delta", {
    sessionId: "s1",
    viewCursor: "c2",
    itemId: "i1",
    field: "text",
    delta: "hello",
  });
  assert.equal(delta?.type, "message.delta");
  assert.equal(delta?.native_turn_id, "t1");
  assert.equal(delta?.data["text"], "hello");
  const completed = mapper.map("item/completed", {
    sessionId: "s1",
    viewCursor: "c3",
    item: { itemId: "i1", turnId: "t1", kind: "agentMessage", status: "completed", text: "hello" },
  });
  assert.equal(completed?.type, "message.completed");
  assert.equal(completed?.data["text"], "hello");
});

test("maps cancelled and unqueued turns to terminal events", () => {
  const mapper = new EventMapper();
  const cancelled = mapper.map("turn/completed", {
    sessionId: "s1",
    turnId: "t1",
    viewCursor: "c1",
    terminal: "cancelled",
  });
  const unqueued = mapper.map("turn/unqueued", {
    sessionId: "s1",
    turnId: "t2",
    viewCursor: "c2",
  });
  assert.equal(cancelled?.type, "turn.cancelled");
  assert.equal(unqueued?.type, "turn.unqueued");
});

test("does not report unknown turn terminals as successful", () => {
  const mapper = new EventMapper();
  const unknown = mapper.map("turn/completed", {
    sessionId: "session-a",
    turnId: "turn-a",
    viewCursor: "c1",
    terminal: "future-terminal",
  });
  const malformed = mapper.map("turn/completed", {
    sessionId: "session-a",
    turnId: "turn-a",
    viewCursor: "c2",
  });

  assert.equal(unknown?.type, "turn.failed");
  assert.equal(malformed?.type, "session.recovery_required");
});

test("approval event omits rawArgs", () => {
  const event = new EventMapper().map("approval/requested", {
    sessionId: "s1",
    turnId: "t1",
    viewCursor: "c1",
    approvalId: "a1",
    rawArgs: "secret command",
    availableChoices: [{ choiceId: "once", decision: "approved", scope: "once" }],
  });
  assert.equal(event?.type, "approval.requested");
  assert.equal("rawArgs" in (event?.data ?? {}), false);
  assert.deepEqual(event?.data["available_choices"], ["allow_once"]);
});

test("maps server-request interaction methods", () => {
  const mapper = new EventMapper();
  const approval = mapper.map("approval/request", {
    sessionId: "s1",
    turnId: "t1",
    viewCursor: "c1",
    approvalId: "a1",
    availableChoices: [],
  });
  const userInput = mapper.map("userInput/request", {
    sessionId: "s1",
    turnId: "t1",
    viewCursor: "c2",
    userInputId: "u1",
    questions: [{
      id: "q1",
      header: "Choice",
      question: "Pick one",
      options: [{ label: "A", description: "First", preview: { format: "text", content: "private" } }],
      selection: { mode: "single", minSelections: 1, maxSelections: 1 },
    }],
  });

  assert.equal(approval?.type, "approval.requested");
  assert.equal(userInput?.type, "user_input.requested");
  assert.equal(JSON.stringify(userInput?.data).includes("private"), false);
  assert.deepEqual(userInput?.data["questions"], [{
    id: "q1",
    question: "Pick one",
    header: "Choice",
    options: [{ label: "A", description: "First" }],
    selection: { mode: "single", min_selections: 1, max_selections: 1 },
  }]);
});

test("keeps item metadata isolated between sessions", () => {
  const mapper = new EventMapper();
  mapper.map("item/started", {
    sessionId: "s1",
    viewCursor: "c1",
    item: { itemId: "shared", turnId: "t1", kind: "agentMessage", status: "inProgress" },
  });
  mapper.map("item/started", {
    sessionId: "s2",
    viewCursor: "c2",
    item: { itemId: "shared", turnId: "t2", kind: "toolCall", status: "inProgress" },
  });

  const delta = mapper.map("item/delta", {
    sessionId: "s1",
    viewCursor: "c3",
    itemId: "shared",
    field: "text",
    delta: "hello",
  });

  assert.equal(delta?.type, "message.delta");
  assert.equal(delta?.native_turn_id, "t1");
});

test("maps delivery gaps and invariant changes to recovery events", () => {
  const mapper = new EventMapper();
  const gap = mapper.map("view/gap", {
    sessionId: "s1",
    after: "safe-cursor",
    next: "next-cursor",
  });
  const changed = mapper.map("session/modelChanged", {
    sessionId: "s1",
    viewCursor: "c2",
    modelId: "unexpected-model",
  });

  assert.equal(gap?.type, "session.recovery_required");
  assert.equal(gap?.native_cursor, "safe-cursor");
  assert.equal(gap?.data["next_cursor"], "next-cursor");
  assert.equal(changed?.type, "session.invariant_changed");
});
