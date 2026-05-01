"""SQLite-backed session persistence for guided provisioning."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4


class SessionPhase(str, Enum):
    INTENT_DETECTED = "intent_detected"
    AWAITING_VM_TYPE = "awaiting_vm_type"
    AWAITING_SPECS = "awaiting_specs"
    AWAITING_APPROVAL = "awaiting_approval"
    PROVISIONING = "provisioning"
    AWAITING_FIRST_LOGIN = "awaiting_first_login"
    AWAITING_POST_LOGIN_CHOICES = "awaiting_post_login_choices"
    BOOTSTRAPPING = "bootstrapping"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_PHASES = {SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: Dict[str, Any]) -> str:
    return json.dumps(value or {}, sort_keys=True)


def _json_loads(value: str | None) -> Dict[str, Any]:
    if not value:
        return {}
    return json.loads(value)


@dataclass(slots=True)
class ProvisioningSession:
    """Persistent conversation state for one provisioning request."""

    session_id: str
    user_id: str
    phase: SessionPhase
    intent: str = "create_vm"
    role: str | None = None
    provider: str | None = None
    collected_answers: Dict[str, Any] = field(default_factory=dict)
    desired_state: Dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None
    credential_id: str | None = None
    instance_id: str | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def with_updates(self, **changes: Any) -> "ProvisioningSession":
        """Return an updated copy with a fresh timestamp."""

        return replace(self, updated_at=_utc_now(), **changes)


class SessionManager:
    """Small SQLite store for resumable v2 provisioning sessions."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if self.db_path.parent:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provisioning_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    role TEXT,
                    provider TEXT,
                    collected_answers TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    approval_id TEXT,
                    credential_id TEXT,
                    instance_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(provisioning_sessions)").fetchall()
            }
            if "credential_id" not in columns:
                conn.execute("ALTER TABLE provisioning_sessions ADD COLUMN credential_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_provisioning_sessions_user_phase "
                "ON provisioning_sessions(user_id, phase)"
            )

    def create_session(self, user_id: str, intent: str = "create_vm") -> ProvisioningSession:
        session = ProvisioningSession(
            session_id=str(uuid4()),
            user_id=str(user_id or "default"),
            phase=SessionPhase.INTENT_DETECTED,
            intent=intent,
        )
        self.save(session)
        return session

    def save(self, session: ProvisioningSession) -> ProvisioningSession:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provisioning_sessions (
                    session_id, user_id, phase, intent, role, provider,
                    collected_answers, desired_state, approval_id, credential_id, instance_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    phase=excluded.phase,
                    intent=excluded.intent,
                    role=excluded.role,
                    provider=excluded.provider,
                    collected_answers=excluded.collected_answers,
                    desired_state=excluded.desired_state,
                    approval_id=excluded.approval_id,
                    credential_id=excluded.credential_id,
                    instance_id=excluded.instance_id,
                    updated_at=excluded.updated_at
                """,
                (
                    session.session_id,
                    session.user_id,
                    session.phase.value,
                    session.intent,
                    session.role,
                    session.provider,
                    _json_dumps(session.collected_answers),
                    _json_dumps(session.desired_state),
                    session.approval_id,
                    session.credential_id,
                    session.instance_id,
                    session.created_at,
                    session.updated_at,
                ),
            )
        return session

    def get(self, session_id: str) -> ProvisioningSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provisioning_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def require(self, session_id: str) -> ProvisioningSession:
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"Provisioning session not found: {session_id}")
        return session

    def record_answers(self, session_id: str, answers: Dict[str, Any]) -> ProvisioningSession:
        session = self.require(session_id)
        merged = dict(session.collected_answers)
        merged.update({key: value for key, value in (answers or {}).items() if value not in (None, "")})
        updated = session.with_updates(collected_answers=merged)
        return self.save(updated)

    def update_phase(self, session_id: str, phase: SessionPhase) -> ProvisioningSession:
        session = self.require(session_id)
        updated = session.with_updates(phase=phase)
        return self.save(updated)

    def cancel(self, session_id: str) -> ProvisioningSession:
        return self.update_phase(session_id, SessionPhase.CANCELLED)

    def list_active(self, user_id: str) -> list[ProvisioningSession]:
        terminal_values = tuple(phase.value for phase in TERMINAL_PHASES)
        placeholders = ",".join("?" for _ in terminal_values)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM provisioning_sessions WHERE user_id = ? AND phase NOT IN ({placeholders}) ORDER BY updated_at DESC",
                (user_id, *terminal_values),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def _from_row(self, row: sqlite3.Row) -> ProvisioningSession:
        return ProvisioningSession(
            session_id=row["session_id"],
            user_id=row["user_id"],
            phase=SessionPhase(row["phase"]),
            intent=row["intent"],
            role=row["role"],
            provider=row["provider"],
            collected_answers=_json_loads(row["collected_answers"]),
            desired_state=_json_loads(row["desired_state"]),
            approval_id=row["approval_id"],
            credential_id=row["credential_id"],
            instance_id=row["instance_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
