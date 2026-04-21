#!/usr/bin/env python3
"""Signed command envelopes for future AVA Core -> Agent dispatch.

This module intentionally does not send commands anywhere. It provides the
cryptographic envelope that remote agents can verify before accepting work.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any


SIGNING_ALGORITHM = "hmac-sha256"
SIGNING_KEY_ENV = "AVA_COMMAND_SIGNING_KEY"
REPLAY_CACHE_PATH_ENV = "AVA_COMMAND_REPLAY_CACHE_PATH"
REQUIRED_FIELDS = {
    "version",
    "command_id",
    "issuer",
    "target",
    "action",
    "payload",
    "issued_at",
    "expires_at",
    "nonce",
    "algorithm",
    "signature",
}


class CommandSigningError(RuntimeError):
    """Raised when command signing cannot be completed safely."""


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str
    command_id: str | None = None
    action: str | None = None
    target: str | None = None
    expires_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "command_id": self.command_id,
            "action": self.action,
            "target": self.target,
            "expires_at": self.expires_at,
        }


class CommandReplayCache:
    """Small file-backed replay cache for command_id + nonce consumption.

    Future remote agents should call ``consume_signed_command`` before executing
    a command. This cache is intentionally simple JSON so it works in the
    lightweight Python agent we expect to add later.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.getenv(REPLAY_CACHE_PATH_ENV, "").strip()
        self._memory_entries: dict[str, int] = {}

    def consume(self, command_id: str, nonce: str, expires_at: int, *, now: int | None = None) -> bool:
        current_time = int(now if now is not None else time.time())
        key = self._entry_key(command_id, nonce)
        entries = self._load()
        self._prune(entries, current_time)
        if key in entries:
            self._save(entries)
            return False
        entries[key] = int(expires_at)
        self._save(entries)
        return True

    def _load(self) -> dict[str, int]:
        if not self.path:
            return dict(self._memory_entries)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        entries: dict[str, int] = {}
        for key, value in data.items():
            try:
                entries[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return entries

    def _save(self, entries: dict[str, int]) -> None:
        if not self.path:
            self._memory_entries = dict(entries)
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, sort_keys=True, separators=(",", ":"))
        os.replace(tmp_path, self.path)

    @staticmethod
    def _entry_key(command_id: str, nonce: str) -> str:
        digest = hashlib.sha256(f"{command_id}:{nonce}".encode("utf-8")).hexdigest()
        return digest

    @staticmethod
    def _prune(entries: dict[str, int], now: int) -> None:
        for key, expires_at in list(entries.items()):
            if expires_at < now:
                entries.pop(key, None)


def signing_key_configured() -> bool:
    """Return whether the deployment has a command-signing key configured."""
    return bool(os.getenv(SIGNING_KEY_ENV, "").strip())


def replay_cache_configured() -> bool:
    """Return whether a persistent signed-command replay cache is configured."""
    return bool(os.getenv(REPLAY_CACHE_PATH_ENV, "").strip())


def _get_signing_key() -> bytes:
    key = os.getenv(SIGNING_KEY_ENV, "").strip()
    if not key:
        raise CommandSigningError(f"{SIGNING_KEY_ENV} is required for signed command envelopes")
    return key.encode("utf-8")


def _canonical_payload(envelope: dict[str, Any]) -> bytes:
    unsigned = copy.deepcopy(envelope)
    unsigned.pop("signature", None)
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _signature_for(envelope: dict[str, Any], key: bytes) -> str:
    digest = hmac.new(key, _canonical_payload(envelope), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_signed_command(
    *,
    command_id: str,
    action: str,
    target: str,
    payload: dict[str, Any] | None = None,
    issuer: str = "ava-core",
    ttl_seconds: int = 300,
    now: int | None = None,
) -> dict[str, Any]:
    """Create a signed command envelope with expiry and a nonce field."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if not command_id or not action or not target:
        raise ValueError("command_id, action, and target are required")

    issued_at = int(now if now is not None else time.time())
    envelope: dict[str, Any] = {
        "version": 1,
        "command_id": command_id,
        "issuer": issuer,
        "target": target,
        "action": action,
        "payload": payload or {},
        "issued_at": issued_at,
        "expires_at": issued_at + int(ttl_seconds),
        "nonce": secrets.token_urlsafe(24),
        "algorithm": SIGNING_ALGORITHM,
    }
    envelope["signature"] = _signature_for(envelope, _get_signing_key())
    return envelope


def verify_signed_command(envelope: dict[str, Any], *, now: int | None = None) -> VerificationResult:
    """Verify a signed command envelope and fail closed on missing config."""
    if not isinstance(envelope, dict):
        return VerificationResult(False, "invalid_envelope")

    missing = sorted(REQUIRED_FIELDS - set(envelope))
    if missing:
        return VerificationResult(False, f"missing_fields:{','.join(missing)}")

    command_id = str(envelope.get("command_id") or "")
    action = str(envelope.get("action") or "")
    target = str(envelope.get("target") or "")
    expires_at = envelope.get("expires_at")

    if envelope.get("algorithm") != SIGNING_ALGORITHM:
        return VerificationResult(False, "unsupported_algorithm", command_id, action, target, _safe_int(expires_at))

    try:
        expiry = int(expires_at)
    except (TypeError, ValueError):
        return VerificationResult(False, "invalid_expiry", command_id, action, target, None)

    current_time = int(now if now is not None else time.time())
    if expiry < current_time:
        return VerificationResult(False, "expired", command_id, action, target, expiry)

    try:
        expected = _signature_for(envelope, _get_signing_key())
    except CommandSigningError:
        return VerificationResult(False, "signing_key_missing", command_id, action, target, expiry)

    supplied = str(envelope.get("signature") or "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        return VerificationResult(False, "bad_signature", command_id, action, target, expiry)

    return VerificationResult(True, "verified", command_id, action, target, expiry)


def consume_signed_command(
    envelope: dict[str, Any],
    *,
    replay_cache: CommandReplayCache | None = None,
    now: int | None = None,
) -> VerificationResult:
    """Verify a signed command and consume its command_id + nonce once."""
    result = verify_signed_command(envelope, now=now)
    if not result.ok:
        return result

    cache = replay_cache or CommandReplayCache()
    command_id = str(envelope.get("command_id") or "")
    nonce = str(envelope.get("nonce") or "")
    expires_at = int(envelope.get("expires_at"))
    if not command_id or not nonce:
        return VerificationResult(False, "invalid_replay_identity", result.command_id, result.action, result.target, result.expires_at)

    if not cache.consume(command_id, nonce, expires_at, now=now):
        return VerificationResult(False, "replayed", result.command_id, result.action, result.target, result.expires_at)

    return VerificationResult(True, "verified_and_consumed", result.command_id, result.action, result.target, result.expires_at)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
