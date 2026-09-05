const SECRET_KEY = /authorization|api[_-]?key|(?:access|refresh|session|id)?[_-]?token|client[_-]?secret|password|cookie/i;
const SECRET_TEXT =
  /(["']?(?:authorization|api[_-]?key|(?:access|refresh|session|id)?[_-]?token|client[_-]?secret|password|cookie)["']?\s*[:=]\s*["']?(?:bearer\s+)?)([^"'\s,;}]+)/gi;
const BEARER_TEXT = /\bbearer\s+[a-z0-9._~+/=-]+/gi;

export function redactText(value: string, limit = 4096): string {
  return value
    .replace(SECRET_TEXT, "$1[REDACTED]")
    .replace(BEARER_TEXT, "Bearer [REDACTED]")
    .slice(0, limit);
}

export function redactValue(value: unknown, depth = 0): unknown {
  if (depth > 8) return "[TRUNCATED]";
  if (typeof value === "string") return redactText(value, 100_000);
  if (Array.isArray(value)) return value.slice(0, 1000).map((entry) => redactValue(entry, depth + 1));
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, member] of Object.entries(value)) {
      out[key] = SECRET_KEY.test(key) ? "[REDACTED]" : redactValue(member, depth + 1);
    }
    return out;
  }
  return value;
}
