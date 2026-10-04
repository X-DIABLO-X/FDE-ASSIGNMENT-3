"""Thin LLM layer: one provider (Anthropic) behind a tiny interface, plus a generic tool-use loop.

``chat()`` is the only provider-specific method, so tests (and other providers) can swap in a scripted
implementation. When no API key is configured ``get_llm()`` returns ``None`` and the architectures run in
*deterministic-only* mode (0 LLM calls) - the product still works, it just writes templated prose.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass
class LLMResponse:
    blocks: list[dict]  # {"type": "text", "text"} | {"type": "tool_use", "id", "name", "input"}
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    model: str

    def chat(self, system: str, messages: list[dict], tools: list[dict], tool_choice: dict | None = None) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, model: str | None = None, max_tokens: int = 2048):
        import anthropic

        self.model = model or os.getenv("MODEL_NAME") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(timeout=60.0, max_retries=2)

    def chat(self, system, messages, tools, tool_choice=None) -> LLMResponse:
        kwargs = dict(model=self.model, max_tokens=self.max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        resp = self.client.messages.create(**kwargs)
        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
        return LLMResponse(blocks, resp.stop_reason or "", resp.usage.input_tokens, resp.usage.output_tokens)


def get_llm() -> LLM | None:
    """Return a configured LLM or None (deterministic-only mode)."""
    if os.getenv("DISABLE_LLM") == "1":
        return None
    if os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicLLM()
    return None


@dataclass
class AgentRun:
    final: dict | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    transcript: list[dict] = field(default_factory=list)
    error: str | None = None


def run_agent(llm: LLM, system: str, user: str, tools: list[dict], execute: Callable[[str, dict], dict],
              final_tool: dict, max_turns: int = 6, on_llm_call: Callable[[], None] | None = None) -> AgentRun:
    """Generic tool-use loop that ends when the model calls ``final_tool`` (structured output).

    Parallel tool calls in one turn are executed together (one LLM call), which keeps the call count low.
    If the turn budget is exhausted the final tool is forced.
    """
    run = AgentRun()
    messages: list[dict] = [{"role": "user", "content": user}]
    all_tools = tools + [final_tool]
    for turn in range(max_turns + 1):
        force = turn == max_turns
        choice = {"type": "tool", "name": final_tool["name"]} if force else {"type": "any"}
        resp = llm.chat(system, messages, all_tools, choice)
        run.llm_calls += 1
        run.input_tokens += resp.input_tokens
        run.output_tokens += resp.output_tokens
        if on_llm_call:
            on_llm_call()
        calls = [b for b in resp.blocks if b["type"] == "tool_use"]
        final = next((b for b in calls if b["name"] == final_tool["name"]), None)
        if final:
            run.final = final["input"]
            return run
        if not calls:
            run.error = "model returned no tool call"
            return run
        messages.append({"role": "assistant", "content": resp.blocks})
        results = []
        for c in calls:
            out = execute(c["name"], c["input"])
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": json.dumps(out, default=str)})
            run.transcript.append({"tool": c["name"], "input": c["input"]})
        messages.append({"role": "user", "content": results})
    run.error = "turn budget exhausted"
    return run
