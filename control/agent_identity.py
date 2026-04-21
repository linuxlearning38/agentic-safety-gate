#!/usr/bin/env python3
"""Per-agent identity registry foundation for future AVA remote agents.

This module does not expose a network transport. It provides the identity and
token-validation primitive that a future remote-agent API can enforce.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any


AGENT_REGISTRY_PATH_ENV = "AVA_AGENT_IDENTITY_REGISTRY_PATH"
DEFAULT_TOKEN_BYTES = 32


class AgentIdentityError(RuntimeError):
    """Raised when an agent identity cannot be created or stored safely."""


@dataclass(frozen=True)
class AgentValidationResult:
    ok: bool
    reason: str
    agent_id: str | None = None
    scopes: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "agent_id": self.agent_id,
            "scopes": self.scopes or [],
        }


def agent_registry_configured() -> bool:
    return bool(os.getenv(AGENT_REGISTRY_PATH_ENV, "").strip())


def hash_agent_token(token: str) -> str:
    if not token:
        raise ValueError("agent token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AgentIdentityRegistry:
    """File-backed registry storing agent metadata and token hashes only."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.getenv(AGENT_REGISTRY_PATH_ENV, "").strip()
        self._memory_records: dict[str, dict[str, Any]] = {}

    def create_agent(
        self,
        *,
        name: str,
        scopes: list[str],
        cert_fingerprint: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise AgentIdentityError("agent name is required")
        normalized_scopes = sorted({scope.strip() for scope in scopes if scope.strip()})
        if not normalized_scopes:
            raise AgentIdentityError("at least one agent scope is required")

        agent_id = f"agent_{uuid.uuid4().hex}"
        token = secrets.token_urlsafe(DEFAULT_TOKEN_BYTES)
        issued_at = int(now if now is not None else time.time())
        record = {
            "agent_id": agent_id,
            "name": name.strip(),
            "scopes": normalized_scopes,
            "token_hash": hash_agent_token(token),
            "cert_fingerprint": _normalize_fingerprint(cert_fingerprint),
            "metadata": metadata or {},
            "issued_at": issued_at,
            "revoked": False,
        }

        records = self._load()
        records[agent_id] = record
        self._save(records)
        public_record = {key: value for key, value in record.items() if key != "token_hash"}
        return {"agent": public_record, "token": token}

    def validate_agent(
        self,
        *,
        agent_id: str,
        token: str,
        required_scope: str | None = None,
        cert_fingerprint: str | None = None,
    ) -> AgentValidationResult:
        records = self._load()
        record = records.get(agent_id)
        if not isinstance(record, dict):
            return AgentValidationResult(False, "unknown_agent", agent_id)
        if record.get("revoked"):
            return AgentValidationResult(False, "revoked", agent_id, _record_scopes(record))
        if record.get("token_hash") != hash_agent_token(token):
            return AgentValidationResult(False, "bad_token", agent_id, _record_scopes(record))

        expected_fingerprint = _normalize_fingerprint(record.get("cert_fingerprint"))
        supplied_fingerprint = _normalize_fingerprint(cert_fingerprint)
        if expected_fingerprint and expected_fingerprint != supplied_fingerprint:
            return AgentValidationResult(False, "certificate_fingerprint_mismatch", agent_id, _record_scopes(record))

        scopes = _record_scopes(record)
        if required_scope and required_scope not in scopes:
            return AgentValidationResult(False, "scope_denied", agent_id, scopes)

        return AgentValidationResult(True, "validated", agent_id, scopes)

    def revoke_agent(self, agent_id: str) -> bool:
        records = self._load()
        record = records.get(agent_id)
        if not isinstance(record, dict):
            return False
        record["revoked"] = True
        self._save(records)
        return True

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path:
            return dict(self._memory_records)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def _save(self, records: dict[str, dict[str, Any]]) -> None:
        if not self.path:
            self._memory_records = dict(records)
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, sort_keys=True, separators=(",", ":"))
        os.replace(tmp_path, self.path)


def _record_scopes(record: dict[str, Any]) -> list[str]:
    scopes = record.get("scopes", [])
    if not isinstance(scopes, list):
        return []
    return sorted({str(scope) for scope in scopes if str(scope).strip()})


def _normalize_fingerprint(value: Any) -> str:
    if not value:
        return ""
    return str(value).replace(":", "").replace(" ", "").lower()

