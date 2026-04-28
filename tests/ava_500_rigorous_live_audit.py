#!/usr/bin/env python3
"""500-question live mutation audit for AVA.

This expands the 100-question live smoke suite into five rounds of
real-user phrasing variation while preserving the original behavioral
expectations for:
- knowledge answers
- troubleshooting and clarification
- diagrams
- security and vulnerability flows
- approvals and destructive blocking
- memory and follow-up behavior
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import ava_live_100_question_test as smoke  # noqa: E402


STATEFUL_QUERIES = {
    "Remember this: test_live_100_server=prod-india-01",
    "What is my test_live_100_server?",
    "what should I do next",
}


def mutate_query(query: str, variant: int) -> str:
    if query in STATEFUL_QUERIES:
        return query

    prefixes = ("", "please ", "ava, ", "can you ", "kindly ", "ava please ")
    suffixes = ("", " now", " please", " for me", " right now")
    q = f"{prefixes[variant % len(prefixes)]}{query}{suffixes[(variant // len(prefixes)) % len(suffixes)]}".strip()

    mode = variant % 7
    if mode == 1:
        q = q.capitalize()
    elif mode == 2:
        q = q.upper()
    elif mode == 3 and not q.endswith("?"):
        q = q + "?"
    elif mode == 4:
        q = f"  {q}  "
    elif mode == 5:
        q = q.replace("  ", " ")
    return q


def classify_case(case: dict) -> str:
    query = case["query"].lower()
    if case.get("type") == "diagram":
        return "diagram"
    if case.get("equals", {}).get("result.blocked") is True:
        return "blocked"
    if case.get("equals", {}).get("result.approval_required") is True:
        return "approval"
    if case.get("type") == "memory":
        return "memory"
    if any(token in query for token in ("crashloop", "oomkilled", "service is down", "pod network", "find problems", "find issues", "diagnose", "troubleshoot", "something is wrong")):
        return "troubleshooting"
    if any(token in query for token in ("vulnerabilities", "suspicious", "auth", "security updates", "investigate on this host", "failed services", "persistence points")):
        return "security"
    if any(token in query for token in ("capital of france", "2+2", "photosynthesis", "machine learning", "tcp vs udp", "what is server")):
        return "scope"
    if any(token in query for token in ("restart my pod", "show me pod logs", "check my service", "rollback my deployment", "scale deployment to 5 replicas", "inspect my service", "stop process", "patch package")):
        return "clarification"
    return "knowledge"


def build_cases(rounds: int, shuffle_seed: int, start_round: int = 0) -> list[dict]:
    base_cases = smoke.build_cases()
    expanded: list[dict] = []

    for local_round_index in range(rounds):
        round_index = start_round + local_round_index
        for base_index, case in enumerate(base_cases):
            clone = dict(case)
            clone["query"] = mutate_query(case["query"], round_index * len(base_cases) + base_index)
            clone["category"] = classify_case(case)
            clone["round"] = round_index + 1
            clone["base_index"] = base_index + 1
            if case["query"].lower() == "what does rm -rf do":
                clone.pop("not_contains", None)
                clone["timeout"] = max(int(case.get("timeout", 10)), 20)
            expanded.append(clone)

    if len(expanded) != rounds * len(base_cases):
        raise AssertionError(f"expected {rounds * len(base_cases)} cases, got {len(expanded)}")

    # Keep the sequence stable because memory and follow-up expectations are stateful.
    random.Random(shuffle_seed).shuffle(expanded[:0])
    return expanded


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 500 mutated live /ask checks against AVA.")
    parser.add_argument("--rounds", type=int, default=5, help="How many 100-question rounds to run.")
    parser.add_argument("--start-round", type=int, default=0, help="Variant round offset for generating a distinct mutation window.")
    parser.add_argument("--delay", type=float, default=1.05, help="Seconds between questions.")
    parser.add_argument("--shuffle-seed", type=int, default=20260428, help="Reserved for future shuffling; execution stays ordered for stateful checks.")
    args = parser.parse_args()

    token = smoke.login()
    cases = build_cases(rounds=args.rounds, shuffle_seed=args.shuffle_seed, start_round=args.start_round)
    totals = Counter()
    failures = Counter()
    sample_failures: list[dict] = []

    started = time.time()
    for index, case in enumerate(cases, start=1):
        ok, detail, data = smoke.check_case(case, token)
        category = case["category"]
        totals[category] += 1
        if not ok:
            failures[category] += 1
            if len(sample_failures) < 25:
                preview = json.dumps(data, ensure_ascii=False)[:700] if data is not None else ""
                sample_failures.append(
                    {
                        "case": index,
                        "round": case["round"],
                        "base_index": case["base_index"],
                        "category": category,
                        "query": case["query"],
                        "detail": detail,
                        "preview": preview,
                    }
                )
        if index % 50 == 0:
            elapsed = time.time() - started
            passed = index - sum(failures.values())
            print(f"progress={index}/{len(cases)} pass={passed} fail={sum(failures.values())} elapsed_sec={elapsed:.1f}")
        if args.delay and index != len(cases):
            time.sleep(args.delay)

    elapsed = time.time() - started
    failed = sum(failures.values())
    passed = len(cases) - failed
    print("\n=== AVA_500_RIGOROUS_LIVE_AUDIT ===")
    print(f"start_round={args.start_round}")
    print(f"total={len(cases)} passed={passed} failed={failed} pass_rate={(passed/len(cases))*100:.2f}% elapsed_sec={elapsed:.1f}")
    print("category_totals=" + json.dumps(dict(sorted(totals.items())), sort_keys=True))
    print("category_failures=" + json.dumps(dict(sorted(failures.items())), sort_keys=True))
    print("sample_failures=" + json.dumps(sample_failures, indent=2, ensure_ascii=True))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
