import ollama
import chromadb
import os

chroma = chromadb.PersistentClient(path="/mnt/i/ai-lab/chromadb")
collection = chroma.get_or_create_collection("devops_policies")

DOCS = {
    "infrastructure.rego":      "/home/manoj85/agentic-api/policies/infrastructure.rego",
    "gatekeeper.py":            "/home/manoj85/agentic-api/gatekeeper.py",
    "secure_apply.sh":          "/home/manoj85/agentic-api/secure_apply.sh",
    "docker-compose.yml":       "/home/manoj85/agentic-api/docker-compose.yml",
    "main.tf":                  "/home/manoj85/agentic-api/terraform-test/main.tf",
    "tests_runner.py":          "/home/manoj85/agentic-api/tests/runner.py",
    "deny_cost_exceeded":       "/home/manoj85/agentic-api/tests/scenarios/deny_cost_exceeded.json",
    "deny_iam_wildcard":        "/home/manoj85/agentic-api/tests/scenarios/deny_iam_wildcard.json",
    "deny_public_ssh":          "/home/manoj85/agentic-api/tests/scenarios/deny_public_ssh.json",
    "deny_unencrypted":         "/home/manoj85/agentic-api/tests/scenarios/deny_unencrypted.json",
    "deny_public_s3":           "/home/manoj85/agentic-api/tests/scenarios/deny_public_s3.json",
    "deny_public_rds":          "/home/manoj85/agentic-api/tests/scenarios/deny_public_rds.json",
    "allow_valid_instance":     "/home/manoj85/agentic-api/tests/scenarios/allow_valid_instance.json",
    "deny_multiple_violations": "/home/manoj85/agentic-api/tests/scenarios/deny_multiple_violations.json",
    "deny_t2_micro":            "/home/manoj85/agentic-api/tests/scenarios/deny_t2_micro.json",
    "deny_missing_tag":         "/home/manoj85/agentic-api/tests/scenarios/deny_missing_tag.json",
    "deny_bad_region":          "/home/manoj85/agentic-api/tests/scenarios/deny_bad_region.json",
}

existing = collection.get()["ids"]

for name, path in DOCS.items():
    if name in existing:
        print(f"⏭️  Already indexed: {name}")
        continue
    try:
        content = open(path).read()
        response = ollama.embeddings(model="llama3.1:8b", prompt=content)
        collection.add(
            ids=[name],
            embeddings=[response["embedding"]],
            documents=[content],
            metadatas=[{"source": name, "path": path}]
        )
        print(f"✅ Indexed: {name}")
    except Exception as e:
        print(f"❌ Failed {name}: {e}")

print(f"\nDone. Total documents: {collection.count()}")
