#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_runtime.grok.config import AUTH_METHOD, GrokConfig
from agent_runtime.grok.runtime import GrokRuntime


def static_report(config: GrokConfig) -> dict[str, object]:
    evidence, evidence_error = config.validate_billing_evidence()
    return {
        "runtime": "grok",
        "enabled": config.enabled,
        "grokBinary": config.resolved_binary(),
        "profileRoot": str(config.profile_root.resolve()) if config.profile_root else None,
        "profileExists": bool(config.profile_root and config.profile_root.is_dir()),
        "expectedVersion": config.expected_version or None,
        "acpProtocolVersion": config.acp_protocol_version,
        "authMethod": AUTH_METHOD,
        "allowedModels": list(config.allowed_models),
        "sandbox": config.sandbox_profile,
        "webSearch": config.allow_web_search,
        "projectExtensions": config.allow_project_extensions,
        "staticError": config.validate_static(),
        "billingEvidence": {
            "valid": evidence is not None,
            "error": evidence_error,
            "assurance": evidence.billing_assurance if evidence else "unverified",
            "verifiedAt": evidence.verified_at if evidence else None,
            "expiresAt": evidence.expires_at if evidence else None,
        },
    }


async def run_preflight(config: GrokConfig) -> dict[str, object]:
    runtime = GrokRuntime(config)
    try:
        models = await runtime.list_models()
        status = await runtime.status()
        return {
            "status": status.status,
            "billingMode": status.billing_mode,
            "billingAssurance": status.billing_assurance,
            "auth": status.auth,
            "protocol": {"name": status.protocol_name, "version": status.protocol_version},
            "protocolFingerprint": status.protocol_fingerprint,
            "hostVersion": status.host_version,
            "models": [model.id for model in models],
        }
    finally:
        await runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Grok Runtime configuration without running a model turn.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Also spawn Grok ACP and validate its handshake. This does not start a turn.",
    )
    args = parser.parse_args()
    load_dotenv(REPO_ROOT / ".env")
    config = GrokConfig.from_env(repo_root=REPO_ROOT)
    report = static_report(config)
    exit_code = 0
    if args.preflight:
        try:
            report["preflight"] = asyncio.run(run_preflight(config))
        except Exception as exc:
            report["preflight"] = {
                "status": "error",
                "type": type(exc).__name__,
                "message": str(exc),
            }
            exit_code = 1
    elif not config.enabled or report["staticError"] or not report["billingEvidence"]["valid"]:
        exit_code = 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
