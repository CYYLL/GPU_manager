"""ReAct-style tool loop: chat → LLM → tool calls → observe → reply.

External LLM calls happen between short DB transactions (caller manages DB);
this function is pure orchestration and holds no session.

Run health is logged (INFO per round/tool, WARNING on an empty reply) so a
model/gateway that returns no text is visible in the service logs instead of
surfacing only as a generic user-facing fallback string.
"""
import logging
import re
import time
from typing import List, Dict, Callable

from .llm_client import LLMClient, LLMResult
from .validation import REJECTION_PREFIX

logger = logging.getLogger(__name__)


def _pull_job_id(name, ok, result):
    if name != "pull_hub_image" or not ok:
        return None
    match = re.search(r"\bjob_id=([a-f0-9]{32})\b", result)
    return match.group(1) if match else None


def _pull_result(job):
    if job["state"] == "done":
        return True, "镜像 %s 拉取完成，已加入预设镜像" % job["image"]
    return False, job["message"]

# 两类"模型没给文字"必须用不同文案，否则用户分不清"预算耗尽被掐断"和
# "网关/model 返回了空内容"：
#   EMPTY_REPLY_FALLBACK    —— 预算未耗尽时某轮既没调工具也没给文字（例如
#                              上游返回一个 200 的空响应），与调用上限无关。
#   TOOL_CALL_LIMIT_FALLBACK —— 工具调用轮数真被用完（每轮都在调工具），
#                              强制收尾轮仍没给出文字，此时"达到上限"属实。
# 日志始终记录真实原因（下面 EMPTY_*/LIMIT_* 标记），用户文案只是提示。
EMPTY_REPLY_FALLBACK = "模型未返回有效内容，请重试"
TOOL_CALL_LIMIT_FALLBACK = "已达到单次对话工具调用上限，请重试或重新描述问题"
# 用户主动中断（停止/取消）时返回的统一文案：由 cancel_check 触发，区别于
# 空回复与预算耗尽 —— 请求是被用户中止的，不是模型出问题。
INTERRUPT_REPLY = "请求中断"
_START_ACTION = re.compile(r"启动.{0,40}容器|容器.{0,40}启动", re.IGNORECASE)
_CREATE_ACTION = re.compile(r"创建.{0,40}容器|容器.{0,40}创建", re.IGNORECASE)
_QUESTION = re.compile(r"为什么|为何|如何|怎么|查询|查看|什么原因|问题")
_SUCCESS_CLAIM = re.compile(r"已.{0,18}(启动|创建)|成功.{0,12}(启动|创建)|"
                            r"(启动|创建).{0,12}成功|为您.{0,12}(启动|创建)|运行中")
_START_TOOLS = {"start_container", "rebuild_container"}
_TERMINAL_START_TOOLS = {"create_container", "start_container", "rebuild_container"}


def _requested_action(messages):
    user_text = next((item.get("content") for item in reversed(messages)
                      if item.get("role") == "user"), "")
    if not isinstance(user_text, str) or _QUESTION.search(user_text):
        return None
    if _CREATE_ACTION.search(user_text):
        return "create"
    if _START_ACTION.search(user_text):
        return "start"
    return None


def _verified_reply(reply, trace, messages):
    action = _requested_action(messages)
    if not action or not _SUCCESS_CLAIM.search(reply):
        return reply
    required = {"create_container"} if action == "create" else _START_TOOLS
    if any(item["tool"] in required and item["ok"] for item in trace):
        return reply
    failed = next((item["result"] for item in reversed(trace)
                   if item["tool"] in required and not item["ok"]), None)
    if failed:
        return "容器操作未成功：" + failed
    verb = "创建" if action == "create" else "启动"
    return f"本轮没有执行{verb}容器操作，不能确认容器已{verb}。请重试或到容器列表核对状态。"


def _correction_instruction(messages):
    action = _requested_action(messages)
    if action == "create":
        return "你尚未执行创建操作。先确定镜像和 GPU 配置，再调用 create_container；不要声称已创建。"
    return "你尚未执行启动操作。先查询容器确定 id，再调用 start_container；不要声称已启动。"


def run_agent(llm_client: LLMClient, system: str, messages: List[Dict],
              tools: List[Dict], tool_executor: Callable[[str, Dict], tuple],
              max_calls: int = 5, cancel_check=None, pull_status=None) -> dict:
    """Run the agent loop. tool_executor(name, input) -> (ok, result_text).

    Returns {'reply': str, 'tool_trace': [{'tool','ok','result'}, ...]}.

    cancel_check: optional Callable[[], bool]. When it returns True the loop
    stops before the next external effect (LLM round or tool execution) and
    returns reply=INTERRUPT_REPLY, so a user "stop" never triggers new work.
    """
    working = list(messages)
    trace: List[Dict] = []
    # tools 可以是普通 list（原样发，兼容既有调用），或渐进式披露目录
    # （暴露 round_specs()/activation_text()/__contains__，见 tools.ToolCatalog）。
    catalog = tools if hasattr(tools, "round_specs") else None
    active: set = set()  # 本调用内已加载完整 schema 的工具（sticky）
    started = time.monotonic()
    rounds = 0
    correction_attempted = False
    for _ in range(max_calls):  # up to max_calls tool rounds
        if cancel_check is not None and cancel_check():
            logger.info("agent cancelled by user (before round=%d/%d, tools=%d); "
                        "reply=INTERRUPT", rounds + 1, max_calls, len(trace))
            return {"reply": INTERRUPT_REPLY, "tool_trace": trace}
        rounds += 1
        round_tools = catalog.round_specs(active) if catalog is not None else tools
        result: LLMResult = llm_client.complete(system, working, round_tools)
        logger.info("agent round=%d/%d tool_calls=%d text_len=%d stop=%s",
                    rounds, max_calls, len(result.tool_calls), len(result.text),
                    result.stop_reason)
        if not result.tool_calls:
            # 非流式下"回复生成"是一次阻塞 complete：若取消在这期间到达，答案虽已
            # 生成也不应返回 —— 用户已点停止，按中断处理（能立即起效的仍是流式路径）。
            if cancel_check is not None and cancel_check():
                logger.info("agent cancelled by user (reply arrived after cancel, "
                            "rounds=%d); reply=INTERRUPT", rounds)
                return {"reply": INTERRUPT_REPLY, "tool_trace": trace}
            if not result.text:
                # 预算未耗尽：模型这一轮既没调工具、也没给文字（如网关空返回）。
                logger.warning(
                    "agent EMPTY reply at round=%d/%d: model returned no tools "
                    "and no text (elapsed=%.1fs); user will see EMPTY fallback",
                    rounds, max_calls, time.monotonic() - started)
                return {"reply": EMPTY_REPLY_FALLBACK, "tool_trace": trace}
            verified = _verified_reply(result.text, trace, messages)
            if verified != result.text and not correction_attempted and rounds < max_calls:
                correction_attempted = True
                working.append({"role": "assistant", "content": result.text})
                working.append({"role": "user", "content": _correction_instruction(messages)})
                continue
            logger.info("agent done reply_len=%d tools=%d rounds=%d "
                        "elapsed=%.1fs",
                        len(result.text), len(trace), rounds,
                        time.monotonic() - started)
            return {"reply": verified, "tool_trace": trace}
        for call in result.tool_calls:
            if cancel_check is not None and cancel_check():
                logger.info("agent cancelled by user (mid-round tools=%d); "
                            "reply=INTERRUPT", len(trace))
                return {"reply": INTERRUPT_REPLY, "tool_trace": trace}
            name = call["name"]
            if catalog is not None and name in catalog and name not in active:
                # 未激活工具首次被调用：不执行、不入 trace。回注完整 schema 并
                # sticky 激活，下一轮该工具全量声明，让模型用正确参数重发。
                active.add(name)
                working.append({"role": "assistant",
                                "content": [{"type": "tool_use", "id": call["id"],
                                             "name": name, "input": call.get("input", {})}]})
                working.append({"role": "user",
                                "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                             "content": catalog.activation_text(name)}]})
                logger.info("agent disclosure loaded name=%s", name)
                continue
            ok, text = tool_executor(name, call.get("input", {}))
            job_id = _pull_job_id(name, ok, text)
            if job_id and pull_status:
                deadline = time.monotonic() + 1800
                while time.monotonic() < deadline:
                    if cancel_check is not None and cancel_check():
                        return {"reply": INTERRUPT_REPLY, "tool_trace": trace}
                    job = pull_status(job_id)
                    if job and job["state"] in ("done", "error"):
                        ok, text = _pull_result(job)
                        break
                    time.sleep(1)
                else:
                    ok, text = False, "镜像拉取仍在后台进行，等待超过 30 分钟；请稍后查看镜像列表"
            trace.append({"tool": name, "ok": ok, "result": text})
            logger.info("agent tool ok=%s name=%s result_len=%d",
                        ok, name, len(text))
            if name == "create_container" and not ok and text.startswith(REJECTION_PREFIX):
                return {"reply": text, "tool_trace": trace}
            if name in _TERMINAL_START_TOOLS and ok:
                return {"reply": text, "tool_trace": trace}
            working.append({"role": "assistant",
                            "content": [{"type": "tool_use", "id": call["id"],
                                         "name": name, "input": call.get("input", {})}]})
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
        # 工具轮数已全部用完、强制收尾轮仍没给文字 → 真·达到调用上限。
        logger.warning(
            "agent EMPTY reply after %d tool rounds: forced no-tool final turn "
            "returned no text (elapsed=%.1fs); user will see LIMIT fallback",
            rounds, time.monotonic() - started)
    reply = final.text or TOOL_CALL_LIMIT_FALLBACK
    return {"reply": _verified_reply(reply, trace, messages), "tool_trace": trace}


def run_agent_stream(llm_client: LLMClient, system: str, messages: List[Dict],
                     tools: List[Dict], tool_executor: Callable[[str, Dict], tuple],
                     max_calls: int = 5, cancel_check=None, pull_status=None):
    """Streaming agent loop. Yields SSE-style events:
    {"event":"text","delta"} / {"event":"tool_use","tool","input"} /
    {"event":"tool_result","tool","ok","result"} / {"event":"done","reply","tool_trace"}.

    cancel_check: optional Callable[[], bool]. When True the loop stops before
    the next external effect (LLM round or tool execution) and yields a final
    {"event":"done","reply":INTERRUPT_REPLY} so the SSE stream ends cleanly and
    the caller can persist "请求中断".
    """
    working = list(messages)
    trace: List[Dict] = []
    # 同 run_agent：普通 list 或渐进式披露目录（见 tools.ToolCatalog）。
    catalog = tools if hasattr(tools, "round_specs") else None
    active: set = set()
    started = time.monotonic()
    hold_text = _requested_action(messages) is not None
    correction_attempted = False
    for round_idx in range(max_calls + 1):
        if cancel_check is not None and cancel_check():
            logger.info("agent stream cancelled by user (before round=%d/%d, "
                        "tools=%d); reply=INTERRUPT",
                        round_idx, max_calls + 1, len(trace))
            yield {"event": "done", "reply": INTERRUPT_REPLY, "tool_trace": trace}
            return
        if catalog is not None:
            round_tools = catalog.round_specs(active) if round_idx < max_calls else []
        else:
            round_tools = tools if round_idx < max_calls else []  # final round: force plain answer
        reply_parts: List[str] = []
        tool_calls: List[Dict] = []
        for ev in llm_client.stream_complete(system, working, round_tools):
            # 回复期间(每个增量间)也轮询取消：模型正在逐字产出回复时点"停止"，
            # 也要能立刻中止 —— 否则要等整段答完才收到 done，停止形同虚设。
            # 已发出的增量不可撤销，但后续不再产出，收尾统一回 INTERRUPT_REPLY。
            if cancel_check is not None and cancel_check():
                logger.info("agent stream cancelled by user (mid-reply text_chars=%d); "
                            "reply=INTERRUPT", sum(len(p) for p in reply_parts))
                yield {"event": "done", "reply": INTERRUPT_REPLY, "tool_trace": trace}
                return
            if ev["type"] == "text":
                reply_parts.append(ev["delta"])
                if not hold_text:
                    yield {"event": "text", "delta": ev["delta"]}
            elif ev["type"] == "tool_use":
                tool_calls.append(ev)
        logger.info("agent stream round=%d/%d tool_calls=%d text_chars=%d",
                    round_idx, max_calls, len(tool_calls),
                    sum(len(p) for p in reply_parts))
        if not tool_calls:
            reply = "".join(reply_parts)
            if (reply and _verified_reply(reply, trace, messages) != reply
                    and not correction_attempted and round_idx < max_calls):
                correction_attempted = True
                working.append({"role": "assistant", "content": reply})
                working.append({"role": "user", "content": _correction_instruction(messages)})
                continue
            if not reply:
                if round_idx < max_calls:
                    # 预算未耗尽就空手而归 → 空回复（如网关空返回），与上限无关。
                    logger.warning(
                        "agent stream EMPTY reply at round=%d/%d: no tools and "
                        "no text (elapsed=%.1fs); user will see EMPTY fallback",
                        round_idx, max_calls, time.monotonic() - started)
                    reply = EMPTY_REPLY_FALLBACK
                else:
                    # round_idx == max_calls：工具轮数全部用完后的强制收尾轮仍空手。
                    logger.warning(
                        "agent stream EMPTY reply after %d tool rounds: forced "
                        "no-tool final returned no text (elapsed=%.1fs); "
                        "user will see LIMIT fallback",
                        max_calls, time.monotonic() - started)
                    reply = TOOL_CALL_LIMIT_FALLBACK
            else:
                logger.info("agent stream done rounds=%d tools=%d reply_len=%d "
                            "elapsed=%.1fs",
                            round_idx + 1, len(trace), len(reply),
                            time.monotonic() - started)
            yield {"event": "done", "reply": _verified_reply(reply, trace, messages),
                   "tool_trace": trace}
            return
        for call in tool_calls:
            if cancel_check is not None and cancel_check():
                logger.info("agent stream cancelled by user (mid-round tools=%d); "
                            "reply=INTERRUPT", len(trace))
                yield {"event": "done", "reply": INTERRUPT_REPLY, "tool_trace": trace}
                return
            name = call["name"]
            if catalog is not None and name in catalog and name not in active:
                # 拦截轮：不产生任何 SSE 事件、不执行 —— 只回注 schema 并 sticky 激活。
                active.add(name)
                working.append({"role": "assistant",
                                "content": [{"type": "tool_use", "id": call["id"],
                                             "name": name, "input": call.get("input", {})}]})
                working.append({"role": "user",
                                "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                             "content": catalog.activation_text(name)}]})
                logger.info("agent stream disclosure loaded name=%s", name)
                continue
            yield {"event": "tool_use", "tool": name, "input": call.get("input", {})}
            ok, text = tool_executor(name, call.get("input", {}))
            job_id = _pull_job_id(name, ok, text)
            if job_id and pull_status:
                deadline = time.monotonic() + 1800
                last = None
                while time.monotonic() < deadline:
                    if cancel_check is not None and cancel_check():
                        yield {"event": "done", "reply": INTERRUPT_REPLY, "tool_trace": trace}
                        return
                    job = pull_status(job_id)
                    if job:
                        progress = (job["state"], job["percent"], job["message"])
                        if progress != last:
                            yield {"event": "tool_progress", "tool": name, "job_id": job_id,
                                   "percent": job["percent"], "message": job["message"]}
                            last = progress
                        if job["state"] in ("done", "error"):
                            ok, text = _pull_result(job)
                            break
                    time.sleep(1)
                else:
                    ok, text = False, "镜像拉取仍在后台进行，等待超过 30 分钟；请稍后查看镜像列表"
            trace.append({"tool": name, "ok": ok, "result": text})
            logger.info("agent stream tool ok=%s name=%s result_len=%d",
                        ok, name, len(text))
            yield {"event": "tool_result", "tool": name, "ok": ok, "result": text}
            if name == "create_container" and not ok and text.startswith(REJECTION_PREFIX):
                yield {"event": "done", "reply": text, "tool_trace": trace}
                return
            if name in _TERMINAL_START_TOOLS and ok:
                yield {"event": "done", "reply": text, "tool_trace": trace}
                return
            working.append({"role": "assistant",
                            "content": [{"type": "tool_use", "id": call["id"],
                                         "name": name, "input": call.get("input", {})}]})
            working.append({"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                         "content": text}]})
    # 预算耗尽：模型连"无工具"的强制收尾轮都还在调工具 → 真·达到调用上限。
    logger.warning(
        "agent stream EMPTY reply after %d rounds (tool budget exhausted, "
        "model kept calling tools on the forced no-tool round; elapsed=%.1fs); "
        "user will see LIMIT fallback",
        max_calls + 1, time.monotonic() - started)
    yield {"event": "done", "reply": TOOL_CALL_LIMIT_FALLBACK, "tool_trace": trace}
