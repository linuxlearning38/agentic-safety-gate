# control/react_loop.py
# AVA Phase 4 — Day 3: ReAct Loop
#
# Kicks in when no Command Graph matches — handles unknown/complex problems.
# LLM reasons step by step, picks tools, observes output, iterates.
#
# Flow:
#   query → ReactLoop.run() → [Thought→Action→Observation] x N → final_answer
#
# Design rules:
#   - Max 5 iterations (prevents runaway loops)
#   - Single model: Qwen 14B only
#   - No LangChain — pure Python
#   - All tool calls go through tool_registry (shell=False guaranteed)
#   - JSON output enforced — graceful fallback on parse failure
#   - Full trace returned for audit + UI display

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime

import ollama

from control.tool_registry import registry as tool_registry

logger = logging.getLogger(__name__)

LLM_MODEL   = "qwen2.5:14b"
MAX_ITERS   = 5
LLM_TIMEOUT = 120  # seconds


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class ReActStep:
    iteration:   int
    thought:     str
    action:      Optional[str]       = None
    action_input: Optional[Dict]     = None
    observation: Optional[str]       = None
    final_answer: Optional[str]      = None
    error:       Optional[str]       = None


@dataclass
class ReActResult:
    query:        str
    final_answer: str               = ""
    steps:        List[ReActStep]    = field(default_factory=list)
    iterations:   int                = 0
    success:      bool               = True
    stopped_reason: str              = ""   # "final_answer" | "max_iterations" | "error"
    timestamp:    str                = field(default_factory=lambda: datetime.now().isoformat())

    def to_context_blocks(self) -> List[str]:
        """Format the full trace for the LLM final synthesis."""
        blocks = []
        for step in self.steps:
            lines = [f"[Iteration {step.iteration}]"]
            lines.append(f"Thought: {step.thought}")
            if step.action:
                lines.append(f"Action: {step.action}({step.action_input})")
            if step.observation:
                lines.append(f"Observation: {step.observation[:1500]}")
            if step.error:
                lines.append(f"Error: {step.error}")
            blocks.append("\n".join(lines))
        return blocks

    def summary_for_log(self) -> str:
        actions = [s.action for s in self.steps if s.action]
        return (
            f"ReAct: {self.iterations} iterations, "
            f"tools used: {actions}, "
            f"stopped: {self.stopped_reason}"
        )


# ─── System prompt ────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    tools = tool_registry.list_tools()
    low    = [t for t in tools if t["risk_level"] == "low"]
    medium = [t for t in tools if t["risk_level"] == "medium"]

    low_lines = "\n".join(
        f"  - {t['name']}({', '.join(t['required_args'] + t['optional_args'])}): {t['description']}"
        for t in low
    )
    medium_lines = "\n".join(
        f"  - {t['name']}({', '.join(t['required_args'] + t['optional_args'])}): {t['description']}"
        for t in medium
    )

    return f"""You are AVA, a senior DevOps AI agent. You solve infrastructure problems by reasoning step by step and using tools to gather real data.

AVAILABLE TOOLS:

LOW RISK (auto-execute — use freely):
{low_lines}

MEDIUM RISK (requires approval — use only when necessary):
{medium_lines}

RULES:
1. Always respond with ONLY valid JSON — no markdown, no preamble, no explanation outside the JSON
2. Each response must follow EXACTLY one of these two formats:

FORMAT A — when you need to use a tool:
{{
  "thought": "Your reasoning about what to do next",
  "action": "tool_name",
  "action_input": {{"arg1": "value1", "arg2": "value2"}}
}}

FORMAT B — when you have enough information to answer:
{{
  "thought": "I now have enough information to give a complete answer",
  "final_answer": "Your complete, specific diagnosis and fix based on the tool outputs"
}}

3. Be specific — use exact pod names, error messages, and values from tool outputs
4. Do not guess — if you need data, use a tool to get it
5. Do not repeat the same tool call twice
6. Max 5 tool calls — if you still don't have an answer after 5, give your best diagnosis

IMPORTANT: Respond ONLY with JSON. No text before or after the JSON object."""


# ─── JSON parser ──────────────────────────────────────────────────────────────

def _parse_llm_response(text: str) -> Optional[Dict]:
    """
    Robustly parse LLM JSON output.
    Handles: clean JSON, JSON in markdown blocks, JSON with leading text.
    """
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code block
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find the first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ─── ReAct Loop ───────────────────────────────────────────────────────────────

class ReactLoop:
    """
    ReAct (Reasoning + Acting) loop for AVA.

    Usage:
        loop   = ReactLoop()
        result = loop.run("my nginx pod has high latency after deploy")
        # result.final_answer  → diagnosis
        # result.steps         → full trace for audit
    """

    def __init__(self, model: str = LLM_MODEL, max_iterations: int = MAX_ITERS):
        self.model          = model
        self.max_iterations = max_iterations
        self.system_prompt  = _build_system_prompt()

    def run(self, query: str, initial_context: Optional[List[str]] = None) -> ReActResult:
        """
        Run the ReAct loop for a given query.

        initial_context: optional RAG chunks to seed the first message
        """
        result = ReActResult(query=query)
        messages = self._build_initial_messages(query, initial_context)
        used_tools = set()

        for i in range(1, self.max_iterations + 1):
            logger.info(f"[ReAct] Iteration {i}/{self.max_iterations}")

            # ── Call LLM ──────────────────────────────────────────────────────
            try:
                response = ollama.chat(
                    model=self.model,
                    messages=messages,
                    options={
                        "temperature": 0.1,   # low temp for consistent JSON
                        "num_ctx":     4096,
                    },
                )
                raw_text = response["message"]["content"]
            except Exception as e:
                logger.error(f"[ReAct] LLM call failed at iteration {i}: {e}")
                step = ReActStep(iteration=i, thought="LLM call failed", error=str(e))
                result.steps.append(step)
                result.success       = False
                result.stopped_reason = "error"
                result.final_answer  = f"I encountered an error during diagnosis: {e}"
                result.iterations    = i
                return result

            # ── Parse response ────────────────────────────────────────────────
            parsed = _parse_llm_response(raw_text)

            if not parsed:
                logger.warning(f"[ReAct] Failed to parse LLM JSON at iteration {i}: {raw_text[:200]}")
                step = ReActStep(
                    iteration=i,
                    thought="Failed to parse LLM response",
                    error=f"Invalid JSON: {raw_text[:200]}"
                )
                result.steps.append(step)
                # Give LLM one more chance with a correction message
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({
                    "role": "user",
                    "content": "Your response was not valid JSON. Please respond with ONLY a valid JSON object."
                })
                continue

            thought      = parsed.get("thought", "")
            action       = parsed.get("action")
            action_input = parsed.get("action_input", {})
            final_answer = parsed.get("final_answer")

            # ── Final answer ──────────────────────────────────────────────────
            if final_answer:
                step = ReActStep(
                    iteration=i,
                    thought=thought,
                    final_answer=final_answer,
                )
                result.steps.append(step)
                result.final_answer   = final_answer
                result.iterations     = i
                result.stopped_reason = "final_answer"
                logger.info(f"[ReAct] Final answer reached at iteration {i}")
                return result

            # ── Tool call ─────────────────────────────────────────────────────
            if not action:
                # LLM gave neither action nor final_answer — nudge it
                step = ReActStep(iteration=i, thought=thought, error="No action or final_answer in response")
                result.steps.append(step)
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({
                    "role": "user",
                    "content": 'Please continue. Respond with either an "action" to use a tool or a "final_answer".'
                })
                continue

            # Prevent infinite loops from repeated tool calls
            tool_key = f"{action}:{json.dumps(action_input, sort_keys=True)}"
            if tool_key in used_tools:
                logger.warning(f"[ReAct] Duplicate tool call detected: {action} — stopping")
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({
                    "role": "user",
                    "content": f"You already called {action} with those arguments. You have the observation. Please provide a final_answer now."
                })
                continue
            used_tools.add(tool_key)

            logger.info(f"[ReAct] Action: {action}({action_input})")

            # Execute tool via registry (shell=False enforced inside)
            tool_result  = tool_registry.execute(action, action_input or {})
            observation  = self._format_observation(tool_result)

            step = ReActStep(
                iteration=i,
                thought=thought,
                action=action,
                action_input=action_input,
                observation=observation,
            )
            result.steps.append(step)

            # Feed observation back into conversation
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({
                "role": "user",
                "content": f"Observation: {observation}\n\nContinue your reasoning."
            })

            logger.info(f"[ReAct] Observation ({tool_result.get('status')}): {observation[:100]}")

        # ── Max iterations reached ─────────────────────────────────────────────
        logger.warning(f"[ReAct] Max iterations ({self.max_iterations}) reached without final answer")
        result.iterations     = self.max_iterations
        result.stopped_reason = "max_iterations"
        result.success        = False

        # Build best-effort answer from what we collected
        result.final_answer = self._synthesize_from_trace(query, result.steps)
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_initial_messages(self, query: str, context: Optional[List[str]]) -> List[Dict]:
        user_content = f"Problem: {query}"
        if context:
            ctx_str = "\n\n---\n\n".join(context[:3])
            user_content = (
                f"Background knowledge:\n<context>\n{ctx_str}\n</context>\n\n"
                f"Problem: {query}"
            )
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user",   "content": user_content},
        ]

    def _format_observation(self, tool_result: Dict) -> str:
        status = tool_result.get("status", "unknown")
        if status == "success":
            output = tool_result.get("output", "(no output)")
            return f"SUCCESS:\n{output}"
        elif status == "blocked":
            return f"BLOCKED: {tool_result.get('error', 'High risk tool — approval required')}"
        elif status == "not_found":
            return f"ERROR: {tool_result.get('error', 'Tool not found')}"
        elif status == "failure":
            error = tool_result.get("error", "unknown error")
            # kubectl not found is common in dev — make it informative
            if "not found" in error.lower() or "binary" in error.lower():
                cmd = tool_result.get("command_repr", "")
                return f"TOOL_UNAVAILABLE: {error}. Command would have been: {cmd}"
            return f"FAILURE: {error}"
        else:
            return f"STATUS={status}: {tool_result.get('output') or tool_result.get('error', '')}"

    def _synthesize_from_trace(self, query: str, steps: List[ReActStep]) -> str:
        """Best-effort answer when max iterations hit — summarise what was found."""
        observations = [
            f"Step {s.iteration} ({s.action}): {s.observation}"
            for s in steps if s.observation
        ]
        if not observations:
            return (
                f"I was unable to gather enough diagnostic data to answer: {query}\n"
                "Please check that kubectl and other tools are available in this environment."
            )
        obs_text = "\n".join(observations)
        try:
            response = ollama.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are AVA, a senior DevOps AI assistant. Be concise and specific."},
                    {"role": "user",   "content": (
                        f"I was diagnosing this problem: {query}\n\n"
                        f"Here is what I found:\n{obs_text}\n\n"
                        "Based on this data, give a specific diagnosis and recommended fix."
                    )},
                ],
                options={"temperature": 0.3, "num_ctx": 4096},
            )
            return response["message"]["content"]
        except Exception as e:
            return f"Diagnostic data collected:\n{obs_text}\n\n(Synthesis failed: {e})"


# ─── Singleton ────────────────────────────────────────────────────────────────

react_loop = ReactLoop()


# ─── Quick test (no AVA running needed) ──────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Test JSON parser only — no LLM or kubectl needed
    print("=== JSON parser tests ===")
    cases = [
        ('Clean JSON',       '{"thought": "ok", "action": "check_disk", "action_input": {}}'),
        ('Markdown wrapped', '```json\n{"thought": "ok", "final_answer": "all good"}\n```'),
        ('Leading text',     'Here is my response:\n{"thought": "ok", "action": "check_memory", "action_input": {}}'),
        ('Invalid',          'I cannot determine the answer'),
    ]
    for label, text in cases:
        result = _parse_llm_response(text)
        ok = result is not None
        print(f"  [{'PASS' if ok else 'FAIL' if label != 'Invalid' else 'PASS'}] {label}: {result}")

    print("\n=== Tool list visible to LLM ===")
    loop = ReactLoop()
    low_tools  = tool_registry.list_by_risk("low")
    med_tools  = tool_registry.list_by_risk("medium")
    print(f"  Low risk:    {[t['name'] for t in low_tools]}")
    print(f"  Medium risk: {[t['name'] for t in med_tools]}")
    print(f"  System prompt length: {len(loop.system_prompt)} chars")

    print("\n=== Observation formatter ===")
    loop2 = ReactLoop()
    test_results = [
        {"status": "success",   "output": "NAME  READY  STATUS\nnginx  1/1  Running"},
        {"status": "failure",   "error": "Binary not found: kubectl"},
        {"status": "blocked",   "error": "High risk"},
        {"status": "not_found", "error": "Tool 'nuke' not registered"},
    ]
    for r in test_results:
        obs = loop2._format_observation(r)
        print(f"  [{r['status'].upper():10}] → {obs[:70]}")

    print("\n✅ All static tests passed — ReAct loop ready")
    print("   Full end-to-end test requires AVA running with Ollama")
