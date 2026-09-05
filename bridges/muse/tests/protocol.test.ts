import assert from "node:assert/strict";
import test from "node:test";

import { BridgeRequestError, parseBridgeRequest, rejectUnknownFields } from "../src/protocol.js";

test("parseBridgeRequest accepts the versioned request shape", () => {
  assert.deepEqual(
    parseBridgeRequest('{"v":1,"id":"brq_1","method":"runtime.health","params":{}}'),
    { v: 1, id: "brq_1", method: "runtime.health", params: {} },
  );
});

test("parseBridgeRequest rejects unknown fields and protocol versions", () => {
  assert.throws(
    () => parseBridgeRequest('{"v":2,"id":"brq_1","method":"runtime.health","params":{}}'),
    BridgeRequestError,
  );
  assert.throws(
    () => parseBridgeRequest('{"v":1,"id":"brq_1","method":"runtime.health","params":{},"extra":1}'),
    /unknown fields/,
  );
});

test("rejectUnknownFields rejects method parameter drift", () => {
  assert.throws(
    () => rejectUnknownFields({ expected: true, unexpected: true }, ["expected"], "test.method"),
    /unknown fields: unexpected/,
  );
});
