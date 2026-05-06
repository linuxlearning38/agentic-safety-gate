"""Redis-backed job queue for the AVA v2 host-side runner bridge."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import urlparse, unquote
import socket
from typing import Any, Protocol
from uuid import uuid4


JOB_QUEUE_KEY = "ava:provisioning:jobs:approved"
STATUS_KEY_PREFIX = "ava:provisioning:jobs:status:"
RESULT_KEY_PREFIX = "ava:provisioning:jobs:result:"
RUNNER_HEARTBEAT_KEY = "ava:provisioning:runner:heartbeat"
RUNNER_HEARTBEAT_TTL_SECONDS = 90
ALLOWED_STATUSES = {
    "queued",
    "picked_up",
    "provisioning",
    "bootstrapping",
    "hardening",
    "verifying",
    "completed",
    "failed",
    "cancelled",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_redis_url() -> str:
    return (
        os.getenv("AVA_PROVISIONING_REDIS_URL")
        or os.getenv("REDIS_URL")
        or os.getenv("RATELIMIT_STORAGE_URI")
        or "redis://127.0.0.1:6379/0"
    )


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | bytes | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


@dataclass(slots=True)
class ProvisioningJob:
    """Approved provisioning job consumed by the Windows host runner."""

    job_id: str
    session_id: str
    desired_state: dict[str, Any]
    credentials_seed_data: dict[str, Any]
    enqueued_at: str
    expires_at: str

    def to_dict(self, *, include_secret: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_secret:
            data["credentials_seed_data"] = {
                "credential_id": self.credentials_seed_data.get("credential_id"),
                "username": self.credentials_seed_data.get("username"),
                "temporary_password": "[REDACTED]" if self.credentials_seed_data.get("temporary_password") else None,
            }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProvisioningJob":
        return cls(
            job_id=str(data["job_id"]),
            session_id=str(data["session_id"]),
            desired_state=dict(data.get("desired_state") or {}),
            credentials_seed_data=dict(data.get("credentials_seed_data") or {}),
            enqueued_at=str(data["enqueued_at"]),
            expires_at=str(data["expires_at"]),
        )


@dataclass(slots=True)
class ProvisioningJobResult:
    """Runner result safe for AVA chat status/evidence responses."""

    job_id: str
    instance_id: str | None
    instance_name: str | None
    ssh_host: str | None = None
    ssh_port: int | None = None
    http_port: int | None = None
    verification_evidence: dict[str, Any] = field(default_factory=dict)
    completion_timestamp: str = field(default_factory=_utc_now)
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProvisioningJobResult":
        return cls(
            job_id=str(data["job_id"]),
            instance_id=data.get("instance_id"),
            instance_name=data.get("instance_name"),
            ssh_host=data.get("ssh_host"),
            ssh_port=int(data["ssh_port"]) if data.get("ssh_port") is not None else None,
            http_port=int(data["http_port"]) if data.get("http_port") is not None else None,
            verification_evidence=dict(data.get("verification_evidence") or {}),
            completion_timestamp=str(data.get("completion_timestamp") or _utc_now()),
            error=data.get("error"),
        )


class ProvisioningJobQueue(Protocol):
    """Small protocol used by serving and runner code."""

    def enqueue_approved_job(
        self,
        *,
        session_id: str,
        desired_state: dict[str, Any],
        credential_id: str,
        username: str,
        temporary_password: str,
    ) -> ProvisioningJob:
        ...

    def claim_next_job(self, *, timeout_seconds: int = 30) -> ProvisioningJob | None:
        ...

    def get_status(self, job_id: str) -> str | None:
        ...

    def write_status(self, job_id: str, status: str) -> None:
        ...

    def get_result(self, job_id: str) -> ProvisioningJobResult | None:
        ...

    def write_result(self, result: ProvisioningJobResult) -> None:
        ...

    def write_runner_heartbeat(self, status: str = "idle", metadata: dict[str, Any] | None = None) -> None:
        ...

    def is_runner_healthy(self) -> bool:
        ...


class RedisProvisioningJobQueue:
    """Redis implementation of the Phase 9 approved-job queue contract."""

    def __init__(self, redis_url: str | None = None, *, client: Any | None = None, ttl_seconds: int = 1800):
        self.redis_url = redis_url or _default_redis_url()
        self.client = client or _build_redis_client(self.redis_url)
        self.ttl_seconds = int(ttl_seconds)

    def enqueue_approved_job(
        self,
        *,
        session_id: str,
        desired_state: dict[str, Any],
        credential_id: str,
        username: str,
        temporary_password: str,
    ) -> ProvisioningJob:
        enqueued_at = _utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)).isoformat(timespec="seconds")
        job = ProvisioningJob(
            job_id=str(uuid4()),
            session_id=session_id,
            desired_state=dict(desired_state or {}),
            credentials_seed_data={
                "credential_id": credential_id,
                "username": username,
                "temporary_password": temporary_password,
            },
            enqueued_at=enqueued_at,
            expires_at=expires_at,
        )
        self.client.rpush(JOB_QUEUE_KEY, _json_dumps(job.to_dict()))
        self.client.expire(JOB_QUEUE_KEY, self.ttl_seconds)
        self.write_status(job.job_id, "queued")
        return job

    def claim_next_job(self, *, timeout_seconds: int = 30) -> ProvisioningJob | None:
        item = self.client.blpop(JOB_QUEUE_KEY, timeout=timeout_seconds)
        if not item:
            return None
        _queue_name, raw = item
        job = ProvisioningJob.from_dict(_json_loads(raw))
        current_status = self.get_status(job.job_id)
        if current_status == "cancelled":
            return None
        self.write_status(job.job_id, "picked_up")
        return job

    def get_status(self, job_id: str) -> str | None:
        value = self.client.get(_status_key(job_id))
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return value

    def write_status(self, job_id: str, status: str) -> None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Unsupported provisioning job status: {status}")
        self.client.set(_status_key(job_id), status, ex=self.ttl_seconds)

    def get_result(self, job_id: str) -> ProvisioningJobResult | None:
        raw = self.client.get(_result_key(job_id))
        if raw is None:
            return None
        return ProvisioningJobResult.from_dict(_json_loads(raw))

    def write_result(self, result: ProvisioningJobResult) -> None:
        self.client.set(_result_key(result.job_id), _json_dumps(result.to_dict()), ex=max(self.ttl_seconds, 86400))

    def write_runner_heartbeat(self, status: str = "idle", metadata: dict[str, Any] | None = None) -> None:
        self.client.set(
            RUNNER_HEARTBEAT_KEY,
            _json_dumps(
                {
                    "status": status,
                    "updated_at": _utc_now(),
                    "metadata": dict(metadata or {}),
                }
            ),
            ex=RUNNER_HEARTBEAT_TTL_SECONDS,
        )

    def is_runner_healthy(self) -> bool:
        return bool(self.client.get(RUNNER_HEARTBEAT_KEY))


def _status_key(job_id: str) -> str:
    return f"{STATUS_KEY_PREFIX}{job_id}"


def _result_key(job_id: str) -> str:
    return f"{RESULT_KEY_PREFIX}{job_id}"


def _build_redis_client(redis_url: str):
    try:
        import redis
    except ModuleNotFoundError:
        return SimpleRedisClient.from_url(redis_url)
    return redis.Redis.from_url(redis_url, decode_responses=True)


class SimpleRedisClient:
    """Tiny RESP client for the runner's required Redis commands.

    This keeps the Windows host runner usable even when the local Python
    environment has not installed the optional `redis` package yet.
    """

    def __init__(self, host: str, port: int, db: int = 0, username: str | None = None, password: str | None = None):
        self.host = host
        self.port = int(port)
        self.db = int(db)
        self.username = username
        self.password = password

    @classmethod
    def from_url(cls, redis_url: str) -> "SimpleRedisClient":
        parsed = urlparse(redis_url)
        db = int((parsed.path or "/0").lstrip("/") or "0")
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 6379,
            db=db,
            username=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
        )

    def rpush(self, key: str, value: str) -> int:
        return int(self._command("RPUSH", key, value) or 0)

    def blpop(self, key: str, timeout: int = 0):
        result = self._command("BLPOP", key, str(int(timeout)), timeout=int(timeout) + 5)
        if result is None:
            return None
        return tuple(result)

    def set(self, key: str, value: str, ex: int | None = None):
        if ex:
            return self._command("SET", key, value, "EX", str(int(ex)))
        return self._command("SET", key, value)

    def get(self, key: str):
        return self._command("GET", key)

    def expire(self, key: str, ttl: int):
        return self._command("EXPIRE", key, str(int(ttl)))

    def _command(self, *parts: str, timeout: int = 10):
        with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
            file = sock.makefile("rb")
            if self.password:
                if self.username:
                    self._send(sock, "AUTH", self.username, self.password)
                else:
                    self._send(sock, "AUTH", self.password)
                self._read_response(file)
            if self.db:
                self._send(sock, "SELECT", str(self.db))
                self._read_response(file)
            self._send(sock, *parts)
            return self._read_response(file)

    @staticmethod
    def _send(sock: socket.socket, *parts: str) -> None:
        encoded = [str(part).encode("utf-8") for part in parts]
        payload = [f"*{len(encoded)}\r\n".encode("utf-8")]
        for item in encoded:
            payload.append(f"${len(item)}\r\n".encode("utf-8"))
            payload.append(item + b"\r\n")
        sock.sendall(b"".join(payload))

    def _read_response(self, file):
        prefix = file.read(1)
        if not prefix:
            raise RuntimeError("Redis connection closed")
        if prefix == b"+":
            return file.readline().decode("utf-8").rstrip("\r\n")
        if prefix == b"-":
            raise RuntimeError(file.readline().decode("utf-8").rstrip("\r\n"))
        if prefix == b":":
            return int(file.readline().decode("utf-8").rstrip("\r\n"))
        if prefix == b"$":
            length = int(file.readline().decode("utf-8").rstrip("\r\n"))
            if length == -1:
                return None
            data = file.read(length)
            file.read(2)
            return data.decode("utf-8")
        if prefix == b"*":
            count = int(file.readline().decode("utf-8").rstrip("\r\n"))
            if count == -1:
                return None
            return [self._read_response(file) for _ in range(count)]
        raise RuntimeError(f"Unsupported Redis response prefix: {prefix!r}")
