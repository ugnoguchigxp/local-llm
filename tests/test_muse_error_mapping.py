from agent_runtime.muse.error_mapping import map_bridge_error


def test_muse_error_mapping_normalizes_quota_and_cursor_errors():
    quota = map_bridge_error("quota_exceeded", "quota")
    cursor = map_bridge_error("viewTruncated", "old cursor")

    assert (quota.status_code, quota.code) == (429, "runtime_quota_exceeded")
    assert (cursor.status_code, cursor.code) == (410, "event_cursor_expired")


def test_muse_error_mapping_covers_official_msp_error_kinds():
    not_loaded = map_bridge_error("sessionNotLoaded", "not loaded")
    missing_cursor = map_bridge_error(
        "notFound",
        "missing",
        data={"reason": "missingAnchor"},
    )
    protocol = map_bridge_error("schemaFingerprintMismatch", "schema")
    oversized = map_bridge_error("outputResultTooLarge", "large")

    assert (not_loaded.status_code, not_loaded.code) == (409, "agent_session_not_loaded")
    assert (missing_cursor.status_code, missing_cursor.code) == (410, "event_cursor_expired")
    assert (protocol.status_code, protocol.code) == (503, "runtime_protocol_mismatch")
    assert (oversized.status_code, oversized.code) == (502, "provider_response_too_large")
