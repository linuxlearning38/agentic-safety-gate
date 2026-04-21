#!/usr/bin/env python3
"""Regression checks for future remote-agent identity primitives."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control.agent_identity import (  # noqa: E402
    AGENT_REGISTRY_PATH_ENV,
    AgentIdentityRegistry,
    agent_registry_configured,
)


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def main() -> int:
    previous_path = os.environ.get(AGENT_REGISTRY_PATH_ENV)
    failures: list[bool] = []
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = str(Path(tmpdir) / "agents.json")
            os.environ[AGENT_REGISTRY_PATH_ENV] = registry_path
            registry = AgentIdentityRegistry()
            created = registry.create_agent(
                name="server-01",
                scopes=["inspect", "execute:low", "inspect"],
                cert_fingerprint="AA:BB:CC",
                metadata={"env": "test"},
                now=1_000,
            )
            agent = created["agent"]
            token = created["token"]
            agent_id = agent["agent_id"]

            valid = registry.validate_agent(
                agent_id=agent_id,
                token=token,
                required_scope="inspect",
                cert_fingerprint="aa bb cc",
            )
            bad_token = registry.validate_agent(
                agent_id=agent_id,
                token="wrong-token",
                required_scope="inspect",
                cert_fingerprint="aabbcc",
            )
            bad_scope = registry.validate_agent(
                agent_id=agent_id,
                token=token,
                required_scope="execute:high",
                cert_fingerprint="aabbcc",
            )
            bad_cert = registry.validate_agent(
                agent_id=agent_id,
                token=token,
                required_scope="inspect",
                cert_fingerprint="ddeeff",
            )
            registry.revoke_agent(agent_id)
            revoked = registry.validate_agent(
                agent_id=agent_id,
                token=token,
                required_scope="inspect",
                cert_fingerprint="aabbcc",
            )

            registry_data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
            stored_record = registry_data[agent_id]

            failures.extend(
                [
                    check("agent registry configuration is detected", agent_registry_configured() is True),
                    check("created agent response returns token once", bool(token) and "token_hash" not in agent),
                    check("registry stores token hash, not plaintext token", stored_record.get("token_hash") and token not in json.dumps(registry_data)),
                    check("duplicate scopes are normalized", stored_record["scopes"] == ["execute:low", "inspect"]),
                    check("certificate fingerprint is normalized", stored_record["cert_fingerprint"] == "aabbcc"),
                    check("valid agent token, scope, and certificate validate", valid.ok and valid.reason == "validated"),
                    check("bad token is rejected", not bad_token.ok and bad_token.reason == "bad_token"),
                    check("missing scope is rejected", not bad_scope.ok and bad_scope.reason == "scope_denied"),
                    check("certificate fingerprint mismatch is rejected", not bad_cert.ok and bad_cert.reason == "certificate_fingerprint_mismatch"),
                    check("revoked agent is rejected", not revoked.ok and revoked.reason == "revoked"),
                ]
            )
    finally:
        if previous_path is None:
            os.environ.pop(AGENT_REGISTRY_PATH_ENV, None)
        else:
            os.environ[AGENT_REGISTRY_PATH_ENV] = previous_path

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nAgent identity regression failed: {failed} issue(s)")
        return 1
    print("\nAgent identity regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

