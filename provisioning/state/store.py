"""SQLite persistence for AVA v2 provisioning lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Any

from provisioning.verify import VerificationReport


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value or {}, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    return json.loads(value)


@dataclass(slots=True)
class ProvisioningRecord:
    """Stored lifecycle record for one provisioned instance."""

    instance_id: str
    session_id: str
    desired_state: dict[str, Any]
    actual_state: dict[str, Any]
    verification_result: dict[str, Any]
    outcome: str
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)


class ProvisioningStateStore:
    """Small SQLite store for provisioning outcomes and evidence."""

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
                CREATE TABLE IF NOT EXISTS provisioning_records (
                    instance_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    actual_state TEXT NOT NULL,
                    verification_result TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_provisioning_records_session "
                "ON provisioning_records(session_id)"
            )

    def save_verification(
        self,
        *,
        session_id: str,
        desired_state: dict[str, Any],
        actual_state: dict[str, Any],
        verification_report: VerificationReport,
    ) -> ProvisioningRecord:
        """Persist a verification-backed outcome."""

        outcome = "completed" if verification_report.passed else "failed"
        now = _utc_now()
        existing = self.get(verification_report.instance_id)
        created_at = existing.created_at if existing else now
        record = ProvisioningRecord(
            instance_id=verification_report.instance_id,
            session_id=session_id,
            desired_state=desired_state,
            actual_state=actual_state,
            verification_result=verification_report.to_dict(),
            outcome=outcome,
            created_at=created_at,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provisioning_records (
                    instance_id, session_id, desired_state, actual_state,
                    verification_result, outcome, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    desired_state=excluded.desired_state,
                    actual_state=excluded.actual_state,
                    verification_result=excluded.verification_result,
                    outcome=excluded.outcome,
                    updated_at=excluded.updated_at
                """,
                (
                    record.instance_id,
                    record.session_id,
                    _json_dumps(record.desired_state),
                    _json_dumps(record.actual_state),
                    _json_dumps(record.verification_result),
                    record.outcome,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def get(self, instance_id: str) -> ProvisioningRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provisioning_records WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def _from_row(self, row: sqlite3.Row) -> ProvisioningRecord:
        return ProvisioningRecord(
            instance_id=row["instance_id"],
            session_id=row["session_id"],
            desired_state=_json_loads(row["desired_state"]),
            actual_state=_json_loads(row["actual_state"]),
            verification_result=_json_loads(row["verification_result"]),
            outcome=row["outcome"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
