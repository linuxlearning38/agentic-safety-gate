import requests
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
        allow_response = requests.post(
            f"{OPA_BASE}/allow",
            json={"input": input_data},
            timeout=TIMEOUT_SECONDS,
        )
        allow_response.raise_for_status()
        allow_result = allow_response.json().get("result", False)

        if allow_result:
            decision = "APPROVED"
            return decision, reasons

        violation_response = requests.post(
            f"{OPA_BASE}/violation",
            json={"input": input_data},
            timeout=TIMEOUT_SECONDS,
        )
        violation_response.raise_for_status()

        violations = violation_response.json().get("result", {})
        reasons = list(violations.keys())

    except Exception as e:
        reasons = [f"OPA unreachable – fail closed ({type(e).__name__})"]

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
        print(f"[AUDIT ERROR] {e}")


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
