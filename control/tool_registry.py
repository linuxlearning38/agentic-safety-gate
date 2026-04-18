# control/tool_registry.py
# AVA Phase 4 — Day 1: Tool Registry
#
# Design principles:
#   1. shell=False ALWAYS — arg lists only, no string interpolation into shell
#   2. Tools are pre-defined functions, not raw command strings
#   3. Each tool validates its own inputs before building the arg list
#   4. Registry owns execution — secure_executor delegates here for tool calls
#   5. Standardised return: {"status", "output", "error", "command_repr"}

import subprocess
import shlex
import re
import shutil
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Any, List, Optional, Tuple
from datetime import datetime

from control.docker_runtime import inspect_docker, list_containers
from control.vuln_scanner import scan_trivy, check_tools as check_vuln_tools
from control import database as db


# ─── Return type ──────────────────────────────────────────────────────────────

def _ok(output: str, command_repr: str) -> Dict:
    return {
        "status": "success",
        "output": output,
        "error": "",
        "command_repr": command_repr,
        "timestamp": datetime.now().isoformat(),
    }

def _fail(error: str, command_repr: str = "") -> Dict:
    return {
        "status": "failure",
        "output": "",
        "error": error,
        "command_repr": command_repr,
        "timestamp": datetime.now().isoformat(),
    }


def _metadata_result(output: str, command_repr: str, metadata: Dict[str, Any]) -> Dict:
    return {
        "status": "success",
        "output": output,
        "error": "",
        "command_repr": command_repr,
        "timestamp": datetime.now().isoformat(),
        "metadata": metadata,
    }


def _concern_metadata(
    title: str,
    severity: str,
    evidence: List[str],
    next_action: str,
    confidence: str = "medium",
    is_novel: bool = False,
) -> Dict[str, Any]:
    return {
        "title": title,
        "severity": severity,
        "evidence": evidence[:3],
        "next_action": next_action,
        "confidence": confidence,
        "is_novel": is_novel,
    }


def _select_primary_concern(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Weighted scoring for primary concern selection.

    Score = severity(×10) + confidence_bonus + evidence_count + novelty_bonus
    Novel findings (new since baseline) get +5 to surface over persistent background noise.
    High confidence adds +3, medium adds +1.  Up to 3 evidence items add +1 each.
    """
    if not candidates:
        return None
    severity_rank  = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    confidence_rank = {"high": 3, "medium": 1, "low": 0}

    def _score(item: Dict[str, Any]) -> int:
        sev     = severity_rank.get(str(item.get("severity", "")).lower(), 0)
        conf    = confidence_rank.get(str(item.get("confidence", "")).lower(), 1)
        ev_cnt  = min(len(item.get("evidence") or []), 3)
        novelty = 5 if item.get("is_novel", False) else 0
        return sev * 10 + conf + ev_cnt + novelty

    return sorted(candidates, key=_score, reverse=True)[0]


def _build_correlated_assessment(
    *,
    new_listeners: List[str],
    auth_failure_delta: int,
    auth_failure_count: int,
    new_failed_services: List[str],
    suspicious_listener_findings: List[str],
    suspicious_process_findings: List[str],
    unique_findings: List[str],
) -> Optional[Dict[str, Any]]:
    correlated_signals: List[str] = []
    evidence: List[str] = []

    if auth_failure_delta > 0:
        correlated_signals.append("auth_failure_spike")
        evidence.append(f"Authentication failures increased by +{auth_failure_delta} (current count: {auth_failure_count}).")
    if new_listeners:
        correlated_signals.append("new_listener")
        evidence.append(f"New listener observed since baseline: {new_listeners[0]}")
    if new_failed_services:
        correlated_signals.append("service_drift")
        evidence.append(f"New failed service since baseline: {new_failed_services[0]}")
    if suspicious_listener_findings:
        correlated_signals.append("suspicious_listener")
        evidence.append(suspicious_listener_findings[0])
    if suspicious_process_findings:
        correlated_signals.append("suspicious_process")
        evidence.append(suspicious_process_findings[0])

    signal_set = set(correlated_signals)

    if {"auth_failure_spike", "new_listener"} <= signal_set:
        return {
            "title": "Authentication pressure and new network exposure detected together",
            "severity": "high",
            "evidence": evidence[:3],
            "next_action": "inspect process <pid>",
            "confidence": "high",
            "is_novel": True,
            "signals": correlated_signals,
            "summary": "A new listening endpoint appearing alongside rising authentication failures can indicate service exposure or attacker-driven foothold activity.",
        }

    if {"new_listener", "suspicious_process"} <= signal_set:
        return {
            "title": "New listener appears to be backed by unusual process activity",
            "severity": "high",
            "evidence": evidence[:3],
            "next_action": "inspect process <pid>",
            "confidence": "high",
            "is_novel": True,
            "signals": correlated_signals,
            "summary": "An unexpected listener plus a process command worth review is stronger than either signal on its own.",
        }

    if {"auth_failure_spike", "service_drift"} <= signal_set:
        return {
            "title": "Service instability coincides with increased authentication failures",
            "severity": "medium",
            "evidence": evidence[:3],
            "next_action": "inspect service <name>",
            "confidence": "medium",
            "is_novel": True,
            "signals": correlated_signals,
            "summary": "Rising auth failures alongside newly failed services can indicate misconfiguration, lockout pressure, or hostile probing affecting availability.",
        }

    if len(signal_set) >= 3:
        return {
            "title": "Multiple suspicious signals are aligned in the same snapshot",
            "severity": "high",
            "evidence": evidence[:3],
            "next_action": "isolate the highest-risk process or service before applying broader remediation",
            "confidence": "medium",
            "is_novel": bool(new_listeners or new_failed_services or auth_failure_delta > 0),
            "signals": correlated_signals,
            "summary": "Several independent signals are pointing in the same direction, which raises confidence that this is more than background noise.",
        }

    if len(unique_findings) >= 2 and signal_set:
        return {
            "title": "More than one suspicious signal is present in the current snapshot",
            "severity": "medium",
            "evidence": evidence[:3],
            "next_action": "inspect the highest-risk process or service first",
            "confidence": "medium",
            "is_novel": bool(new_listeners or new_failed_services or auth_failure_delta > 0),
            "signals": correlated_signals,
            "summary": "Multiple low-to-medium signals together deserve investigation even if none is conclusive on its own.",
        }

    return None


def _build_host_risk_correlation(
    *,
    vuln_primary: Optional[Dict[str, Any]],
    suspicious_primary: Optional[Dict[str, Any]],
    suspicious_metadata: Dict[str, Any],
    vuln_summary: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    critical_count = int(vuln_summary.get("CRITICAL", 0) or 0)
    high_count = int(vuln_summary.get("HIGH", 0) or 0)
    new_listeners = list(suspicious_metadata.get("new_listeners") or [])
    auth_failure_delta = int(suspicious_metadata.get("auth_failure_delta", 0) or 0)
    new_failed_services = list(suspicious_metadata.get("new_failed_services") or [])

    if (critical_count > 0 or high_count > 0) and (new_listeners or auth_failure_delta > 0):
        evidence = [
            f"Runtime CVE summary: CRITICAL={critical_count}, HIGH={high_count}",
        ]
        if vuln_primary and vuln_primary.get("title"):
            evidence.append(str(vuln_primary["title"]))
        if new_listeners:
            evidence.append(f"New listener observed: {new_listeners[0]}")
        elif auth_failure_delta > 0:
            evidence.append(f"Authentication failures increased by +{auth_failure_delta}")
        return {
            "title": "Patch exposure and runtime drift are both elevated",
            "severity": "high" if critical_count > 0 else "medium",
            "evidence": evidence[:3],
            "next_action": vuln_primary.get("next_action") if vuln_primary else "scan my system for vulnerabilities",
            "confidence": "high" if critical_count > 0 and new_listeners else "medium",
            "is_novel": bool(new_listeners or auth_failure_delta > 0),
            "signals": ["runtime_cves", "runtime_drift"],
            "summary": "High-priority CVEs matter more when the host is also showing new exposure or suspicious drift.",
        }

    if (critical_count > 0 or high_count > 3) and new_failed_services:
        evidence = [
            f"Runtime CVE summary: CRITICAL={critical_count}, HIGH={high_count}",
            f"New failed service: {new_failed_services[0]}",
        ]
        if suspicious_primary and suspicious_primary.get("title"):
            evidence.append(str(suspicious_primary["title"]))
        return {
            "title": "Service instability is happening on a vulnerable runtime",
            "severity": "medium",
            "evidence": evidence[:3],
            "next_action": f"inspect service {new_failed_services[0]}" if new_failed_services else "check failed services",
            "confidence": "medium",
            "is_novel": True,
            "signals": ["runtime_cves", "service_drift"],
            "summary": "New service failures on a vulnerable runtime deserve investigation before they compound into a wider incident.",
        }

    return None


# ─── Safe subprocess helper ───────────────────────────────────────────────────

def _run(args: List[str], timeout: int = 15) -> Dict:
    """
    Execute a command with shell=False.
    args must be a list — never a string.
    """
    command_repr = shlex.join(args)
    try:
        result = subprocess.run(
            args,
            shell=False,          # ← NEVER shell=True
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return _ok(result.stdout.strip() or "(no output)", command_repr)
        else:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "Command returned a non-zero exit code with no output"
            # Non-zero exit is a failure, not an exception
            return _fail(
                f"Exit {result.returncode}: {detail}",
                command_repr,
            )
    except subprocess.TimeoutExpired:
        return _fail(f"Timed out after {timeout}s", command_repr)
    except FileNotFoundError:
        return _fail(f"Binary not found: {args[0]}", command_repr)
    except Exception as e:
        return _fail(str(e), command_repr)


# ─── Input validators ─────────────────────────────────────────────────────────

_SAFE_NAME   = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*$')   # pod/svc/node names
_SAFE_NS     = re.compile(r'^[a-z][a-z0-9\-]*$')                # k8s namespace
_SAFE_IMAGE  = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\./\:@]*$')  # container image
_SAFE_SVC    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*$')   # systemd service
_SAFE_PKG    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9+._:-]*$')


def _validate_pid(value: Any, label: str = "pid") -> Tuple[bool, str]:
    if isinstance(value, str):
        if not value.strip().isdigit():
            return False, f"{label} must be a positive integer"
        value = int(value.strip())
    if not isinstance(value, int) or value <= 0:
        return False, f"{label} must be a positive integer"
    if value > 999999:
        return False, f"{label} is unrealistically large"
    return True, ""

def _validate(value: str, pattern: re.Pattern, label: str) -> Tuple[bool, str]:
    if not value or not isinstance(value, str):
        return False, f"{label} must be a non-empty string"
    if len(value) > 253:
        return False, f"{label} too long (max 253 chars)"
    if not pattern.match(value):
        return False, f"{label} '{value}' contains invalid characters"
    return True, ""


# ─── Tool implementations ─────────────────────────────────────────────────────
# Each function:
#   - Accepts a dict of validated kwargs
#   - Builds a List[str] arg list (never string interpolation into shell)
#   - Returns _ok() or _fail()

def _check_pod_status(args: Dict) -> Dict:
    namespace = args.get("namespace", "default")
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    return _run(["kubectl", "get", "pods", "-n", namespace, "-o", "wide"])


def _check_logs(args: Dict) -> Dict:
    pod_name  = args.get("pod_name", "")
    namespace = args.get("namespace", "default")
    lines     = args.get("lines", 50)

    ok, err = _validate(pod_name, _SAFE_NAME, "pod_name")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    if not isinstance(lines, int) or not (1 <= lines <= 1000):
        return _fail("lines must be an integer between 1 and 1000")

    return _run([
        "kubectl", "logs", pod_name,
        "-n", namespace,
        "--tail", str(lines),
    ])


def _check_disk(args: Dict) -> Dict:
    return _run(["df", "-h"])


def _check_memory(args: Dict) -> Dict:
    return _run(["free", "-h"])


def _check_processes(args: Dict) -> Dict:
    result = _run(["ps", "aux", "--sort=-%cpu"])
    if result.get("status") != "success":
        return result
    lines = result.get("output", "").splitlines()
    trimmed = "\n".join(lines[:11]).strip() if lines else "(no output)"
    return _ok(trimmed, result.get("command_repr", "ps aux --sort=-%cpu"))


def _suggest_package_patch_command(package: str) -> str:
    if shutil.which("apt-get"):
        return f"apt-get install --only-upgrade -y {package}"
    if shutil.which("dnf"):
        return f"dnf upgrade -y {package}"
    if shutil.which("yum"):
        return f"yum update -y {package}"
    if shutil.which("apk"):
        return f"apk upgrade {package}"
    return f"upgrade package {package} with the local package manager"


def _process_remediation_suggestions(pid: int, command_text: str) -> List[str]:
    suggestions = [
        f"Inspect the process before killing it: inspect process {pid}",
        f"If confirmed malicious or stuck, queue: stop suspicious process {pid}",
    ]
    lower = (command_text or "").lower()
    if any(marker in lower for marker in ("python", "node", "java", "gunicorn", "nginx", "redis", "postgres")):
        suggestions.append("Check whether the process belongs to a known service before stopping it.")
    return suggestions


def _service_remediation_suggestions(service: str, status_output: str) -> List[str]:
    lowered = (status_output or "").lower()
    if (
        "systemd is not available" in lowered
        or "systemctl is not installed" in lowered
        or "systemd is not running" in lowered
    ):
        return [
            "This container does not run systemd. Inspect the relevant container process or the host service manager instead.",
            "If this service should exist on the host, run the inspection from the host environment rather than inside the AVA container.",
        ]
    suggestions = [f"Inspect the service in more detail: inspect service {service}"]
    if any(marker in lowered for marker in ("failed", "inactive", "dead", "error")):
        suggestions.append(f"If the service should be running, queue: restart service {service}")
    if "permission denied" in lowered:
        suggestions.append("Review file ownership, runtime user, and service unit permissions before restarting.")
    return suggestions


def _suspicious_listener_findings(listening_output: str) -> List[str]:
    findings: List[str] = []
    for line in (listening_output or "").splitlines():
        lower = line.lower()
        if not line.strip() or line.lower().startswith(("state", "proto", "active")):
            continue
        if "127.0.0.11:" in lower:
            continue
        if any(marker in lower for marker in ("nc ", "netcat", "socat", "python", "perl", "ruby")):
            findings.append(f"Possible ad-hoc network listener: {line.strip()}")
        if re.search(r":(4444|5555|6666|7777|1337|31337)\b", lower):
            findings.append(f"Unusual listening port: {line.strip()}")
    return findings[:10]


def _suspicious_process_findings(process_output: str) -> List[str]:
    findings: List[str] = []
    for line in (process_output or "").splitlines()[1:]:
        lower = line.lower()
        if any(marker in lower for marker in ("curl ", "wget ", "nc ", "netcat", "socat", "xmrig", "minerd")):
            findings.append(f"Process command worth review: {line.strip()}")
    return findings[:10]


def _normalize_listener_baseline(listening_output: str) -> List[str]:
    entries: List[str] = []
    for line in (listening_output or "").splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if not stripped or lower.startswith(("state", "proto", "active", "recv-q")):
            continue
        if "127.0.0.11:" in lower:
            continue
        entries.append(stripped)
    return entries[:100]


def _extract_auth_event_count(auth_output: str) -> int:
    match = re.search(r"Recent authentication failure markers \((\d+)\):", auth_output or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    if "No recent auth failure markers found" in (auth_output or ""):
        return 0
    return 0


def _extract_failed_service_names(service_output: str) -> List[str]:
    names: List[str] = []
    for line in (service_output or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(("unit ", "0 loaded units listed")):
            continue
        parts = stripped.split()
        if parts and parts[0].endswith(".service"):
            names.append(parts[0])
    return sorted(dict.fromkeys(names))[:50]


def _load_linux_operator_baseline() -> Dict[str, Any]:
    return db.get_memory("linux_operator_baseline", default={}) or {}


def _save_linux_operator_baseline(data: Dict[str, Any]) -> None:
    db.save_memory("linux_operator_baseline", data)


def _inspect_process(args: Dict) -> Dict:
    pid = args.get("pid")
    ok, err = _validate_pid(pid)
    if not ok:
        return _fail(err)
    pid = int(pid)
    result = _run(["ps", "-p", str(pid), "-o", "pid,ppid,user,%cpu,%mem,etime,stat,command"])
    if result.get("status") != "success":
        error_text = (result.get("error") or "").lower()
        if error_text.startswith("exit 1:"):
            return _fail(f"No process found for PID {pid}", f"inspect_process:{pid}")
        return result
    lines = [line for line in result.get("output", "").splitlines() if line.strip()]
    if len(lines) <= 1:
        return _fail(f"No process found for PID {pid}", f"ps -p {pid}")
    detail_sections = [result.get("output", "").strip()]
    proc_status_path = f"/proc/{pid}/status"
    if os.path.exists(proc_status_path):
        try:
            with open(proc_status_path, "r", encoding="utf-8", errors="replace") as handle:
                status_lines = []
                for line in handle:
                    if line.startswith(("Name:", "State:", "Uid:", "Gid:", "Threads:", "VmRSS:", "VmSize:")):
                        status_lines.append(line.strip())
                if status_lines:
                    detail_sections.append("[/proc status]\n" + "\n".join(status_lines))
        except Exception:
            pass
    remediation = _process_remediation_suggestions(pid, result.get("output", ""))
    if remediation:
        detail_sections.append("[Suggested Next Steps]\n" + "\n".join(f"- {item}" for item in remediation))
    return _metadata_result(
        "\n\n".join(detail_sections).strip(),
        f"inspect_process:{pid}",
        {
            "inspection_type": "process",
            "pid": pid,
            "suggested_actions": remediation,
        },
    )


def _check_listening_ports(args: Dict) -> Dict:
    if shutil.which("ss"):
        result = _run(["ss", "-ltnp"])
    elif shutil.which("netstat"):
        result = _run(["netstat", "-ltnp"])
    else:
        return _fail("Neither 'ss' nor 'netstat' is available")
    if result.get("status") != "success":
        return result
    lines = result.get("output", "").splitlines()
    trimmed = "\n".join(lines[:21]).strip() if lines else "(no output)"
    return _ok(trimmed, result.get("command_repr", "ss -ltnp"))


def _systemd_unavailable_message(action_label: str) -> str:
    return (
        f"{action_label} is unavailable in this AVA container because systemd is not running here. "
        "Use a host environment with systemd if you need real service-state inspection."
    )


def _check_failed_services(args: Dict) -> Dict:
    if not shutil.which("systemctl"):
        return _metadata_result(
            _systemd_unavailable_message("Failed service inspection"),
            "systemctl --failed",
            {
                "inspection_type": "failed_services",
                "failed_service_count": 0,
                "failed_services": [],
                "environment_note": "systemd_unavailable",
            },
        )
    result = _run(["systemctl", "--failed", "--no-pager", "--no-legend"])
    if result.get("status") != "success":
        return result
    output = result.get("output", "").strip()
    if not output or "0 loaded units listed" in output.lower():
        return _metadata_result(
            "No failed systemd units detected.",
            result.get("command_repr", "systemctl --failed"),
            {
                "inspection_type": "failed_services",
                "failed_service_count": 0,
                "failed_services": [],
            },
        )
    return _metadata_result(
        output,
        result.get("command_repr", "systemctl --failed"),
        {
            "inspection_type": "failed_services",
            "failed_service_count": len(_extract_failed_service_names(output)),
            "failed_services": _extract_failed_service_names(output),
        },
    )


def _inspect_service(args: Dict) -> Dict:
    service = args.get("service", "")
    ok, err = _validate(service, _SAFE_SVC, "service")
    if not ok:
        return _fail(err)
    sections: List[str] = []
    if not shutil.which("systemctl"):
        unavailable = _systemd_unavailable_message(f"Service inspection for '{service}'")
        remediation = _service_remediation_suggestions(service, unavailable)
        sections.append("[service inspection]\n" + unavailable)
        sections.append("[Suggested Next Steps]\n" + "\n".join(f"- {item}" for item in remediation))
        return _metadata_result(
            "\n\n".join(sections).strip(),
            f"inspect_service:{service}",
            {
                "inspection_type": "service",
                "service": service,
                "suggested_actions": remediation,
                "environment_note": "systemd_unavailable",
            },
        )
    status = _run(["systemctl", "status", service, "--no-pager"])
    if status.get("status") == "success":
        sections.append("[systemctl status]\n" + status.get("output", ""))
    else:
        sections.append("[systemctl status]\n" + status.get("error", "status unavailable"))

    if shutil.which("journalctl"):
        logs = _run(["journalctl", "-u", service, "-n", "40", "--no-pager"], timeout=20)
        if logs.get("status") == "success":
            sections.append("[recent logs]\n" + logs.get("output", ""))
        else:
            sections.append("[recent logs]\n" + logs.get("error", "logs unavailable"))

    remediation = _service_remediation_suggestions(service, "\n\n".join(sections))
    if remediation:
        sections.append("[Suggested Next Steps]\n" + "\n".join(f"- {item}" for item in remediation))

    return _metadata_result(
        "\n\n".join(section for section in sections if section).strip(),
        f"inspect_service:{service}",
        {
            "inspection_type": "service",
            "service": service,
            "suggested_actions": remediation,
        },
    )


def _check_auth_events(args: Dict) -> Dict:
    markers = (
        "failed password",
        "authentication failure",
        "invalid user",
        "failed publickey",
        "maximum authentication attempts exceeded",
    )
    log_paths = ["/var/log/auth.log", "/var/log/secure"]
    findings: List[str] = []

    for path in log_paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-400:]
            for line in lines:
                lower = line.lower()
                if any(marker in lower for marker in markers):
                    findings.append(line.strip())
        except FileNotFoundError:
            continue
        except Exception as exc:
            return _fail(f"Could not read auth log {path}: {exc}", f"read:{path}")

    if not findings and shutil.which("journalctl"):
        try:
            proc = subprocess.run(
                ["journalctl", "-n", "300", "--no-pager"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    lower = line.lower()
                    if any(marker in lower for marker in markers):
                        findings.append(line.strip())
        except Exception:
            pass

    if not findings:
        return _metadata_result(
            "No recent auth failure markers found in available logs.",
            "auth_events",
            {
                "inspection_type": "auth_events",
                "auth_failure_count": 0,
            },
        )

    findings = findings[-20:]
    output = f"Recent authentication failure markers ({len(findings)}):\n" + "\n".join(findings)
    return _metadata_result(
        output,
        "auth_events",
        {
            "inspection_type": "auth_events",
            "auth_failure_count": len(findings),
        },
    )


def _check_persistence_points(args: Dict) -> Dict:
    sections: List[str] = []

    cron_paths = ["/etc/crontab", "/etc/cron.d"]
    cron_entries: List[str] = []
    for path in cron_paths:
        if os.path.isdir(path):
            try:
                for entry in sorted(os.listdir(path))[:20]:
                    cron_entries.append(f"{path}/{entry}")
            except Exception:
                pass
        elif os.path.exists(path):
            cron_entries.append(path)
    sections.append("[Cron Paths]\n" + ("\n".join(cron_entries) if cron_entries else "No cron paths found."))

    if shutil.which("systemctl"):
        timers = _run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
        if timers.get("status") == "success":
            timer_lines = timers.get("output", "").splitlines()
            sections.append("[Systemd Timers]\n" + ("\n".join(timer_lines[:15]) if timer_lines else "No timers found."))
        else:
            sections.append("[Systemd Timers]\n" + timers.get("error", "timer listing unavailable"))

    return _ok("\n\n".join(sections).strip(), "persistence_points")


def _check_updates(args: Dict) -> Dict:
    def _trim(lines: List[str]) -> str:
        if not lines:
            return "No pending package updates detected."
        trimmed = lines[:20]
        suffix = f"\n...and {len(lines) - len(trimmed)} more" if len(lines) > len(trimmed) else ""
        return f"Pending package updates ({len(lines)}):\n" + "\n".join(trimmed) + suffix

    try:
        if shutil.which("apt"):
            proc = subprocess.run(
                ["apt", "list", "--upgradable"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if proc.returncode != 0:
                return _fail((proc.stderr or proc.stdout or "apt list failed").strip(), "apt list --upgradable")
            lines = [
                line.strip()
                for line in proc.stdout.splitlines()
                if line.strip() and not line.lower().startswith("listing")
            ]
            return _ok(_trim(lines), "apt list --upgradable")

        if shutil.which("dnf"):
            proc = subprocess.run(
                ["dnf", "check-update"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode not in (0, 100):
                return _fail((proc.stderr or proc.stdout or "dnf check-update failed").strip(), "dnf check-update")
            lines = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
            return _ok(_trim(lines), "dnf check-update")

        if shutil.which("yum"):
            proc = subprocess.run(
                ["yum", "check-update"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode not in (0, 100):
                return _fail((proc.stderr or proc.stdout or "yum check-update failed").strip(), "yum check-update")
            lines = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
            return _ok(_trim(lines), "yum check-update")

        if shutil.which("apk"):
            proc = subprocess.run(
                ["apk", "version", "-l", "<"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                return _fail((proc.stderr or proc.stdout or "apk version failed").strip(), "apk version -l <")
            lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            return _ok(_trim(lines), "apk version -l <")
    except subprocess.TimeoutExpired:
        return _fail("Timed out while checking package updates")
    except Exception as exc:
        return _fail(str(exc))

    return _fail("No supported package manager found (apt/dnf/yum/apk)")


def _scan_host_vulnerabilities(args: Dict) -> Dict:
    tools = check_vuln_tools()
    if not tools.get("trivy"):
        return _fail("Trivy is not installed. Install Trivy to scan the runtime filesystem for CVEs.", "trivy filesystem /")

    result = scan_trivy("/", severity_filter="CRITICAL,HIGH")
    if result.get("status") != "success":
        return _fail(result.get("error", "Trivy scan failed"), "trivy filesystem /")

    summary = result.get("summary", {})
    findings = result.get("top_findings", [])
    primary_concern = None
    lines = [
        f"Runtime filesystem vulnerability summary: CRITICAL={summary.get('CRITICAL', 0)}, HIGH={summary.get('HIGH', 0)}, MEDIUM={summary.get('MEDIUM', 0)}, LOW={summary.get('LOW', 0)}",
        f"Recommendation: {result.get('recommendation', 'Review findings and patch affected packages.')}",
    ]
    if findings:
        _cve_severity_rank = {"CRITICAL": 40, "HIGH": 30, "MEDIUM": 20, "LOW": 10}

        def _cve_score(f: Dict) -> int:
            sev      = _cve_severity_rank.get(str(f.get("severity", "")).upper(), 0)
            fixed    = (f.get("fixed_version") or "").strip().lower()
            has_fix  = 2 if fixed and fixed != "no fix available" else 0
            return sev + has_fix

        top_finding = sorted(findings, key=_cve_score, reverse=True)[0]
        top_package = top_finding.get("package", "unknown package")
        top_cve = top_finding.get("cve_id", "UNKNOWN")
        top_fixed = top_finding.get("fixed_version") or "no fix available"
        primary_concern = _concern_metadata(
            title=f"Top runtime CVE: {top_cve} in {top_package}",
            severity=str(top_finding.get("severity", "high")).lower(),
            evidence=[
                f"Affected package: {top_package} {top_finding.get('installed_version', '').strip()}",
                f"Recommended fix version: {top_fixed}",
                f"CRITICAL={summary.get('CRITICAL', 0)}, HIGH={summary.get('HIGH', 0)} across the runtime filesystem",
            ],
            next_action=f"patch package {top_package}" if top_fixed != "no fix available" else "install security updates",
            confidence="high",
        )
        lines.append("")
        lines.append("[Primary Concern]")
        lines.append(f"- {primary_concern['title']}")
        lines.append(f"- Why it matters: {primary_concern['evidence'][0]}")
        lines.append(f"- Next action: {primary_concern['next_action']}")
        lines.append("")
        lines.append("Top findings:")
        for finding in findings[:10]:
            fixed = finding.get("fixed_version") or "no fix available"
            lines.append(
                f"- {finding.get('cve_id', 'UNKNOWN')} | {finding.get('severity', 'UNKNOWN')} | "
                f"{finding.get('package', 'unknown package')} {finding.get('installed_version', '')} -> fix {fixed}"
            )
        patchable_packages = []
        remediation_candidates = []
        seen_packages = set()
        for finding in findings:
            package = finding.get("package", "").strip()
            fixed = (finding.get("fixed_version") or "").strip().lower()
            if not package or package in seen_packages or fixed in {"", "no fix available"}:
                continue
            seen_packages.add(package)
            patchable_packages.append(package)
            remediation_candidates.append({
                "package": package,
                "action": "patch_package",
                "prompt": f"patch package {package}",
                "command_intent": _suggest_package_patch_command(package),
            })
        if patchable_packages:
            lines.append("")
            lines.append("Suggested remediation actions:")
            for package in patchable_packages[:5]:
                lines.append(f"- Queue targeted patch: patch package {package}")
                lines.append(f"  Command intent: {_suggest_package_patch_command(package)}")
            lines.append("- Or queue broad remediation: install security updates")
    else:
        primary_concern = _concern_metadata(
            title="No CRITICAL/HIGH runtime CVEs found",
            severity="low",
            evidence=["The latest runtime filesystem scan did not report CRITICAL or HIGH CVEs."],
            next_action="No urgent patch action is required from this scan.",
            confidence="high",
        )
        lines.append("")
        lines.append("[Primary Concern]")
        lines.append(f"- {primary_concern['title']}")
        lines.append(f"- Next action: {primary_concern['next_action']}")
        lines.append("")
        lines.append("No CRITICAL/HIGH CVEs were found in the runtime filesystem.")
        remediation_candidates = []
    return _metadata_result(
        "\n".join(lines).strip(),
        "trivy filesystem /",
        {
            "inspection_type": "vulnerability_scan",
            "summary": summary,
            "top_findings": findings,
            "primary_concern": primary_concern,
            "remediation_candidates": remediation_candidates[:5],
            "broad_remediation": {
                "action": "install_updates",
                "prompt": "install security updates",
            },
        },
    )


def _check_suspicious_activity(args: Dict) -> Dict:
    sections: List[str] = []
    findings: List[str] = []
    suspicious_listener_findings: List[str] = []
    suspicious_process_findings: List[str] = []

    auth = _check_auth_events({})
    auth_failure_count = 0
    if auth.get("status") == "success":
        auth_output = auth.get("output", "")
        auth_failure_count = int((auth.get("metadata") or {}).get("auth_failure_count", _extract_auth_event_count(auth_output)) or 0)
        sections.append(f"[Auth Events]\n{auth_output}")
        if "No recent auth failure markers found" not in auth_output:
            findings.append("authentication failure markers detected")
    else:
        sections.append(f"[Auth Events]\n{auth.get('error', 'auth inspection failed')}")

    failed_services = _check_failed_services({})
    failed_service_names: List[str] = []
    if failed_services.get("status") == "success":
        failed_output = failed_services.get("output", "")
        failed_services_metadata = failed_services.get("metadata") or {}
        failed_service_names = list((failed_services.get("metadata") or {}).get("failed_services", _extract_failed_service_names(failed_output)) or [])
        sections.append(f"[Failed Services]\n{failed_output}")
        if (
            "No failed systemd units detected." not in failed_output
            and failed_services_metadata.get("environment_note") != "systemd_unavailable"
        ):
            findings.append("failed systemd services detected")
    else:
        sections.append(f"[Failed Services]\n{failed_services.get('error', 'service inspection failed')}")

    ports = _check_listening_ports({})
    current_listeners: List[str] = []
    if ports.get("status") == "success":
        listening_output = ports.get('output', '')
        sections.append(f"[Listening Ports]\n{listening_output}")
        suspicious_listener_findings = _suspicious_listener_findings(listening_output)
        findings.extend(suspicious_listener_findings)
        current_listeners = _normalize_listener_baseline(listening_output)
    else:
        sections.append(f"[Listening Ports]\n{ports.get('error', 'port inspection failed')}")

    processes = _check_processes({})
    if processes.get("status") == "success":
        process_output = processes.get('output', '')
        sections.append(f"[Top Processes]\n{process_output}")
        suspicious_process_findings = _suspicious_process_findings(process_output)
        findings.extend(suspicious_process_findings)
    else:
        sections.append(f"[Top Processes]\n{processes.get('error', 'process inspection failed')}")

    persistence = _check_persistence_points({})
    if persistence.get("status") == "success":
        sections.append(f"[Persistence Points]\n{persistence.get('output', '')}")
    else:
        sections.append(f"[Persistence Points]\n{persistence.get('error', 'persistence inspection failed')}")

    baseline = _load_linux_operator_baseline()
    previous_listeners = set(baseline.get("listeners", []))
    current_listener_set = set(current_listeners)
    new_listeners = sorted(current_listener_set - previous_listeners)
    if previous_listeners and new_listeners:
        findings.extend([f"New listening endpoint since last baseline: {item}" for item in new_listeners[:8]])
    previous_auth_failures = int(baseline.get("auth_failure_count", 0) or 0)
    auth_failure_delta = auth_failure_count - previous_auth_failures
    if previous_auth_failures and auth_failure_delta > 0:
        findings.append(f"Authentication failure count increased by {auth_failure_delta} since last baseline")

    previous_failed_services = set(baseline.get("failed_services", []))
    current_failed_services = set(failed_service_names)
    new_failed_services = sorted(current_failed_services - previous_failed_services)
    if previous_failed_services and new_failed_services:
        findings.extend([f"New failed service since last baseline: {item}" for item in new_failed_services[:8]])

    baseline_state = "initialized" if not previous_listeners and not previous_auth_failures and not previous_failed_services else "updated"
    _save_linux_operator_baseline({
        "listeners": current_listeners,
        "auth_failure_count": auth_failure_count,
        "failed_services": failed_service_names[:50],
        "updated_at": datetime.now().isoformat(),
    })

    unique_findings = []
    seen = set()
    for finding in findings:
        if finding not in seen:
            unique_findings.append(finding)
            seen.add(finding)

    correlated_assessment = _build_correlated_assessment(
        new_listeners=new_listeners,
        auth_failure_delta=auth_failure_delta,
        auth_failure_count=auth_failure_count,
        new_failed_services=new_failed_services,
        suspicious_listener_findings=suspicious_listener_findings,
        suspicious_process_findings=suspicious_process_findings,
        unique_findings=unique_findings,
    )

    suggestion_lines: List[str] = []
    if any("authentication failure" in item for item in unique_findings):
        suggestion_lines.append("- Review auth logs in detail and confirm whether the failures are expected administration attempts.")
    if any("failed systemd services" in item for item in unique_findings):
        suggestion_lines.append("- Inspect the failed service directly with: inspect service <name>")
    if any("network listener" in item.lower() or "unusual listening port" in item.lower() for item in unique_findings):
        suggestion_lines.append("- Inspect the owning process for unusual listeners with: inspect process <pid>")
    if any("process command worth review" in item.lower() for item in unique_findings):
        suggestion_lines.append("- If a suspicious process is confirmed, queue: stop suspicious process <pid>")
    if any("new listening endpoint" in item.lower() for item in unique_findings):
        suggestion_lines.append("- Compare the new listener with expected services before stopping it or patching the host.")
    if any("authentication failure count increased" in item.lower() for item in unique_findings):
        suggestion_lines.append("- Review source IPs and usernames in auth logs to decide whether to block, rotate credentials, or tighten SSH access.")
    if any("new failed service since last baseline" in item.lower() for item in unique_findings):
        suggestion_lines.append("- Investigate newly failed services first; inspect service <name> gives the fastest next step.")
    if not suggestion_lines:
        suggestion_lines.append("- No immediate remediation is suggested from the current signals.")

    concern_candidates: List[Dict[str, Any]] = []
    if correlated_assessment:
        concern_candidates.append(_concern_metadata(
            title=correlated_assessment["title"],
            severity=correlated_assessment["severity"],
            evidence=correlated_assessment["evidence"],
            next_action=correlated_assessment["next_action"],
            confidence=correlated_assessment["confidence"],
            is_novel=correlated_assessment["is_novel"],
        ))
    if new_listeners:
        concern_candidates.append(_concern_metadata(
            title="New listening endpoint detected since the previous baseline",
            severity="high",
            evidence=[
                f"New listener observed: {item}" for item in new_listeners[:2]
            ] + ["A new externally reachable listener can expand attack surface unexpectedly."],
            next_action="inspect process <pid>",
            confidence="medium",
            is_novel=True,
        ))
    if new_failed_services:
        concern_candidates.append(_concern_metadata(
            title="Service health drift detected",
            severity="medium",
            evidence=[f"New failed service: {item}" for item in new_failed_services[:2]],
            next_action="inspect service <name>",
            confidence="medium",
            is_novel=True,
        ))
    if auth_failure_delta > 0:
        concern_candidates.append(_concern_metadata(
            title="Authentication failures increased since the previous baseline",
            severity="medium",
            evidence=[
                f"Auth failure count delta: +{auth_failure_delta}",
                f"Current auth failure count: {auth_failure_count}",
            ],
            next_action="check ssh failures",
            confidence="medium",
            is_novel=True,
        ))
    if not concern_candidates and unique_findings:
        # Persistent/always-present findings — lower priority than novel baseline deltas
        concern_candidates.append(_concern_metadata(
            title=unique_findings[0],
            severity="medium",
            evidence=unique_findings[:3],
            next_action=suggestion_lines[0].lstrip("- ").strip() if suggestion_lines else "Review the findings before making changes.",
            confidence="medium",
            is_novel=False,
        ))
    if not concern_candidates:
        concern_candidates.append(_concern_metadata(
            title="No strong suspicious indicators detected",
            severity="low",
            evidence=["Auth logs, services, listeners, processes, and persistence points did not show a strong risk signal."],
            next_action="No urgent action is required from the current snapshot.",
            confidence="high",
            is_novel=False,
        ))
    primary_concern = _select_primary_concern(concern_candidates)

    header = "Potential suspicious indicators detected." if unique_findings else "No strong suspicious indicators detected from auth logs, service state, ports, processes, and persistence points."
    if correlated_assessment:
        correlated_lines = [
            f"- {correlated_assessment['title']}",
            f"- Assessment: {correlated_assessment['summary']}",
            *(f"- Evidence: {item}" for item in correlated_assessment.get("evidence", [])[:2]),
            f"- Next action: {correlated_assessment['next_action']}",
        ]
        sections.insert(0, "[Correlated Assessment]\n" + "\n".join(correlated_lines))
    if primary_concern:
        insert_at = 1 if correlated_assessment else 0
        sections.insert(insert_at, "[Primary Concern]\n" + "\n".join([
            f"- {primary_concern['title']}",
            *(f"- Evidence: {item}" for item in primary_concern.get("evidence", [])[:2]),
            f"- Next action: {primary_concern['next_action']}",
        ]))
    if unique_findings:
        insert_at = 0
        if correlated_assessment:
            insert_at += 1
        if primary_concern:
            insert_at += 1
        sections.insert(insert_at, "[Alerts]\n" + "\n".join(f"- {item}" for item in unique_findings[:12]))
    sections.append("[Suggested Next Steps]\n" + "\n".join(suggestion_lines))

    return _metadata_result(
        header + "\n\n" + "\n\n".join(section for section in sections if section).strip(),
        "tool:check_suspicious_activity",
        {
            "inspection_type": "suspicious_activity",
            "alerts": unique_findings[:12],
            "suggested_actions": suggestion_lines,
            "correlated_assessment": correlated_assessment,
            "primary_concern": primary_concern,
            "baseline_state": baseline_state,
            "new_listeners": new_listeners[:8],
            "auth_failure_count": auth_failure_count,
            "auth_failure_delta": auth_failure_delta,
            "new_failed_services": new_failed_services[:8],
        },
    )


def _assess_host_risk(args: Dict) -> Dict:
    suspicious = _check_suspicious_activity({})
    vulnerabilities = _scan_host_vulnerabilities({})
    updates = _check_updates({})

    sections: List[str] = []
    suggested_actions: List[str] = []
    remediation_candidates: List[Dict[str, Any]] = []
    concern_candidates: List[Dict[str, Any]] = []

    suspicious_metadata = suspicious.get("metadata") or {}
    vuln_metadata = vulnerabilities.get("metadata") or {}
    suspicious_primary = suspicious_metadata.get("primary_concern")
    vuln_primary = vuln_metadata.get("primary_concern")

    if suspicious.get("status") == "success":
        sections.append("[Suspicious Activity Summary]\n" + suspicious.get("output", ""))
        if suspicious_primary:
            concern_candidates.append(dict(suspicious_primary))
        suggested_actions.extend(list(suspicious_metadata.get("suggested_actions") or []))
    else:
        sections.append("[Suspicious Activity Summary]\n" + suspicious.get("error", "suspicious activity check failed"))

    if vulnerabilities.get("status") == "success":
        sections.append("[Vulnerability Summary]\n" + vulnerabilities.get("output", ""))
        if vuln_primary:
            concern_candidates.append(dict(vuln_primary))
        remediation_candidates.extend(list(vuln_metadata.get("remediation_candidates") or []))
        broad_remediation = vuln_metadata.get("broad_remediation")
        if broad_remediation and broad_remediation.get("prompt"):
            remediation_candidates.append({
                "package": "all security updates",
                "action": broad_remediation.get("action", "install_updates"),
                "prompt": broad_remediation["prompt"],
                "command_intent": "apply broad package remediation through the local package manager",
            })
    else:
        sections.append("[Vulnerability Summary]\n" + vulnerabilities.get("error", "vulnerability scan failed"))

    if updates.get("status") == "success":
        sections.append("[Package Update Summary]\n" + updates.get("output", ""))
    else:
        sections.append("[Package Update Summary]\n" + updates.get("error", "package update check failed"))

    correlated_assessment = _build_host_risk_correlation(
        vuln_primary=vuln_primary if isinstance(vuln_primary, dict) else None,
        suspicious_primary=suspicious_primary if isinstance(suspicious_primary, dict) else None,
        suspicious_metadata=suspicious_metadata,
        vuln_summary=vuln_metadata.get("summary") or {},
    )
    if correlated_assessment:
        concern_candidates.append(_concern_metadata(
            title=correlated_assessment["title"],
            severity=correlated_assessment["severity"],
            evidence=correlated_assessment["evidence"],
            next_action=correlated_assessment["next_action"],
            confidence=correlated_assessment["confidence"],
            is_novel=correlated_assessment["is_novel"],
        ))

    if not suggested_actions:
        suggested_actions.append("- Review the highest-risk panel first before taking action.")
    seen_actions = set()
    deduped_actions: List[str] = []
    for item in suggested_actions:
        if item not in seen_actions:
            deduped_actions.append(item)
            seen_actions.add(item)

    seen_prompts = set()
    deduped_candidates: List[Dict[str, Any]] = []
    for item in remediation_candidates:
        prompt = str(item.get("prompt") or "").strip()
        if not prompt or prompt in seen_prompts:
            continue
        deduped_candidates.append(item)
        seen_prompts.add(prompt)

    primary_concern = _select_primary_concern(concern_candidates)

    if correlated_assessment:
        sections.insert(0, "[Correlated Assessment]\n" + "\n".join([
            f"- {correlated_assessment['title']}",
            f"- Assessment: {correlated_assessment['summary']}",
            *(f"- Evidence: {item}" for item in correlated_assessment.get("evidence", [])[:2]),
            f"- Next action: {correlated_assessment['next_action']}",
        ]))
    if primary_concern:
        insert_at = 1 if correlated_assessment else 0
        sections.insert(insert_at, "[Primary Concern]\n" + "\n".join([
            f"- {primary_concern['title']}",
            *(f"- Evidence: {item}" for item in primary_concern.get("evidence", [])[:2]),
            f"- Next action: {primary_concern['next_action']}",
        ]))
    sections.append("[Suggested Next Steps]\n" + "\n".join(deduped_actions))

    return _metadata_result(
        "Overall host risk assessment generated from runtime CVEs, suspicious activity, and package update posture.\n\n"
        + "\n\n".join(section for section in sections if section).strip(),
        "tool:assess_host_risk",
        {
            "inspection_type": "host_risk_assessment",
            "correlated_assessment": correlated_assessment,
            "primary_concern": primary_concern,
            "suggested_actions": deduped_actions,
            "remediation_candidates": deduped_candidates[:6],
            "new_listeners": list(suspicious_metadata.get("new_listeners") or [])[:8],
            "new_failed_services": list(suspicious_metadata.get("new_failed_services") or [])[:8],
            "auth_failure_delta": int(suspicious_metadata.get("auth_failure_delta", 0) or 0),
            "risk_summary": {
                "runtime_cves": vuln_metadata.get("summary") or {},
                "suspicious_primary": suspicious_primary,
                "vulnerability_primary": vuln_primary,
            },
        },
    )


def _install_updates(args: Dict) -> Dict:
    try:
        if shutil.which("apt-get"):
            update = _run(["apt-get", "update"], timeout=120)
            if update.get("status") != "success":
                return update
            upgrade = _run(["apt-get", "upgrade", "-y"], timeout=900)
            if upgrade.get("status") != "success":
                return upgrade
            return _ok(
                "System package metadata refreshed and upgrades applied via apt-get upgrade -y.",
                "apt-get update && apt-get upgrade -y",
            )

        if shutil.which("dnf"):
            upgrade = _run(["dnf", "upgrade", "-y"], timeout=900)
            if upgrade.get("status") != "success":
                return upgrade
            return _ok("System upgrades applied via dnf upgrade -y.", "dnf upgrade -y")

        if shutil.which("yum"):
            upgrade = _run(["yum", "update", "-y"], timeout=900)
            if upgrade.get("status") != "success":
                return upgrade
            return _ok("System updates applied via yum update -y.", "yum update -y")

        if shutil.which("apk"):
            update = _run(["apk", "update"], timeout=120)
            if update.get("status") != "success":
                return update
            upgrade = _run(["apk", "upgrade"], timeout=900)
            if upgrade.get("status") != "success":
                return upgrade
            return _ok("System packages upgraded via apk upgrade.", "apk update && apk upgrade")
    except Exception as exc:
        return _fail(str(exc))

    return _fail("No supported package manager found for update installation")


def _patch_package(args: Dict) -> Dict:
    package = args.get("package", "")
    ok, err = _validate(package, _SAFE_PKG, "package")
    if not ok:
        return _fail(err)
    try:
        if shutil.which("apt-get"):
            update = _run(["apt-get", "update"], timeout=120)
            if update.get("status") != "success":
                return update
            install = _run(["apt-get", "install", "--only-upgrade", "-y", package], timeout=600)
            if install.get("status") != "success":
                return install
            return _ok(f"Package '{package}' upgraded via apt-get.", f"apt-get install --only-upgrade -y {package}")
        if shutil.which("dnf"):
            result = _run(["dnf", "upgrade", "-y", package], timeout=600)
            if result.get("status") != "success":
                return result
            return _ok(f"Package '{package}' upgraded via dnf.", f"dnf upgrade -y {package}")
        if shutil.which("yum"):
            result = _run(["yum", "update", "-y", package], timeout=600)
            if result.get("status") != "success":
                return result
            return _ok(f"Package '{package}' updated via yum.", f"yum update -y {package}")
        if shutil.which("apk"):
            result = _run(["apk", "upgrade", package], timeout=600)
            if result.get("status") != "success":
                return result
            return _ok(f"Package '{package}' upgraded via apk.", f"apk upgrade {package}")
    except Exception as exc:
        return _fail(str(exc))
    return _fail("No supported package manager found for package patching")


def _stop_process(args: Dict) -> Dict:
    pid = args.get("pid")
    ok, err = _validate_pid(pid)
    if not ok:
        return _fail(err)
    return _run(["kill", "-TERM", str(int(pid))])


def _check_docker(args: Dict) -> Dict:
    return inspect_docker()


def _list_containers(args: Dict) -> Dict:
    all_containers = bool(args.get("all", False))
    return list_containers(all_containers=all_containers)


def _verify_system(args: Dict) -> Dict:
    sections: List[str] = []
    failures: List[str] = []

    disk = _check_disk({})
    if disk.get("status") == "success":
        sections.append(f"[Disk]\n{disk.get('output', '')}")
    else:
        failures.append(f"disk: {disk.get('error', 'unknown error')}")

    memory = _check_memory({})
    if memory.get("status") == "success":
        sections.append(f"[Memory]\n{memory.get('output', '')}")
    else:
        failures.append(f"memory: {memory.get('error', 'unknown error')}")

    docker = _check_docker({})
    if docker.get("status") == "success":
        sections.append(f"[Docker]\n{docker.get('output', '')}")
    else:
        failures.append(f"docker: {docker.get('error', 'unknown error')}")

    containers = _list_containers({})
    if containers.get("status") == "success":
        sections.append(f"[Running Containers]\n{containers.get('output', '')}")
    else:
        failures.append(f"containers: {containers.get('error', 'unknown error')}")

    if failures:
        sections.append("[Warnings]\n" + "\n".join(failures))

    status = "success" if sections else "failure"
    return {
        "status": status,
        "output": "\n\n".join(section for section in sections if section).strip(),
        "error": "" if status == "success" else "; ".join(failures) or "System verification failed",
        "command_repr": "tool:verify_system",
        "timestamp": datetime.now().isoformat(),
    }


def _check_node_status(args: Dict) -> Dict:
    return _run(["kubectl", "get", "nodes", "-o", "wide"])


def _check_service_health(args: Dict) -> Dict:
    service = args.get("service", "")
    ok, err = _validate(service, _SAFE_SVC, "service")
    if not ok:
        return _fail(err)
    return _run(["systemctl", "status", service, "--no-pager"])


def _trivy_scan(args: Dict) -> Dict:
    image = args.get("image", "")
    ok, err = _validate(image, _SAFE_IMAGE, "image")
    if not ok:
        return _fail(err)
    return _run(["trivy", "image", "--no-progress", image], timeout=120)


def _check_pod_describe(args: Dict) -> Dict:
    pod_name  = args.get("pod_name", "")
    namespace = args.get("namespace", "default")
    ok, err = _validate(pod_name, _SAFE_NAME, "pod_name")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    return _run(["kubectl", "describe", "pod", pod_name, "-n", namespace])


# ── MEDIUM RISK ───────────────────────────────────────────────────────────────

def _restart_pod(args: Dict) -> Dict:
    deployment = args.get("deployment", "")
    namespace  = args.get("namespace", "default")
    ok, err = _validate(deployment, _SAFE_NAME, "deployment")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    return _run([
        "kubectl", "rollout", "restart",
        f"deployment/{deployment}",
        "-n", namespace,
    ])


def _scale_deployment(args: Dict) -> Dict:
    deployment = args.get("deployment", "")
    namespace  = args.get("namespace", "default")
    replicas   = args.get("replicas", None)

    ok, err = _validate(deployment, _SAFE_NAME, "deployment")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    if not isinstance(replicas, int) or not (0 <= replicas <= 50):
        return _fail("replicas must be an integer between 0 and 50")

    return _run([
        "kubectl", "scale",
        f"deployment/{deployment}",
        f"--replicas={replicas}",
        "-n", namespace,
    ])


def _restart_service(args: Dict) -> Dict:
    service = args.get("service", "")
    ok, err = _validate(service, _SAFE_SVC, "service")
    if not ok:
        return _fail(err)
    return _run(["systemctl", "restart", service])


def _rollback_deployment(args: Dict) -> Dict:
    deployment = args.get("deployment", "")
    namespace  = args.get("namespace", "default")
    ok, err = _validate(deployment, _SAFE_NAME, "deployment")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    return _run([
        "kubectl", "rollout", "undo",
        f"deployment/{deployment}",
        "-n", namespace,
    ])


# ── HIGH RISK — always blocked here, approval flow in secure_executor ─────────

def _delete_service(args: Dict) -> Dict:
    # Should never reach execution — gated at HIGH risk in registry
    return _fail("delete_service requires manual approval and is never auto-executed")


def _drain_node(args: Dict) -> Dict:
    return _fail("drain_node requires manual approval and is never auto-executed")


def _apply_config(args: Dict) -> Dict:
    return _fail("apply_config requires manual approval and is never auto-executed")


# ─── Tool dataclass ───────────────────────────────────────────────────────────

@dataclass
class Tool:
    name:        str
    function:    Callable[[Dict], Dict]
    risk_level:  str           # "low" | "medium" | "high"
    description: str
    required_args: List[str]   = field(default_factory=list)
    optional_args: List[str]   = field(default_factory=list)


# ─── Registry ─────────────────────────────────────────────────────────────────

class ToolRegistry:
    """
    Central registry for all AVA tools.

    Usage:
        registry = ToolRegistry()
        result   = registry.execute("check_logs", {"pod_name": "nginx-pod"})
    """

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._register_defaults()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        function: Callable,
        risk_level: str,
        description: str,
        required_args: Optional[List[str]] = None,
        optional_args: Optional[List[str]] = None,
    ) -> None:
        if risk_level not in ("low", "medium", "high"):
            raise ValueError(f"risk_level must be low/medium/high, got: {risk_level}")
        self._tools[name] = Tool(
            name=name,
            function=function,
            risk_level=risk_level,
            description=description,
            required_args=required_args or [],
            optional_args=optional_args or [],
        )

    def _register_defaults(self) -> None:
        # ── LOW RISK — auto-execute ────────────────────────────────────────────
        self.register(
            name="check_pod_status",
            function=_check_pod_status,
            risk_level="low",
            description="List all pods with status in a namespace",
            optional_args=["namespace"],
        )
        self.register(
            name="check_logs",
            function=_check_logs,
            risk_level="low",
            description="Fetch recent logs from a pod",
            required_args=["pod_name"],
            optional_args=["namespace", "lines"],
        )
        self.register(
            name="check_disk",
            function=_check_disk,
            risk_level="low",
            description="Show disk usage with df -h",
        )
        self.register(
            name="check_memory",
            function=_check_memory,
            risk_level="low",
            description="Show memory usage with free -h",
        )
        self.register(
            name="check_docker",
            function=_check_docker,
            risk_level="low",
            description="Inspect Docker daemon health and summary",
        )
        self.register(
            name="list_containers",
            function=_list_containers,
            risk_level="low",
            description="List running Docker containers",
            optional_args=["all"],
        )
        self.register(
            name="verify_system",
            function=_verify_system,
            risk_level="low",
            description="Run a combined low-risk system verification",
        )
        self.register(
            name="check_node_status",
            function=_check_node_status,
            risk_level="low",
            description="List all Kubernetes nodes with status",
        )
        self.register(
            name="check_service_health",
            function=_check_service_health,
            risk_level="low",
            description="Check systemd service status",
            required_args=["service"],
        )
        self.register(
            name="check_pod_describe",
            function=_check_pod_describe,
            risk_level="low",
            description="Describe a pod (events, limits, mounts)",
            required_args=["pod_name"],
            optional_args=["namespace"],
        )
        self.register(
            name="trivy_scan",
            function=_trivy_scan,
            risk_level="low",
            description="Scan a container image for CVEs with Trivy",
            required_args=["image"],
        )
        self.register(
            name="check_processes",
            function=_check_processes,
            risk_level="low",
            description="Show top CPU-consuming processes",
        )
        self.register(
            name="check_listening_ports",
            function=_check_listening_ports,
            risk_level="low",
            description="Show listening TCP ports and owning processes",
        )
        self.register(
            name="check_failed_services",
            function=_check_failed_services,
            risk_level="low",
            description="Show failed systemd services",
        )
        self.register(
            name="inspect_service",
            function=_inspect_service,
            risk_level="low",
            description="Inspect a systemd service status and recent logs",
            required_args=["service"],
        )
        self.register(
            name="check_auth_events",
            function=_check_auth_events,
            risk_level="low",
            description="Inspect recent authentication failure markers",
        )
        self.register(
            name="check_persistence_points",
            function=_check_persistence_points,
            risk_level="low",
            description="Inspect cron paths and systemd timers for persistence points",
        )
        self.register(
            name="inspect_process",
            function=_inspect_process,
            risk_level="low",
            description="Inspect a process by PID",
            required_args=["pid"],
        )
        self.register(
            name="check_updates",
            function=_check_updates,
            risk_level="low",
            description="Show pending package updates from the local package manager",
        )
        self.register(
            name="scan_host_vulnerabilities",
            function=_scan_host_vulnerabilities,
            risk_level="low",
            description="Scan the runtime filesystem for CRITICAL/HIGH CVEs with Trivy",
        )
        self.register(
            name="check_suspicious_activity",
            function=_check_suspicious_activity,
            risk_level="low",
            description="Inspect auth failures, failed services, ports, and top processes for suspicious signals",
        )
        self.register(
            name="assess_host_risk",
            function=_assess_host_risk,
            risk_level="low",
            description="Combine suspicious activity, runtime CVEs, and update posture into one host risk assessment",
        )

        # ── MEDIUM RISK — approval required ───────────────────────────────────
        self.register(
            name="restart_pod",
            function=_restart_pod,
            risk_level="medium",
            description="Rollout restart a deployment",
            required_args=["deployment"],
            optional_args=["namespace"],
        )
        self.register(
            name="scale_deployment",
            function=_scale_deployment,
            risk_level="medium",
            description="Scale a deployment to N replicas",
            required_args=["deployment", "replicas"],
            optional_args=["namespace"],
        )
        self.register(
            name="restart_service",
            function=_restart_service,
            risk_level="medium",
            description="Restart a systemd service",
            required_args=["service"],
        )
        self.register(
            name="rollback_deployment",
            function=_rollback_deployment,
            risk_level="medium",
            description="Rollback a Kubernetes deployment to the previous revision",
            required_args=["deployment"],
            optional_args=["namespace"],
        )
        self.register(
            name="install_updates",
            function=_install_updates,
            risk_level="medium",
            description="Install pending system package updates",
        )
        self.register(
            name="stop_process",
            function=_stop_process,
            risk_level="medium",
            description="Stop a process by PID using SIGTERM",
            required_args=["pid"],
        )
        self.register(
            name="patch_package",
            function=_patch_package,
            risk_level="medium",
            description="Upgrade a specific package through the local package manager",
            required_args=["package"],
        )

        # ── HIGH RISK — always blocked, human approval only ───────────────────
        self.register(
            name="delete_service",
            function=_delete_service,
            risk_level="high",
            description="Delete a Kubernetes service (DESTRUCTIVE)",
            required_args=["service", "namespace"],
        )
        self.register(
            name="drain_node",
            function=_drain_node,
            risk_level="high",
            description="Drain a Kubernetes node (DESTRUCTIVE)",
            required_args=["node"],
        )
        self.register(
            name="apply_config",
            function=_apply_config,
            risk_level="high",
            description="Apply a Kubernetes config file (DESTRUCTIVE)",
            required_args=["filepath"],
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    def register_native(self, name: str, handler, description: str,
                        args: dict, risk_level: str = "low",
                        requires_approval: bool = False,
                        available: bool = True):
        """Register a Python callable as a tool (not a shell command). Day 8."""
        self._tools[name] = type("Tool", (), {
            "name":              name,
            "description":       description,
            "args":              args,
            "risk_level":        risk_level,
            "type":              "native",
            "handler":           handler,
            "requires_approval": requires_approval,
            "available":         available,
        })()

    def execute(self, name: str, input_args: Dict) -> Dict:
        """
        Execute a registered tool.

        Returns standardised dict:
            {
                "status":       "success" | "failure" | "blocked" | "not_found",
                "output":       str,
                "error":        str,
                "command_repr": str,   # safe, loggable representation
                "risk_level":   str,
                "tool_name":    str,
                "timestamp":    str,
            }
        """
        tool = self._tools.get(name)
        # Day 8: native tool dispatch (Python callable, not shell)
        if tool and getattr(tool, "type", None) == "native":
            handler = getattr(tool, "handler", None)
            if not callable(handler):
                return {"status": "error", "error": f"Tool '{name}' has no callable handler", "tool_name": name}
            if not getattr(tool, "available", True):
                return {"status": "error", "error": f"Tool '{name}' unavailable (binary not installed)", "tool_name": name}
            try:
                result = handler(**input_args) if input_args else handler()
                if isinstance(result, dict):
                    result["tool_name"] = name
                return result
            except TypeError as e:
                return {"status": "error", "error": f"Tool '{name}' argument error: {e}", "tool_name": name}
            except Exception as e:
                return {"status": "error", "error": f"Tool '{name}' error: {e}", "tool_name": name}


        if not tool:
            return {
                "status":    "not_found",
                "output":    "",
                "error":     f"Tool '{name}' is not registered",
                "tool_name": name,
                "timestamp": datetime.now().isoformat(),
            }

        # High-risk tools are never auto-executed — always blocked here
        if tool.risk_level == "high":
            return {
                "status":    "blocked",
                "output":    "",
                "error":     (
                    f"Tool '{name}' is HIGH RISK and cannot be auto-executed. "
                    "Use the approval workflow."
                ),
                "risk_level": "high",
                "tool_name":  name,
                "timestamp":  datetime.now().isoformat(),
            }

        # Validate required args are present
        missing = [a for a in tool.required_args if a not in input_args]
        if missing:
            return {
                "status":    "failure",
                "output":    "",
                "error":     f"Missing required args for '{name}': {missing}",
                "tool_name": name,
                "timestamp": datetime.now().isoformat(),
            }

        # Call tool function
        result = tool.function(input_args)
        result["risk_level"] = tool.risk_level
        result["tool_name"]  = name
        return result

    # ── Introspection ─────────────────────────────────────────────────────────

    def get_tool(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Dict]:
        return [
            {
                "name":          t.name,
                "risk_level":    t.risk_level,
                "description":   t.description,
                "required_args": t.required_args,
                "optional_args": t.optional_args,
            }
            for t in self._tools.values()
        ]

    def list_by_risk(self, risk_level: str) -> List[Dict]:
        return [t for t in self.list_tools() if t["risk_level"] == risk_level]


# ─── Singleton ────────────────────────────────────────────────────────────────
# Import this everywhere — one registry for the whole app

registry = ToolRegistry()


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    print("=== Registered tools ===")
    for t in registry.list_tools():
        print(f"  [{t['risk_level'].upper():6}] {t['name']:25} — {t['description']}")

    print("\n=== Injection test (should FAIL validation) ===")
    result = registry.execute("check_logs", {
        "pod_name": "nginx; rm -rf /",
        "namespace": "default",
    })
    print(json.dumps(result, indent=2))

    print("\n=== High-risk block test ===")
    result = registry.execute("delete_service", {
        "service": "nginx", "namespace": "default"
    })
    print(json.dumps(result, indent=2))

    print("\n=== Missing args test ===")
    result = registry.execute("check_logs", {})
    print(json.dumps(result, indent=2))

    print("\n=== Valid low-risk tool (dry check — kubectl may not be present) ===")
    result = registry.execute("check_disk", {})
    print(json.dumps(result, indent=2))
