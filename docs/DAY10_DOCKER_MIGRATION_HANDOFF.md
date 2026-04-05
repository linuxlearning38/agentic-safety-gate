# Day 10 Docker Migration Handoff

Date: 2026-04-03
Project: AVA / DevOps Agent
Primary file: `web_agent_v2.1_guardrail.py`

## Goal
Dockerize the AVA agent and make the HTTPS app, auth flow, retrieval flow, and `/ask` path work reliably inside the container.

## What Was Changed

### 1. Restored the main app file
At one point `web_agent_v2.1_guardrail.py` became empty after a bad edit. It was restored from:
- `web_agent_v2.1_guardrail.py.bak_day10_154131`

This was a critical recovery step.

### 2. Made writable/data paths configurable
The app had hardcoded host paths. These were changed to support container/runtime overrides.

Implemented changes in `web_agent_v2.1_guardrail.py`:
- `CHROMA_PATH = os.getenv("CHROMA_PATH", "/mnt/i/ai-lab/chromadb")`
- `HISTORY_FILE = os.getenv("HISTORY_FILE", "/mnt/i/ai-lab/projects/devops-agent/query_history.json")`
- `MEMORY_PATH = os.getenv("MEMORY_PATH", "/mnt/i/ai-lab/ava_memory.json")`

Why:
- avoid hard dependency on one fixed host path
- make Docker configuration easier
- reduce file-write failures in containerized runs

### 3. Increased embedding timeout
In `knowledge_updater/hybrid_retrieval.py`:
- timeout changed from `15` to `60`

Why:
- embedding requests to Ollama were occasionally timing out during containerized execution

### 4. Added `/health` endpoint
Added route:
```python
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok'}), 200
```

Why:
- support container health checks
- verify app is alive independently of login and `/ask`

### 5. Rebuilt and recreated the Docker container
Container was rebuilt and restarted with force recreate.

Verified outcome:
- `ava-agent` came up healthy
- HTTPS endpoint responded

### 6. Reduced generation context size once already
In `generate_response()` the Ollama options were reduced from:
- `num_ctx: 8192`
To:
- `num_ctx: 4096`

Why:
- reduce cold-start and runtime load for `qwen2.5:14b`
- lower VRAM pressure and prompt-processing cost

## What Was Verified Successfully

### HTTPS and auth
Verified working:
- `https://localhost:5443/health`
- `https://localhost:5443/auth/login`
- bearer token issuance
- token-based authenticated requests

### Docker/container health
Verified:
- container starts
- container becomes `healthy`
- image rebuild completes successfully

### ChromaDB / retrieval side
Verified from logs and direct tests:
- `devops_policies_v2` loaded with `3885` chunks
- `devops_blogs_v1` loaded with `2513` chunks
- retrieval path is fast

Direct Python validation showed:
- `query_knowledge_base("What is a Kubernetes readiness probe?")` completed in about `0.19s`
- `hybrid_retriever.query(...)` returned quickly
- `assemble_context(...)` returned quickly

Conclusion:
- retrieval is not the main bottleneck

### Ollama models
Verified:
- embedding model works
- `qwen2.5:14b` works on direct raw Ollama chat tests

Direct raw Ollama chat test:
```bash
curl -s http://localhost:11434/api/chat \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Say ready in one word."}]}' \
  --max-time 240
```

Returned streamed NDJSON with final answer:
- `Ready.`

Conclusion:
- Qwen itself is not broken
- the main issue is how AVA feeds and uses it in `/ask`

## Major Issues Found

### 1. Hardcoded writable paths caused save/write failures
Observed error:
- `Error saving history: [Errno 2] No such file or directory: '/mnt/i/ai-lab/projects/devops-agent/query_history.json'`

Status:
- partly addressed by env-driven path configuration

### 2. WSL `/mnt/i` path permissions are awkward for non-root containers
Because the repo/data live on `/mnt/i`, the container had permission friction for writes.

Temporary workaround used:
- container runs as root via compose-level user override

Important note:
- this is a workaround, not the ideal final design

Better long-term fix:
- move writable runtime data to WSL-native storage such as `/home/...`
- or keep only code on `/mnt/i` and mount writable app data to a Linux-native path

### 3. `/ask` felt frozen for a long time
This was the main debugging focus.

What we proved:
- auth is not the problem
- health is not the problem
- retrieval is not the problem
- raw Ollama chat is not the problem

Most likely bottleneck now:
- prompt size and generation path inside `generate_response()`
- repeated cold/warm behavior of `qwen2.5:14b`
- AVA sends a much larger prompt than the minimal direct Ollama test

### 4. `python3 -m json.tool` caused misleading failures during streamed responses
For Ollama `/api/chat`, `json.tool` was misleading because Ollama streams newline-delimited JSON objects, not one single JSON object.

Observed error:
- `Extra data: line 2 column 1`

Conclusion:
- raw curl output is better for testing Ollama streaming endpoints

## Root Cause Analysis for Current Slowness

The current slowness is primarily in `generate_response()` in `web_agent_v2.1_guardrail.py`.

Current behavior:
- retrieval returns quickly
- assembled context is built quickly
- then AVA sends a relatively heavy system prompt + several context blocks + optional "Think step by step" instruction to `qwen2.5:14b`

Relevant code traits:
- joins `context[:5]`
- may append `Think step by step before answering.`
- uses `num_ctx: 4096`
- retrieval currently requests `n_blogs=12`

This makes `/ask` slower than the tiny direct Qwen chat test.

## Recommended Next Optimizations

These were identified as the best next improvements.

### Priority 1: shrink the prompt AVA sends to Qwen
Recommended edits in `generate_response()`:
- reduce `context[:5]` to `context[:3]`
- remove default `Think step by step` for ordinary Q&A
- lower temperature from `0.7` to `0.2` or `0.3`
- optionally hard-cap context text length before joining

Expected effect:
- faster `/ask`
- less prompt processing overhead
- more consistent output

### Priority 2: reduce retrieval payload slightly
Recommended edit in `query_knowledge_base()`:
- reduce `n_blogs=12` to `6` or `8`

Expected effect:
- smaller context
- less generation latency
- lower token load with minimal quality impact for normal questions

### Priority 3: keep Qwen warm longer
Suggested Ollama runtime tuning:
- `OLLAMA_KEEP_ALIVE=30m`
- `OLLAMA_MAX_LOADED_MODELS=2`

Why:
- reduce model reload frequency
- avoid repeated cold-start penalties

Status:
- discussed and tested conceptually, but not yet confirmed as the final stable setup for this environment

## Commands Used for Verification

### Login and token test
```bash
TOKEN=$(curl -sk --max-time 10 https://localhost:5443/auth/login \
  -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"ava-admin-2026"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "${TOKEN:0:40}"
```

### `/ask` test
```bash
curl -sk https://localhost:5443/ask \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"What is a Kubernetes readiness probe?"}' \
  --max-time 240
```

### Health check
```bash
curl -sk https://localhost:5443/health | python3 -m json.tool
```

### Direct retrieval timing test
Used Python importlib to load `web_agent_v2.1_guardrail.py` directly and call:
- `query_knowledge_base(...)`
- `hybrid_retriever.query(...)`
- `assemble_context(...)`

Result:
- retrieval completed in about `0.19s`

### Raw Ollama chat test
```bash
curl -s http://localhost:11434/api/chat \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Say ready in one word."}]}' \
  --max-time 240
```

Result:
- streamed response succeeded quickly

## Current Status

What is working:
- Docker image builds
- container recreates successfully
- app becomes healthy
- HTTPS works
- auth works
- token issuance works
- retrieval works
- Ollama embedding works
- raw Qwen chat works

What is not yet great:
- `/ask` is still slower than desired
- response generation is not yet tuned for responsiveness
- root-based container workaround still exists because of `/mnt/i` path behavior

## Suggested Next Session Plan

1. Edit `generate_response()` to reduce context and remove expensive prompt instructions for normal requests.
2. Reduce retrieval payload slightly.
3. Rebuild and retest `/ask` latency.
4. If still slow, add precise timing logs around:
   - retrieval
   - prompt build
   - Ollama chat call
   - history save
5. After performance is acceptable, replace the root-container workaround with a cleaner writable-data layout.

## Final Conclusion

The project is not broken and the migration is not lost.

The major infrastructure work is now largely functional.
The remaining issue is application-level performance tuning in the final response-generation step.

This is now a smaller, well-isolated problem rather than a full Docker migration failure.
