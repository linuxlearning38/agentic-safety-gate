import ollama
import chromadb

# ── Load your actual policy files ──────────────────────────────
DOCS = {
    "infrastructure.rego": open("/home/manoj85/agentic-api/policies/infrastructure.rego").read(),
    "gatekeeper.py":       open("/home/manoj85/agentic-api/gatekeeper.py").read(),

    # ── DISABLED (AWS) ── uncomment when needed ────────────────
    # "aws_audit.sh":             open("/home/manoj85/agentic-api/aws_audit.sh").read(),
    # "aws_strict_cost_guard.sh": open("/home/manoj85/agentic-api/aws_strict_cost_guard.sh").read(),
}

# ── Set up ChromaDB ────────────────────────────────────────────
chroma = chromadb.PersistentClient(path="/mnt/i/ai-lab/chromadb")
collection = chroma.get_or_create_collection("devops_policies")

# ── Index documents ────────────────────────────────────────────
existing = collection.get()["ids"]
for name, content in DOCS.items():
    if name not in existing:
        print(f"📚 Indexing {name}...")
        response = ollama.embeddings(model="llama3.1:8b", prompt=content)
        collection.add(
            ids=[name],
            embeddings=[response["embedding"]],
            documents=[content],
            metadatas=[{"source": name}]
        )
        print(f"✅ Indexed {name}")

print("✅ Knowledge base ready\n")

# ── RAG Query Function ─────────────────────────────────────────
def ask(question):
    q_embed = ollama.embeddings(model="llama3.1:8b", prompt=question)
    results = collection.query(
        query_embeddings=[q_embed["embedding"]],
        n_results=2
    )
    context = "\n\n---\n\n".join(results["documents"][0])
    prompt = f"""You are a DevOps assistant. Use the following policy files as context to answer accurately.

CONTEXT:
{context}

QUESTION: {question}

Answer based on the actual policies shown above:"""

    response = ollama.chat(
        model="llama3.1:8b",
        messages=[{"role": "user", "content": prompt}]
    )
    return response["message"]["content"]

# ── Interactive Loop ───────────────────────────────────────────
print("🤖 DevOps RAG Agent — answers from YOUR actual policies")
print("Type 'exit' to quit\n")

while True:
    question = input("You: ").strip()
    if question.lower() == "exit":
        break
    if not question:
        continue
    print(f"\nAgent: {ask(question)}\n")
