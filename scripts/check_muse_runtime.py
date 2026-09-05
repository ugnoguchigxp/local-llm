#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_runtime.muse.config import EXPECTED_SDK_VERSION, MuseConfig
from agent_runtime.muse.runtime import MuseRuntime


def static_report(config: MuseConfig) -> dict[str, object]:
    evidence, evidence_error = config.validate_billing_evidence()
    return {
        "runtime": "muse",
        "enabled": config.enabled,
        "sdkVersion": EXPECTED_SDK_VERSION,
        "museBinary": config.resolved_binary(),
        "nodeBinary": config.resolved_node_binary(),
        "bridgeBuilt": config.bridge_entry.is_file(),
        "bridgeEntry": str(config.bridge_entry),
        "profileRoot": str(config.profile_root.resolve()) if config.profile_root else None,
        "profileExists": bool(config.profile_root and config.profile_root.is_dir()),
        "expectedFingerprint": config.expected_fingerprint or None,
        "allowedProviderIds": list(config.allowed_provider_ids),
        "allowedModels": list(config.allowed_models),
        "approvalMode": config.native_approval_mode or None,
        "billingEvidence": {
            "valid": evidence is not None,
            "error": evidence_error,
            "verifiedAt": evidence.verified_at if evidence else None,
        },
    }


async def run_preflight(config: MuseConfig) -> dict[str, object]:
    runtime = MuseRuntime(config)
    try:
        status = await runtime.preflight()
        return {
            "status": status.status,
            "billingMode": status.billing_mode,
            "auth": status.auth,
            "protocolFingerprint": status.protocol_fingerprint,
        }
    finally:
        await runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Muse Runtime configuration without installing or logging in.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Also spawn the configured Muse host and perform the MSP handshake.",
    )
    args = parser.parse_args()
    config = MuseConfig.from_env(repo_root=REPO_ROOT)
    report = static_report(config)
    exit_code = 0
    if args.preflight:
        try:
            report["preflight"] = asyncio.run(run_preflight(config))
        except Exception as exc:
            report["preflight"] = {"status": "error", "message": str(exc)}
            exit_code = 1
    elif not config.enabled or config.resolved_binary() is None or not config.bridge_entry.is_file():
        exit_code = 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
