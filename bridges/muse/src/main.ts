import { once } from "node:events";
import { createInterface } from "node:readline";

import {
  EXPECTED_SCHEMA_FINGERPRINT,
  MspError,
  ProtocolError,
  spawnMspConnection,
} from "@muse-code/sdk";
import type { MspHandshake, SpawnedMspConnection } from "@muse-code/sdk";

import { EventMapper } from "./event_mapper.js";
import {
  BridgeRequestError,
  isRecord,
  optionalString,
  parseBridgeRequest,
  rejectUnknownFields,
  requireArray,
  requireInteger,
  requireString,
} from "./protocol.js";
import { redactText, redactValue } from "./redact.js";

const BRIDGE_VERSION = 1;
const SDK_VERSION = "0.1.1";
const MAX_LINE_BYTES = 10 * 1024 * 1024;
const MAX_DIAGNOSTIC_BUFFER = 64 * 1024;
const MAX_PENDING_OUTPUT_BYTES = 16 * 1024 * 1024;

interface PendingApproval {
  readonly sessionId: string;
  readonly approvalId: string;
  readonly requirementId: unknown;
  readonly choices: readonly unknown[];
  timer: NodeJS.Timeout | undefined;
  resolving: boolean;
}

interface PendingUserInput {
  readonly sessionId: string;
  readonly questionIds: ReadonlySet<string>;
}

let host: SpawnedMspConnection | undefined;
let handshake: MspHandshake | undefined;
let shutdownRequested = false;
let writeTail = Promise.resolve();
let pendingOutputBytes = 0;
const mapper = new EventMapper();
const approvals = new Map<string, PendingApproval>();
const userInputs = new Map<string, PendingUserInput>();
let approvalTimeoutMs = 300_000;
let museDiagnosticBuffer = "";
let museDiagnosticDiscardingLine = false;
let fatalTriggered = false;

function recordMuseStderr(rawChunk: string): void {
  let chunk = rawChunk;
  if (museDiagnosticDiscardingLine) {
    const newline = chunk.indexOf("\n");
    if (newline < 0) return;
    chunk = chunk.slice(newline + 1);
    museDiagnosticDiscardingLine = false;
  }
  museDiagnosticBuffer += chunk;
  let newline = museDiagnosticBuffer.indexOf("\n");
  while (newline >= 0) {
    const line = museDiagnosticBuffer.slice(0, newline + 1);
    museDiagnosticBuffer = museDiagnosticBuffer.slice(newline + 1);
    if (line.length > MAX_DIAGNOSTIC_BUFFER) {
      process.stderr.write("Muse diagnostic line exceeded the safe buffer and was discarded.\n");
    } else {
      process.stderr.write(redactText(line));
    }
    newline = museDiagnosticBuffer.indexOf("\n");
  }
  if (museDiagnosticBuffer.length > MAX_DIAGNOSTIC_BUFFER) {
    process.stderr.write("Muse diagnostic line exceeded the safe buffer and was discarded.\n");
    museDiagnosticBuffer = "";
    museDiagnosticDiscardingLine = true;
  }
}

function flushMuseStderr(): void {
  if (!museDiagnosticDiscardingLine && museDiagnosticBuffer.length > 0) {
    process.stderr.write(redactText(museDiagnosticBuffer));
  }
  museDiagnosticBuffer = "";
  museDiagnosticDiscardingLine = false;
}

function enqueueFrame(frame: Record<string, unknown>): Promise<void> {
  const encoded = `${JSON.stringify(frame)}\n`;
  const encodedBytes = Buffer.byteLength(encoded, "utf8");
  if (encodedBytes > MAX_LINE_BYTES) {
    return Promise.reject(new BridgeRequestError("output_too_large", "Bridge output frame is too large."));
  }
  if (pendingOutputBytes + encodedBytes > MAX_PENDING_OUTPUT_BYTES) {
    return Promise.reject(new BridgeRequestError("backpressured", "Bridge output queue is full."));
  }
  pendingOutputBytes += encodedBytes;
  const write = writeTail
    .then(async () => {
      if (!process.stdout.write(encoded, "utf8")) await once(process.stdout, "drain");
    })
    .finally(() => {
      pendingOutputBytes -= encodedBytes;
    });
  writeTail = write.catch(() => undefined);
  return write;
}

function interactionKey(sessionId: string, interactionId: string): string {
  return `${sessionId.length}:${sessionId}${interactionId}`;
}

function clearApprovalByKey(key: string): void {
  const pending = approvals.get(key);
  if (pending?.timer !== undefined) clearTimeout(pending.timer);
  approvals.delete(key);
}

function clearApproval(sessionId: string, approvalId: string): void {
  clearApprovalByKey(interactionKey(sessionId, approvalId));
}

function scheduleApprovalTimeout(key: string, pending: PendingApproval): void {
  if (pending.timer !== undefined) clearTimeout(pending.timer);
  pending.timer = setTimeout(() => {
    void denyTimedOutApproval(key).catch(fatal);
  }, approvalTimeoutMs);
  pending.timer.unref();
}

async function denyTimedOutApproval(key: string): Promise<void> {
  const pending = approvals.get(key);
  if (pending === undefined || pending.resolving || !isRecord(pending.requirementId)) return;
  const choice = pending.choices.find(
    (candidate) => isRecord(candidate) && candidate["decision"] === "denied" && typeof candidate["choiceId"] === "string",
  );
  if (!isRecord(choice) || typeof choice["choiceId"] !== "string") {
    process.stderr.write(`Approval ${redactText(pending.approvalId)} timed out, but Muse offered no deny choice.\n`);
    clearApprovalByKey(key);
    return;
  }
  const currentHost = host;
  if (currentHost === undefined) {
    clearApprovalByKey(key);
    return;
  }
  pending.resolving = true;
  pending.timer = undefined;
  try {
    const commandId = currentHost.connection.mintCommandId();
    const result = await currentHost.connection.command(
      "approval/decide",
      {
        sessionId: pending.sessionId,
        approvalId: pending.approvalId,
        choiceId: choice["choiceId"],
        requirementId: pending.requirementId,
      },
      { commandId, maxAttempts: 1 },
    );
    requireAcceptedCommand(result, commandId, "approval/decide timeout");
    if (requireResultString(result, "approvalId", "approval/decide timeout") !== pending.approvalId) {
      throw new BridgeRequestError("protocol_error", "approval/decide timeout returned a different approval.");
    }
    const terminal = requireResultBoolean(result, "terminal", "approval/decide timeout");
    if (terminal && approvals.get(key) === pending) clearApprovalByKey(key);
  } catch (error) {
    if (approvals.get(key) === pending) pending.resolving = false;
    process.stderr.write(`Timed-out approval deny failed: ${normalizeError(error).message}\n`);
  }
}

function recordInteraction(method: string, params: unknown): void {
  if (!isRecord(params)) return;
  if ((method === "approval/requested" || method === "approval/updated" || method === "approval/request") && typeof params["approvalId"] === "string" && typeof params["sessionId"] === "string") {
    const key = interactionKey(params["sessionId"], params["approvalId"]);
    clearApprovalByKey(key);
    const choices = Array.isArray(params["availableChoices"]) ? params["availableChoices"] : [];
    const pending: PendingApproval = {
      sessionId: params["sessionId"],
      approvalId: params["approvalId"],
      requirementId: params["currentRequirementId"],
      choices,
      timer: undefined,
      resolving: false,
    };
    scheduleApprovalTimeout(key, pending);
    approvals.set(key, pending);
  }
  if (method === "approval/resolved" && typeof params["approvalId"] === "string" && typeof params["sessionId"] === "string") {
    clearApproval(params["sessionId"], params["approvalId"]);
  }
  if ((method === "userInput/requested" || method === "userInput/request") && typeof params["userInputId"] === "string" && typeof params["sessionId"] === "string") {
    const questions = Array.isArray(params["questions"]) ? params["questions"] : [];
    const questionIds = new Set<string>();
    for (const question of questions) {
      if (isRecord(question) && typeof question["id"] === "string") questionIds.add(question["id"]);
    }
    userInputs.set(interactionKey(params["sessionId"], params["userInputId"]), { sessionId: params["sessionId"], questionIds });
  }
  if (method === "userInput/settled" && typeof params["userInputId"] === "string" && typeof params["sessionId"] === "string") {
    userInputs.delete(interactionKey(params["sessionId"], params["userInputId"]));
  }
}

function clearSessionInteractions(sessionId: string): void {
  for (const [key, pending] of approvals) {
    if (pending.sessionId === sessionId) clearApprovalByKey(key);
  }
  for (const [key, pending] of userInputs) {
    if (pending.sessionId === sessionId) userInputs.delete(key);
  }
}

function attachHandlers(pending: MspHandshake): void {
  pending.onNotification((notification) => {
    recordInteraction(notification.method, notification.params);
    const event = mapper.map(notification.method, notification.params);
    if (event !== undefined) void enqueueFrame({ ...event }).catch(fatal);
  });
  pending.onServerRequest(async (request) => {
    recordInteraction(request.method, request.params);
    if (request.method === "approval/request" || request.method === "userInput/request") {
      const event = mapper.map(request.method, request.params);
      if (event !== undefined) await enqueueFrame({ ...event });
      return {};
    }
    throw new BridgeRequestError("method_not_found", `Unsupported server request: ${request.method}`);
  });
  pending.onProtocolError((error) => {
    process.stderr.write(`${redactText(error.message)}\n`);
  });
}

async function initialize(params: Record<string, unknown>): Promise<Record<string, unknown>> {
  rejectUnknownFields(
    params,
    ["muse_binary", "expected_fingerprint", "shutdown_timeout_ms", "approval_timeout_ms", "sdk_version"],
    "runtime.initialize",
  );
  if (host !== undefined || handshake !== undefined) {
    throw new BridgeRequestError("already_initialized", "Muse bridge is already initialized.");
  }
  const museBinary = requireString(params, "muse_binary", "runtime.initialize");
  const expectedFingerprint = requireString(params, "expected_fingerprint", "runtime.initialize");
  const requestedSdk = requireString(params, "sdk_version", "runtime.initialize");
  const shutdownTimeoutMs = requireInteger(params, "shutdown_timeout_ms", "runtime.initialize");
  approvalTimeoutMs = requireInteger(params, "approval_timeout_ms", "runtime.initialize");
  if (
    shutdownTimeoutMs < 1 ||
    shutdownTimeoutMs > 600_000 ||
    approvalTimeoutMs < 1 ||
    approvalTimeoutMs > 86_400_000
  ) {
    throw new BridgeRequestError("invalid_params", "runtime.initialize timeouts are outside the safe range.");
  }
  if (requestedSdk !== SDK_VERSION) {
    throw new BridgeRequestError("fingerprint_mismatch", `Configured SDK version ${requestedSdk} does not match bridge SDK ${SDK_VERSION}.`);
  }
  if (expectedFingerprint !== EXPECTED_SCHEMA_FINGERPRINT) {
    throw new BridgeRequestError("fingerprint_mismatch", "Configured fingerprint does not match the pinned Muse SDK.");
  }
  const pending = spawnMspConnection({
    command: museBinary,
    args: ["serve"],
    cwd: process.cwd(),
    env: process.env,
    shutdownTimeoutMs,
    onStderr: recordMuseStderr,
  });
  handshake = pending;
  attachHandlers(pending);
  try {
    host = await pending.initialize({
      clientInfo: { name: "local-llm-muse-bridge", version: "0.1.0" },
    });
  } catch (error) {
    await pending.close().catch(() => undefined);
    flushMuseStderr();
    throw error;
  } finally {
    handshake = undefined;
  }
  const result = host.initializeResult;
  if (result.schema.fingerprint !== expectedFingerprint) {
    await host.close().catch(() => undefined);
    host = undefined;
    throw new BridgeRequestError("fingerprint_mismatch", "Muse host fingerprint does not match verified configuration.");
  }
  return {
    bridge_protocol: BRIDGE_VERSION,
    sdk_version: SDK_VERSION,
    schema_fingerprint: result.schema.fingerprint,
    server_name: result.serverInfo.name,
    server_version: result.serverInfo.version,
    session_durability: result.sessionDurability ?? "durable",
    experimental_api: result.experimentalApi,
    granted_capabilities: result.grantedCapabilities,
  };
}

function requireHost(): SpawnedMspConnection {
  if (host === undefined) throw new BridgeRequestError("bridge_not_running", "Muse host is not initialized.");
  return host;
}

async function dispatch(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
  if (method === "runtime.initialize") return await initialize(params);
  if (method === "runtime.health") {
    rejectUnknownFields(params, [], method);
    return {
      ready: host !== undefined,
      sdk_version: SDK_VERSION,
      schema_fingerprint: host?.initializeResult.schema.fingerprint ?? null,
    };
  }
  if (method === "runtime.shutdown") {
    rejectUnknownFields(params, [], method);
    for (const key of approvals.keys()) clearApprovalByKey(key);
    userInputs.clear();
    if (host !== undefined) await host.close();
    else if (handshake !== undefined) await handshake.close();
    flushMuseStderr();
    host = undefined;
    shutdownRequested = true;
    return { status: "closed" };
  }

  const connection = requireHost().connection;
  if (method === "models.list") {
    rejectUnknownFields(params, [], method);
    return await connection.request("model/list");
  }
  if (method === "session.start") {
    rejectUnknownFields(params, ["workspace_root", "model_id", "provider_id", "approval_mode", "command_id"], method);
    const commandId = requireString(params, "command_id", method);
    const result = await connection.command(
      "session/start",
      {
        workspaceRoot: requireString(params, "workspace_root", method),
        modelId: requireString(params, "model_id", method),
        providerId: requireString(params, "provider_id", method),
        approvalMode: requireString(params, "approval_mode", method),
      },
      { commandId, maxAttempts: 1 },
    );
    return sessionResult(result, commandId);
  }
  if (method === "session.resume") {
    rejectUnknownFields(params, ["native_session_id", "cursor", "command_id"], method);
    const commandId = requireString(params, "command_id", method);
    const nativeSessionId = requireString(params, "native_session_id", method);
    const cursor = optionalString(params, "cursor", method);
    const result = await connection.command(
      "session/resume",
      { sessionId: nativeSessionId, excludeItems: true, ...(cursor === undefined ? {} : { cursor }) },
      { commandId, maxAttempts: 1 },
    );
    return sessionResult(result, commandId);
  }
  if (method === "session.release") {
    rejectUnknownFields(params, ["native_session_id"], method);
    const nativeSessionId = requireString(params, "native_session_id", method);
    await connection.request("view/unsubscribe", { sessionId: nativeSessionId });
    clearSessionInteractions(nativeSessionId);
    return { status: "released", native_session_id: nativeSessionId };
  }
  if (method === "turn.start") {
    rejectUnknownFields(params, ["native_session_id", "text", "command_id"], method);
    const commandId = requireString(params, "command_id", method);
    const result = await connection.command(
      "turn/start",
      {
        sessionId: requireString(params, "native_session_id", method),
        input: [{ type: "text", text: requireString(params, "text", method) }],
        ifBusy: "queue",
      },
      { commandId, maxAttempts: 1 },
    );
    requireAcceptedCommand(result, commandId, method);
    if (requireResultString(result, "disposition", method) !== "started" || result["startedNewTurn"] !== true) {
      throw new BridgeRequestError("sessionStreamMismatch", "turn.start did not start a fresh turn.");
    }
    return {
      native_turn_id: requireResultString(result, "turnId", method),
      status: requireResultString(result, "status", method),
      disposition: requireResultString(result, "disposition", method),
      command_id: commandId,
    };
  }
  if (method === "turn.cancel") {
    rejectUnknownFields(params, ["native_session_id", "native_turn_id", "command_id"], method);
    const commandId = requireString(params, "command_id", method);
    const result = await connection.command(
      "turn/cancel",
      {
        sessionId: requireString(params, "native_session_id", method),
        turnId: requireString(params, "native_turn_id", method),
      },
      { commandId, maxAttempts: 1 },
    );
    requireAcceptedCommand(result, commandId, method);
    return {
      native_turn_id: requireResultString(result, "turnId", method),
      status: requireResultString(result, "status", method),
      command_id: commandId,
    };
  }
  if (method === "approval.decide") return await decideApproval(connection, params);
  if (method === "user_input.answer") return await answerUserInput(connection, params);
  if (method === "events.page") {
    rejectUnknownFields(params, ["native_session_id", "cursor", "limit"], method);
    const nativeSessionId = requireString(params, "native_session_id", method);
    const cursor = optionalString(params, "cursor", method);
    const limit = requireInteger(params, "limit", method);
    if (limit < 1 || limit > 1000) throw new BridgeRequestError("invalid_params", "events.page.limit must be between 1 and 1000.");
    const result = await connection.request("view/page", {
      sessionId: nativeSessionId,
      limit,
      direction: "forward",
      ...(cursor === undefined ? {} : { cursor }),
    });
    const events = result["events"];
    if (!Array.isArray(events)) throw new BridgeRequestError("protocol_error", "view/page returned invalid events.");
    return {
      events: events.flatMap((entry) => {
        if (!isRecord(entry) || typeof entry["method"] !== "string") return [];
        const mapped = mapper.map(entry["method"], entry["params"]);
        return mapped === undefined ? [] : [mapped];
      }),
      next_cursor: result["nextCursor"] ?? null,
    };
  }
  throw new BridgeRequestError("method_not_found", `Unknown bridge method: ${method}`);
}

function sessionResult(result: Record<string, unknown>, commandId: string): Record<string, unknown> {
  const session = result["session"];
  if (!isRecord(session)) throw new BridgeRequestError("protocol_error", "Muse session result is invalid.");
  const status = requireResultString(session, "status", "session result");
  if (status !== "idle" && status !== "running") {
    throw new BridgeRequestError("protocol_error", "Muse session result has an unsupported status.");
  }
  return {
    native_session_id: requireResultString(session, "sessionId", "session result"),
    view_cursor: requireResultString(result, "viewCursor", "session result"),
    status,
    model_id: typeof session["modelId"] === "string" ? session["modelId"] : null,
    provider_id: typeof session["providerId"] === "string" ? session["providerId"] : null,
    command_id: commandId,
  };
}

async function decideApproval(
  connection: SpawnedMspConnection["connection"],
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  rejectUnknownFields(
    params,
    ["native_session_id", "approval_id", "decision", "command_id"],
    "approval.decide",
  );
  const approvalId = requireString(params, "approval_id", "approval.decide");
  const sessionId = requireString(params, "native_session_id", "approval.decide");
  const decision = requireString(params, "decision", "approval.decide");
  const commandId = requireString(params, "command_id", "approval.decide");
  const key = interactionKey(sessionId, approvalId);
  const pending = approvals.get(key);
  if (pending === undefined || pending.sessionId !== sessionId || !isRecord(pending.requirementId)) {
    throw new BridgeRequestError("approvalNotFound", "The approval is not pending on this session.");
  }
  if (pending.resolving) {
    throw new BridgeRequestError("approvalAlreadyResolved", "The approval decision is already in progress.");
  }
  const choice = pending.choices.find((candidate) => {
    if (!isRecord(candidate)) return false;
    if (decision === "allow_once") return candidate["scope"] === "once" && candidate["decision"] === "approved";
    return decision === "deny" && candidate["decision"] === "denied";
  });
  if (!isRecord(choice) || typeof choice["choiceId"] !== "string") {
    throw new BridgeRequestError("approvalChoiceInvalid", `Muse did not offer a ${decision} choice.`);
  }
  if (pending.timer !== undefined) clearTimeout(pending.timer);
  pending.timer = undefined;
  pending.resolving = true;
  try {
    const result = await connection.command(
      "approval/decide",
      {
        sessionId,
        approvalId,
        choiceId: choice["choiceId"],
        requirementId: pending.requirementId,
      },
      { commandId, maxAttempts: 1 },
    );
    requireAcceptedCommand(result, commandId, "approval.decide");
    if (requireResultString(result, "approvalId", "approval.decide") !== approvalId) {
      throw new BridgeRequestError("protocol_error", "approval.decide returned a different approval.");
    }
    const terminal = requireResultBoolean(result, "terminal", "approval.decide");
    if (terminal && approvals.get(key) === pending) clearApprovalByKey(key);
    return result;
  } catch (error) {
    if (approvals.get(key) === pending) {
      pending.resolving = false;
      scheduleApprovalTimeout(key, pending);
    }
    throw error;
  }
}

async function answerUserInput(
  connection: SpawnedMspConnection["connection"],
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  rejectUnknownFields(
    params,
    ["native_session_id", "user_input_id", "answers", "command_id"],
    "user_input.answer",
  );
  const userInputId = requireString(params, "user_input_id", "user_input.answer");
  const sessionId = requireString(params, "native_session_id", "user_input.answer");
  const commandId = requireString(params, "command_id", "user_input.answer");
  const pending = userInputs.get(interactionKey(sessionId, userInputId));
  if (pending === undefined || pending.sessionId !== sessionId) {
    throw new BridgeRequestError("userInputNotFound", "The user input request is not pending on this session.");
  }
  const answers = requireArray(params, "answers", "user_input.answer").map((answer) => {
    if (!isRecord(answer)) throw new BridgeRequestError("invalid_params", "Each user input answer must be an object.");
    const questionId = requireString(answer, "questionId", "user_input.answer.answers");
    if (!pending.questionIds.has(questionId)) {
      throw new BridgeRequestError("userInputAnswerInvalid", `Unknown question id: ${questionId}`);
    }
    return answer;
  });
  const result = await connection.command(
    "userInput/answer",
    { sessionId, userInputId, answers },
    { commandId, maxAttempts: 1 },
  );
  requireAcceptedCommand(result, commandId, "user_input.answer");
  if (requireResultString(result, "userInputId", "user_input.answer") !== userInputId) {
    throw new BridgeRequestError("protocol_error", "user_input.answer returned a different request.");
  }
  userInputs.delete(interactionKey(sessionId, userInputId));
  return result;
}

function requireResultString(value: Record<string, unknown>, key: string, where: string): string {
  const member = value[key];
  if (typeof member !== "string" || member.length === 0) {
    throw new BridgeRequestError("protocol_error", `${where}.${key} is invalid.`);
  }
  return member;
}

function requireResultBoolean(value: Record<string, unknown>, key: string, where: string): boolean {
  const member = value[key];
  if (typeof member !== "boolean") {
    throw new BridgeRequestError("protocol_error", `${where}.${key} is invalid.`);
  }
  return member;
}

function requireAcceptedCommand(
  result: Record<string, unknown>,
  commandId: string,
  where: string,
): void {
  if (
    requireResultString(result, "commandId", where) !== commandId ||
    requireResultString(result, "status", where) !== "accepted"
  ) {
    throw new BridgeRequestError("protocol_error", `${where} returned mismatched command metadata.`);
  }
}

function normalizeError(error: unknown): { kind: string; message: string; retryable: boolean; data: Record<string, unknown> } {
  if (error instanceof BridgeRequestError) {
    const data = redactValue(error.data);
    return { kind: error.kind, message: redactText(error.message), retryable: error.retryable, data: isRecord(data) ? data : {} };
  }
  if (error instanceof MspError) {
    const data = redactValue(error.data);
    return { kind: error.kind, message: redactText(error.message), retryable: error.retryable ?? false, data: isRecord(data) ? data : {} };
  }
  if (error instanceof ProtocolError) {
    return { kind: "protocol_error", message: redactText(error.message), retryable: false, data: {} };
  }
  return { kind: "internal", message: redactText(error instanceof Error ? error.message : String(error)), retryable: false, data: {} };
}

function fatal(error: unknown): void {
  if (fatalTriggered) return;
  fatalTriggered = true;
  process.stderr.write(`${redactText(error instanceof Error ? error.message : String(error))}\n`);
  process.exitCode = 1;
  input.close();
  process.stdin.pause();
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  let id = "unknown";
  try {
    if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) {
      throw new BridgeRequestError("input_too_large", "Bridge input frame is too large.");
    }
    const request = parseBridgeRequest(line);
    id = request.id;
    const result = await dispatch(request.method, request.params);
    await enqueueFrame({ v: BRIDGE_VERSION, id, ok: true, result });
  } catch (error) {
    await enqueueFrame({ v: BRIDGE_VERSION, id, ok: false, error: normalizeError(error) });
  }
  if (shutdownRequested) break;
}
input.close();
process.stdin.pause();

if (!shutdownRequested) {
  if (host !== undefined) await host.close().catch(() => undefined);
  else if (handshake !== undefined) await handshake.close().catch(() => undefined);
}
flushMuseStderr();
await writeTail;
