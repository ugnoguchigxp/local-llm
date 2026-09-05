import { isRecord } from "./protocol.js";
import { redactValue } from "./redact.js";

const MAX_TRACKED_ITEMS = 10_000;

function setBounded(map: Map<string, string>, key: string, value: string): void {
  if (!map.has(key) && map.size >= MAX_TRACKED_ITEMS) {
    const oldest = map.keys().next().value;
    if (typeof oldest === "string") map.delete(oldest);
  }
  map.set(key, value);
}

function approvalChoices(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const choices = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)) continue;
    if (candidate["decision"] === "denied") choices.add("deny");
    if (candidate["decision"] === "approved" && candidate["scope"] === "once") {
      choices.add("allow_once");
    }
  }
  return [...choices];
}

function approvalSubject(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value) || typeof value["kind"] !== "string") return null;
  const subject: Record<string, unknown> = { kind: value["kind"] };
  for (const key of ["access", "command", "host", "path", "protocol", "target"] as const) {
    if (typeof value[key] === "string") subject[key] = redactValue(value[key]);
  }
  if (typeof value["port"] === "number") subject["port"] = value["port"];
  if (typeof value["toolName"] === "string") subject["tool_name"] = value["toolName"];
  return subject;
}

function userInputQuestions(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((question) => {
    if (!isRecord(question) || typeof question["id"] !== "string" || typeof question["question"] !== "string") {
      return [];
    }
    const options = Array.isArray(question["options"])
      ? question["options"].flatMap((option) => {
          if (!isRecord(option) || typeof option["label"] !== "string") return [];
          return [{
            label: redactValue(option["label"]),
            ...(typeof option["description"] === "string" ? { description: redactValue(option["description"]) } : {}),
          }];
        })
      : [];
    const selection = isRecord(question["selection"]) ? question["selection"] : undefined;
    return [{
      id: question["id"],
      question: redactValue(question["question"]),
      ...(typeof question["header"] === "string" ? { header: redactValue(question["header"]) } : {}),
      options,
      ...(selection === undefined
        ? {}
        : {
            selection: {
              ...(typeof selection["mode"] === "string" ? { mode: selection["mode"] } : {}),
              ...(typeof selection["minSelections"] === "number" ? { min_selections: selection["minSelections"] } : {}),
              ...(typeof selection["maxSelections"] === "number" ? { max_selections: selection["maxSelections"] } : {}),
            },
          }),
    }];
  });
}

export interface BridgeEvent {
  readonly v: 1;
  readonly event: true;
  readonly type: string;
  readonly native_session_id: string;
  readonly native_turn_id: string | null;
  readonly native_cursor: string;
  readonly data: Record<string, unknown>;
}

export class EventMapper {
  readonly #itemKinds = new Map<string, string>();
  readonly #itemTurns = new Map<string, string>();

  map(method: string, params: unknown): BridgeEvent | undefined {
    if (!isRecord(params)) return undefined;
    const sessionId = params["sessionId"];
    if (typeof sessionId !== "string") return undefined;
    if (method === "view/gap") {
      const after = params["after"];
      const next = params["next"];
      if (typeof after !== "string" || typeof next !== "string") return undefined;
      return {
        v: 1,
        event: true,
        type: "session.recovery_required",
        native_session_id: sessionId,
        native_turn_id: null,
        native_cursor: after,
        data: { reason: "event_gap", next_cursor: next },
      };
    }
    const cursor = params["viewCursor"];
    if (typeof cursor !== "string") return undefined;

    const item = isRecord(params["item"]) ? params["item"] : undefined;
    const itemId = item?.["itemId"] ?? params["itemId"];
    const itemKind = item?.["kind"];
    const itemKey = typeof itemId === "string" ? `${sessionId}\u0000${itemId}` : undefined;
    if (itemKey !== undefined && typeof itemKind === "string") {
      setBounded(this.#itemKinds, itemKey, itemKind);
    }
    if (itemKey !== undefined && typeof item?.["turnId"] === "string") {
      setBounded(this.#itemTurns, itemKey, item["turnId"]);
    }
    const knownKind = itemKey === undefined ? undefined : this.#itemKinds.get(itemKey);
    const knownTurn = itemKey === undefined ? undefined : this.#itemTurns.get(itemKey);
    const turnId =
      typeof params["turnId"] === "string"
        ? params["turnId"]
        : typeof item?.["turnId"] === "string"
          ? item["turnId"]
          : knownTurn ?? null;

    const mapped = this.#mapType(method, params, item, knownKind);
    const event = {
      v: 1,
      event: true,
      type: mapped.type,
      native_session_id: sessionId,
      native_turn_id: turnId,
      native_cursor: cursor,
      data: mapped.data,
    } as const;
    if (method === "item/completed" && itemKey !== undefined) {
      this.#itemKinds.delete(itemKey);
      this.#itemTurns.delete(itemKey);
    }
    return event;
  }

  #mapType(
    method: string,
    params: Record<string, unknown>,
    item: Record<string, unknown> | undefined,
    itemKind: string | undefined,
  ): { readonly type: string; readonly data: Record<string, unknown> } {
    if (method === "turn/started") return { type: "turn.started", data: {} };
    if (method === "turn/unqueued") return { type: "turn.unqueued", data: {} };
    if (method === "turn/completed") {
      const terminal = params["terminal"];
      if (typeof terminal !== "string") {
        return {
          type: "session.recovery_required",
          data: { reason: "invalid_turn_terminal" },
        };
      }
      const type =
        terminal === "cancelled"
          ? "turn.cancelled"
          : terminal === "failed"
            ? "turn.failed"
            : terminal === "completed"
              ? "turn.completed"
              : "turn.failed";
      const data: Record<string, unknown> = { terminal };
      if (typeof params["durationMs"] === "number") data["duration_ms"] = params["durationMs"];
      if (typeof params["reason"] === "string") data["reason"] = params["reason"];
      if (isRecord(params["error"])) data["error"] = redactValue(params["error"]);
      return { type, data };
    }
    if (method === "item/delta") {
      const field = typeof params["field"] === "string" ? params["field"] : "text";
      const data = {
        item_id: params["itemId"],
        field,
        text: typeof params["delta"] === "string" ? params["delta"] : "",
      };
      return { type: itemKind === "agentMessage" && field === "text" ? "message.delta" : "item.delta", data };
    }
    if (method === "item/started" || method === "item/updated" || method === "item/completed") {
      const phase = method.slice("item/".length);
      if (itemKind === "agentMessage") {
        return {
          type: `message.${phase}`,
          data: {
            item_id: item?.["itemId"],
            status: item?.["status"],
            ...(typeof item?.["text"] === "string" ? { text: item["text"] } : {}),
          },
        };
      }
      if (itemKind === "toolCall" || itemKind === "userShell") {
        return {
          type: `tool.${phase}`,
          data: {
            item_id: item?.["itemId"],
            kind: itemKind,
            status: item?.["status"],
            tool_name: item?.["toolName"],
          },
        };
      }
      return {
        type: `item.${phase}`,
        data: {
          item_id: item?.["itemId"],
          kind: itemKind ?? "unknown",
          status: item?.["status"],
        },
      };
    }
    if (method === "approval/request" || method === "approval/requested" || method === "approval/updated") {
      return {
        type: "approval.requested",
        data: {
          approval_id: params["approvalId"],
          tool_name: params["toolName"],
          protected_write: params["protectedWrite"],
          subject: approvalSubject(params["subject"]),
          available_choices: approvalChoices(params["availableChoices"]),
        },
      };
    }
    if (method === "approval/resolved") {
      return {
        type: "approval.resolved",
        data: {
          approval_id: params["approvalId"],
          decision: params["decision"],
          resolved_by: params["resolvedBy"],
        },
      };
    }
    if (method === "userInput/request" || method === "userInput/requested") {
      return {
        type: "user_input.requested",
        data: {
          user_input_id: params["userInputId"],
          questions: userInputQuestions(params["questions"]),
          tool_name: params["toolName"],
        },
      };
    }
    if (method === "userInput/settled") {
      return {
        type: "user_input.resolved",
        data: {
          user_input_id: params["userInputId"],
          outcome: params["outcome"],
        },
      };
    }
    if (method === "session/modelChanged" || method === "session/approvalModeChanged") {
      return {
        type: "session.invariant_changed",
        data: { reason: method === "session/modelChanged" ? "model_changed" : "approval_mode_changed" },
      };
    }
    return { type: "provider.event", data: { method } };
  }
}
