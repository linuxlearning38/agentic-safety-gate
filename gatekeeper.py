import requests
import json
import sys

OPA_URL = "http://localhost:8181/v1/data/infrastructure/allow"
TIMEOUT_SECONDS = 2


def evaluate(input_data):
    # Fail-closed default
    decision = "DENIED"

    try:
        response = requests.post(
            OPA_URL,
            json={"input": input_data},
            timeout=TIMEOUT_SECONDS,
        )

        response.raise_for_status()

        data = response.json()

        if data.get("result") is True:
            decision = "APPROVED"

    except Exception as e:
        print(f"[GATEKEEPER ERROR] {type(e).__name__}: {e}")

    return decision


if __name__ == "__main__":
    test_input = {
        "instance_type": "t3.micro",
        "security_groups": [{"cidr": "10.0.0.0/16"}],
    }

    result = evaluate(test_input)
    print(f"Decision: {result}")

