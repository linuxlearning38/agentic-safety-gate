#!/usr/bin/env python3
"""Regression checks for AVA's read-only Docker runtime boundary."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def main() -> int:
    from control import docker_runtime

    checks = []
    for path in ["/version", "/info", "/containers/json?all=0", "/containers/json?all=1"]:
        try:
            docker_runtime._assert_allowed_read_path(path)
            allowed = True
        except Exception:
            allowed = False
        checks.append(check(f"allows read-only Docker API path {path}", allowed))

    for path in ["/containers/create", "/containers/x/start", "/images/create", "/build", "/volumes/create"]:
        try:
            docker_runtime._assert_allowed_read_path(path)
            rejected = False
        except RuntimeError:
            rejected = True
        checks.append(check(f"rejects mutating Docker API path {path}", rejected))

    old_host = os.environ.get("DOCKER_HOST")
    old_module_host = docker_runtime.DOCKER_HOST
    try:
        os.environ["DOCKER_HOST"] = "http://docker-socket-proxy:2375"
        docker_runtime.DOCKER_HOST = os.environ["DOCKER_HOST"]
        ok, err = docker_runtime.docker_socket_available()
        checks.append(check("HTTP Docker proxy mode is considered available", ok and not err))
    finally:
        if old_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = old_host
        docker_runtime.DOCKER_HOST = old_module_host

    failed = len([item for item in checks if not item])
    if failed:
        print(f"\nDocker runtime security regression failed: {failed} issue(s)")
        return 1
    print("\nDocker runtime security regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
