#!/usr/bin/env python3
"""Regression checks for signed command envelopes."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control.signed_commands import (  # noqa: E402
    CommandReplayCache,
    REPLAY_CACHE_PATH_ENV,
    SIGNING_KEY_ENV,
    consume_signed_command,
    create_signed_command,
    replay_cache_configured,
    signing_key_configured,
    verify_signed_command,
)


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def main() -> int:
    previous_key = os.environ.get(SIGNING_KEY_ENV)
    previous_cache_path = os.environ.get(REPLAY_CACHE_PATH_ENV)
    failures: list[bool] = []
    try:
        os.environ[SIGNING_KEY_ENV] = "test-only-command-signing-key"

        envelope = create_signed_command(
            command_id="cmd-001",
            action="inspect_service",
            target="server-01",
            payload={"service": "nginx"},
            now=1_000,
            ttl_seconds=300,
        )
        valid = verify_signed_command(envelope, now=1_100)

        tampered = dict(envelope)
        tampered["payload"] = {"service": "ssh"}
        tampered_result = verify_signed_command(tampered, now=1_100)

        expired = verify_signed_command(envelope, now=1_301)

        missing = dict(envelope)
        missing.pop("signature")
        missing_result = verify_signed_command(missing, now=1_100)

        os.environ.pop(SIGNING_KEY_ENV, None)
        missing_key_result = verify_signed_command(envelope, now=1_100)
        missing_key_configured = signing_key_configured()

        os.environ[SIGNING_KEY_ENV] = "test-only-command-signing-key"
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = str(Path(tmpdir) / "replay-cache.json")
            os.environ[REPLAY_CACHE_PATH_ENV] = cache_path
            persistent_cache = CommandReplayCache()
            consumed = consume_signed_command(envelope, replay_cache=persistent_cache, now=1_100)
            replayed = consume_signed_command(envelope, replay_cache=CommandReplayCache(cache_path), now=1_101)

            second_envelope = create_signed_command(
                command_id="cmd-002",
                action="inspect_service",
                target="server-01",
                payload={"service": "nginx"},
                now=1_000,
                ttl_seconds=50,
            )
            expired_pruned = consume_signed_command(
                second_envelope,
                replay_cache=CommandReplayCache(cache_path),
                now=1_010,
            )

            cache_text = Path(cache_path).read_text(encoding="utf-8")

        failures.extend(
            [
                check("signing key configuration is detected", missing_key_configured is False),
                check("valid signed command verifies", valid.ok and valid.reason == "verified"),
                check("tampered payload fails verification", not tampered_result.ok and tampered_result.reason == "bad_signature"),
                check("expired command fails verification", not expired.ok and expired.reason == "expired"),
                check("missing required fields fail closed", not missing_result.ok and missing_result.reason.startswith("missing_fields:")),
                check("missing signing key fails closed", not missing_key_result.ok and missing_key_result.reason == "signing_key_missing"),
                check("persistent replay cache configuration is detected", replay_cache_configured() is True),
                check("first command consumption succeeds", consumed.ok and consumed.reason == "verified_and_consumed"),
                check("second command consumption with same command_id and nonce is rejected", not replayed.ok and replayed.reason == "replayed"),
                check("different signed command can still be consumed", expired_pruned.ok and expired_pruned.reason == "verified_and_consumed"),
                check("replay cache stores hashed identifiers only", "cmd-001" not in cache_text and envelope["nonce"] not in cache_text),
            ]
        )
    finally:
        if previous_key is None:
            os.environ.pop(SIGNING_KEY_ENV, None)
        else:
            os.environ[SIGNING_KEY_ENV] = previous_key
        if previous_cache_path is None:
            os.environ.pop(REPLAY_CACHE_PATH_ENV, None)
        else:
            os.environ[REPLAY_CACHE_PATH_ENV] = previous_cache_path

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nSigned command regression failed: {failed} issue(s)")
        return 1
    print("\nSigned command regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
