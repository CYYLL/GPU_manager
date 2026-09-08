"""ReAct-style tool loop: chat → LLM → tool calls → observe → reply.

External LLM calls happen between short DB transactions (caller manages DB);
this function is pure orchestration and holds no session.

Run health is logged (INFO per round/tool, WARNING on an empty reply) so a
model/gateway that returns no text is visible in the service logs instead of
surfacing only as the generic user-facing fallback string.
"""
import logging
import time
from typing import List, Dict, Callable

from .llm_client import LLMClient, LLMResult

logger = logging.getLogger(__name__)

# User-facing fallback whenever the model returns no text at all. It reads like
# "tool budget reached", but it is really an *empty-reply* marker; the code logs
# the real outcome (EMPTY_* below) so the two are never conflated in diagnosis.
EMPTY_REPLY_FALLBACK = "已达到单次对话工具调用上限"


def run_agent(llm_client: LLMClient, system: str, messages: List[Dict],
              tools: List[Dict], tool_executor: Callable[[str, Dict], tuple],
              max_calls: int = 5) -> dict:
    """Run the agent loop. tool_executor(name, input) -> (ok, result_text).

    Returns {'reply': str, 'tool_trace': [{'tool','ok','result'}, ...]}.
    """
    working = list(messages)
    trace: List[Dict] = []
    started = time.monotonic()
    rounds = 0
    for _ in range(max_calls):  # up to max_calls tool rounds
        rounds += 1
        result: LLMResult = llm_client.complete(system, working, tools)
        logger.info("agent round=%d/%d tool_calls=%d text_len=%d stop=%s",
                    rounds, max_calls, len(result.tool_calls), len(result.text),
                    result.stop_reason)
        if not result.tool_calls:
            if not result.text:
                logger.warning(
                    "agent EMPTY reply at round=%d/%d: model returned no tools "
                    "and no text (elapsed=%.1fs); user will see fallback",
                    rounds, max_calls, time.monotonic() - started)
            else:
                logger.info("agent done reply_len=%d tools=%d rounds=%d "
                            "elapsed=%.1fs",
                            len(result.text), len(trace), rounds,
                            time.monotonic() - started)
            return {"reply": result.text, "tool_trace": trace}
        for call in result.tool_calls:
            ok, text = tool_executor(call["name"], call.get("input", {}))
            trace.append({"tool": call["name"], "ok": ok, "result": text})
            logger.info("agent tool ok=%s name=%s result_len=%d",
                        ok, call["name"], len(text))
            working.append({"role": "assistant",
                            "content": [{"type": "tool_use", "id": call["id"],
                                         "name": call["name"], "input": call.get("input", {})}]})
            working.append({"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                         "content": text}]})
    # tool budget exhausted; take one final forced-answer turn (no tools, so a
    # model that keeps insisting on tool_use cannot return empty text)
    final = llm_client.complete(system, working, [])
    logger.info("agent forced-final tool_calls=%d text_len=%d stop=%s "
                "(elapsed=%.1fs)",
                len(final.tool_calls), len(final.text), final.stop_reason,
                time.monotonic() - started)
    if not final.text:
        logger.warning(
            "agent EMPTY reply after %d tool rounds: forced no-tool final turn "
            "returned no text (elapsed=%.1fs); user will see fallback",
            rounds, time.monotonic() - started)
    return {"reply": final.text or EMPTY_REPLY_FALLBACK, "tool_trace": trace}


def run_agent_stream(llm_client: LLMClient, system: str, messages: List[Dict],
                     tools: List[Dict], tool_executor: Callable[[str, Dict], tuple],
                     max_calls: int = 5):
    """Streaming agent loop. Yields SSE-style events:
    {"event":"text","delta"} / {"event":"tool_use","tool","input"} /
    {"event":"tool_result","tool","ok","result"} / {"event":"done","reply","tool_trace"}.
    """
    working = list(messages)
    trace: List[Dict] = []
    started = time.monotonic()
    for round_idx in range(max_calls + 1):
        round_tools = tools if round_idx < max_calls else []  # final round: force plain answer
        reply_parts: List[str] = []
        tool_calls: List[Dict] = []
        for ev in llm_client.stream_complete(system, working, round_tools):
            if ev["type"] == "text":
                reply_parts.append(ev["delta"])
                yield {"event": "text", "delta": ev["delta"]}
            elif ev["type"] == "tool_use":
                tool_calls.append(ev)
        logger.info("agent stream round=%d/%d tool_calls=%d text_chars=%d",
                    round_idx, max_calls, len(tool_calls),
                    sum(len(p) for p in reply_parts))
        if not tool_calls:
            reply = "".join(reply_parts)
            if not reply:
                logger.warning(
                    "agent stream EMPTY reply at round=%d/%d: no tools and no "
                    "text (elapsed=%.1fs); user will see fallback",
                    round_idx, max_calls, time.monotonic() - started)
                reply = EMPTY_REPLY_FALLBACK
            else:
                logger.info("agent stream done rounds=%d tools=%d reply_len=%d "
                            "elapsed=%.1fs",
                            round_idx + 1, len(trace), len(reply),
                            time.monotonic() - started)
            yield {"event": "done", "reply": reply, "tool_trace": trace}
            return
        for call in tool_calls:
            yield {"event": "tool_use", "tool": call["name"], "input": call.get("input", {})}
            ok, text = tool_executor(call["name"], call.get("input", {}))
            trace.append({"tool": call["name"], "ok": ok, "result": text})
            logger.info("agent stream tool ok=%s name=%s result_len=%d",
                        ok, call["name"], len(text))
            yield {"event": "tool_result", "tool": call["name"], "ok": ok, "result": text}
            working.append({"role": "assistant",
                            "content": [{"type": "tool_use", "id": call["id"],
                                         "name": call["name"], "input": call.get("input", {})}]})
            working.append({"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                         "content": text}]})
    # budget exhausted
    logger.warning(
        "agent stream EMPTY reply after %d rounds (tool budget exhausted, "
        "forced final produced nothing; elapsed=%.1fs); user will see fallback",
        max_calls + 1, time.monotonic() - started)
    yield {"event": "done", "reply": EMPTY_REPLY_FALLBACK, "tool_trace": trace}
