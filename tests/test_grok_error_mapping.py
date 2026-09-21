from agent_runtime.grok.error_mapping import map_rpc_error


def test_maps_auth_rate_limit_and_generic_rpc_errors():
    auth = map_rpc_error({"code": -32000, "message": "Access denied"})
    quota = map_rpc_error({"code": -32603, "message": "Weekly usage limit reached"})
    generic = map_rpc_error({"code": -32603, "message": "Path not found"})
    cancelled = map_rpc_error({"code": -32800, "message": "Stopped"})

    assert (auth.code, auth.status_code) == ("runtime_auth_required", 401)
    assert (quota.code, quota.status_code, quota.retryable) == (
        "provider_rate_limited",
        429,
        True,
    )
    assert (generic.code, generic.status_code) == ("provider_request_failed", 502)
    assert cancelled.code == "provider_cancelled"


def test_provider_error_secrets_are_redacted():
    error = map_rpc_error(
        {"code": -32000, "message": "request used Bearer abcdefghijklmnop"}
    )
    assert error.message == "request used Bearer [redacted]"

    assignment = map_rpc_error(
        {"code": -32000, "message": "failed with password=hunter2"}
    )
    assert assignment.message == "failed with password=[redacted]"


def test_invalid_rpc_error_shape_is_protocol_mismatch():
    for error in (
        {"code": "xai-secret-value", "message": "failed"},
        {"code": True, "message": "failed"},
        {"code": 2**31, "message": "failed"},
        {"code": -32000, "message": ""},
    ):
        mapped = map_rpc_error(error)
        assert mapped.code == "runtime_protocol_mismatch"
        assert mapped.data == {}
