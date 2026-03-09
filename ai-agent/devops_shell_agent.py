import ollama
import chromadb
import subprocess

chroma = chromadb.PersistentClient(path="/mnt/i/ai-lab/chromadb")
collection = chroma.get_or_create_collection("devops_policies")

ALLOWED_COMMANDS = [
    "docker", "kubectl", "cat", "ls", "find",
    "systemctl", "df", "free", "uname", "whoami",
    "ollama", "python3", "opa", "grep", "nproc",
    "ps", "which", "echo", "pwd", "date", "uptime", "lscpu"
]

def clean(command):
    return command.strip().strip('`').strip('"').strip("'")

def is_safe(command):
    first_word = clean(command).split()[0]
    return first_word in ALLOWED_COMMANDS

def run_shell(command):
    command = clean(command)
    if not is_safe(command):
        return "Command not allowed: " + command
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
        output = result.stdout or result.stderr
        return output.strip() or "Done with no output."
    except subprocess.TimeoutExpired:
        return "Timed out."
    except Exception as e:
        return "Error: " + str(e)

def get_context(question):
    q_embed = ollama.embeddings(model="llama3.1:8b", prompt=question)
    results = collection.query(query_embeddings=[q_embed["embedding"]], n_results=2)
    return "\n---\n".join(results["documents"][0])

def ask(question):
    context = get_context(question)
    prompt = (
        "You are a DevOps assistant with shell access and a policy knowledge base.\n\n"
        "POLICY CONTEXT:\n" + context + "\n\n"
        "To run a shell command respond with exactly:\n"
        "COMMAND: <command here>\n\n"
        "No backticks. Plain text only after COMMAND:\n\n"
        "QUESTION: " + question
    )
    response = ollama.chat(model="llama3.1:8b", messages=[{"role": "user", "content": prompt}])
    reply = response["message"]["content"]
    if "COMMAND:" in reply:
        for line in reply.split("\n"):
            if line.startswith("COMMAND:"):
                command = clean(line.replace("COMMAND:", ""))
                print("\n  Running: " + command)
                output = run_shell(command)
                print("  Output:\n" + output + "\n")
                followup = ollama.chat(
                    model="llama3.1:8b",
                    messages=[
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": reply},
                        {"role": "user", "content": "Command output:\n" + output + "\n\nGive a clear answer."}
                    ]
                )
                return followup["message"]["content"]
    return reply

print("DevOps Shell Agent - RAG + Shell Access")
print("Type exit to quit\n")

while True:
    question = input("You: ").strip()
    if question.lower() == "exit":
        break
    if not question:
        continue
    print("\nAgent: " + ask(question) + "\n")
