import os
import socket
from pathlib import Path
from typing import Any, Dict, List


HOST_PROC = Path(os.getenv("AVA_HOST_PROC", "/host/proc"))
CONTAINER_PROC = Path("/proc")
HOST_ROOT = Path(os.getenv("AVA_HOST_ROOT", "/host/proc/1/root"))


def _read_text(path: Path, limit: int = 65536) -> str:
    with path.open("rb") as handle:
        return handle.read(limit).decode("utf-8", errors="replace")


def _proc_root() -> tuple[Path, str]:
    if (HOST_PROC / "1" / "status").exists() and (HOST_PROC / "net").exists():
        return HOST_PROC, "host_observed"
    return CONTAINER_PROC, "container_observed"


def _proc_file(proc_root: Path, relative_path: str) -> Path:
    return proc_root / relative_path


def _parse_meminfo(text: str) -> Dict[str, str]:
    parsed = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _format_kb(value: str) -> str:
    number = int((value or "0").split()[0])
    if number >= 1024 * 1024:
        return f"{number / 1024 / 1024:.1f} GiB"
    if number >= 1024:
        return f"{number / 1024:.1f} MiB"
    return f"{number} KiB"


def _memory_summary(proc_root: Path) -> Dict[str, Any]:
    try:
        meminfo = _parse_meminfo(_read_text(_proc_file(proc_root, "meminfo")))
        total = meminfo.get("MemTotal", "0 kB")
        available = meminfo.get("MemAvailable", meminfo.get("MemFree", "0 kB"))
        return {
            "available": True,
            "total": _format_kb(total),
            "available_memory": _format_kb(available),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _os_summary(proc_root: Path) -> Dict[str, Any]:
    try:
        kernel = _read_text(_proc_file(proc_root, "sys/kernel/ostype")).strip()
        release = _read_text(_proc_file(proc_root, "sys/kernel/osrelease")).strip()
        hostname = _read_text(_proc_file(proc_root, "sys/kernel/hostname")).strip()
        return {
            "available": True,
            "kernel": f"{kernel} {release}".strip(),
            "hostname": hostname or socket.gethostname(),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc), "hostname": socket.gethostname()}


def _process_summary(proc_root: Path, limit: int = 8) -> Dict[str, Any]:
    if not proc_root.exists():
        return {"available": False, "error": f"proc root not found: {proc_root}", "processes": []}
    processes = []
    for pid_path in sorted(proc_root.iterdir(), key=lambda path: int(path.name) if path.name.isdigit() else 10**12):
        if not pid_path.name.isdigit():
            continue
        try:
            cmdline = _read_text(pid_path / "cmdline", limit=4096).replace("\x00", " ").strip()
            comm = _read_text(pid_path / "comm", limit=256).strip()
            processes.append({
                "pid": int(pid_path.name),
                "command": cmdline or comm or "(unknown)",
            })
        except Exception:
            continue
        if len(processes) >= limit:
            break
    return {"available": True, "count_sampled": len(processes), "processes": processes}


def _decode_ipv4(hex_addr: str) -> str:
    raw = bytes.fromhex(hex_addr)
    return ".".join(str(byte) for byte in raw[::-1])


def _decode_ip_port(value: str) -> str:
    address, port = value.split(":", 1)
    if len(address) == 8:
        ip = _decode_ipv4(address)
    else:
        ip = "::"
    return f"{ip}:{int(port, 16)}"


def _socket_inode_pids(proc_root: Path) -> Dict[str, List[int]]:
    mapping: Dict[str, List[int]] = {}
    if not proc_root.exists():
        return mapping
    for pid_path in proc_root.iterdir():
        if not pid_path.name.isdigit():
            continue
        fd_dir = pid_path / "fd"
        try:
            for fd_path in fd_dir.iterdir():
                try:
                    target = os.readlink(fd_path)
                except Exception:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    inode = target[8:-1]
                    mapping.setdefault(inode, []).append(int(pid_path.name))
        except Exception:
            continue
    return mapping


def _listening_ports(proc_root: Path, limit: int = 12) -> Dict[str, Any]:
    if not proc_root.exists():
        return {"available": False, "error": f"proc root not found: {proc_root}", "listeners": []}
    inode_pids = _socket_inode_pids(proc_root)
    listeners = []
    for relative in ("net/tcp", "net/tcp6"):
        path = _proc_file(proc_root, relative)
        if not path.exists():
            continue
        try:
            rows = _read_text(path).splitlines()[1:]
        except Exception:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            local = _decode_ip_port(parts[1])
            inode = parts[9]
            listeners.append({
                "local": local,
                "pids": inode_pids.get(inode, []),
            })
            if len(listeners) >= limit:
                break
        if len(listeners) >= limit:
            break
    return {"available": True, "listeners": listeners}


def _auth_summary() -> Dict[str, Any]:
    # Host auth logs are intentionally not mounted by default to avoid exposing
    # sensitive log contents. This phase records availability, not contents.
    return {
        "available": False,
        "note": "host auth logs are not mounted into AVA; no auth log content was read",
    }


def _package_summary() -> Dict[str, Any]:
    # Package database files are also not mounted by default in this first
    # bridge. Package/CVE checks remain on the existing runtime-scoped tools.
    return {
        "available": False,
        "note": "host package database is not mounted; package state remains runtime-scoped",
    }


def collect_host_telemetry() -> Dict[str, Any]:
    proc_root, runtime_scope = _proc_root()
    return {
        "runtime_scope": runtime_scope,
        "proc_root": str(proc_root),
        "read_only": True,
        "os": _os_summary(proc_root),
        "memory": _memory_summary(proc_root),
        "processes": _process_summary(proc_root),
        "listening_ports": _listening_ports(proc_root),
        "auth_logs": _auth_summary(),
        "package_state": _package_summary(),
    }


def detect_host_systemd() -> Dict[str, Any]:
    proc_root, runtime_scope = _proc_root()
    if runtime_scope != "host_observed":
        return {
            "available": False,
            "runtime_scope": "container_observed",
            "environment_note": "host_proc_unavailable",
            "detail": "AVA does not have the read-only host proc mount needed to inspect host service-manager facts.",
        }

    try:
        pid1_comm = _read_text(proc_root / "1" / "comm", limit=256).strip()
    except Exception:
        pid1_comm = ""

    has_systemd_tree = any(
        path.exists()
        for path in (
            HOST_ROOT / "run/systemd/system",
            HOST_ROOT / "etc/systemd/system",
            HOST_ROOT / "usr/lib/systemd/system",
            HOST_ROOT / "lib/systemd/system",
        )
    )
    if pid1_comm == "systemd" or has_systemd_tree:
        return {
            "available": True,
            "runtime_scope": "host_observed",
            "environment_note": "host_systemd_detected",
            "pid1": pid1_comm or "unknown",
            "host_root": str(HOST_ROOT),
            "detail": "Host systemd evidence is visible through the read-only host proc bridge.",
        }

    return {
        "available": False,
        "runtime_scope": "host_observed_limited",
        "environment_note": "host_systemd_unavailable",
        "pid1": pid1_comm or "unknown",
        "host_root": str(HOST_ROOT),
        "detail": "Host proc is visible, but PID 1/systemd evidence is not available through the read-only bridge.",
    }


def inspect_host_service_unit(service: str) -> Dict[str, Any]:
    systemd = detect_host_systemd()
    service = (service or "").strip()
    unit_name = service if service.endswith(".service") else f"{service}.service"
    result: Dict[str, Any] = {
        "available": systemd.get("available") is True,
        "runtime_scope": systemd.get("runtime_scope", "container_observed"),
        "environment_note": systemd.get("environment_note", "host_systemd_unknown"),
        "service": service,
        "unit": unit_name,
        "host_root": str(HOST_ROOT),
        "unit_found": False,
        "unit_paths": [],
        "unit_preview": "",
        "detail": systemd.get("detail", ""),
    }
    if not result["available"]:
        return result

    candidates = [
        HOST_ROOT / "etc/systemd/system" / unit_name,
        HOST_ROOT / "run/systemd/system" / unit_name,
        HOST_ROOT / "usr/lib/systemd/system" / unit_name,
        HOST_ROOT / "lib/systemd/system" / unit_name,
    ]
    for path in candidates:
        if not path.exists():
            continue
        result["unit_found"] = True
        result["unit_paths"].append(str(path))
        try:
            preview_lines = [
                line
                for line in _read_text(path, limit=8192).splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ][:20]
            result["unit_preview"] = "\n".join(preview_lines)
        except Exception as exc:
            result["unit_preview"] = f"Unit file exists but could not be read: {exc}"
        break
    return result


def format_host_telemetry(snapshot: Dict[str, Any]) -> str:
    lines = [
        f"[Telemetry Scope]\n{snapshot['runtime_scope']} via {snapshot['proc_root']} (read-only)",
        "",
    ]
    os_info = snapshot.get("os", {})
    if os_info.get("available"):
        lines.append(f"[OS]\nHostname: {os_info.get('hostname')}\nKernel: {os_info.get('kernel')}")
    else:
        lines.append(f"[OS]\nUnavailable: {os_info.get('error', 'unknown error')}")

    memory = snapshot.get("memory", {})
    if memory.get("available"):
        lines.append(f"[Memory]\nTotal: {memory.get('total')}\nAvailable: {memory.get('available_memory')}")
    else:
        lines.append(f"[Memory]\nUnavailable: {memory.get('error', 'unknown error')}")

    processes = snapshot.get("processes", {}).get("processes", [])
    process_lines = [f"{item['pid']}: {item['command']}" for item in processes[:8]]
    lines.append("[Processes]\n" + ("\n".join(process_lines) if process_lines else "No processes sampled."))

    listeners = snapshot.get("listening_ports", {}).get("listeners", [])
    listener_lines = [
        f"{item['local']} pids={','.join(str(pid) for pid in item.get('pids', [])) or 'unknown'}"
        for item in listeners[:12]
    ]
    lines.append("[Listening Ports]\n" + ("\n".join(listener_lines) if listener_lines else "No listening TCP ports observed."))

    lines.append(f"[Auth Logs]\n{snapshot.get('auth_logs', {}).get('note', 'Unavailable')}")
    lines.append(f"[Package State]\n{snapshot.get('package_state', {}).get('note', 'Unavailable')}")
    return "\n\n".join(lines)
