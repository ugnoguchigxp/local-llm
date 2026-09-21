from __future__ import annotations

import json
import sys


def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


provider_request = None
for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": 1}})
    elif method == "echo":
        send({"jsonrpc": "2.0", "id": request_id, "result": message.get("params")})
    elif method == "permission/test":
        provider_request = request_id
        send(
            {
                "jsonrpc": "2.0",
                "id": "provider-1",
                "method": "session/request_permission",
                "params": {"sessionId": "s1", "options": []},
            }
        )
    elif request_id == "provider-1" and provider_request is not None:
        send({"jsonrpc": "2.0", "id": provider_request, "result": message.get("result")})
        provider_request = None
    elif method == "notification/test":
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": "s1", "update": {"sessionUpdate": "noop"}},
            }
        )
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "malformed":
        sys.stdout.write("{broken\n")
        sys.stdout.flush()
    elif method == "duplicate":
        sys.stdout.write(
            '{"jsonrpc":"2.0","id":'
            + str(request_id)
            + ',"result":{},"result":[]}\n'
        )
        sys.stdout.flush()
    elif method == "oversized":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"value": "x" * 70000}})
    elif method == "hang":
        pass
    elif method == "stderr":
        sys.stderr.write("diagnostic Bearer abcdefghijklmnop\n")
        sys.stderr.flush()
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "stderr_long":
        sys.stderr.write("x" * 70000 + "\n")
        sys.stderr.flush()
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "exit":
        sys.exit(7)
