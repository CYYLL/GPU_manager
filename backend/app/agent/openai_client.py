"""OpenAI Chat Completions client (official OpenAI + OpenAI-compatible endpoints).

Env (read only when LLM_PROVIDER=openai):
  LLM_API_KEY     required; OPENAI_API_KEY is a legacy fallback
  LLM_BASE_URL    optional; OPENAI_BASE_URL is a legacy fallback;
                  defaults to https://api.openai.com/v1
  LLM_MODEL       required; OPENAI_MODEL is a legacy fallback
  OPENAI_STREAMING auto|true|false (same semantics as LLM_STREAMING)

All protocol conversion lives here (boundary A): the agent loop still speaks
Anthropic-style messages; this class translates to/from OpenAI Chat Completions
and exposes the neutral complete()/stream_complete() shapes. When switching
providers, the shared LLM_API_KEY, LLM_BASE_URL and LLM_MODEL must be updated to values
from the selected provider or compatible gateway.
"""
import json
import os
from typing import Iterator, List, Dict, Optional

from .llm_client import BaseLLMClient, LLMResult

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def _env_value(primary: str, fallback: str = "") -> str:
    return (os.environ.get(primary, "").strip()
            or (os.environ.get(fallback, "").strip() if fallback else ""))


def _require(env_var: str, fallback: str = "") -> str:
    value = _env_value(env_var, fallback)
    if not value:
        raise ValueError("%s must be set when LLM_PROVIDER=openai" % env_var)
    return value


class OpenAIClient(BaseLLMClient):
    def __init__(self):
        super().__init__(
            api_key=_require("LLM_API_KEY", "OPENAI_API_KEY"),
            base_url=_env_value("LLM_BASE_URL", "OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL,
            model=_require("LLM_MODEL", "OPENAI_MODEL"),
            streaming=os.environ.get("OPENAI_STREAMING", "auto"),
        )
        from openai import OpenAI  # lazy: only when this provider is chosen
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                             timeout=self.timeout, max_retries=self.max_retries)

    # ── outbound: tools & messages ─────────────────────────────────
    @staticmethod
    def _to_openai_tools(tools: Optional[List[Dict]]) -> Optional[List[Dict]]:
        """[{name,description,input_schema}] -> OpenAI function tools; None when empty."""
        if not tools:
            return None
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t.get("input_schema", {"type": "object"})}}
                for t in tools]

    @staticmethod
    def _to_openai_messages(messages: List[Dict]) -> List[Dict]:
        """Translate agent messages to OpenAI chat format.

        Plain {role, content: str} pass through. Anthropic content-block turns
        (built by agent_loop only, as tool feedback) are split: assistant
        tool_use -> assistant message with tool_calls; user tool_result ->
        role:"tool" messages keyed by tool_call_id.
        """
        out: List[Dict] = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text_parts: List[str] = []
                tool_calls: List[Dict] = []
                for block in content:
                    btype = block.get("type") if isinstance(block, dict) else ""
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block["id"], "type": "function",
                            "function": {"name": block["name"],
                                         "arguments": json.dumps(block.get("input", {}),
                                                                 ensure_ascii=False)}})
                if tool_calls:
                    # Strict OpenAI-compatible servers (vLLM et al.) reject an
                    # assistant turn that carries both tool_calls and non-null
                    # content; agent_loop never emits text beside tool_use in a
                    # single turn, so omit content whenever tool_calls are present.
                    out.append({"role": "assistant", "tool_calls": tool_calls})
                else:
                    out.append({"role": "assistant", "content": "".join(text_parts) or ""})
            elif role == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out.append({"role": "tool",
                                    "tool_call_id": block["tool_use_id"],
                                    "content": str(block.get("content", ""))})
            else:
                out.append({"role": role, "content": str(content)})
        return out

    # ── inbound ────────────────────────────────────────────────────
    @staticmethod
    def _parse_choice(choice) -> LLMResult:
        """One chat completion choice -> neutral LLMResult."""
        message = getattr(choice, "message", None)
        text_parts: List[str] = []
        if message is not None:
            body = getattr(message, "content", None)
            if isinstance(body, str):
                text_parts.append(body)
            elif body:  # a few compat endpoints return content segments as a list
                for part in body:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif getattr(part, "type", "") == "text":
                        text_parts.append(getattr(part, "text", "") or "")
        tool_calls: List[Dict] = []
        if message is not None:
            for tc in (getattr(message, "tool_calls", None) or []):
                fn = getattr(tc, "function", None)
                raw = getattr(fn, "arguments", "") or ""
                try:
                    inp = json.loads(raw) if raw else {}
                except Exception:
                    inp = {}
                tool_calls.append({"id": getattr(tc, "id", None),
                                   "name": getattr(fn, "name", "") or "",
                                   "input": inp})
        return LLMResult(text="".join(text_parts), tool_calls=tool_calls,
                         stop_reason=getattr(choice, "finish_reason", None))

    # ── neutral interface ──────────────────────────────────────────
    def complete(self, system, messages, tools=None, max_tokens=1024) -> LLMResult:
        kwargs = dict(model=self.model, max_tokens=max_tokens,
                      messages=[{"role": "system", "content": system}]
                      + self._to_openai_messages(messages))
        oa_tools = self._to_openai_tools(tools)
        if oa_tools:
            kwargs["tools"] = oa_tools
        resp = self.client.chat.completions.create(**kwargs)
        return self._parse_choice(resp.choices[0])

    def stream_complete(self, system, messages, tools=None, max_tokens=1024) -> Iterator[dict]:
        if not self.stream_enabled():
            yield from self._emulated_stream(system, messages, tools, max_tokens=max_tokens)
            return
        kwargs = dict(model=self.model, max_tokens=max_tokens, stream=True,
                      messages=[{"role": "system", "content": system}]
                      + self._to_openai_messages(messages))
        oa_tools = self._to_openai_tools(tools)
        if oa_tools:
            kwargs["tools"] = oa_tools
        stream = self.client.chat.completions.create(**kwargs)
        slots = {}  # tool_call index -> {'id','name','arguments'}
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue  # e.g. an empty usage-only chunk
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                yield {"type": "text", "delta": delta.content}
            for tc in (getattr(delta, "tool_calls", None) or []):
                slot = slots.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments
        for slot in slots.values():
            try:
                inp = json.loads(slot["arguments"] or "{}")
            except Exception:
                inp = {}
            yield {"type": "tool_use", "id": slot["id"],
                   "name": slot["name"], "input": inp}
