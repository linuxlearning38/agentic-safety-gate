from pathlib import Path
import importlib.util
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "control" / "capability_router.py"


def load_router():
    spec = importlib.util.spec_from_file_location("ava_capability_router_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"[PASS] {name}")


def main():
    router = load_router()

    cases = [
        ("show docker workloads", "container.list", "list_containers", {}),
        ("docker ps", "container.list", "list_containers", {}),
        ("are containers up", "container.list", "list_containers", {}),
        ("docker daemon status", "docker.status", "check_docker", {}),
        ("who is listening on ports", "host.ports", "check_listening_ports", {}),
        ("check auth events", "auth.events", "check_auth_events", {}),
        ("look for suspicious activity", "security.suspicious_activity", "check_suspicious_activity", {}),
        ("run system verification", "host.verify", "verify_system", {}),
        ("can you check disk usage", "host.disk", "check_disk", {}),
        ("show me ram usage", "host.memory", "check_memory", {}),
        ("do i have failed services", "service.failed", "check_failed_services", {}),
        ("do i need updates", "package.updates", "check_updates", {}),
        ("scan cves", "vulnerability.scan", "scan_host_vulnerabilities", {}),
        ("what should i fix first", "host.risk", "assess_host_risk", {}),
        ("is nginx running", "service.inspect", "inspect_service", {"service": "nginx"}),
        ("nginx service status", "service.inspect", "inspect_service", {"service": "nginx"}),
        ("pid 4321 details", "process.inspect", "inspect_process", {"pid": "4321"}),
    ]

    for query, capability_id, tool_name, args in cases:
        match = router.route_capability(query)
        check(f"{query!r} routes", match is not None)
        check(f"{query!r} capability", match.capability_id == capability_id)
        check(f"{query!r} tool", match.tool_name == tool_name)
        check(f"{query!r} args", match.tool_args == args)
        check(f"{query!r} no missing args", not match.missing)

    service_missing = router.route_capability("service health")
    check("missing service clarifies", service_missing is not None and service_missing.missing == ("service",))
    check("missing service message", "service name" in service_missing.clarification.lower())

    pid_missing = router.route_capability("inspect pid")
    check("missing pid clarifies", pid_missing is not None and pid_missing.missing == ("pid",))
    check("missing pid message", "pid" in pid_missing.clarification.lower())

    check("non-devops query ignored", router.route_capability("write a poem about clouds") is None)
    check("restart docker service defers to approval routing", router.route_capability("restart docker service") is None)
    check("install security updates defers to approval routing", router.route_capability("install security updates") is None)
    check("apply security updates defers to approval routing", router.route_capability("apply security updates") is None)
    check("stop process 1234 defers to approval routing", router.route_capability("stop process 1234") is None)
    check("kill process 1234 defers to approval routing", router.route_capability("kill process 1234") is None)
    check("kubernetes service definition defers to knowledge routing", router.route_capability("what is a kubernetes service") is None)
    check("docker containers and ports introspection defers to ava_self routing", router.route_capability("what docker containers and ports are you running") is None)

    print("[PASS] capability router regression complete")


if __name__ == "__main__":
    main()
