import assert from "node:assert/strict";
import test from "node:test";

import { redactText, redactValue } from "../src/redact.js";

test("redacts secrets in diagnostics and nested values", () => {
  assert.equal(redactText("api_key=secret"), "api_key=[REDACTED]");
  const json = redactText('{"Authorization":"Bearer super-secret","api_key":"json-secret"}');
  assert.equal(json.includes("super-secret"), false);
  assert.equal(json.includes("json-secret"), false);
  assert.equal(redactText("request failed: Bearer bare-secret"), "request failed: Bearer [REDACTED]");
  assert.equal(redactText("password=secret"), "password=[REDACTED]");
  assert.deepEqual(redactValue({ accessToken: "secret", safe: "ok" }), {
    accessToken: "[REDACTED]",
    safe: "ok",
  });
  assert.deepEqual(redactValue({ client_secret: "secret", token: "secret" }), {
    client_secret: "[REDACTED]",
    token: "[REDACTED]",
  });
});
