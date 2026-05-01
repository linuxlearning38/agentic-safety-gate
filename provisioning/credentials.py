"""Temporary credential issuance for AVA v2 provisioning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import secrets
import sqlite3
import string
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_secret(secret: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{secret}".encode("utf-8")).hexdigest()


def _generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%+-_=."
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(char.islower() for char in password)
            and any(char.isupper() for char in password)
            and any(char.isdigit() for char in password)
            and any(char in "!@#%+-_=." for char in password)
        ):
            return password


@dataclass(slots=True)
class TemporaryCredential:
    """One-time credential material returned immediately after generation."""

    credential_id: str
    session_id: str
    username: str
    temporary_password: str | None
    must_change_password: bool
    display_once: bool
    created_at: str


class CredentialManager:
    """SQLite-backed audit record for temporary access credentials."""

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
                CREATE TABLE IF NOT EXISTS provisioning_credentials (
                    credential_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    displayed_at TEXT,
                    must_change_password INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_provisioning_credentials_session "
                "ON provisioning_credentials(session_id)"
            )

    def issue_temporary_credential(self, session_id: str, username: str = "avaadmin") -> TemporaryCredential:
        """Generate and return a temporary password exactly once."""

        credential_id = secrets.token_hex(8)
        salt = secrets.token_hex(16)
        password = _generate_password()
        created_at = _utc_now()
        password_hash = _hash_secret(password, salt)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provisioning_credentials (
                    credential_id, session_id, username, password_hash, salt,
                    created_at, displayed_at, must_change_password
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    session_id,
                    username,
                    password_hash,
                    salt,
                    created_at,
                    created_at,
                    1,
                ),
            )
        return TemporaryCredential(
            credential_id=credential_id,
            session_id=session_id,
            username=username,
            temporary_password=password,
            must_change_password=True,
            display_once=True,
            created_at=created_at,
        )

    def get_display_record(self, credential_id: str) -> TemporaryCredential | None:
        """Return metadata only; the temporary password is intentionally not recoverable."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provisioning_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            return None
        return TemporaryCredential(
            credential_id=row["credential_id"],
            session_id=row["session_id"],
            username=row["username"],
            temporary_password=None,
            must_change_password=bool(row["must_change_password"]),
            display_once=True,
            created_at=row["created_at"],
        )

    def verify_password(self, credential_id: str, candidate_password: str) -> bool:
        """Verify a candidate password without exposing the stored secret."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash, salt FROM provisioning_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            return False
        return secrets.compare_digest(row["password_hash"], _hash_secret(candidate_password, row["salt"]))
