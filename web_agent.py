from flask import Flask, request, jsonify
import ollama
import chromadb
import subprocess

app = Flask(__name__)

chroma = chromadb.PersistentClient(path="/mnt/i/ai-lab/chromadb")
collection = chroma.get_or_create_collection("devops_policies_v2")

ALLOWED_COMMANDS = [
    "docker", "kubectl", "cat", "ls", "find",
    "systemctl", "df", "free", "uname", "whoami",
    "ollama", "python3", "opa", "grep", "nproc",
    "ps", "which", "echo", "pwd", "date", "uptime", "lscpu"
]

conversation_history = []
token_stats = {"prompt": 0, "completion": 0}


def clean(command):
    return command.strip().strip('`').strip('"').strip("'")


def is_safe(command):
    first_word = clean(command).split()[0]
    return first_word in ALLOWED_COMMANDS


# Sensitive paths that should never be read
BLOCKED_PATHS = [
    "/etc/shadow", "/etc/passwd", "/etc/sudoers",
    "/.ssh/", "id_rsa", "id_ed25519", ".env",
    "/root/", "authorized_keys", ".aws/credentials",
    "/etc/ssl", "private_key", ".pem", ".key"
]

def run_shell(command):
    command = clean(command)
    if not is_safe(command):
        return "Command not allowed: " + command
    # Block sensitive paths
    if any(p in command for p in BLOCKED_PATHS):
        return "Access denied: sensitive path blocked."
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
        output = result.stdout or result.stderr
        # Filter to show only real Linux partitions
        if command.strip().startswith("df"):
            lines = output.split("\n")
            allowed = []
            for l in lines:
                if l.startswith("Filesystem"):
                    allowed.append(l)
                elif l.startswith("/dev/"):
                    allowed.append(l)
            output = "\n".join(allowed)
        return output.strip() or "Done with no output."
    except subprocess.TimeoutExpired:
        return "Timed out."
    except Exception as e:
        return "Error: " + str(e)


def get_context(question):
    base = collection.get(ids=["infrastructure.rego_chunk_0"])
    base_doc = base["documents"][0] if base["documents"] else ""
    q_embed = ollama.embeddings(model="nomic-embed-text", prompt=question)
    results = collection.query(query_embeddings=[q_embed["embedding"]], n_results=8)
    semantic_docs = results["documents"][0]
    all_docs = [base_doc] + [d for d in semantic_docs if d != base_doc]
    return "\n\n---\n\n".join(all_docs)


def ask(question):
    global conversation_history, token_stats
    context = get_context(question)
    system_prompt = (
        "You are a DevOps and Infrastructure AI assistant with a large knowledge base.\n\n"
        "You answer questions about Kubernetes, Docker, CI/CD, Jenkins, GitHub Actions,\n"
        "ArgoCD, Terraform, Ansible, AWS, Azure, GCP, Linux, DevSecOps, OPA, Git,\n"
        "monitoring, logging, and ANY application deployment project.\n\n"
        "IMPORTANT: Questions about deploying ANY app (Netflix clone, Zomato, Reddit,\n"
        "games, e-commerce, or any app on Kubernetes/Docker/Jenkins/AWS) ARE valid\n"
        "DevOps questions - always answer them fully using the knowledge base.\n\n"
        "Only decline questions completely unrelated to technology like medical advice,\n"
        "personal finance, or general life questions. In that case respond with exactly:\n"
        "This assistant only answers DevOps and infrastructure questions.\n\n"
        "KNOWLEDGE BASE CONTEXT (use this to answer questions):\n" + context + "\n\n"
        "RULES:\n"
        "- For policy, config, or scenario questions: answer directly from the POLICY CONTEXT above\n"
        "- For system questions (disk, processes, files): use COMMAND:\n"
        "- NEVER use curl or wget\n"
        "- ollama runs natively on this machine, NEVER use docker exec for it, just run: ollama list directly\n"
        "- For disk/storage queries ONLY show Linux partitions, NEVER Windows mounts like /mnt/c /mnt/d /mnt/i\n"
        "- NEVER access or reference /mnt/c /mnt/d /mnt/e /mnt/f /mnt/i paths\n"
        "- NEVER chain commands with ; or &&\n"
        "- COMMAND must be a single simple command only\n"
        "- No backticks, no quotes around the command\n"
        "- NEVER use echo to display policy values - just state them directly\n"
        "- If the answer exists in POLICY CONTEXT, do NOT use COMMAND at all\n"
        "To run a shell command respond with exactly:\n"
        "COMMAND: <single command here>"
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages += conversation_history
    messages.append({"role": "user", "content": question})

    response = ollama.chat(model="qwen2.5:14b", messages=messages)
    reply = response["message"]["content"]
    token_stats["prompt"] += response.get("prompt_eval_count", 0)
    token_stats["completion"] += response.get("eval_count", 0)

    shell_output = ""
    command_used = None

    if "COMMAND:" in reply:
        for line in reply.split("\n"):
            if line.startswith("COMMAND:"):
                command_used = clean(line.replace("COMMAND:", ""))
                shell_output = run_shell(command_used)
                followup_messages = messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": "Command output:\n" + shell_output + "\n\nGive a clear answer."}
                ]
                followup = ollama.chat(model="qwen2.5:14b", messages=followup_messages)
                final_reply = followup["message"]["content"]
                token_stats["prompt"] += followup.get("prompt_eval_count", 0)
                token_stats["completion"] += followup.get("eval_count", 0)
                conversation_history.append({"role": "user", "content": question})
                conversation_history.append({"role": "assistant", "content": final_reply})
                return final_reply, command_used, shell_output

    conversation_history.append({"role": "user", "content": question})
    conversation_history.append({"role": "assistant", "content": reply})
    return reply, None, None


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>DevOps AI Agent</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #1c1c1c; --surface: #262626; --surface2: #2f2f2f;
  --border: #383838; --text: #ececec; --text-muted: #8a8a8a;
  --accent: #cc785c; --accent-light: #d4956e; --code-bg: #1a1a1a;
}
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

#header { display: flex; align-items: center; justify-content: space-between; padding: 14px 24px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.header-left { display: flex; align-items: center; gap: 10px; }
.logo { width: 28px; height: 28px; background: var(--accent); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 700; color: white; }
#header h1 { font-size: 0.95rem; font-weight: 500; }
.header-right { display: flex; align-items: center; gap: 8px; }
.badge { font-size: 0.72rem; color: var(--text-muted); background: var(--surface); border: 1px solid var(--border); padding: 3px 8px; border-radius: 20px; }
#token-badge { color: var(--accent); }
#btn-clear { font-size: 0.8rem; color: var(--text-muted); background: none; border: 1px solid var(--border); padding: 5px 12px; border-radius: 8px; cursor: pointer; }
#btn-clear:hover { border-color: #e05252; color: #e05252; }

#chat { flex: 1; overflow-y: auto; padding: 32px 0; }
#chat::-webkit-scrollbar { width: 6px; }
#chat::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

#welcome { max-width: 600px; margin: 40px auto 0; padding: 0 24px; text-align: center; }
.w-icon { width: 52px; height: 52px; background: var(--accent); border-radius: 14px; margin: 0 auto 20px; display: flex; align-items: center; justify-content: center; font-size: 22px; color: white; font-weight: 700; }
#welcome h2 { font-size: 1.6rem; font-weight: 500; margin-bottom: 10px; }
#welcome p { color: var(--text-muted); font-size: 0.95rem; line-height: 1.6; margin-bottom: 24px; }
.suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
.suggestion { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 8px 14px; font-size: 0.83rem; color: var(--text-muted); cursor: pointer; transition: all 0.15s; }
.suggestion:hover { border-color: var(--accent); color: var(--text); }

.turn { max-width: 720px; margin: 0 auto; padding: 0 24px 24px; }
.turn.user { display: flex; justify-content: flex-end; }
.turn.user .bubble { background: var(--surface2); border: 1px solid var(--border); border-radius: 18px; padding: 12px 16px; max-width: 80%; font-size: 0.95rem; line-height: 1.55; }
.turn.agent { display: flex; gap: 12px; align-items: flex-start; }
.agent-icon { width: 28px; height: 28px; flex-shrink: 0; background: var(--accent); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; color: white; margin-top: 2px; }
.turn.agent .bubble { font-size: 0.95rem; line-height: 1.7; flex: 1; }

.thinking-dots { display: flex; gap: 4px; padding: 8px 0; }
.thinking-dots span { width: 6px; height: 6px; background: var(--text-muted); border-radius: 50%; animation: pulse 1.2s ease-in-out infinite; }
.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes pulse { 0%,80%,100% { opacity:0.3; transform:scale(0.85); } 40% { opacity:1; transform:scale(1); } }

.cmd-block { margin-top: 14px; background: var(--code-bg); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; font-family: "SF Mono", "Fira Code", monospace; font-size: 0.82rem; }
.cmd-header { display: flex; align-items: center; gap: 6px; padding: 8px 12px; background: var(--surface); border-bottom: 1px solid var(--border); }
.cmd-dot { width: 8px; height: 8px; border-radius: 50%; }
.cmd-label { color: var(--text-muted); font-size: 0.75rem; font-family: sans-serif; margin-left: 4px; }
.cmd-body { padding: 12px; }
.cmd-line { color: #f0883e; margin-bottom: 6px; }
.cmd-line::before { content: "$ "; color: var(--text-muted); }
.cmd-output { color: #57c97d; white-space: pre-wrap; line-height: 1.6; }

#input-area { padding: 16px 24px 20px; border-top: 1px solid var(--border); flex-shrink: 0; }
.input-box { max-width: 720px; margin: 0 auto; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; display: flex; align-items: center; gap: 8px; padding: 10px 10px 10px 16px; transition: border-color 0.15s; }
.input-box:focus-within { border-color: var(--accent); }
#input { flex: 1; background: none; border: none; outline: none; color: var(--text); font-size: 0.95rem; line-height: 1.5; font-family: inherit; }
#input::placeholder { color: var(--text-muted); }
#btn-send { width: 34px; height: 34px; flex-shrink: 0; background: var(--accent); border: none; border-radius: 8px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background 0.15s; }
#btn-send:hover { background: var(--accent-light); }
#btn-send:disabled { background: var(--surface2); cursor: not-allowed; }
#btn-send svg { width: 16px; height: 16px; fill: white; }
</style>
</head>
<body>

<div id="header">
  <div class="header-left">
    <div class="logo">D</div>
    <h1>DevOps AI Agent</h1>
  </div>
  <div class="header-right">
    <span class="badge">Qwen 2.5 14B</span>
    <span class="badge">2242 chunks</span>
    <span class="badge">Shell enabled</span>
    <span class="badge" id="token-badge">0 tokens</span>
    <button id="btn-clear" onclick="clearChat()">New chat</button>
  </div>
</div>

<div id="chat">
  <div id="welcome">
    <div class="w-icon">D</div>
    <h2>What can I help with?</h2>
    <p>Ask about your OPA policies, infrastructure config, Terraform plans, or run shell commands on your system.</p>
    <div class="suggestions">
      <div class="suggestion" onclick="usePrompt('What violation rules are in our policy?')">What violation rules are in our policy?</div>
      <div class="suggestion" onclick="usePrompt('How much disk space is available?')">How much disk space is available?</div>
      <div class="suggestion" onclick="usePrompt('What approved regions are defined?')">What approved regions are defined?</div>
      <div class="suggestion" onclick="usePrompt('Show running ollama processes')">Show running ollama processes</div>
      <div class="suggestion" onclick="usePrompt('Explain how gatekeeper.py works')">Explain how gatekeeper.py works</div>
      <div class="suggestion" onclick="usePrompt('What services are in docker-compose?')">What services are in docker-compose?</div>
    </div>
  </div>
</div>

<div id="input-area">
  <div class="input-box">
    <input type="text" id="input" placeholder="Ask anything..." />
    <button id="btn-send" onclick="sendMessage()">
      <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
    </button>
  </div>
</div>

<script>
document.getElementById("input").addEventListener("keydown", function(e) {
  if (e.key === "Enter") { e.preventDefault(); sendMessage(); }
});

function usePrompt(text) {
  document.getElementById("input").value = text;
  sendMessage();
}

function escHtml(t) {
  return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function sendMessage() {
  var input = document.getElementById("input");
  var question = input.value.trim();
  if (!question) return;

  var w = document.getElementById("welcome");
  if (w) w.remove();

  input.value = "";
  document.getElementById("btn-send").disabled = true;

  var chat = document.getElementById("chat");
  chat.innerHTML += '<div class="turn user"><div class="bubble">' + escHtml(question) + '</div></div>';

  var tid = "t" + Date.now();
  chat.innerHTML += '<div class="turn agent" id="' + tid + '"><div class="agent-icon">D</div><div class="bubble"><div class="thinking-dots"><span></span><span></span><span></span></div></div></div>';
  chat.scrollTop = chat.scrollHeight;

  fetch("/ask", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({question: question})
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var html = escHtml(data.answer || "").replace(/\\n/g, "<br>");
    if (data.command) {
      html += '<div class="cmd-block"><div class="cmd-header"><div class="cmd-dot" style="background:#ff5f57"></div><div class="cmd-dot" style="background:#febc2e"></div><div class="cmd-dot" style="background:#28c840"></div><span class="cmd-label">Terminal</span></div><div class="cmd-body"><div class="cmd-line">' + escHtml(data.command) + '</div><div class="cmd-output">' + escHtml(data.output || "") + '</div></div></div>';
    }
    document.getElementById(tid).outerHTML = '<div class="turn agent"><div class="agent-icon">D</div><div class="bubble">' + html + '</div></div>';
    if (data.tokens_total) {
      var t = data.tokens_total;
      document.getElementById("token-badge").textContent = (t > 1000 ? (t/1000).toFixed(1)+"k" : t) + " tokens";
    }
    document.getElementById("btn-send").disabled = false;
    document.getElementById("input").focus();
    document.getElementById("chat").scrollTop = 99999;
  })
  .catch(function(e) {
    document.getElementById(tid).outerHTML = '<div class="turn agent"><div class="agent-icon">D</div><div class="bubble">Error: ' + e + '</div></div>';
    document.getElementById("btn-send").disabled = false;
  });
}

function clearChat() {
  fetch("/clear", {method: "POST"});
  document.getElementById("chat").innerHTML = '<div id="welcome" style="max-width:600px;margin:40px auto 0;padding:0 24px;text-align:center"><div class="w-icon">D</div><h2 style="font-size:1.5rem;font-weight:500;margin-bottom:10px">What can I help with?</h2></div>';
  document.getElementById("token-badge").textContent = "0 tokens";
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML


@app.route("/clear", methods=["POST"])
def clear_history():
    global conversation_history, token_stats
    conversation_history = []
    token_stats["prompt"] = 0
    token_stats["completion"] = 0
    return jsonify({"status": "cleared"})


@app.route("/ask", methods=["POST"])
def ask_endpoint():
    question = request.json.get("question", "")
    answer, command, output = ask(question)
    return jsonify({
        "answer": answer,
        "command": command,
        "output": output,
        "tokens_prompt": token_stats["prompt"],
        "tokens_completion": token_stats["completion"],
        "tokens_total": token_stats["prompt"] + token_stats["completion"]
    })


if __name__ == "__main__":
    print("DevOps AI Agent running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
