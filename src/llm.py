"""Thin LLM layer: Groq (default when GROQ_API_KEY is set) or Anthropic behind a tiny interface, plus a generic tool-use loop.

``chat()`` is the only provider-specific method, so tests (and other providers) can swap in a scripted
implementation. When no API key is configured ``get_llm()`` returns ``None`` and the architectures run in
*deterministic-only* mode (0 LLM calls) - the product still works, it just writes templated prose.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import requests

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"  # emits parallel tool calls; the gpt-oss models serialise them (see README)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


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


class GroqLLM:
    """Groq's OpenAI-compatible chat API. Translates the Anthropic-style blocks used by ``run_agent``.

    The free tier has a tokens-per-minute budget. ``chat`` paces itself against the ``x-ratelimit-*`` headers and
    records the time spent waiting in ``throttled_s`` so evaluations can report latency *excluding* quota waits.
    """

    def __init__(self, model: str | None = None, max_tokens: int = 2048):
        self.model = model or os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODEL
        self.max_tokens = max_tokens
        self.api_key = os.environ["GROQ_API_KEY"]
        self.reasoning_effort = os.getenv("GROQ_REASONING_EFFORT", "low")
        self.throttled_s = 0.0
        self._tokens_left: float | None = None
        self._tokens_reset_at = 0.0

    def _pace(self, est_tokens: int) -> None:
        if self._tokens_left is not None and self._tokens_left < est_tokens and time.monotonic() < self._tokens_reset_at:
            wait = self._tokens_reset_at - time.monotonic() + 0.5
            time.sleep(wait)
            self.throttled_s += wait
            self._tokens_left = None

    def _remember_limits(self, headers) -> None:
        try:
            self._tokens_left = float(headers["x-ratelimit-remaining-tokens"])
            reset = headers.get("x-ratelimit-reset-tokens", "0s")
            secs = 0.0
            for amount, unit in __import__("re").findall(r"([\d.]+)(ms|m|s)", reset):
                secs += float(amount) * {"ms": 0.001, "s": 1, "m": 60}[unit]
            self._tokens_reset_at = time.monotonic() + secs
        except (KeyError, ValueError):
            self._tokens_left = None

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": m["role"], "content": content})
            elif m["role"] == "assistant":
                text = "".join(b.get("text", "") for b in content if b["type"] == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                         for b in content if b["type"] == "tool_use"]
                msg: dict = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:  # user turn made of tool_result blocks
                for b in content:
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
        return out

    def chat(self, system, messages, tools, tool_choice=None) -> LLMResponse:
        body: dict = {"model": self.model, "messages": self._messages(system, messages),
                      "max_completion_tokens": self.max_tokens, "temperature": 0}
        if "gpt-oss" in self.model:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
            if tool_choice and tool_choice.get("type") == "tool":
                body["tool_choice"] = {"type": "function", "function": {"name": tool_choice["name"]}}
            else:
                body["tool_choice"] = "required" if tool_choice else "auto"
            body["parallel_tool_calls"] = True
        est = len(json.dumps(body)) // 3 + self.max_tokens // 4
        self._pace(est)
        for attempt in range(6):
            resp = requests.post(GROQ_URL, json=body, timeout=90,
                                 headers={"Authorization": f"Bearer {self.api_key}"})
            self._remember_limits(resp.headers)
            if resp.status_code == 429 or resp.status_code >= 500:  # rate limit / transient: back off and retry
                wait = min(float(resp.headers.get("retry-after", 0) or 0) or 2.0 * (attempt + 1), 60)
                time.sleep(wait)
                self.throttled_s += wait if resp.status_code == 429 else 0
                continue
            break
        if not resp.ok:
            raise RuntimeError(f"Groq HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        blocks = []
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
        usage = data.get("usage", {})
        return LLMResponse(blocks, data["choices"][0].get("finish_reason", ""),
                           usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


_instances: dict[tuple, LLM] = {}


def get_llm() -> LLM | None:
    """Return a configured LLM (shared instance, so rate-limit pacing state persists) or None."""
    if os.getenv("DISABLE_LLM") == "1":
        return None
    provider = os.getenv("LLM_PROVIDER", "").lower()
    if (provider == "groq" or not provider) and os.getenv("GROQ_API_KEY"):
        key = ("groq", os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODEL, os.getenv("GROQ_REASONING_EFFORT", "low"))
        return _instances.setdefault(key, GroqLLM())
    if (provider == "anthropic" or not provider) and os.getenv("ANTHROPIC_API_KEY"):
        key = ("anthropic", os.getenv("MODEL_NAME") or DEFAULT_MODEL)
        return _instances.setdefault(key, AnthropicLLM())
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
        try:
            resp = llm.chat(system, messages, all_tools, choice)
        except Exception as exc:
            if force:  # a model that still wants more tools when forced to answer: report, let caller fall back
                run.llm_calls += 1
                run.error = f"forced final answer failed: {str(exc)[:120]}"
                return run
            raise
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
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": json.dumps(out, default=str, separators=(",", ":"))})
            run.transcript.append({"tool": c["name"], "input": c["input"]})
        messages.append({"role": "user", "content": results})
    run.error = "turn budget exhausted"
    return run
