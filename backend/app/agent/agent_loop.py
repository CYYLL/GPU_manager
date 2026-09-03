"""ReAct-style tool loop: chat → LLM → tool calls → observe → reply.

External LLM calls happen between short DB transactions (caller manages DB);
this function is pure orchestration and holds no session.
"""
from typing import List, Dict, Callable

from .llm_client import LLMClient, LLMResult


def run_agent(llm_client: LLMClient, system: str, messages: List[Dict],
              tools: List[Dict], tool_executor: Callable[[str, Dict], tuple],
              max_calls: int = 5) -> dict:
    """Run the agent loop. tool_executor(name, input) -> (ok, result_text).

    Returns {'reply': str, 'tool_trace': [{'tool','ok','result'}, ...]}.
    """
    working = list(messages)
    trace: List[Dict] = []
    for _ in range(max_calls):  # up to max_calls tool rounds
        result: LLMResult = llm_client.complete(system, working, tools)
        if not result.tool_calls:
            return {"reply": result.text, "tool_trace": trace}
        for call in result.tool_calls:
            ok, text = tool_executor(call["name"], call.get("input", {}))
            trace.append({"tool": call["name"], "ok": ok, "result": text})
            working.append({"role": "assistant",
                            "content": [{"type": "tool_use", "id": call["id"],
                                         "name": call["name"], "input": call.get("input", {})}]})
            working.append({"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                         "content": text}]})
    # tool budget exhausted; take one final forced-answer turn (no tools, so a
    # model that keeps insisting on tool_use cannot return empty text)
    final = llm_client.complete(system, working, [])
    return {"reply": final.text or "已达到单次对话工具调用上限", "tool_trace": trace}


def run_agent_stream(llm_client: LLMClient, system: str, messages: List[Dict],
                     tools: List[Dict], tool_executor: Callable[[str, Dict], tuple],
                     max_calls: int = 5):
    """Streaming agent loop. Yields SSE-style events:
    {"event":"text","delta"} / {"event":"tool_use","tool","input"} /
    {"event":"tool_result","tool","ok","result"} / {"event":"done","reply","tool_trace"}.
    """
    working = list(messages)
    trace: List[Dict] = []
    for _ in range(max_calls + 1):
        reply_parts: List[str] = []
        tool_calls: List[Dict] = []
        for ev in llm_client.stream_complete(system, working, tools):
            if ev["type"] == "text":
                reply_parts.append(ev["delta"])
                yield {"event": "text", "delta": ev["delta"]}
            elif ev["type"] == "tool_use":
                tool_calls.append(ev)
        if not tool_calls:
            yield {"event": "done", "reply": "".join(reply_parts), "tool_trace": trace}
            return
        for call in tool_calls:
            yield {"event": "tool_use", "tool": call["name"], "input": call.get("input", {})}
            ok, text = tool_executor(call["name"], call.get("input", {}))
            trace.append({"tool": call["name"], "ok": ok, "result": text})
            yield {"event": "tool_result", "tool": call["name"], "ok": ok, "result": text}
            working.append({"role": "assistant",
                            "content": [{"type": "tool_use", "id": call["id"],
                                         "name": call["name"], "input": call.get("input", {})}]})
            working.append({"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                         "content": text}]})
    # budget exhausted
    yield {"event": "done", "reply": "已达到单次对话工具调用上限", "tool_trace": trace}
