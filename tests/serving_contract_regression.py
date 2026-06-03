#!/usr/bin/env python3
"""Regression checks for serving-layer splitting and command extraction."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "web_agent_v2.1_guardrail.py"
ROUTER_PATH = ROOT / "control" / "input_router.py"


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def _extract_assignments(source: str, names: set[str]) -> str:
    tree = ast.parse(source)
    segments: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    segments.append(ast.get_source_segment(source, node) or "")
                    break
    return "\n\n".join(segment for segment in segments if segment)


def _extract_functions(source: str, names: set[str]) -> str:
    tree = ast.parse(source)
    segments: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            segments.append(ast.get_source_segment(source, node) or "")
    return "\n\n".join(segment for segment in segments if segment)


def _build_guardrail_namespace() -> dict:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    assignments = _extract_assignments(source, {"_RAW_COMMAND_PREFIXES", "_RAW_COMMAND_STARTERS", "_NOISE_MULTI_FRAGMENTS"})
    functions = _extract_functions(source, {"extract_explicit_command_request", "detect_multiple_questions", "split_multi_query", "_normalize_user_query", "_strip_outer_query_labels", "_remove_repeated_wrappers", "_is_noise_question_fragment", "_normalize_text", "extract_operational_clarification"})
    ns = {"re": re}
    exec(assignments + "\n\n" + functions, ns)
    return ns


def _build_router_namespace() -> dict:
    source = ROUTER_PATH.read_text(encoding="utf-8")
    assignments = _extract_assignments(source, {"AVA_SELF_TOPIC_PATTERNS"})
    functions = _extract_functions(source, {"classify_ava_self_topic"})
    ns = {}
    exec(assignments + "\n\n" + functions, ns)
    return ns


def main() -> int:
    guardrail = _build_guardrail_namespace()
    router = _build_router_namespace()

    failures: list[bool] = []
    failures.extend(
        [
            check(
                "question splitter ignores trailing please fragment",
                guardrail["detect_multiple_questions"]("please What is Kubernetes? please") == ["please What is Kubernetes? please"],
            ),
            check(
                "question splitter ignores trailing for-me fragment",
                guardrail["detect_multiple_questions"]("What is a Pod? for me?") == ["What is a Pod? for me?"],
            ),
            check(
                "question splitter still handles true two-question prompt",
                len(guardrail["detect_multiple_questions"]("What is Docker? What is Kubernetes?")) == 2,
            ),
            check(
                "explicit run strips right-now decoration",
                guardrail["extract_explicit_command_request"]("run date right now") == "date",
            ),
            check(
                "explicit run accepts polite can-you prefix",
                guardrail["extract_explicit_command_request"]("can you run date right now") == "date",
            ),
            check(
                "explicit run lowercases uppercase command starter",
                guardrail["extract_explicit_command_request"]("KINDLY RUN WHOAMI RIGHT NOW") == "whoami",
            ),
            check(
                "explicit run strips please decoration",
                guardrail["extract_explicit_command_request"]("run whoami please") == "whoami",
            ),
            check(
                "operational clarification survives kindly-now wrapper",
                "deployment name" in (guardrail["extract_operational_clarification"]("kindly restart my pod now") or "").lower(),
            ),
            check(
                "operational clarification survives ava comma wrapper",
                "deployment name" in (guardrail["extract_operational_clarification"]("ava, restart my pod now") or "").lower(),
            ),
            check(
                "normalizer preserves leading cpu quantity",
                guardrail["_normalize_user_query"]("3 CPU, 8gb RAM, 40 GB disk").startswith("3 CPU"),
            ),
            check(
                "normalizer preserves lowercase cpu quantity",
                guardrail["_normalize_user_query"]("3 cpu, 8gb ram, 60 gb disk").startswith("3 cpu"),
            ),
            check(
                "normalizer still strips copied numbered question prefix",
                guardrail["_normalize_user_query"]("3. What is Kubernetes?") == "What is Kubernetes?",
            ),
            check(
                "ava mention alone no longer forces ava-self topic",
                router["classify_ava_self_topic"]("ava please tcp vs udp now") is None,
            ),
        ]
    )

    source = SOURCE_PATH.read_text(encoding="utf-8")
    ask_source = _extract_functions(source, {"ask"})
    controlled_source = _extract_functions(source, {"_resolve_controlled_query"})
    failures.extend(
        [
            check(
                "ask endpoint does not re-run detect_query_intent after initial route",
                "detect_query_intent(query)" not in ask_source,
            ),
            check(
                "controlled resolver does not re-run detect_query_intent after initial route",
                "detect_query_intent(query)" not in controlled_source,
            ),
            check(
                "ask endpoint routes before meta fallback",
                ask_source.find("controlled_route = _route_query(query)") < ask_source.find("if is_meta_question(query)"),
            ),
        ]
    )

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nServing contract regression failed: {failed} issue(s)")
        return 1
    print("\nServing contract regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
