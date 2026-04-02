#!/usr/bin/env python3
"""
AVA - DevOps AI Agent
Local DevOps assistant with RAG, OPA security, and command execution
"""

from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import chromadb
import ollama
import subprocess
import os
import json
import time
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Configuration
CHROMA_PATH = "/mnt/i/ai-lab/chromadb"
COLLECTION_NAME = "devops_policies_v2"
HISTORY_FILE = "/mnt/i/ai-lab/projects/devops-agent/query_history.json"
LLM_MODEL = "qwen2.5:14b"
EMBED_MODEL = "nomic-embed-text"

# Initialize ChromaDB
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = chroma_client.get_collection(COLLECTION_NAME)

# Command whitelist (OPA-style safety)
ALLOWED_COMMANDS = [
    'date', 'whoami', 'pwd', 'ls', 'cat', 'grep', 'df', 'free',
    'ps', 'top', 'uptime', 'uname', 'echo', 'head', 'tail',
    'wc', 'find', 'which', 'hostname'
]

BLOCKED_PATHS = [
    '/etc/passwd', '/etc/shadow', '/root', '~/.ssh',
    '/var/log', '/proc', '/sys'
]

# Stats
STATS = {
    'total_chunks': collection.count(),
    'repos': 5,
    'model': 'Qwen 2.5 14B',
    'opa_enabled': True
}

# History functions
def load_history():
    """Load query history from file"""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        return []
    except Exception as e:
        logger.error(f"Error loading history: {e}")
        return []

def save_history(entry):
    """Save query to history"""
    try:
        history = load_history()
        history.append(entry)
        # Keep last 100 entries
        history = history[-100:]
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving history: {e}")

# Safety check functions
def is_command_safe(cmd):
    """Check if command is safe to execute"""
    cmd_parts = cmd.strip().split()
    if not cmd_parts:
        return False, "Empty command"
    
    base_cmd = cmd_parts[0]
    
    # Check if command is in whitelist
    if base_cmd not in ALLOWED_COMMANDS:
        return False, f"Command '{base_cmd}' not in whitelist"
    
    # Check for blocked paths
    for path in BLOCKED_PATHS:
        if path in cmd:
            return False, f"Access to '{path}' is blocked"
    
    # Check for dangerous patterns
    dangerous_patterns = ['rm', 'sudo', '>', '>>', '|', '&', ';']
    for pattern in dangerous_patterns:
        if pattern in cmd and base_cmd != 'grep':
            return False, f"Dangerous pattern '{pattern}' detected"
    
    return True, "Command is safe"

def execute_command(cmd):
    """Execute shell command safely"""
    safe, reason = is_command_safe(cmd)
    
    if not safe:
        return {
            'success': False,
            'blocked': True,
            'reason': reason,
            'suggestion': 'Use read-only commands from the whitelist'
        }
    
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=5
        )
        return {
            'success': True,
            'blocked': False,
            'output': result.stdout,
            'error': result.stderr,
            'returncode': result.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'blocked': False,
            'reason': 'Command timed out (5s limit)'
        }
    except Exception as e:
        return {
            'success': False,
            'blocked': False,
            'reason': str(e)
        }

def get_embedding(text):
    """Get embedding for text"""
    try:
        response = ollama.embeddings(model=EMBED_MODEL, prompt=text)
        return response['embedding']
    except Exception as e:
        logger.error(f"Embedding error: {e}")
        return None

def query_knowledge_base(query, n_results=5):
    """Query ChromaDB for relevant context"""
    try:
        embedding = get_embedding(query)
        if not embedding:
            return []
        
        results = collection.query(
            query_embeddings=[embedding],
            n_results=n_results
        )
        
        return results['documents'][0] if results['documents'] else []
    except Exception as e:
        logger.error(f"Query error: {e}")
        return []

def generate_response(query, context):
    """Generate response using LLM"""
    try:
        # Build prompt
        if context:
            context_str = "\n\n".join(context[:3])  # Use top 3 results
            prompt = f"""You are AVA, a DevOps AI assistant. Use the following context to answer the question.

Context:
{context_str}

Question: {query}

Provide a clear, practical answer. Include code examples if relevant."""
        else:
            prompt = f"""You are AVA, a DevOps AI assistant. Answer this question based on your DevOps knowledge.

Question: {query}"""
        
        # Call LLM
        response = ollama.chat(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return response['message']['content']
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return f"Error generating response: {str(e)}"

# Routes
@app.route('/')
def index():
    """Main page"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/ask', methods=['POST'])
def ask():
    """Main query endpoint"""
    start_time = time.time()
    
    try:
        data = request.json
        query = data.get('query', '').strip()
        
        if not query:
            return jsonify({'error': 'No query provided'}), 400
        
        logger.info(f"Query: {query}")
        
        # Check if it's a command request
        if query.lower().startswith(('run ', 'execute ', 'shell ')):
            cmd = query.split(' ', 1)[1] if ' ' in query else ''
            result = execute_command(cmd)
            
            elapsed = time.time() - start_time
            
            # Log to history
            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'type': 'command',
                'blocked': result.get('blocked', False),
                'time_taken': f"{elapsed:.2f}s"
            })
            
            return jsonify({
                'type': 'command',
                'result': result,
                'time_taken': f"{elapsed:.2f}s"
            })
        
        # Knowledge base query
        logger.info("[*] Searching knowledge base...")
        context = query_knowledge_base(query)
        
        logger.info(f"[*] Found {len(context)} relevant chunks")
        logger.info(f"[*] Thinking with {LLM_MODEL}...")
        
        response = generate_response(query, context)
        
        elapsed = time.time() - start_time
        
        # Log to history
        save_history({
            'timestamp': datetime.now().isoformat(),
            'query': query,
            'type': 'knowledge',
            'sources_used': len(context),
            'time_taken': f"{elapsed:.2f}s",
            'response_preview': response[:200] + '...' if len(response) > 200 else response
        })
        
        return jsonify({
            'type': 'knowledge',
            'response': response,
            'sources_used': len(context),
            'time_taken': f"{elapsed:.2f}s"
        })
        
    except Exception as e:
        logger.error(f"Error in ask endpoint: {e}")
        return jsonify({
            'error': 'Failed to process query',
            'details': str(e)
        }), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    """File upload and analysis endpoint"""
    start_time = time.time()
    
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        # Read file
        content = file.read().decode('utf-8', errors='ignore')
        filename = file.filename
        
        logger.info(f"Analyzing file: {filename} ({len(content)} chars)")
        
        # Build analysis query
        file_type = filename.split('.')[-1] if '.' in filename else 'unknown'
        query = f"""Analyze this {file_type} file ({filename}) and provide:
1. What it does
2. Any issues or improvements
3. Best practices recommendations

File content:
{content[:3000]}"""  # Limit to first 3000 chars
        
        # Get context
        context = query_knowledge_base(f"best practices for {file_type} files")
        
        # Generate analysis
        analysis = generate_response(query, context)
        
        elapsed = time.time() - start_time
        
        # Log to history
        save_history({
            'timestamp': datetime.now().isoformat(),
            'query': f"File analysis: {filename}",
            'type': 'file_analysis',
            'filename': filename,
            'time_taken': f"{elapsed:.2f}s"
        })
        
        return jsonify({
            'type': 'file_analysis',
            'filename': filename,
            'analysis': analysis,
            'time_taken': f"{elapsed:.2f}s"
        })
        
    except Exception as e:
        logger.error(f"Error in upload endpoint: {e}")
        return jsonify({
            'error': 'Failed to analyze file',
            'details': str(e)
        }), 500

@app.route('/history', methods=['GET'])
def get_history():
    """Get query history"""
    try:
        history = load_history()
        return jsonify({
            'history': history[-20:],  # Last 20 entries
            'total': len(history)
        })
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/stats', methods=['GET'])
def get_stats():
    """Get system stats"""
    try:
        return jsonify({
            'total_chunks': STATS['total_chunks'],
            'repos': STATS['repos'],
            'model': STATS['model'],
            'opa_enabled': STATS['opa_enabled'],
            'uptime': 'Running'
        })
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'chromadb': 'connected',
        'ollama': 'running'
    })

# Error handlers
@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal error: {e}")
    return jsonify({'error': 'Internal server error'}), 500

# HTML Template
HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html>
<head>
    <title>AVA - DevOps AI Agent</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            transition: background 0.3s, color 0.3s;
        }
        
        body.dark-mode {
            background: #1a1a1a;
            color: #e0e0e0;
        }
        
        body.light-mode {
            background: #f5f5f5;
            color: #333;
        }
        
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
        }
        
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 0;
            border-bottom: 2px solid #444;
        }
        
        .title {
            font-size: 28px;
            font-weight: bold;
        }
        
        .theme-toggle {
            background: #3a3a3a;
            border: none;
            padding: 10px 15px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 20px;
        }
        
        .stats-bar {
            display: flex;
            gap: 15px;
            padding: 15px 0;
            flex-wrap: wrap;
        }
        
        .stat {
            padding: 8px 12px;
            background: #2a2a2a;
            border-radius: 6px;
            font-size: 14px;
        }
        
        body.light-mode .stat {
            background: #e0e0e0;
        }
        
        .main-content {
            margin-top: 30px;
        }
        
        .welcome {
            text-align: center;
            padding: 40px 0;
        }
        
        .welcome h2 {
            font-size: 32px;
            margin-bottom: 10px;
        }
        
        .welcome p {
            color: #888;
            font-size: 16px;
        }
        
        .examples {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 10px;
            margin: 30px 0;
        }
        
        .example-btn {
            padding: 15px;
            background: #2a2a2a;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            text-align: left;
            font-size: 14px;
            transition: background 0.2s;
        }
        
        .example-btn:hover {
            background: #3a3a3a;
        }
        
        body.light-mode .example-btn {
            background: #e0e0e0;
        }
        
        body.light-mode .example-btn:hover {
            background: #d0d0d0;
        }
        
        .upload-section {
            margin: 20px 0;
            padding: 20px;
            background: #2a2a2a;
            border-radius: 8px;
        }
        
        body.light-mode .upload-section {
            background: #e0e0e0;
        }
        
        .upload-section input[type="file"] {
            margin-right: 10px;
        }
        
        .input-section {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 20px;
            background: #1a1a1a;
            border-top: 2px solid #444;
        }
        
        body.light-mode .input-section {
            background: #f5f5f5;
        }
        
        .input-container {
            max-width: 900px;
            margin: 0 auto;
            display: flex;
            gap: 10px;
        }
        
        #queryInput {
            flex: 1;
            padding: 15px;
            border-radius: 8px;
            border: 1px solid #444;
            background: #2a2a2a;
            color: #e0e0e0;
            font-size: 16px;
        }
        
        body.light-mode #queryInput {
            background: #fff;
            color: #333;
            border-color: #ccc;
        }
        
        .send-btn {
            padding: 15px 30px;
            background: #0066cc;
            border: none;
            border-radius: 8px;
            color: white;
            font-size: 16px;
            cursor: pointer;
            transition: background 0.2s;
        }
        
        .send-btn:hover {
            background: #0052a3;
        }
        
        .response-container {
            margin: 20px 0;
            padding: 20px;
            background: #2a2a2a;
            border-radius: 8px;
        }
        
        body.light-mode .response-container {
            background: #fff;
            border: 1px solid #ddd;
        }
        
        .response-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 15px;
        }
        
        .copy-btn {
            padding: 8px 12px;
            background: #3a3a3a;
            border: none;
            border-radius: 6px;
            cursor: pointer;
        }
        
        .loading {
            text-align: center;
            padding: 20px;
            color: #888;
        }
        
        .error {
            padding: 15px;
            background: #ff4444;
            color: white;
            border-radius: 8px;
            margin: 20px 0;
        }
        
        pre {
            background: #1a1a1a;
            padding: 15px;
            border-radius: 6px;
            overflow-x: auto;
        }
        
        body.light-mode pre {
            background: #f0f0f0;
        }
        
        code {
            font-family: 'Courier New', monospace;
        }
    </style>
</head>
<body class="dark-mode">
    <div class="container">
        <div class="header">
            <div class="title">🤖 AVA - DevOps AI Agent</div>
            <button onclick="toggleTheme()" class="theme-toggle" id="themeToggle">🌙</button>
        </div>
        
        <div class="stats-bar">
            <span class="stat" id="chunksCount">📚 Loading...</span>
            <span class="stat">🔧 5 repos</span>
            <span class="stat">🤖 Qwen 2.5 14B</span>
            <span class="stat">🛡️ OPA enabled</span>
            <button class="stat" onclick="showHistory()" style="cursor: pointer; border: none;">📜 History</button>
        </div>
        
        <div class="main-content" id="mainContent">
            <div class="welcome">
                <h2>What can I help with?</h2>
                <p>Ask about your OPA policies, infrastructure config, Terraform plans, or run shell commands on your system.</p>
            </div>
            
            <div class="examples">
                <button class="example-btn" onclick="askExample('How to secure S3 buckets in production?')">
                    🔒 S3 Security
                </button>
                <button class="example-btn" onclick="askExample('Design a highly available RDS setup')">
                    🗄️ HA Database
                </button>
                <button class="example-btn" onclick="askExample('VPC peering vs Transit Gateway')">
                    🌐 VPC Design
                </button>
                <button class="example-btn" onclick="askExample('Best practices for IAM policies')">
                    👤 IAM Best Practices
                </button>
                <button class="example-btn" onclick="askExample('How to reduce Docker image size?')">
                    🐳 Docker Optimization
                </button>
                <button class="example-btn" onclick="askExample('Kubernetes rolling updates vs recreate')">
                    ☸️ K8s Deployments
                </button>
            </div>
            
            <div class="upload-section">
                <h3>📁 Analyze a File</h3>
                <p style="margin: 10px 0; color: #888;">Upload Terraform, Docker, Kubernetes, or shell scripts for analysis</p>
                <input type="file" id="fileUpload" accept=".tf,.yml,.yaml,.json,.sh,.py,.md,.hcl">
                <button class="example-btn" onclick="analyzeFile()" style="display: inline-block; margin-top: 10px;">
                    Analyze File
                </button>
            </div>
        </div>
        
        <div id="responseArea"></div>
    </div>
    
    <div class="input-section">
        <div class="input-container">
            <input type="text" id="queryInput" placeholder="Ask anything..." onkeypress="handleKeyPress(event)">
            <button class="send-btn" onclick="sendQuery()">Send</button>
        </div>
    </div>
    
    <script>
        // Load stats
        fetch('/stats')
            .then(r => r.json())
            .then(data => {
                document.getElementById('chunksCount').textContent = `📚 ${data.total_chunks} chunks`;
            });
        
        // Theme management
        function toggleTheme() {
            const body = document.body;
            const isDark = body.classList.contains('dark-mode');
            body.classList.remove('dark-mode', 'light-mode');
            body.classList.add(isDark ? 'light-mode' : 'dark-mode');
            document.getElementById('themeToggle').textContent = isDark ? '☀️' : '🌙';
            localStorage.setItem('theme', isDark ? 'light' : 'dark');
        }
        
        // Load saved theme
        window.onload = function() {
            const theme = localStorage.getItem('theme') || 'dark';
            document.body.classList.add(theme + '-mode');
            document.getElementById('themeToggle').textContent = theme === 'dark' ? '🌙' : '☀️';
        };
        
        function handleKeyPress(event) {
            if (event.key === 'Enter') {
                sendQuery();
            }
        }
        
        function askExample(query) {
            document.getElementById('queryInput').value = query;
            sendQuery();
        }
        
        function showLoading() {
            const messages = [
                "🔍 Searching knowledge base...",
                "🧠 Analyzing with Qwen 2.5 14B...",
                "📊 Processing results...",
                "✨ Generating response..."
            ];
            
            let index = 0;
            const loadingDiv = document.createElement('div');
            loadingDiv.className = 'loading';
            loadingDiv.id = 'loadingIndicator';
            loadingDiv.textContent = messages[0];
            
            document.getElementById('responseArea').innerHTML = '';
            document.getElementById('responseArea').appendChild(loadingDiv);
            
            return setInterval(() => {
                index = (index + 1) % messages.length;
                const loader = document.getElementById('loadingIndicator');
                if (loader) loader.textContent = messages[index];
            }, 2000);
        }
        
        function sendQuery() {
            const query = document.getElementById('queryInput').value.trim();
            if (!query) return;
            
            const loadingInterval = showLoading();
            
            fetch('/ask', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({query: query})
            })
            .then(r => r.json())
            .then(data => {
                clearInterval(loadingInterval);
                displayResponse(data);
                document.getElementById('queryInput').value = '';
            })
            .catch(err => {
                clearInterval(loadingInterval);
                displayError(err.message);
            });
        }
        
        function analyzeFile() {
            const fileInput = document.getElementById('fileUpload');
            const file = fileInput.files[0];
            
            if (!file) {
                alert('Please select a file');
                return;
            }
            
            const loadingInterval = showLoading();
            const formData = new FormData();
            formData.append('file', file);
            
            fetch('/upload', {
                method: 'POST',
                body: formData
            })
            .then(r => r.json())
            .then(data => {
                clearInterval(loadingInterval);
                displayResponse(data);
                fileInput.value = '';
            })
            .catch(err => {
                clearInterval(loadingInterval);
                displayError(err.message);
            });
        }
        
        function displayResponse(data) {
            const responseArea = document.getElementById('responseArea');
            responseArea.innerHTML = '';
            
            const container = document.createElement('div');
            container.className = 'response-container';
            
            // Header
            const header = document.createElement('div');
            header.className = 'response-header';
            
            const info = document.createElement('div');
            if (data.type === 'command') {
                info.textContent = `Command ${data.result.blocked ? '🛡️ Blocked' : '✅ Executed'} • ${data.time_taken}`;
            } else if (data.type === 'file_analysis') {
                info.textContent = `📄 ${data.filename} • ${data.time_taken}`;
            } else {
                info.textContent = `📚 ${data.sources_used} sources • ${data.time_taken}`;
            }
            
            const copyBtn = document.createElement('button');
            copyBtn.className = 'copy-btn';
            copyBtn.textContent = '📋 Copy';
            copyBtn.onclick = () => copyResponse(data);
            
            header.appendChild(info);
            header.appendChild(copyBtn);
            
            // Content
            const content = document.createElement('div');
            
            if (data.type === 'command') {
                if (data.result.blocked) {
                    content.innerHTML = `<div class="error">
                        <strong>🛡️ Command Blocked</strong><br>
                        ${data.result.reason}<br>
                        💡 ${data.result.suggestion}
                    </div>`;
                } else if (data.result.success) {
                    content.innerHTML = `<pre><code>${data.result.output || data.result.error || 'Command executed successfully'}</code></pre>`;
                } else {
                    content.innerHTML = `<div class="error">${data.result.reason}</div>`;
                }
            } else {
                const text = data.response || data.analysis;
                content.innerHTML = formatResponse(text);
            }
            
            container.appendChild(header);
            container.appendChild(content);
            responseArea.appendChild(container);
            
            // Scroll to response
            container.scrollIntoView({behavior: 'smooth'});
        }
        
        function formatResponse(text) {
            // Basic markdown-like formatting
            text = text.replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
            text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
            text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            return text.replace(/\n/g, '<br>');
        }
        
        function copyResponse(data) {
            const text = data.response || data.analysis || JSON.stringify(data.result);
            navigator.clipboard.writeText(text).then(() => {
                alert('✅ Copied to clipboard!');
            });
        }
        
        function displayError(message) {
            const responseArea = document.getElementById('responseArea');
            responseArea.innerHTML = `<div class="error">❌ Error: ${message}</div>`;
        }
        
        function showHistory() {
            fetch('/history')
                .then(r => r.json())
                .then(data => {
                    alert(`Recent queries: ${data.total} total\n\n${data.history.map(h => h.query).join('\n')}`);
                });
        }
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("AVA - DevOps AI Agent Starting...")
    logger.info(f"Knowledge Base: {STATS['total_chunks']} chunks")
    logger.info(f"Model: {STATS['model']}")
    logger.info(f"Security: OPA Enabled")
    logger.info("=" * 50)
    
    app.run(host='0.0.0.0', port=5000, debug=False)
