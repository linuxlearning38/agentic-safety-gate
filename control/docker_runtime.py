"""
Minimal Docker socket client for local AVA inspections.

Uses the mounted Unix socket directly instead of depending on a docker CLI
inside the container. Read-only inspection endpoints only.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode


DOCKER_SOCKET_PATH = os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")


def _result(status: str, output: str = "", error: str = "", command_repr: str = "docker_api") -> Dict[str, Any]:
    return {
        "status": status,
        "output": output,
        "error": error,
        "command_repr": command_repr,
        "timestamp": datetime.now().isoformat(),
    }


def docker_socket_available() -> Tuple[bool, str]:
    if not os.path.exists(DOCKER_SOCKET_PATH):
        return False, f"Docker socket not found: {DOCKER_SOCKET_PATH}"
    if not os.access(DOCKER_SOCKET_PATH, os.R_OK | os.W_OK):
        return False, f"Docker socket not accessible: {DOCKER_SOCKET_PATH}"
    return True, ""


def _decode_chunked_body(body: bytes) -> bytes:
    decoded = bytearray()
    cursor = 0
    total = len(body)
    while cursor < total:
        line_end = body.find(b"\r\n", cursor)
        if line_end == -1:
            raise RuntimeError("Malformed chunked Docker API response")
        size_line = body[cursor:line_end].split(b";", 1)[0].strip()
        try:
            chunk_size = int(size_line, 16)
        except ValueError as exc:
            raise RuntimeError(f"Invalid chunk size in Docker API response: {size_line!r}") from exc
        cursor = line_end + 2
        if chunk_size == 0:
            return bytes(decoded)
        next_cursor = cursor + chunk_size
        if next_cursor > total:
            raise RuntimeError("Chunked Docker API response truncated")
        decoded.extend(body[cursor:next_cursor])
        cursor = next_cursor + 2
    return bytes(decoded)


def _request(path: str, timeout: int = 5) -> Tuple[int, bytes]:
    ok, err = docker_socket_available()
    if not ok:
        raise RuntimeError(err)

    req = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "User-Agent: ava-docker-runtime/1.0\r\n"
        "Connection: close\r\n\r\n"
    ).encode("utf-8")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(DOCKER_SOCKET_PATH)
        sock.sendall(req)
        chunks: List[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)

    raw = b"".join(chunks)
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    header_lines = header_blob.splitlines()
    status_line = header_lines[0].decode("utf-8", errors="replace")
    headers = {}
    for line in header_lines[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        headers[key.decode("utf-8", errors="replace").strip().lower()] = value.decode("utf-8", errors="replace").strip()
    try:
        status_code = int(status_line.split()[1])
    except Exception as exc:
        raise RuntimeError(f"Malformed Docker API response: {status_line}") from exc
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _decode_chunked_body(body)
    return status_code, body


def _json_request(path: str, timeout: int = 5) -> Any:
    status_code, body = _request(path, timeout=timeout)
    if status_code >= 400:
        raise RuntimeError(f"Docker API returned HTTP {status_code}: {body.decode('utf-8', errors='replace')}")
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def inspect_docker() -> Dict[str, Any]:
    try:
        version = _json_request("/version")
        info = _json_request("/info")
        text = (
            f"Docker server: {version.get('Version', 'unknown')}\n"
            f"API version: {version.get('ApiVersion', 'unknown')}\n"
            f"OS/Arch: {version.get('Os', 'unknown')}/{version.get('Arch', 'unknown')}\n"
            f"Containers: running={info.get('ContainersRunning', 0)}, "
            f"paused={info.get('ContainersPaused', 0)}, stopped={info.get('ContainersStopped', 0)}\n"
            f"Images: {info.get('Images', 0)}\n"
            f"Driver: {info.get('Driver', 'unknown')}"
        )
        return _result("success", output=text, command_repr="docker_api:/version+/info")
    except Exception as exc:
        return _result("failure", error=str(exc), command_repr="docker_api:/version+/info")


def list_containers(all_containers: bool = False) -> Dict[str, Any]:
    try:
        query = urlencode({"all": int(bool(all_containers))})
        containers = _json_request(f"/containers/json?{query}")
        if not containers:
            scope = "containers" if all_containers else "running containers"
            return _result("success", output=f"No {scope} found.", command_repr="docker_api:/containers/json")

        lines = []
        for item in containers:
            names = ", ".join(name.lstrip("/") for name in item.get("Names", [])) or item.get("Id", "")[:12]
            state = item.get("State", "unknown")
            status = item.get("Status", "unknown")
            image = item.get("Image", "unknown")
            lines.append(f"{names} | {state} | {status} | {image}")
        return _result("success", output="\n".join(lines), command_repr="docker_api:/containers/json")
    except Exception as exc:
        return _result("failure", error=str(exc), command_repr="docker_api:/containers/json")


def get_non_running_containers() -> Tuple[List[Dict[str, str]], str]:
    try:
        containers = _json_request("/containers/json?all=1")
    except Exception as exc:
        return [], str(exc)

    issues: List[Dict[str, str]] = []
    for item in containers:
        state = (item.get("State") or "").lower()
        if state == "running":
            continue
        names = ", ".join(name.lstrip("/") for name in item.get("Names", [])) or item.get("Id", "")[:12]
        issues.append({
            "name": names,
            "status": item.get("Status", state or "unknown"),
        })
    return issues, ""
