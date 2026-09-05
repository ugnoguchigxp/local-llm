export interface BridgeRequest {
  readonly v: 1;
  readonly id: string;
  readonly method: string;
  readonly params: Record<string, unknown>;
}

export class BridgeRequestError extends Error {
  readonly kind: string;
  readonly retryable: boolean;
  readonly data: Readonly<Record<string, unknown>>;

  constructor(
    kind: string,
    message: string,
    options?: { readonly retryable?: boolean; readonly data?: Record<string, unknown> },
  ) {
    super(message);
    this.name = "BridgeRequestError";
    this.kind = kind;
    this.retryable = options?.retryable ?? false;
    this.data = options?.data ?? {};
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function parseBridgeRequest(line: string): BridgeRequest {
  let decoded: unknown;
  try {
    decoded = JSON.parse(line);
  } catch {
    throw new BridgeRequestError("invalid_request", "Bridge request is not valid JSON.");
  }
  if (!isRecord(decoded)) {
    throw new BridgeRequestError("invalid_request", "Bridge request must be an object.");
  }
  const unknown = Object.keys(decoded).filter(
    (key) => key !== "v" && key !== "id" && key !== "method" && key !== "params",
  );
  if (unknown.length > 0) {
    throw new BridgeRequestError(
      "invalid_request",
      `Bridge request contains unknown fields: ${unknown.join(", ")}.`,
    );
  }
  if (decoded["v"] !== 1) {
    throw new BridgeRequestError("invalid_request", "Unsupported bridge protocol version.");
  }
  if (typeof decoded["id"] !== "string" || decoded["id"].length === 0) {
    throw new BridgeRequestError("invalid_request", "Bridge request id must be a string.");
  }
  if (typeof decoded["method"] !== "string" || decoded["method"].length === 0) {
    throw new BridgeRequestError("invalid_request", "Bridge request method must be a string.");
  }
  if (!isRecord(decoded["params"])) {
    throw new BridgeRequestError("invalid_request", "Bridge request params must be an object.");
  }
  return {
    v: 1,
    id: decoded["id"],
    method: decoded["method"],
    params: decoded["params"],
  };
}

export function requireString(
  params: Record<string, unknown>,
  key: string,
  where: string,
): string {
  const value = params[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new BridgeRequestError("invalid_params", `${where}.${key} must be a non-empty string.`);
  }
  return value;
}

export function optionalString(
  params: Record<string, unknown>,
  key: string,
  where: string,
): string | undefined {
  const value = params[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") {
    throw new BridgeRequestError("invalid_params", `${where}.${key} must be a string.`);
  }
  return value;
}

export function requireInteger(
  params: Record<string, unknown>,
  key: string,
  where: string,
): number {
  const value = params[key];
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new BridgeRequestError("invalid_params", `${where}.${key} must be an integer.`);
  }
  return value;
}

export function requireArray(
  params: Record<string, unknown>,
  key: string,
  where: string,
): readonly unknown[] {
  const value = params[key];
  if (!Array.isArray(value)) {
    throw new BridgeRequestError("invalid_params", `${where}.${key} must be an array.`);
  }
  return value;
}

export function rejectUnknownFields(
  params: Record<string, unknown>,
  allowed: readonly string[],
  where: string,
): void {
  const accepted = new Set(allowed);
  const unknown = Object.keys(params).filter((key) => !accepted.has(key));
  if (unknown.length > 0) {
    throw new BridgeRequestError(
      "invalid_params",
      `${where} contains unknown fields: ${unknown.join(", ")}.`,
    );
  }
}
