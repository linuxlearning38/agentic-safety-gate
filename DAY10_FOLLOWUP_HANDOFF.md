# Day 10 Follow-up Handoff

Date: 2026-04-03
Project: AVA / DevOps Agent
Primary file: `web_agent_v2.1_guardrail.py`
Follow-up to: `DAY10_DOCKER_MIGRATION_HANDOFF.md`

## Why This Second Note Exists
This document records the work completed after the first Day 10 handoff note was created.

It focuses on:
- performance tuning work in `web_agent_v2.1_guardrail.py`
- warm-up logic added for the LLM
- runtime findings from direct tests
- the current conclusion about why `/ask` still feels slow

## Changes Made After The First Handoff

### 1. Reduced prompt and retrieval load in `web_agent_v2.1_guardrail.py`
The following changes were applied to make `/ask` more responsive.

#### A. Retrieval now respects `n_blogs`
In `query_knowledge_base()`:
- before: `n_blogs=12` was hardcoded
- after: `n_blogs=n_blogs`

Why:
- the function signature already exposed `n_blogs`, but the implementation ignored it
- this caused more blog chunks to be retrieved than intended
- extra chunks increased prompt size and generation cost

#### B. Reduced context blocks sent to Qwen
In `generate_response()`:
- before: `context[:5]`
- after: `context[:3]`

Why:
- fewer merged blocks means fewer tokens sent to the LLM
- this reduces generation latency

#### C. Trimmed each context block before joining
Added:
```python
trimmed_context = [block[:1800] for block in context[:3]]
context_str = "\n\n---\n\n".join(trimmed_context)
```

Why:
- long merged blocks can still be expensive even if the block count is reduced
- trimming caps the payload and makes prompt size more predictable

#### D. Removed default step-by-step expansion
Removed this behavior:
```python
if use_cot:
    user_msg += "\n\nThink step by step before answering."
```

Why:
- this encourages longer reasoning and slower generation
- good for deep explanation, bad for responsiveness in the default web path

#### E. Lowered temperature
In `generate_response()`:
- before: `temperature: 0.7`
- after: `temperature: 0.2`

Why:
- lower temperature usually makes answers shorter and more stable
- also helps reduce wandering / slower output generation

### 2. Confirmed `num_ctx` reduction was active
`generate_response()` is using:
- `num_ctx: 4096`

This was verified from both code and Ollama runtime logs.

Why this matters:
- earlier runs showed heavy cost with larger context
- `4096` reduced KV cache usage versus `8192`
- runtime logs confirmed Qwen now loaded with `KvSize:4096`

### 3. Added a background warm-up path for Qwen
Warm-up logic was added near startup in `web_agent_v2.1_guardrail.py`.

Added concepts:
- `LLM_WARMUP_ENABLED`
- `LLM_WARMUP_FILE`
- `LLM_WARMUP_PROMPT`
- `_run_llm_warmup()`
- `start_llm_warmup()`
- `@app.before_request def maybe_start_warmup()`

Warm-up design:
- non-blocking background thread
- lock file at `/tmp/ava_qwen_warmup.lock`
- intended to prevent both Gunicorn workers from warming the same model at once
- intended to keep Qwen alive with `keep_alive="30m"`

Why this was added:
- the first real `/ask` after restart was paying full Qwen cold-load latency
- goal was to shift that cost into a background warm-up so the first user-visible request feels faster

## Runtime / Log Findings After These Changes

### 1. Retrieval is still fast
Direct Python tests confirmed again:
- `query_knowledge_base(...)` completes in about `0.19s`
- `hybrid_retriever.query(...)` returns about `24` raw chunks quickly
- `assemble_context(...)` completes almost instantly

Conclusion:
- retrieval is not the performance bottleneck

### 2. `generate_response()` is still slow in direct Python execution
Direct Python test of:
- `mod.generate_response("What is a Kubernetes readiness probe?", context)`

Observed result:
- generation completed successfully
- but took about `84.95s` to `103.8s` depending on run

Conclusion:
- main remaining delay is still in the generation step
- code changes helped reduce payload but did not eliminate the biggest latency source

### 3. Raw Ollama chat remains fast on tiny prompts
Minimal Ollama test still worked quickly:
```bash
curl -s http://localhost:11434/api/chat \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Say ready in one word."}]}' \
  --max-time 240
```

Returned streamed NDJSON quickly with final answer:
- `Ready.`

Conclusion:
- Qwen itself is not broken
- small prompts are fast
- AVA’s larger request path is the expensive path

### 4. Ollama runtime confirmed improved context settings
Later Ollama logs showed Qwen starting with:
- `KvSize:4096`
- reduced KV cache size compared to earlier `8192` runs

Also observed in logs:
- `llama runner started in 75.69 seconds`
- `/api/chat` completed in about `1m17s` on a cold path
- later direct chat finished in about `714ms`

Conclusion:
- cold-start cost is still very large
- warm model behavior is much faster

### 5. Warm-up hook did not yet visibly solve the issue
During log-follow testing:
- AVA logs showed normal startup and request flow
- `/health` worked
- `/ask` still paused
- expected `[Warmup] ...` log lines were not clearly observed in the AVA logs during the live tests

Conclusion:
- warm-up logic exists in code
- but it has not yet been confirmed as effective in the running container
- it may need additional instrumentation or a different trigger strategy

## Runtime / Environment Changes Attempted

### 1. Ollama keep-alive tuning
Ollama was run with:
- `OLLAMA_KEEP_ALIVE=30m`
- `OLLAMA_MAX_LOADED_MODELS=2`
- `OLLAMA_MODELS=/mnt/i/ai-lab/models`

Why:
- try to keep Qwen warm longer
- reduce repeated reloads
- allow both embed + chat models to coexist

### 2. Verified only one Ollama server should be active
At one point a second `ollama serve` was attempted and failed with:
- `bind: address already in use`

Conclusion:
- only one Ollama server should be run
- extra attempts created noise but did not change the main diagnosis

## Issues / Conclusions Found In This Follow-up Phase

### 1. Main bottleneck is still generation, not retrieval
Even after reducing prompt size and context count:
- retrieval remains fast
- generation remains slow

This is the single biggest conclusion.

### 2. Cold-start cost is still severe for `qwen2.5:14b`
The logs show that the first real use of Qwen after restart is still expensive.

So the true user-facing problem is now:
- AVA waits inline for a cold or semi-cold 14B model
- this makes `/ask` feel frozen even though nothing is technically broken

### 3. The current system is functionally working, but not yet great UX
Working:
- build
- recreate container
- health endpoint
- login
- token issuance
- retrieval
- direct Qwen chat
- direct `generate_response()` success

Not yet great:
- `/ask` responsiveness for the first real answer
- warm-up behavior still unverified as effective

### 4. There are still terminal/session issues confusing testing
Examples seen during testing:
- bad or stale `$TOKEN` across shells
- `json.tool` used on streamed or empty responses
- foreground `ollama serve` occupying terminals
- multiple concurrent shells increasing confusion

These do not create the root latency problem, but they make debugging harder.

## Current Best Technical Conclusion

The exact remaining issue is now narrower than before:

1. AVA’s generation path is still expensive enough to be slow even after prompt reduction.
2. `qwen2.5:14b` cold-start remains the dominant first-request penalty.
3. The warm-up mechanism exists but has not been confirmed working end-to-end in the deployed app.
4. The project is no longer in “broken migration” territory; it is now in “runtime performance tuning” territory.

## Suggested Next Session Steps

### Priority 1
Add exact timing logs around:
- start/end of `query_knowledge_base()`
- before `generate_response()`
- before `ollama.chat()`
- after `ollama.chat()`
- before JSON return from `/ask`

Why:
- we now need precise stage timing, not more guessing

### Priority 2
Verify warm-up hook behavior in container logs.

Why:
- if warm-up is not actually firing, first-request latency will stay bad

### Priority 3
Consider replacing `qwen2.5:14b` for the AVA web path with a smaller chat model.

Why:
- if responsiveness is the top priority, a smaller model may be the most effective improvement
- current evidence shows raw Qwen is capable but too expensive for the current first-response UX target

## Files Changed In This Follow-up Phase

Primary changed file:
- `I:\ai-lab\projects\devops-agent\web_agent_v2.1_guardrail.py`

New additions during this phase:
- second-stage prompt optimization
- warm-up functions and warm-up trigger hook

## Final Status At End Of Session

The system is in a much better state than earlier in the migration.

It is now accurate to say:
- infrastructure is largely working
- retrieval works
- app works
- main remaining issue is responsiveness of the final generation step

This is the final key takeaway:
- the project is operational
- the remaining problem is performance tuning around `generate_response()` and `qwen2.5:14b`, not data loss or a failed Docker migration
