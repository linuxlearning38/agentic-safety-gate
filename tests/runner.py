import json
import os
import sys

# Make project root importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gatekeeper import evaluate_policy


def run_scenario(file_path):
    with open(file_path, "r") as f:
        scenario = json.load(f)

    name = scenario["name"]
    input_data = scenario["input"]
    expected_decision = scenario["expected_decision"]
    expected_reason_contains = scenario.get("expected_reason_contains")

    result = evaluate_policy(input_data)

    decision_match = result["decision"] == expected_decision
    reason_match = True

    if expected_reason_contains:
        reason_match = any(
            expected_reason_contains in reason
            for reason in result["reasons"]
        )

    if decision_match and reason_match:
        print(f"[PASS] {name}")
        return True
    else:
        print(f"[FAIL] {name}")
        print("  Expected Decision:", expected_decision)
        print("  Actual Decision:", result["decision"])
        print("  Reasons:", result["reasons"])
        return False


def main():
    scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")
    files = [f for f in os.listdir(scenarios_dir) if f.endswith(".json")]

    if not files:
        print("No test scenarios found.")
        sys.exit(1)

    all_passed = True

    for filename in files:
        file_path = os.path.join(scenarios_dir, filename)
        if not run_scenario(file_path):
            all_passed = False

    if not all_passed:
        sys.exit(1)

    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
