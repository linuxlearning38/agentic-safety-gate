import requests
from requests.exceptions import Timeout, ConnectionError, HTTPError
import uuid
import datetime
import json
import psycopg

OPA_BASE = "http://localhost:8181/v1/data/infrastructure"
TIMEOUT_SECONDS = 2

DB_CONFIG = {
    "dbname": "agent_db",
    "user": "agent",
    "password": "localdev_only",
    "host": "localhost",
    "port": 5432,
}


def evaluate(input_data):
    decision = "DENIED"
    reasons = []

    try:
        # -------------------------
        # 1. Evaluate Allow Rule
        # -------------------------
        allow_response = requests.post(
            f"{OPA_BASE}/allow",
            json={"input": input_data},
            timeout=TIMEOUT_SECONDS,
        )
        allow_response.raise_for_status()

        allow_json = allow_response.json()

        if "result" not in allow_json:
            return decision, ["Malformed OPA allow response – fail closed"]

        allow_result = allow_json["result"]

        if allow_result is True:
            decision = "APPROVED"
            return decision, reasons

        # -------------------------
        # 2. Fetch Violations
        # -------------------------
        violation_response = requests.post(
            f"{OPA_BASE}/violation",
            json={"input": input_data},
            timeout=TIMEOUT_SECONDS,
        )
        violation_response.raise_for_status()

        violation_json = violation_response.json()

        if "result" not in violation_json:
            return decision, ["Malformed OPA violation response – fail closed"]

        violations = violation_json["result"]

        if isinstance(violations, dict):
            reasons = list(violations.keys())
        else:
            reasons = ["Unexpected violation format from OPA – fail closed"]

    except Timeout:
        reasons = ["OPA timeout – fail closed"]

    except ConnectionError:
        reasons = ["OPA connection error – fail closed"]

    except HTTPError as e:
        status = e.response.status_code if e.response else "unknown"
        reasons = [f"OPA HTTP error ({status}) – fail closed"]

    except json.JSONDecodeError:
        reasons = ["OPA returned invalid JSON – fail closed"]

    except Exception as e:
        reasons = [f"Unexpected error ({type(e).__name__}) – fail closed"]

    return decision, reasons


def log_decision(decision_id, timestamp, input_payload, decision, reasons):
    try:
        with psycopg.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO decisions
                    (decision_id, timestamp, input_payload, decision, violation_reasons)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        decision_id,
                        timestamp,
                        json.dumps(input_payload),
                        decision,
                        json.dumps(reasons),
                    ),
                )
            conn.commit()
    except Exception as e:
        raise RuntimeError(f"AUDIT LOGGING FAILED: {e}")


def evaluate_policy(input_data: dict) -> dict:
    decision_id = str(uuid.uuid4())
    timestamp = datetime.datetime.now(datetime.UTC)

    decision, reasons = evaluate(input_data)

    log_decision(decision_id, timestamp, input_data, decision, reasons)

    return {
        "decision_id": decision_id,
        "timestamp": timestamp.isoformat(),
        "decision": decision,
        "reasons": reasons,
    }


if __name__ == "__main__":
    test_input = {
        "instance_type": "t2.micro",
        "region": "ap-south-1",
        "encrypted": True,
        "tags": {
            "Environment": "dev",
            "Owner": "manoj",
            "CostCenter": "cc-101",
        },
        "security_groups": [{"cidr": "0.0.0.0/0"}],
    }

    result = evaluate_policy(test_input)

    print("Decision ID:", result["decision_id"])
    print("Timestamp:", result["timestamp"])
    print("Decision:", result["decision"])

    if result["decision"] == "DENIED":
        print("Reasons:")
        for r in result["reasons"]:
            print(" -", r)
