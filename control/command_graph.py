# control/command_graph.py
# AVA Phase 4 — Day 2: Command Graphs
#
# What this does:
#   Deterministic diagnostic workflows for known problem patterns.
#   Instead of asking the LLM what to do, the graph already knows —
#   it just runs the right tools in order and hands all output to the
#   LLM for analysis.
#
# Flow:
#   User query → match_graph() → CommandGraph.execute() →
#   list of ToolResult dicts → generate_response() (in main app)
#
# Design rules:
#   - Pattern matching is keyword-based, NOT another LLM call
#   - Each step calls tool_registry.execute() — shell=False guaranteed
#   - Steps marked require_approval=True pause and return approval_required
#   - Max 8 steps per graph (prevents runaway chains)
#   - Falls back to None if no graph matches → ReAct loop (Day 3) takes over

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable
from datetime import datetime

from control.tool_registry import registry as tool_registry
from control.secure_executor import execute_tool_safe

logger = logging.getLogger(__name__)

# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class GraphStep:
    tool_name:        str
    args:             Dict                = field(default_factory=dict)
    description:      str                = ""
    require_approval: bool               = False   # medium-risk steps pause here
    skip_on_failure:  bool               = True    # continue graph if this step fails
    args_from_query:  Optional[Callable] = None    # fn(query) -> dict, extracts args from user query


@dataclass
class GraphResult:
    graph_name:   str
    query:        str
    steps_run:    List[Dict]             = field(default_factory=list)
    paused_at:    Optional[str]          = None    # step that needs approval
    approval_id:  Optional[str]          = None
    success:      bool                   = True
    timestamp:    str                    = field(default_factory=lambda: datetime.now().isoformat())

    def to_context_blocks(self) -> List[str]:
        """Format step outputs for the LLM context window."""
        blocks = []
        for step in self.steps_run:
            header = f"[{step['step']}] {step['tool']} — {step['description']}"
            status = step.get('status', '?')
            output = step.get('output', '') or step.get('error', '')
            blocks.append(f"{header}\nStatus: {status}\n{output[:2000]}")
        return blocks

    def summary_for_ui(self) -> str:
        """Short string shown in the UI while graph runs."""
        lines = [f"🔍 Running diagnostic: **{self.graph_name}**\n"]
        for step in self.steps_run:
            icon = "✅" if step['status'] == 'success' else "⚠️"
            lines.append(f"{icon} {step['description']}")
        if self.paused_at:
            lines.append(f"\n⏸️ Paused — approval required for: `{self.paused_at}`")
        return "\n".join(lines)


# ─── Argument extractors ──────────────────────────────────────────────────────
# These pull structured args from the raw user query string.
# Called at graph execution time so args are query-specific.

import re

def _extract_pod_name(query: str) -> Dict:
    """Try to find a pod name in the query. Falls back to empty (kubectl lists all)."""
    # Match things like: nginx-pod, my-app-7d4b9f-xyz, frontend
    match = re.search(
        r'\b([a-z][a-z0-9\-]{2,62}(?:-[a-z0-9]+)*)\b',
        query.lower()
    )
    # Exclude common English words that look like k8s names
    stop_words = {'pod', 'pods', 'my', 'the', 'is', 'are', 'why', 'how',
                  'does', 'did', 'not', 'start', 'run', 'crash', 'loop',
                  'high', 'disk', 'node', 'service', 'down', 'slow'}
    if match and match.group(1) not in stop_words:
        return {"pod_name": match.group(1), "namespace": "default"}
    return {"namespace": "default"}

def _extract_service_name(query: str) -> Dict:
    """Try to find a systemd service name in the query."""
    match = re.search(r'\b([a-zA-Z][a-zA-Z0-9_\-\.]{1,50})\b', query)
    stop_words = {'service', 'the', 'my', 'is', 'down', 'not', 'running',
                  'check', 'why', 'how'}
    if match and match.group(1).lower() not in stop_words:
        return {"service": match.group(1)}
    return {}

def _extract_image_name(query: str) -> Dict:
    """Try to find a container image name in the query."""
    match = re.search(
        r'\b([a-zA-Z0-9][a-zA-Z0-9_\-\./]*(?::[a-zA-Z0-9_\.\-]+)?)\b',
        query
    )
    stop_words = {'scan', 'trivy', 'image', 'my', 'the', 'for', 'cve',
                  'vulnerability', 'vulnerabilities'}
    if match and match.group(1).lower() not in stop_words:
        return {"image": match.group(1)}
    return {}


# ─── Graph definitions ────────────────────────────────────────────────────────

COMMAND_GRAPHS: Dict[str, List[GraphStep]] = {

    # ── Pod crashloop ─────────────────────────────────────────────────────────
    "pod_crashloop": [
        GraphStep(
            tool_name="check_pod_status",
            args_from_query=lambda q: {"namespace": "default"},
            description="Check pod status across namespace",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="check_pod_describe",
            args_from_query=_extract_pod_name,
            description="Describe pod — events, limits, restart count",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="check_logs",
            args_from_query=_extract_pod_name,
            description="Fetch recent pod logs",
            skip_on_failure=True,
        ),
    ],

    # ── High disk usage ───────────────────────────────────────────────────────
    "high_disk": [
        GraphStep(
            tool_name="check_disk",
            description="Check disk usage (df -h)",
            skip_on_failure=False,
        ),
        GraphStep(
            tool_name="check_pod_status",
            args={"namespace": "default"},
            description="Check if any pods are filling disk",
            skip_on_failure=True,
        ),
    ],

    # ── High memory / OOM ─────────────────────────────────────────────────────
    "high_memory": [
        GraphStep(
            tool_name="check_memory",
            description="Check system memory (free -h)",
            skip_on_failure=False,
        ),
        GraphStep(
            tool_name="check_pod_status",
            args={"namespace": "default"},
            description="Check pod resource states",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="check_node_status",
            description="Check node resource pressure",
            skip_on_failure=True,
        ),
    ],

    # ── Service down ──────────────────────────────────────────────────────────
    "service_down": [
        GraphStep(
            tool_name="check_service_health",
            args_from_query=_extract_service_name,
            description="Check systemd service status",
            skip_on_failure=False,
        ),
        GraphStep(
            tool_name="restart_service",
            args_from_query=_extract_service_name,
            description="Restart the service",
            require_approval=True,       # medium risk — pauses for approval
            skip_on_failure=False,
        ),
    ],

    # ── Node not ready ────────────────────────────────────────────────────────
    "node_not_ready": [
        GraphStep(
            tool_name="check_node_status",
            description="Check all node statuses",
            skip_on_failure=False,
        ),
        GraphStep(
            tool_name="check_pod_status",
            args={"namespace": "default"},
            description="Check pods on affected node",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="check_memory",
            description="Check system memory on node",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="check_disk",
            description="Check disk — low disk causes NotReady",
            skip_on_failure=True,
        ),
    ],

    # ── General k8s health check ──────────────────────────────────────────────
    "cluster_health": [
        GraphStep(
            tool_name="check_node_status",
            description="Check all nodes",
            skip_on_failure=False,
        ),
        GraphStep(
            tool_name="check_pod_status",
            args={"namespace": "default"},
            description="Check pods in default namespace",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="check_disk",
            description="Check disk usage",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="check_memory",
            description="Check memory",
            skip_on_failure=True,
        ),
    ],

    # ── Container image scan ──────────────────────────────────────────────────
    "image_scan": [
        GraphStep(
            tool_name="trivy_scan",
            args_from_query=_extract_image_name,
            description="Scan container image for CVEs",
            skip_on_failure=False,
        ),
    ],

    # ── Deployment scaling ────────────────────────────────────────────────────
    "restart_deployment": [
        GraphStep(
            tool_name="check_pod_status",
            args_from_query=lambda q: {"namespace": "default"},
            description="Check current pod state before restart",
            skip_on_failure=True,
        ),
        GraphStep(
            tool_name="restart_pod",
            args_from_query=lambda q: {
                "deployment": re.search(r'\b([a-z][a-z0-9\-]{2,50})\b', q.lower()).group(1)
                if re.search(r'\b([a-z][a-z0-9\-]{2,50})\b', q.lower()) else "unknown",
                "namespace": "default",
            },
            description="Rollout restart deployment",
            require_approval=True,
            skip_on_failure=False,
        ),
    ],
}


# ─── Pattern → Graph mapping ──────────────────────────────────────────────────
# Checked in order — first match wins.
# Keep patterns specific enough to avoid false positives.

GRAPH_PATTERNS: List[tuple] = [
    # (graph_name, list_of_trigger_phrases)
    ("pod_crashloop", [
        "crashloop", "crash loop", "crashloopbackoff", "pod keeps restarting",
        "pod restarting", "pod not starting", "pod failing", "pod is crashing",
        "oomkilled", "pod is stuck", "back-off restarting",
    ]),
    ("high_disk", [
        "disk full", "disk is full", "disk usage", "disk space", "no space left",
        "storage full", "running out of disk", "disk is high",
        "df -h", "disk pressure",
    ]),
    ("high_memory", [
        "out of memory", "oom", "memory usage high", "memory usage is high",
        "memory full", "memory pressure", "high memory", "memory is high",
        "running out of memory", "memory leak", "usage is very high",
        "memory is very high",
    ]),
    ("service_down", [
        "service is down", "service down", "service not running", "is down",
        "service failed", "service not starting", "unit failed",
        "systemctl status",
    ]),
    ("node_not_ready", [
        "node not ready", "node notready", "node is not ready",
        "node unreachable", "node down", "node failed",
    ]),
    ("cluster_health", [
        "cluster health", "check my cluster", "cluster status",
        "overall health", "health check", "check everything",
        "what's wrong with my cluster",
    ]),
    ("image_scan", [
        "scan image", "scan my image", "trivy scan", "cve scan",
        "vulnerability scan", "scan for cve", "scan for cves", "check vulnerabilities",
        "scan container", "scan my ", "for cves", "for cve",
    ]),
    ("restart_deployment", [
        "restart deployment", "rollout restart", "restart my deployment",
        "restart the deployment", "restart my ", "redeploy",
    ]),
]


# ─── Graph matcher ────────────────────────────────────────────────────────────

def match_graph(query: str) -> Optional[str]:
    """
    Return the graph name if the query matches a known pattern, else None.
    Fast keyword match — no LLM call.
    """
    q = query.lower()
    for graph_name, patterns in GRAPH_PATTERNS:
        if any(p in q for p in patterns):
            logger.info(f"[CommandGraph] Matched graph '{graph_name}' for query: {query[:60]}")
            return graph_name
    return None


# ─── Graph executor ───────────────────────────────────────────────────────────

def execute_graph(graph_name: str, query: str) -> GraphResult:
    """
    Execute a command graph step by step.

    - Steps run in order using tool_registry.execute() (shell=False always)
    - Medium/high risk steps that require_approval=True pause execution
      and return a GraphResult with paused_at set
    - If a step fails and skip_on_failure=True, execution continues
    - All outputs are collected in GraphResult.steps_run

    Returns a GraphResult. The caller (main app) passes
    result.to_context_blocks() to generate_response().
    """
    steps = COMMAND_GRAPHS.get(graph_name)
    if not steps:
        return GraphResult(
            graph_name=graph_name,
            query=query,
            success=False,
            steps_run=[{"error": f"Unknown graph: {graph_name}"}],
        )

    result = GraphResult(graph_name=graph_name, query=query)

    for i, step in enumerate(steps, 1):
        # Resolve args — static dict or dynamic extractor
        if step.args_from_query:
            try:
                args = step.args_from_query(query)
            except Exception as e:
                logger.warning(f"[CommandGraph] arg extractor failed for step {i}: {e}")
                args = step.args
        else:
            args = step.args.copy()

        step_record = {
            "step":        f"Step {i}/{len(steps)}",
            "tool":        step.tool_name,
            "args":        args,
            "description": step.description,
        }

        tool_result = execute_tool_safe(step.tool_name, args, query, source="graph")
        step_record["status"] = tool_result.get("status", "unknown")
        step_record["output"] = tool_result.get("output", "")
        step_record["error"]  = tool_result.get("error", "")
        if tool_result.get("approval_id"):
            step_record["approval_id"] = tool_result.get("approval_id")
        result.steps_run.append(step_record)

        if tool_result.get("status") == "approval_required":
            result.paused_at = step.tool_name
            result.approval_id = tool_result.get("approval_id")
            result.success = False
            logger.info(f"[CommandGraph] Paused at step {i} — approval required")
            return result

        logger.info(
            f"[CommandGraph] Step {i} '{step.tool_name}': {step_record['status']}"
        )

        # Stop graph if critical step failed
        if tool_result.get("status") not in ("success",) and not step.skip_on_failure:
            logger.warning(f"[CommandGraph] Critical step {i} failed — stopping graph")
            result.success = False
            break

    return result


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    queries = [
        "my pod is crashlooping",
        "disk is full on the node",
        "check my cluster health",
        "scan my nginx:latest image for CVEs",
        "how do I configure ingress",       # should NOT match any graph
    ]

    print("=== Graph Matcher Tests ===")
    for q in queries:
        matched = match_graph(q)
        print(f"  '{q[:45]}' → {matched or 'NO MATCH (→ LLM path)'}")

    print("\n=== Graph Execution Test (cluster_health) ===")
    r = execute_graph("cluster_health", "check my cluster health")
    print(f"Graph: {r.graph_name} | Steps run: {len(r.steps_run)} | Success: {r.success}")
    for step in r.steps_run:
        print(f"  [{step['status'].upper():8}] {step['tool']} — {step['output'][:60]}")

    print("\n=== Context blocks for LLM ===")
    for block in r.to_context_blocks():
        print(block[:120])
        print("---")
