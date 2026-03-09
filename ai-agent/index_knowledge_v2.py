import ollama
import chromadb
import os

chroma = chromadb.PersistentClient(path="/mnt/i/ai-lab/chromadb")

# Fresh collection with nomic embeddings
try:
    chroma.delete_collection("devops_policies_v2")
except:
    pass
collection = chroma.get_or_create_collection("devops_policies_v2")

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

def chunk_text(text, chunk_size=800, overlap=100):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

total_chunks = 0

for name, path in DOCS.items():
    try:
        content = open(path).read()
        chunks = chunk_text(content)
        for i, chunk in enumerate(chunks):
            chunk_id = f"{name}_chunk_{i}"
            response = ollama.embeddings(model="nomic-embed-text", prompt=chunk)
            collection.add(
                ids=[chunk_id],
                embeddings=[response["embedding"]],
                documents=[chunk],
                metadatas=[{"source": name, "chunk": i}]
            )
            total_chunks += 1
        print(f"✅ {name} → {len(chunks)} chunks")
    except Exception as e:
        print(f"❌ {name}: {e}")

print(f"\nDone. Total chunks indexed: {total_chunks}")
