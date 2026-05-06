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
    DAY2_OPERATION_QUEUE_KEY,
    JOB_QUEUE_KEY,
    RUNNER_HEARTBEAT_KEY,
    Day2OperationResult,
    ProvisioningJobResult,
    ProvisioningResultWriter,
    RedisProvisioningJobQueue,
)
from provisioning.runner.host_runner import (  # noqa: E402
    HostRunner,
    HostRunnerConfig,
    _ISO_UNLINK_ATTEMPTS,
    _windows_private_key_acl_command,
    _write_cloud_init_seed,
)
from provisioning.runner.result_writer import ProvisioningResultWriter as _ResultWriter  # noqa: E402


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


def test_cleanup_failure_after_verification_does_not_rollback() -> list[bool]:
    """Cleanup failure after full verification marks the job completed, not failed.

    VBoxSVC may hold seed.iso for longer than the retry window. When the VM is
    already verified (HTTP 200 + VerificationEngine passed), the cleanup failure
    must NOT trigger Phase 7 rollback or call writer.failed. The job must be
    marked completed with a cleanup_warning in verification_evidence.
    """
    import provisioning.runner.host_runner as hr_mod

    tmp = Path(tempfile.mkdtemp())
    work_root = tmp / "runner-work"

    fake_redis = FakeRedis()
    queue = RedisProvisioningJobQueue(client=fake_redis, ttl_seconds=1800)
    queue.enqueue_approved_job(
        session_id="session-cleanup-race-test",
        desired_state={
            "provider": "virtualbox", "os": "ubuntu", "role": "web_server",
            "vm_name": "ava-web-cleanup-test", "cpu": 2, "ram_gb": 4, "disk_gb": 30,
            "network_mode": "nat", "firewall_profile": "web_public",
            "hardening_profile": "baseline_linux",
        },
        credential_id="cred-cleanup-race",
        username="avaadmin",
        temporary_password="CleanupRace1!",
    )
    job = queue.claim_next_job(timeout_seconds=1)

    # Pre-create work_dir so execute_job's mkdir(exist_ok=True) is a no-op.
    # This lets us seed the files that mocked _run would otherwise create.
    work_dir = work_root / job.job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "ava_runner_ed25519").write_text("fake-private-key", encoding="utf-8")
    (work_dir / "ava_runner_ed25519.pub").write_text("ssh-ed25519 AAAAFAKEKEY ava-runner-test", encoding="utf-8")
    seed_iso = work_dir / "seed.iso"
    seed_iso.write_bytes(b"fake-iso-secret-content")

    destroy_called = [False]

    class MockAdapter:
        image_name = "ubuntu-cloud-image"
        def plan_instance(self, ds): return SimpleNamespace(vm_name="ava-web-cleanup-test")
        def create_instance(self, plan): return "ava-web-cleanup-test"
        def inject_access(self, iid, cfg): return "cloud_init_seed_attached"
        def get_connection_info(self, iid):
            return SimpleNamespace(host="127.0.0.1", port=2222, metadata={"http_host_port": 8080})
        def start_instance(self, iid): pass
        def _run_vboxmanage(self, *args, check=True, timeout=120):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        def get_instance_state(self, iid): return SimpleNamespace(exists=True)
        def destroy_instance(self, iid):
            destroy_called[0] = True
            return "destroyed"

    config = HostRunnerConfig(
        vboxmanage="VBoxManage", ssh_binary="ssh", ssh_keygen="ssh-keygen",
        template_name="ubuntu-cloud-image", work_root=work_root,
        log_path=tmp / "test_runner.log", retain_debug=False,
        timeout_seconds=30, max_jobs=1,
    )

    runner = HostRunner.__new__(HostRunner)
    runner.queue = queue
    runner.writer = _ResultWriter(queue)
    runner.config = config
    runner.adapter = MockAdapter()
    runner.logger = logging.getLogger("ava.test.cleanup_race")

    warnings_logged: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings_logged.append(record.getMessage())

    cap_handler = _CapturingHandler()
    runner.logger.addHandler(cap_handler)

    cloud_init_result = SimpleNamespace(
        exit_code=0,
        stdout="AVA_CLOUD_INIT_READY ava-web-cleanup-test\n",
        stderr="", failure_class="none",
    )
    mock_report = SimpleNamespace(
        passed=True,
        to_dict=lambda: {"checks": [{"name": "host_http_200", "passed": True}]},
    )

    class _MockWebServerRole:
        def bootstrap(self, executor):
            return [SimpleNamespace(exit_code=0, stdout="", stderr="", command="apt install nginx")]

    class _MockVerificationEngine:
        def __init__(self, adapter, executor_factory=None): pass
        def verify_web_server(self, iid): return mock_report

    original_validate = HostRunner._validate_binaries
    original_run = hr_mod._run
    original_wait_tcp = hr_mod._wait_for_tcp
    original_wait_exec = hr_mod._wait_for_executor_command
    original_wait_http = hr_mod._wait_for_http_200
    original_ssh_executor = hr_mod.SSHExecutor
    original_web_server_role = hr_mod.WebServerRole
    original_verification_engine = hr_mod.VerificationEngine
    original_unlink = Path.unlink
    original_sleep = time.sleep

    def always_locked_iso(self_path, missing_ok=False):
        if self_path.suffix == ".iso":
            raise PermissionError("[WinError 32] seed.iso locked by VBoxSVC")
        return original_unlink(self_path, missing_ok=missing_ok)

    try:
        HostRunner._validate_binaries = lambda self: None
        hr_mod._run = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
        hr_mod._wait_for_tcp = lambda host, port, timeout, **kw: True
        hr_mod._wait_for_executor_command = lambda executor, cmd, timeout, *, redact, **kw: cloud_init_result
        hr_mod._wait_for_http_200 = lambda url, timeout_seconds, **kw: (True, "HTTP 200")
        hr_mod.SSHExecutor = lambda conn: SimpleNamespace(run=lambda cmd, **kw: cloud_init_result)
        hr_mod.WebServerRole = _MockWebServerRole
        hr_mod.VerificationEngine = _MockVerificationEngine
        Path.unlink = always_locked_iso
        time.sleep = lambda _: None
        runner.execute_job(job)
    finally:
        HostRunner._validate_binaries = original_validate
        hr_mod._run = original_run
        hr_mod._wait_for_tcp = original_wait_tcp
        hr_mod._wait_for_executor_command = original_wait_exec
        hr_mod._wait_for_http_200 = original_wait_http
        hr_mod.SSHExecutor = original_ssh_executor
        hr_mod.WebServerRole = original_web_server_role
        hr_mod.VerificationEngine = original_verification_engine
        Path.unlink = original_unlink
        time.sleep = original_sleep
        runner.logger.removeHandler(cap_handler)
        shutil.rmtree(tmp, ignore_errors=True)

    final_status = queue.get_status(job.job_id)
    final_result = queue.get_result(job.job_id)

    return [
        check("VM not destroyed by Phase 7 rollback", not destroy_called[0]),
        check("job status is completed (not failed)", final_status == "completed"),
        check("result includes ssh_host", final_result is not None and final_result.ssh_host == "127.0.0.1"),
        check("cleanup_warning recorded in verification_evidence",
              final_result is not None and "cleanup_warning" in (final_result.verification_evidence or {})),
        check("warning was logged about manual cleanup",
              any("seed.iso cleanup failed" in w or "manual cleanup" in w for w in warnings_logged)),
    ]


def test_day2_snapshot_execution_uses_vboxmanage() -> list[bool]:
    fake = FakeRedis()
    queue = RedisProvisioningJobQueue(client=fake, ttl_seconds=1800)
    operation = queue.enqueue_day2_operation(
        session_id="session-day2-snapshot",
        operation="snapshot",
        target="virtualbox_vm",
        instance_id="ava-web-day2",
        instance_name="ava-web-day2",
        ssh_host="127.0.0.1",
        ssh_port=2222,
        http_port=8080,
        metadata={"approval_id": "abc12345"},
    )
    claimed = queue.claim_next_day2_operation(timeout_seconds=1)

    vbox_calls: list[tuple[str, ...]] = []

    class MockAdapter:
        def get_instance_state(self, iid):
            return SimpleNamespace(exists=True, power_state="running")

        def _run_vboxmanage(self, *args, timeout=120, check=True):
            vbox_calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = HostRunner.__new__(HostRunner)
    runner.queue = queue
    runner.config = HostRunnerConfig(
        vboxmanage="VBoxManage",
        ssh_binary="ssh",
        ssh_keygen="ssh-keygen",
        template_name="ubuntu-cloud-image",
        work_root=Path(tempfile.gettempdir()) / "ava-day2-test",
        log_path=Path(tempfile.gettempdir()) / "ava-day2-test.log",
        retain_debug=False,
        timeout_seconds=30,
        max_jobs=1,
    )
    runner.adapter = MockAdapter()
    runner.logger = logging.getLogger("ava.test.day2_snapshot")

    original_validate = HostRunner._validate_vboxmanage
    try:
        HostRunner._validate_vboxmanage = lambda self: None
        runner.execute_day2_operation(claimed)
    finally:
        HostRunner._validate_vboxmanage = original_validate

    loaded = queue.get_day2_result(operation.operation_id)
    snapshot_call = next((call for call in vbox_calls if call[:3] == ("snapshot", "ava-web-day2", "take")), ())

    return [
        check("day-2 operation is enqueued", len(fake.lists.get(DAY2_OPERATION_QUEUE_KEY, [])) == 0),
        check("day-2 claim marks picked_up before execution", claimed is not None),
        check("snapshot uses VBoxManage snapshot take", bool(snapshot_call)),
        check("running VM snapshot uses live flag", "--live" in snapshot_call),
        check("day-2 operation status is completed", queue.get_day2_status(operation.operation_id) == "completed"),
        check("day-2 result is stored", loaded is not None and loaded.status == "completed"),
        check("day-2 result includes snapshot evidence", loaded is not None and bool(loaded.evidence.get("snapshot_name"))),
    ]


def test_day2_live_verify_uses_fresh_host_checks() -> list[bool]:
    fake = FakeRedis()
    queue = RedisProvisioningJobQueue(client=fake, ttl_seconds=1800)
    operation = queue.enqueue_day2_operation(
        session_id="session-day2-verify",
        operation="verify",
        target="web_server",
        instance_id="ava-web-day2",
        instance_name="ava-web-day2",
        ssh_host="127.0.0.1",
        ssh_port=2222,
        http_port=8080,
        metadata={"read_only": True},
    )
    claimed = queue.claim_next_day2_operation(timeout_seconds=1)

    class MockAdapter:
        def get_instance_state(self, iid):
            return SimpleNamespace(exists=True, power_state="running", provider_status="running")

        def get_connection_info(self, iid):
            return SimpleNamespace(host="127.0.0.1", port=2222, metadata={"http_host_port": 8080})

    runner = HostRunner.__new__(HostRunner)
    runner.queue = queue
    runner.config = HostRunnerConfig(
        vboxmanage="VBoxManage",
        ssh_binary="ssh",
        ssh_keygen="ssh-keygen",
        template_name="ubuntu-cloud-image",
        work_root=Path(tempfile.gettempdir()) / "ava-day2-verify-test",
        log_path=Path(tempfile.gettempdir()) / "ava-day2-verify-test.log",
        retain_debug=False,
        timeout_seconds=30,
        max_jobs=1,
    )
    runner.adapter = MockAdapter()
    runner.logger = logging.getLogger("ava.test.day2_verify")

    import provisioning.runner.host_runner as hr_mod

    original_validate = HostRunner._validate_vboxmanage
    original_wait_tcp = hr_mod._wait_for_tcp
    original_wait_http = hr_mod._wait_for_http_200
    try:
        HostRunner._validate_vboxmanage = lambda self: None
        hr_mod._wait_for_tcp = lambda host, port, timeout_seconds, **kw: True
        hr_mod._wait_for_http_200 = lambda url, timeout_seconds, **kw: (True, "HTTP 200")
        runner.execute_day2_operation(claimed)
    finally:
        HostRunner._validate_vboxmanage = original_validate
        hr_mod._wait_for_tcp = original_wait_tcp
        hr_mod._wait_for_http_200 = original_wait_http

    loaded = queue.get_day2_result(operation.operation_id)
    checks = (loaded.evidence or {}).get("checks") if loaded else []
    check_names = {item.get("name") for item in checks if isinstance(item, dict)}

    return [
        check("live verify operation status is completed", queue.get_day2_status(operation.operation_id) == "completed"),
        check("live verify result is stored", loaded is not None and loaded.status == "completed"),
        check("live verify checks VM existence", "vm_exists" in check_names),
        check("live verify checks SSH TCP reachability", "ssh_tcp_reachable" in check_names),
        check("live verify checks host HTTP 200", "host_http_200" in check_names),
    ]


def test_day2_nginx_logs_uses_retained_runner_key() -> list[bool]:
    tmp = Path(tempfile.mkdtemp())
    fake = FakeRedis()
    queue = RedisProvisioningJobQueue(client=fake, ttl_seconds=1800)
    key_path = tmp / "keys" / "ava-web-day2_ava_runner_ed25519"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("fake-private-key", encoding="utf-8")
    operation = queue.enqueue_day2_operation(
        session_id="session-day2-logs",
        operation="nginx_logs",
        target="nginx",
        instance_id="ava-web-day2",
        instance_name="ava-web-day2",
        ssh_host="127.0.0.1",
        ssh_port=2222,
        http_port=8080,
        metadata={"read_only": True},
    )
    claimed = queue.claim_next_day2_operation(timeout_seconds=1)

    class MockAdapter:
        def get_connection_info(self, iid):
            return SimpleNamespace(host="127.0.0.1", port=2222, metadata={"http_host_port": 8080})

    class MockSSHExecutor:
        def __init__(self, connection):
            self.connection = connection

        def run(self, command, **kwargs):
            stdout = (
                "nginx.service active\n"
                "--- AVA_ACCESS_LOG ---\n"
                "GET / HTTP/1.1 200\n"
                "--- AVA_ERROR_LOG ---\n"
                "no errors\n"
            )
            return SimpleNamespace(exit_code=0, stdout=stdout, stderr="", failure_class=None)

    runner = HostRunner.__new__(HostRunner)
    runner.queue = queue
    runner.config = HostRunnerConfig(
        vboxmanage="VBoxManage",
        ssh_binary="ssh",
        ssh_keygen="ssh-keygen",
        template_name="ubuntu-cloud-image",
        work_root=tmp,
        log_path=tmp / "ava-day2-logs-test.log",
        retain_debug=False,
        timeout_seconds=30,
        max_jobs=1,
    )
    runner.adapter = MockAdapter()
    runner.logger = logging.getLogger("ava.test.day2_logs")

    import provisioning.runner.host_runner as hr_mod

    original_validate = HostRunner._validate_binaries
    original_ssh_executor = hr_mod.SSHExecutor
    try:
        HostRunner._validate_binaries = lambda self: None
        hr_mod.SSHExecutor = MockSSHExecutor
        runner.execute_day2_operation(claimed)
    finally:
        HostRunner._validate_binaries = original_validate
        hr_mod.SSHExecutor = original_ssh_executor
        shutil.rmtree(tmp, ignore_errors=True)

    loaded = queue.get_day2_result(operation.operation_id)
    evidence = loaded.evidence if loaded else {}
    return [
        check("nginx logs operation status is completed", queue.get_day2_status(operation.operation_id) == "completed"),
        check("nginx logs result is stored", loaded is not None and loaded.status == "completed"),
        check("nginx logs include service journal", "nginx.service active" in str(evidence.get("journalctl_tail"))),
        check("nginx logs include access tail", "GET / HTTP/1.1 200" in str(evidence.get("access_log_tail"))),
        check("nginx logs include error tail", "no errors" in str(evidence.get("error_log_tail"))),
    ]


def test_windows_runner_key_acl_command_restricts_private_key() -> list[bool]:
    key_path = Path(r"C:\ava test\keys\ava-web_ava_runner_ed25519")
    command = _windows_private_key_acl_command(key_path)
    script = command[-1]
    return [
        check("private key ACL command uses PowerShell", command[:4] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass"]),
        check("private key ACL command disables inherited access", "SetAccessRuleProtection($true, $false)" in script),
        check("private key ACL command removes broad existing rules", "RemoveAccessRuleAll" in script),
        check("private key ACL command grants only current runner identity", "WindowsIdentity]::GetCurrent().User" in script),
        check("private key ACL command protects paths with spaces", "'C:\\ava test\\keys\\ava-web_ava_runner_ed25519'" in script),
    ]


def test_day2_key_lookup_self_heals_private_key_permissions() -> list[bool]:
    tmp = Path(tempfile.mkdtemp())
    key_path = tmp / "keys" / "ava-web-day2_ava_runner_ed25519"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("fake-private-key", encoding="utf-8")

    runner = HostRunner.__new__(HostRunner)
    runner.config = HostRunnerConfig(
        vboxmanage="VBoxManage",
        ssh_binary="ssh",
        ssh_keygen="ssh-keygen",
        template_name="ubuntu-cloud-image",
        work_root=tmp,
        log_path=tmp / "ava-day2-key-self-heal-test.log",
        retain_debug=False,
        timeout_seconds=30,
        max_jobs=1,
    )
    operation = SimpleNamespace(
        instance_id="ava-web-day2",
        instance_name="ava-web-day2",
        metadata={},
    )

    import provisioning.runner.host_runner as hr_mod

    locked: list[Path] = []
    original_lock_down = hr_mod._lock_down_private_key
    try:
        hr_mod._lock_down_private_key = lambda path: locked.append(path)
        found = runner._find_runner_key_path(operation)
    finally:
        hr_mod._lock_down_private_key = original_lock_down
        shutil.rmtree(tmp, ignore_errors=True)

    return [
        check("retained key lookup finds private key", found == key_path),
        check("retained key lookup locks down private key before use", locked == [key_path]),
    ]


def test_day2_nginx_logs_fail_without_runner_key() -> list[bool]:
    fake = FakeRedis()
    queue = RedisProvisioningJobQueue(client=fake, ttl_seconds=1800)
    operation = queue.enqueue_day2_operation(
        session_id="session-day2-logs-missing-key",
        operation="nginx_logs",
        target="nginx",
        instance_id="ava-web-missing-key",
        instance_name="ava-web-missing-key",
        ssh_host="127.0.0.1",
        ssh_port=2222,
        http_port=8080,
        metadata={"read_only": True},
    )
    claimed = queue.claim_next_day2_operation(timeout_seconds=1)
    runner = HostRunner.__new__(HostRunner)
    runner.queue = queue
    runner.config = HostRunnerConfig(
        vboxmanage="VBoxManage",
        ssh_binary="ssh",
        ssh_keygen="ssh-keygen",
        template_name="ubuntu-cloud-image",
        work_root=Path(tempfile.gettempdir()) / "ava-day2-missing-key-test",
        log_path=Path(tempfile.gettempdir()) / "ava-day2-missing-key-test.log",
        retain_debug=False,
        timeout_seconds=30,
        max_jobs=1,
    )
    runner.adapter = SimpleNamespace()
    runner.logger = logging.getLogger("ava.test.day2_missing_key")

    original_validate = HostRunner._validate_binaries
    try:
        HostRunner._validate_binaries = lambda self: None
        runner.execute_day2_operation(claimed)
    finally:
        HostRunner._validate_binaries = original_validate

    loaded = queue.get_day2_result(operation.operation_id)
    return [
        check("missing-key nginx logs status is failed", queue.get_day2_status(operation.operation_id) == "failed"),
        check("missing-key nginx logs result is stored", loaded is not None and loaded.status == "failed"),
        check("missing-key nginx logs explains runner key", loaded is not None and "Runner SSH key" in str(loaded.error)),
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
    queue.write_runner_heartbeat("idle", {"pid": 1234})
    failures.extend(
        [
            check("runner heartbeat is written", bool(fake.values.get(RUNNER_HEARTBEAT_KEY))),
            check("runner heartbeat has ttl", fake.expirations.get(RUNNER_HEARTBEAT_KEY) == 90),
            check("runner health reads heartbeat", queue.is_runner_healthy() is True),
        ]
    )

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

    print("\n--- cleanup race must not destroy verified VM (Phase 9 critical fix) ---")
    failures.extend(test_cleanup_failure_after_verification_does_not_rollback())

    print("\n--- server-management operation queue (v2.1 snapshot execution) ---")
    failures.extend(test_day2_snapshot_execution_uses_vboxmanage())
    failures.extend(test_day2_live_verify_uses_fresh_host_checks())
    failures.extend(test_day2_nginx_logs_uses_retained_runner_key())
    failures.extend(test_windows_runner_key_acl_command_restricts_private_key())
    failures.extend(test_day2_key_lookup_self_heals_private_key_permissions())
    failures.extend(test_day2_nginx_logs_fail_without_runner_key())

    failed_count = len([item for item in failures if not item])
    if failed_count:
        print(f"\nPhase 9 runner bridge regression failed: {failed_count} issue(s)")
        return 1
    print("\nPhase 9 runner bridge regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
