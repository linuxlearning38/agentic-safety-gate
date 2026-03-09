import ollama

SYSTEM_PROMPT = """You are a DevOps assistant specializing in:
- Kubernetes and container orchestration
- Open Policy Agent (OPA) and Rego policies
- Linux system administration
- Docker and infrastructure automation
- Security and compliance

Always provide accurate, production-ready code and configurations.
When writing Rego policies, use commas for AND logic, not the 'and' keyword.
"""

history = []

def chat(user_input, model="llama3.1:8b"):
    history.append({"role": "user", "content": user_input})
    
    response = ollama.chat(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history
    )
    
    reply = response["message"]["content"]
    history.append({"role": "assistant", "content": reply})
    return reply

print("🤖 DevOps Agent (llama3.1:8b) — type 'exit' to quit\n")

while True:
    user_input = input("You: ").strip()
    if user_input.lower() == "exit":
        break
    if not user_input:
        continue
    print(f"\nAgent: {chat(user_input)}\n")
