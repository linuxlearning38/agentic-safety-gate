#!/usr/bin/env python3
"""Regression checks for Phase 9 runner bridge queue/result contracts."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.runner import (  # noqa: E402
    JOB_QUEUE_KEY,
    ProvisioningJobResult,
    ProvisioningResultWriter,
    RedisProvisioningJobQueue,
)
from provisioning.runner.host_runner import (  # noqa: E402
    HostRunner,
    _ISO_UNLINK_ATTEMPTS,
    _write_cloud_init_seed,
)


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expirations: dict[str, int] = {}

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def blpop(self, key, timeout=0):
        values = self.lists.get(key) or []
        if not values:
            return None
        return (key, values.pop(0))

    def set(self, key, value, ex=None):
        self.values[key] = value
        if ex:
            self.expirations[key] = ex

    def get(self, key):
        return self.values.get(key)

    def expire(self, key, ttl):
        self.expirations[key] = ttl


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def test_seed_iso_cleanup_ordering() -> list[bool]:
    """closemedium dvd must be called before seed_iso.unlink().

    VBoxSVC on Windows holds an open file handle for any medium registered in
    VirtualBox's global media registry.  storageattach --medium none removes the
    ISO from the storage controller but does NOT remove it from the registry.
    closemedium dvd <path> does remove it, which releases the handle so that
    the file can be deleted with unlink().

    This test verifies:
    - storageattach --medium none is issued first
    - closemedium dvd is issued second (and the file still exists at that point)
    - seed ISO file is gone after _cleanup_secret_files runs
    """
    tmp = Path(tempfile.mkdtemp())
    seed_iso = tmp / "seed.iso"
    seed_dir = tmp / "seed"
    seed_dir.mkdir()
    seed_iso.write_bytes(b"fake-iso-content")

    call_log: list[tuple[str, ...]] = []
    file_present_at_closemedium: list[bool] = []

    class TrackingAdapter:
        def _run_vboxmanage(self, *args: str, check: bool = True, timeout: int = 120):
            call_log.append(args)
            if args and args[0] == "closemedium":
                file_present_at_closemedium.append(seed_iso.exists())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = HostRunner.__new__(HostRunner)
    runner.adapter = TrackingAdapter()

    try:
        runner._detach_seed_iso("test-vm-01", seed_iso)
        runner._cleanup_secret_files(tmp, seed_dir, seed_iso)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    commands = [c[0] for c in call_log]
    storageattach_idx = next((i for i, c in enumerate(commands) if c == "storageattach"), -1)
    closemedium_idx = next((i for i, c in enumerate(commands) if c == "closemedium"), -1)
    closemedium_args = next((c for c in call_log if c[0] == "closemedium"), ())

    return [
        check("storageattach --medium none was issued", storageattach_idx != -1),
        check("storageattach uses forceunmount", any(c[0] == "storageattach" and "--forceunmount" in c for c in call_log)),
        check("closemedium dvd was issued", closemedium_idx != -1 and "dvd" in closemedium_args),
        check("storageattach precedes closemedium in call order", 0 <= storageattach_idx < closemedium_idx),
        check("seed ISO still existed when closemedium was called", bool(file_present_at_closemedium) and file_present_at_closemedium[0]),
        check("seed ISO deleted after cleanup", not seed_iso.exists()),
    ]


def test_seed_iso_closemedium_uuid_fallback() -> list[bool]:
    tmp = Path(tempfile.mkdtemp())
    seed_iso = tmp / "seed.iso"
    seed_iso.write_bytes(b"fake-iso-content")
    seed_uuid = "bfc35d86-b0a4-4e7a-878a-4ed956991433"
    call_log: list[tuple[str, ...]] = []

    class TrackingAdapter:
        def _run_vboxmanage(self, *args: str, check: bool = True, timeout: int = 120):
            call_log.append(args)
            if args[:3] == ("closemedium", "dvd", str(seed_iso)):
                return SimpleNamespace(returncode=1, stdout="", stderr="medium is still locked")
            if args == ("list", "dvds"):
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"UUID:           {seed_uuid}\nLocation:       {seed_iso}\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = HostRunner.__new__(HostRunner)
    runner.adapter = TrackingAdapter()

    try:
        runner._detach_seed_iso("test-vm-01", seed_iso)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return [
        check("failed path close triggers dvd list fallback", any(c == ("list", "dvds") for c in call_log)),
        check("uuid closemedium fallback is issued", ("closemedium", "dvd", seed_uuid) in call_log),
    ]


def test_cloud_init_seed_quotes_generated_values() -> list[bool]:
    tmp = Path(tempfile.mkdtemp())
    password = 'xE:two#bad"chars'
    public_key = "ssh-ed25519 AAAATEST/with+symbols ava-runner"
    try:
        _write_cloud_init_seed(tmp, "ava-web-test", "avaadmin", password, public_key)
        user_data = (tmp / "user-data").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return [
        check("cloud-init hostname is quoted", 'hostname: "ava-web-test"' in user_data),
        check("cloud-init username is quoted", 'name: "avaadmin"' in user_data),
        check("cloud-init password is quoted", 'password: "xE:two#bad\\"chars"' in user_data),
        check("cloud-init forces password change", "expire: true" in user_data),
        check("cloud-init public key is quoted", f"- {public_key!r}".replace("'", '"') in user_data),
    ]


def test_runner_verification_uses_key_auth() -> list[bool]:
    """ava-runner holds the SSH key; avaadmin has chage -d 0 (password expired by design).

    PAM blocks ALL SSH sessions—key auth included—when a password change is
    required but no TTY is available.  The fix seeds the runner public key into a
    separate ava-runner user that is NOT in the chpasswd expire list, so the
    runner can SSH in without hitting the PAM account check.
    """
    tmp = Path(tempfile.mkdtemp())
    public_key = "ssh-ed25519 AAAATEST/runner+key ava-runner-test"
    try:
        _write_cloud_init_seed(tmp, "ava-web-test", "avaadmin", "Temp!Pass1", public_key)
        user_data = (tmp / "user-data").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    avaadmin_block_start = user_data.find('name: "avaadmin"')
    ava_runner_block_start = user_data.find("name: ava-runner")
    key_pos = user_data.find(public_key)

    return [
        check("ava-runner user is present in user-data", ava_runner_block_start != -1),
        check("runner public key is seeded for ava-runner (not avaadmin)",
              key_pos != -1 and key_pos > ava_runner_block_start),
        check("avaadmin does not hold the runner public key",
              avaadmin_block_start == -1 or key_pos > avaadmin_block_start + (ava_runner_block_start - avaadmin_block_start)),
        check("ava-runner has sudo in user-data",
              user_data.count("sudo: ALL=(ALL) NOPASSWD:ALL") >= 2),
        check("ava-runner is not in chpasswd expire list",
              "ava-runner" not in user_data[user_data.find("chpasswd"):]),
        check("avaadmin password expiry is still enforced", "expire: true" in user_data),
    ]


def test_seed_iso_cleanup_retries_on_permission_error() -> list[bool]:
    """_cleanup_secret_files retries seed_iso.unlink() on PermissionError.

    VBoxSVC releases its file handle asynchronously after closemedium returns.
    The first few unlink attempts may raise PermissionError (WinError 32) before
    VBoxSVC surrenders the handle.  The runner must retry with backoff rather
    than immediately failing the job.
    """
    tmp = Path(tempfile.mkdtemp())
    seed_iso = tmp / "seed.iso"
    seed_dir = tmp / "seed"
    seed_dir.mkdir()
    seed_iso.write_bytes(b"fake-iso-content")

    fail_for = 3  # first 3 calls raise PermissionError; 4th succeeds
    unlink_calls = [0]
    original_unlink = Path.unlink
    original_sleep = time.sleep

    def tracked_unlink(self, missing_ok=False):
        if self == seed_iso:
            unlink_calls[0] += 1
            if unlink_calls[0] <= fail_for:
                raise PermissionError("[WinError 32] file is locked by another process")
        return original_unlink(self, missing_ok=missing_ok)

    runner = HostRunner.__new__(HostRunner)
    runner.logger = logging.getLogger("ava.test.cleanup_retry")

    raised = False
    try:
        Path.unlink = tracked_unlink
        time.sleep = lambda _: None
        runner._cleanup_secret_files(tmp, seed_dir, seed_iso)
    except Exception:
        raised = True
    finally:
        Path.unlink = original_unlink
        time.sleep = original_sleep
        shutil.rmtree(tmp, ignore_errors=True)

    return [
        check("no exception raised after retries succeeded", not raised),
        check(f"unlink retried until success ({fail_for} fails + 1 success)", unlink_calls[0] == fail_for + 1),
        check("seed ISO deleted after retries", not seed_iso.exists()),
    ]


def test_seed_iso_cleanup_fails_after_all_retries() -> list[bool]:
    """_cleanup_secret_files raises RuntimeError when every retry attempt fails."""
    tmp = Path(tempfile.mkdtemp())
    seed_iso = tmp / "seed.iso"
    seed_dir = tmp / "seed"
    seed_dir.mkdir()
    seed_iso.write_bytes(b"fake-iso-content")

    unlink_calls = [0]
    original_unlink = Path.unlink
    original_sleep = time.sleep

    def always_locked(self, missing_ok=False):
        if self == seed_iso:
            unlink_calls[0] += 1
            raise PermissionError("[WinError 32] file is locked by another process")
        return original_unlink(self, missing_ok=missing_ok)

    runner = HostRunner.__new__(HostRunner)
    runner.logger = logging.getLogger("ava.test.cleanup_fail")

    raised_cleanup_error = False
    try:
        Path.unlink = always_locked
        time.sleep = lambda _: None
        runner._cleanup_secret_files(tmp, seed_dir, seed_iso)
    except RuntimeError as exc:
        raised_cleanup_error = "Secret cleanup failed" in str(exc)
    finally:
        Path.unlink = original_unlink
        time.sleep = original_sleep
        shutil.rmtree(tmp, ignore_errors=True)

    return [
        check("RuntimeError raised after all retries exhausted", raised_cleanup_error),
        check(f"unlink attempted exactly {_ISO_UNLINK_ATTEMPTS} times", unlink_calls[0] == _ISO_UNLINK_ATTEMPTS),
    ]


def main() -> int:
    fake = FakeRedis()
    queue = RedisProvisioningJobQueue(client=fake, ttl_seconds=1800)
    failures: list[bool] = []

    job = queue.enqueue_approved_job(
        session_id="session-1",
        desired_state={
            "provider": "virtualbox",
            "os": "ubuntu",
            "role": "web_server",
            "vm_name": "ava-web-01",
            "cpu": 2,
            "ram_gb": 4,
            "disk_gb": 30,
            "network_mode": "nat",
            "firewall_profile": "web_public",
            "hardening_profile": "baseline_linux",
        },
        credential_id="cred-1",
        username="avaadmin",
        temporary_password="DoNotLogThis123!",
    )
    failures.extend(
        [
            check("job is enqueued", len(fake.lists.get(JOB_QUEUE_KEY, [])) == 1),
            check("queue key has ttl", fake.expirations.get(JOB_QUEUE_KEY) == 1800),
            check("job status is queued", queue.get_status(job.job_id) == "queued"),
            check("job contains short-lived seed secret", job.credentials_seed_data.get("temporary_password") == "DoNotLogThis123!"),
            check("redacted job view hides seed secret", "DoNotLogThis123!" not in str(job.to_dict(include_secret=False))),
        ]
    )

    claimed = queue.claim_next_job(timeout_seconds=1)
    failures.extend(
        [
            check("runner can claim job", claimed is not None),
            check("claim updates status", queue.get_status(job.job_id) == "picked_up"),
            check("claimed job preserves desired hostname", claimed.desired_state.get("vm_name") == "ava-web-01" if claimed else False),
        ]
    )

    writer = ProvisioningResultWriter(queue)
    writer.status(job.job_id, "provisioning")
    result = writer.completed(
        job_id=job.job_id,
        instance_id="ava-web-01",
        instance_name="ava-web-01",
        ssh_host="127.0.0.1",
        ssh_port=2222,
        http_port=8080,
        verification_evidence={"checks": [{"name": "host_http_200", "passed": True}]},
    )
    loaded = queue.get_result(job.job_id)
    failures.extend(
        [
            check("writer marks completed", queue.get_status(job.job_id) == "completed"),
            check("result includes ssh host", loaded is not None and loaded.ssh_host == "127.0.0.1"),
            check("result includes ssh port", loaded is not None and loaded.ssh_port == 2222),
            check("result excludes temporary password", "DoNotLogThis123!" not in str(result.to_dict())),
        ]
    )

    failed = writer.failed(
        job_id="failed-job",
        instance_id=None,
        instance_name=None,
        error={"message": "bad password DoNotLogThis123!", "temporary_password": "DoNotLogThis123!"},
    )
    failures.extend(
        [
            check("failed result redacts password field", failed.error.get("temporary_password") == "[REDACTED]"),
            check("failed result redacts message secret", "DoNotLogThis123!" not in str(failed.to_dict())),
        ]
    )

    print("\n--- cleanup ordering (Phase 9 fix: closemedium before unlink) ---")
    failures.extend(test_seed_iso_cleanup_ordering())
    failures.extend(test_seed_iso_closemedium_uuid_fallback())
    failures.extend(test_cloud_init_seed_quotes_generated_values())

    print("\n--- runner verification key auth (Phase 9 fix: ava-runner user, no PAM expiry) ---")
    failures.extend(test_runner_verification_uses_key_auth())

    print("\n--- seed.iso cleanup retry (Phase 9 fix: WinError 32 race with VBoxSVC) ---")
    failures.extend(test_seed_iso_cleanup_retries_on_permission_error())
    failures.extend(test_seed_iso_cleanup_fails_after_all_retries())

    failed_count = len([item for item in failures if not item])
    if failed_count:
        print(f"\nPhase 9 runner bridge regression failed: {failed_count} issue(s)")
        return 1
    print("\nPhase 9 runner bridge regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
