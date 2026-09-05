import assert from "node:assert/strict";
import { once } from "node:events";
import { chmod, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import test from "node:test";

import { EXPECTED_SCHEMA_FINGERPRINT } from "@muse-code/sdk";

interface Frame {
  readonly id?: string;
  readonly event?: boolean;
  readonly ok?: boolean;
  readonly type?: string;
  readonly result?: Record<string, unknown>;
  readonly data?: Record<string, unknown>;
}

test("bridge drives the official SDK against an MSP host", async (context) => {
  const directory = await mkdtemp(join(tmpdir(), "local-llm-muse-bridge-"));
  const hostPath = join(directory, "fake-muse-host.mjs");
  await writeFile(hostPath, fakeHostSource(), { encoding: "utf8", mode: 0o700 });
  await chmod(hostPath, 0o700);
  const bridgePath = fileURLToPath(new URL("../src/main.js", import.meta.url));
  const child = spawn(process.execPath, [bridgePath], {
    env: { PATH: process.env["PATH"] ?? "/usr/bin:/bin", HOME: directory },
    stdio: ["pipe", "pipe", "pipe"],
  });
  context.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    if (child.exitCode === null && child.signalCode === null) await once(child, "close");
  });
  child.stdout.setEncoding("utf8");
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    stderr += chunk;
  });
  const frames: Frame[] = [];
  const waiters = new Map<string, (frame: Frame) => void>();
  lines.on("line", (line) => {
    const frame = JSON.parse(line) as Frame;
    frames.push(frame);
    if (frame.id !== undefined) waiters.get(frame.id)?.(frame);
  });

  let sequence = 0;
  async function request(method: string, params: Record<string, unknown> = {}): Promise<Frame> {
    sequence += 1;
    const id = `brq_${String(sequence)}`;
    const response = new Promise<Frame>((resolve) => waiters.set(id, resolve));
    child.stdin.write(`${JSON.stringify({ v: 1, id, method, params })}\n`);
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(`bridge request ${method} timed out: ${stderr}`)), 3000);
    });
    const frame = await Promise.race([response, timeout]).finally(() => {
      if (timer !== undefined) clearTimeout(timer);
    });
    waiters.delete(id);
    return frame;
  }

  const initialized = await request("runtime.initialize", {
    muse_binary: hostPath,
    expected_fingerprint: EXPECTED_SCHEMA_FINGERPRINT,
    shutdown_timeout_ms: 1000,
    approval_timeout_ms: 100,
    sdk_version: "0.1.1",
  });
  assert.equal(initialized.ok, true);
  assert.equal(initialized.result?.["schema_fingerprint"], EXPECTED_SCHEMA_FINGERPRINT);
  await new Promise<void>((resolve) => setTimeout(resolve, 50));
  assert.equal(stderr.includes("MUSE_RAW_SECRET_TAIL"), false);
  assert.equal(stderr.includes("Muse diagnostic line exceeded the safe buffer"), true);
  assert.equal(stderr.includes("after-oversize"), true);
  const invalidHealth = await request("runtime.health", { unexpected: true });
  assert.equal(invalidHealth.ok, false);

  const models = await request("models.list");
  assert.equal((models.result?.["models"] as unknown[]).length, 1);
  const session = await request("session.start", {
    workspace_root: directory,
    model_id: "model-a",
    provider_id: "provider-a",
    approval_mode: "onRequest",
    command_id: "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f10",
  });
  assert.equal(session.result?.["native_session_id"], "native-session");
  const resumed = await request("session.resume", {
    native_session_id: "native-session",
    cursor: "c0",
    command_id: "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f13",
  });
  assert.equal(resumed.result?.["native_session_id"], "native-session");
  const turn = await request("turn.start", {
    native_session_id: "native-session",
    text: "hello",
    command_id: "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f11",
  });
  assert.equal(turn.result?.["native_turn_id"], "native-turn");
  const allowed = await request("approval.decide", {
    native_session_id: "native-session",
    approval_id: "approval-user",
    decision: "allow_once",
    command_id: "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f12",
  });
  assert.equal(allowed.ok, true);
  const deniedSecondStage = await request("approval.decide", {
    native_session_id: "native-session",
    approval_id: "approval-user",
    decision: "deny",
    command_id: "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f16",
  });
  assert.equal(deniedSecondStage.ok, true);
  const answered = await request("user_input.answer", {
    native_session_id: "native-session",
    user_input_id: "input-user",
    answers: [{ questionId: "question-1", freeText: "answer" }],
    command_id: "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f14",
  });
  assert.equal(answered.ok, true);
  const cancelled = await request("turn.cancel", {
    native_session_id: "native-session",
    native_turn_id: "native-turn",
    command_id: "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f15",
  });
  assert.equal(cancelled.result?.["native_turn_id"], "native-turn");
  const page = await request("events.page", {
    native_session_id: "native-session",
    cursor: "c0",
    limit: 10,
  });
  assert.equal((page.result?.["events"] as unknown[]).length, 1);
  assert.equal(page.result?.["next_cursor"], null);
  await new Promise<void>((resolve) => setTimeout(resolve, 200));
  assert.equal(frames.some((frame) => frame.event === true && frame.type === "message.delta"), true);
  assert.equal(frames.some((frame) => frame.event === true && frame.type === "turn.completed"), true);
  assert.equal(
    frames.some((frame) => frame.event === true && frame.type === "approval.requested"),
    true,
    `missing approval request; frames=${JSON.stringify(frames)} stderr=${stderr}`,
  );
  assert.equal(
    frames.some(
      (frame) =>
        frame.event === true &&
        frame.type === "approval.resolved" &&
        frame.data?.["approval_id"] === "approval-user" &&
        frame.data?.["decision"] === "denied",
    ),
    true,
  );
  assert.equal(frames.some((frame) => frame.event === true && frame.type === "user_input.requested"), true);
  assert.equal(frames.some((frame) => frame.event === true && frame.type === "user_input.resolved"), true);
  assert.equal(
    frames.some(
      (frame) =>
        frame.event === true &&
        frame.type === "approval.resolved" &&
        frame.data?.["approval_id"] === "approval-timeout" &&
        frame.data?.["decision"] === "denied",
    ),
    true,
  );

  await request("session.release", { native_session_id: "native-session" });
  const shutdown = await request("runtime.shutdown");
  assert.equal(shutdown.result?.["status"], "closed");
  if (child.exitCode === null) await once(child, "close");
  assert.equal(child.exitCode, 0);
});

function fakeHostSource(): string {
  return `#!/usr/bin/env node
import { createInterface } from "node:readline";
const fingerprint = ${JSON.stringify(EXPECTED_SCHEMA_FINGERPRINT)};
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
function send(frame) { process.stdout.write(JSON.stringify(frame) + "\\n"); }
let approvalUserStage = 1;
process.stderr.write("x".repeat(70000) + "MUSE_RAW_SECRET_TAIL\\n");
process.stderr.write("after-oversize\\n");
for await (const line of input) {
  const frame = JSON.parse(line);
  if (frame.method === undefined) continue;
  if (frame.method === "initialized") continue;
  if (frame.method === "initialize") {
    send({ jsonrpc: "2.0", id: frame.id, result: {
      experimentalApi: false,
      grantedCapabilities: [],
      museHome: process.env.HOME,
      platformFamily: "unix",
      platformOs: "macos",
      schema: { schemaVersion: 1, fingerprint },
      serverInfo: { name: "fake-muse", version: "1.0.0" },
      sessionDurability: "durable",
      userAgent: "fake"
    }});
    continue;
  }
  if (frame.method === "model/list") {
    send({ jsonrpc: "2.0", id: frame.id, result: { providerId: "provider-a", profileId: null, source: "fakeCatalog", models: [{ providerId: "provider-a", modelId: "model-a", displayLabel: "Model A", isActive: false, isDefault: true, contextLimit: 1000, outputLimit: 100, cost: null, profileId: null, releaseDate: null, description: null }] }});
    continue;
  }
  if (frame.method === "session/start") {
    send({ jsonrpc: "2.0", id: frame.id, result: { session: { sessionId: "native-session", status: "idle", modelId: "model-a", providerId: "provider-a" }, viewCursor: "c0" }});
    continue;
  }
  if (frame.method === "session/resume") {
    send({ jsonrpc: "2.0", id: frame.id, result: { session: { sessionId: frame.params.sessionId, status: "idle", modelId: "model-a", providerId: "provider-a" }, viewCursor: "c0" }});
    continue;
  }
  if (frame.method === "turn/start") {
    send({ jsonrpc: "2.0", id: 900, method: "approval/request", params: { sessionId: "native-session", turnId: "native-turn", viewCursor: "c1a", approvalId: "approval-user", currentRequirementId: { approvalId: "approval-user", sourceIndex: 1 }, availableChoices: [{ choiceId: "allow-user", decision: "approved", scope: "once" }, { choiceId: "deny-user", decision: "denied", scope: "once" }] } });
    send({ jsonrpc: "2.0", id: 901, method: "approval/request", params: { sessionId: "native-session", turnId: "native-turn", viewCursor: "c1b", approvalId: "approval-timeout", currentRequirementId: { approvalId: "approval-timeout", sourceIndex: 1 }, availableChoices: [{ choiceId: "deny-timeout", decision: "denied", scope: "once" }] } });
    send({ jsonrpc: "2.0", id: 902, method: "userInput/request", params: { sessionId: "native-session", turnId: "native-turn", viewCursor: "c1c", userInputId: "input-user", questions: [{ id: "question-1", header: "Answer", question: "Answer?", options: [], selection: { mode: "single", minSelections: 0, maxSelections: 1 } }] } });
    send({ jsonrpc: "2.0", id: frame.id, result: { commandId: frame.params.commandId, status: "accepted", disposition: "started", startedNewTurn: true, turnId: "native-turn" }});
    send({ jsonrpc: "2.0", method: "turn/started", params: { sessionId: "native-session", turnId: "native-turn", commandId: frame.params.commandId, sourceRange: { first: 1, last: 1 }, viewCursor: "c1" } });
    send({ jsonrpc: "2.0", method: "item/started", params: { sessionId: "native-session", viewCursor: "c2", item: { itemId: "item-1", turnId: "native-turn", kind: "agentMessage", status: "inProgress", revision: 1, text: "" } } });
    send({ jsonrpc: "2.0", method: "item/delta", params: { sessionId: "native-session", viewCursor: "c3", itemId: "item-1", field: "text", delta: "hello" } });
    send({ jsonrpc: "2.0", method: "item/completed", params: { sessionId: "native-session", sourceRange: { first: 2, last: 2 }, viewCursor: "c4", item: { itemId: "item-1", turnId: "native-turn", kind: "agentMessage", status: "completed", revision: 2, text: "hello" } } });
    send({ jsonrpc: "2.0", method: "turn/completed", params: { sessionId: "native-session", turnId: "native-turn", terminal: "completed", sourceRange: { first: 3, last: 3 }, viewCursor: "c5" } });
    continue;
  }
  if (frame.method === "approval/decide") {
    if (frame.params.approvalId === "approval-user" && approvalUserStage === 1) {
      approvalUserStage = 2;
      send({ jsonrpc: "2.0", method: "approval/updated", params: { sessionId: "native-session", approvalId: "approval-user", viewCursor: "c5a", currentRequirementId: { approvalId: "approval-user", sourceIndex: 2 }, availableChoices: [{ choiceId: "allow-user-2", decision: "approved", scope: "once" }, { choiceId: "deny-user-2", decision: "denied", scope: "once" }], subject: { kind: "tool" }, sourceRange: { first: 4, last: 4 } } });
      send({ jsonrpc: "2.0", id: frame.id, result: { approvalId: frame.params.approvalId, commandId: frame.params.commandId, status: "accepted", terminal: false } });
      continue;
    }
    send({ jsonrpc: "2.0", id: frame.id, result: { approvalId: frame.params.approvalId, commandId: frame.params.commandId, status: "accepted", terminal: true } });
    const decision = frame.params.choiceId.startsWith("allow-") ? "approved" : "denied";
    const cursor = frame.params.approvalId === "approval-user" ? "c6" : "c7";
    send({ jsonrpc: "2.0", method: "approval/resolved", params: { sessionId: "native-session", turnId: "native-turn", approvalId: frame.params.approvalId, decision, resolvedBy: "client", viewCursor: cursor } });
    continue;
  }
  if (frame.method === "userInput/answer") {
    send({ jsonrpc: "2.0", id: frame.id, result: { commandId: frame.params.commandId, status: "accepted", userInputId: frame.params.userInputId } });
    send({ jsonrpc: "2.0", method: "userInput/settled", params: { sessionId: "native-session", turnId: "native-turn", userInputId: frame.params.userInputId, outcome: "answered", viewCursor: "c8" } });
    continue;
  }
  if (frame.method === "turn/cancel") {
    send({ jsonrpc: "2.0", id: frame.id, result: { commandId: frame.params.commandId, status: "accepted", turnId: frame.params.turnId } });
    continue;
  }
  if (frame.method === "view/page") {
    send({ jsonrpc: "2.0", id: frame.id, result: { events: [{ method: "turn/started", params: { sessionId: frame.params.sessionId, turnId: "native-turn", viewCursor: "page-c1" } }], nextCursor: null } });
    continue;
  }
  if (frame.method === "view/unsubscribe") {
    send({ jsonrpc: "2.0", id: frame.id, result: {} });
    continue;
  }
  send({ jsonrpc: "2.0", id: frame.id, error: { code: -32601, message: "unknown", data: { kind: "methodNotFound" } } });
}
`;
}
